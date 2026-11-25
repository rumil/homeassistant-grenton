from __future__ import annotations
from typing import Any, Callable

import logging
import asyncio
import time
from datetime import datetime, timedelta

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .domain.clu import GrentonClu
from .domain.encryption import GrentonEncryption
from .domain.state_object import GrentonStateObject
from .domain.action import GrentonAction
from .domain.api.clu import ActionResult, GrentonCluApi, PingResult
from .domain.connectivity import CluConnectivity, ConnectivityTransition
from .domain.registration import RegistrationSession, plan_sessions
from .domain.uptime import RestartInfo, UptimeTracker
from .state import GrentonState, GrentonCluState, GrentonCluStateVariableKey, GrentonCluStateAttributeKey, GrentonValue
from .domain.api.clu_messages import GrentonCluApiActionRequest

from .const import DOMAIN, EVENT_CLU_RESTARTED

_LOGGER = logging.getLogger(__name__)

# Seconds between renewals of a CLU's clientRegister sessions. The CLU keeps
# a session in RAM and drops it, silently, when it is not renewed in time
# (after 60 s according to the vclu project; reports still arrive at least
# 42 s after a renewal on a real CLU). Every 30 s, one lost renewal is
# survived. A renewal also re-reads every subscribed value.
RENEW_INTERVAL = 30
# Seconds before the sessions whose renewal failed are tried again, instead
# of waiting for the next renewal.
RENEW_RETRY_DELAY = 3.0
# Answers to the uptime query that are not a number, in a row, after which
# restart detection is turned off for the CLU (e.g. a firmware without the
# feature). No answer at all does not count: it can be a lost packet.
UPTIME_MAX_BAD_ANSWERS = 3

class GrentonCoordinator(DataUpdateCoordinator):
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, clus: list[GrentonClu], encryption: GrentonEncryption):
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
        )
        
        self.config_entry = config_entry
        self.clus = clus
        self.encryption = encryption
        self.state = GrentonState(clus={clu.id: GrentonCluState() for clu in clus})
        
        # Map CLU IDs to their API instances
        self._apis: dict[str, GrentonCluApi] = {}

        # Initialize API instances for each CLU
        for clu in clus:
            self._apis[clu.id] = GrentonCluApi(clu, encryption)

        # Connectivity per CLU. Entities of a CLU are available only while it
        # is connected and its state was refreshed after the last reconnect.
        self._connectivity: dict[str, CluConnectivity] = {clu.id: CluConnectivity() for clu in clus}
        self._connectivity_listeners: dict[str, list[Callable[[], None]]] = {clu.id: [] for clu in clus}
        # Task registering a CLU's sessions (startup, resync, renewal), and a
        # lock so two of them never run for one CLU at the same time.
        self._sync_tasks: dict[str, asyncio.Task[bool]] = {}
        self._sync_locks: dict[str, asyncio.Lock] = {clu.id: asyncio.Lock() for clu in clus}
        # Last contact before the CLU went disconnected, for the reconnect log.
        self._offline_since: dict[str, datetime | None] = {}
        # CLUs answering pings in emergency mode, so it is logged once.
        self._emergency: set[str] = set()
        # CLUs whose entities are unavailable because their sessions could
        # not be renewed while they answered pings.
        self._renewal_lost: set[str] = set()
        # Restart detection: last uptime reading per CLU, when the CLU last
        # booted, and a restart detected but not yet announced (it is
        # announced once the state was read again).
        self._clock: Callable[[], float] = time.monotonic
        self._uptime: dict[str, UptimeTracker] = {clu.id: UptimeTracker() for clu in clus}
        self._uptime_bad_answers: dict[str, int] = {}
        self._uptime_disabled: set[str] = set()
        self._boot_time: dict[str, datetime] = {}
        self._pending_restart: dict[str, RestartInfo] = {}
        self._boot_listeners: dict[str, list[Callable[[], None]]] = {clu.id: [] for clu in clus}
        # Set once async_setup has done the initial sync. Before that, contact
        # does not start a resync because setup registers states itself.
        self._started = False

        # Value-count mismatches already logged, so each is logged once.
        self._count_mismatch_logged: set[tuple[str, str, int, int]] = set()
        # clientRegister sessions per CLU, planned from its subscribed keys,
        # and the number of keys they were planned for.
        self._sessions: dict[str, list[RegistrationSession]] = {}
        self._planned_key_count: dict[str, int] = {}
        # Report session IDs that matched no session, logged once each.
        self._unknown_sessions_logged: set[tuple[str, int]] = set()

        # Listeners for gesture entities. Each entry pairs the entity's state
        # object with a synchronous callback receiving (value, origin), where
        # origin is "push" (clientReport) or "resync" (register response).
        self._gesture_listeners: list[tuple[GrentonStateObject, Callable[[GrentonValue, str], None]]] = []

    async def _async_update_data(self): # type: ignore
        return self.state
    
    async def _send_ping(self, clu_id: str) -> None:
        api = self._apis.get(clu_id)
        if not api:
            _LOGGER.warning("[%s] No API found for CLU during ping", clu_id)
            return
        
        try:
            result = await api.ping()
        except Exception as e:
            _LOGGER.debug("[%s] Error during ping: %s", clu_id, e)
            result = PingResult.NO_ANSWER
        
        # A successful ping is recorded as contact when its response arrives.
        if result is PingResult.OK:
            if clu_id in self._emergency:
                self._emergency.discard(clu_id)
                _LOGGER.info("[%s] CLU is no longer in emergency mode", clu_id)
            return
        if result is PingResult.EMERGENCY and clu_id not in self._emergency:
            self._emergency.add(clu_id)
            _LOGGER.warning("[%s] CLU answers in emergency mode: its project is not running, so it "
                            "counts as not responding", clu_id)
        self._handle_ping_failure(clu_id)
    
    @callback
    def _handle_ping_failure(self, clu_id: str) -> None:
        connectivity = self._connectivity[clu_id]
        was_connected = connectivity.connected
        transition = connectivity.record_ping_failure()
        
        if transition is ConnectivityTransition.DISCONNECTED:
            self._offline_since[clu_id] = connectivity.last_contact
            if was_connected is None:
                _LOGGER.warning("[%s] CLU is not responding: %d pings failed in a row and it has not "
                                "responded since startup; its entities are unavailable",
                                clu_id, connectivity.failed_pings)
            else:
                _LOGGER.warning("[%s] CLU is not responding: %d pings failed in a row; "
                                "its entities are unavailable until it responds again",
                                clu_id, connectivity.failed_pings)
            self._async_connectivity_changed(clu_id)
        elif connectivity.connected is False:
            _LOGGER.debug("[%s] Ping failed, CLU still disconnected (%d failed pings)",
                          clu_id, connectivity.failed_pings)
        else:
            _LOGGER.debug("[%s] Ping failed (%d/%d)",
                          clu_id, connectivity.failed_pings, connectivity.threshold)
    
    @callback
    def _handle_contact(self, clu_id: str) -> None:
        """Handle any message received from a CLU."""
        connectivity = self._connectivity[clu_id]
        # A ping failed since the last contact, but not enough of them to
        # count as an outage. The CLU may have restarted in between, faster
        # than outage detection: its sessions would then be gone.
        after_failed_ping = connectivity.connected is True and connectivity.failed_pings > 0
        transition = connectivity.record_contact(dt_util.utcnow())
        
        if transition is ConnectivityTransition.RECONNECTED:
            _LOGGER.debug("[%s] CLU responded, refreshing its state before marking entities available", clu_id)
            # Only the connectivity sensor changes here: entities stay
            # unavailable until the state refresh completes.
        
        if self._started:
            if not connectivity.synced:
                self._schedule_sync(clu_id, "resync")
            elif after_failed_ping:
                # Check for a restart and renew at once, not at the next renewal.
                _LOGGER.debug("[%s] CLU answers again after a failed ping; checking it", clu_id)
                self._schedule_sync(clu_id, "gap")
        
        self._notify_connectivity_listeners(clu_id)
    
    @callback
    def _schedule_sync(self, clu_id: str, reason: str) -> None:
        """Start a sync of the CLU unless one is already running."""
        task = self._sync_tasks.get(clu_id)
        if task is not None and not task.done():
            return
        self._sync_tasks[clu_id] = asyncio.create_task(self._async_sync(clu_id, reason))
    
    async def _run_sync(self, clu_id: str, reason: str) -> bool:
        """Run a sync of the CLU, or wait for the one already running."""
        task = self._sync_tasks.get(clu_id)
        if task is None or task.done():
            task = self._sync_tasks[clu_id] = asyncio.create_task(self._async_sync(clu_id, reason))
        return await task
    
    async def _async_sync(self, clu_id: str, reason: str) -> bool:
        """Register (or renew) every session of the CLU and re-read its state.
        
        ``reason`` is "startup", "resync" (entities unavailable, e.g. after a
        reconnect), "renew" (periodic, while available) or "gap" (while
        available, after a ping failed and the CLU answered again).
        
        The CLU's uptime is read first: a restart makes the entities
        unavailable before its new state is applied (see ``_check_restart``).
        
        Entities that are unavailable become available only after every
        session was registered. On failure the next contact (e.g. the next
        ping response) starts another attempt. A failed renewal is tried
        again after RENEW_RETRY_DELAY.
        
        Returns True when every session was registered.
        """
        periodic = reason in ("renew", "gap")
        async with self._sync_locks[clu_id]:
            connectivity = self._connectivity[clu_id]
            if periodic and not connectivity.available:
                return False
            if connectivity.connected is not True:
                return False
            
            await self._check_restart(clu_id)
            
            failed = await self._register_sessions(clu_id, self._get_sessions(clu_id))
            if failed and periodic:
                _LOGGER.debug("[%s] Renewal of %d session(s) failed, retrying in %.0f s",
                              clu_id, len(failed), RENEW_RETRY_DELAY)
                await asyncio.sleep(RENEW_RETRY_DELAY)
                failed = await self._register_sessions(clu_id, failed)
                if failed:
                    self._renewal_failed(clu_id, len(failed))
            if failed:
                if not periodic:
                    _LOGGER.debug("[%s] State refresh failed, retrying on next contact", clu_id)
                return False
            
            self._sync_succeeded(clu_id)
            return True
    
    @callback
    def _renewal_failed(self, clu_id: str, failed: int) -> None:
        """A renewal failed twice in a row while the CLU answers pings.
        
        The CLU may have dropped the sessions (they expire when not renewed),
        so the cached state may no longer be kept up to date. Entities become
        unavailable, and every contact (e.g. the next ping response) tries to
        register again until it succeeds.
        """
        if not self._connectivity[clu_id].mark_unsynced():
            return
        self._renewal_lost.add(clu_id)
        _LOGGER.warning("[%s] CLU answers pings but did not accept %d of its session(s) twice in a row; "
                        "its entities are unavailable until it does", clu_id, failed)
        self._async_connectivity_changed(clu_id)
    
    @callback
    def _sync_succeeded(self, clu_id: str) -> None:
        """Every session is registered: entities may show their state."""
        connectivity = self._connectivity[clu_id]
        restart = self._pending_restart.pop(clu_id, None)
        
        if connectivity.mark_synced():
            if clu_id in self._offline_since:
                last_contact = self._offline_since.pop(clu_id)
                _LOGGER.info("[%s] CLU is responding again (last contact before the outage: %s); "
                             "state refreshed from the CLU, entities available",
                             clu_id, last_contact.isoformat() if last_contact else "never")
            elif clu_id in self._renewal_lost and restart is None:
                _LOGGER.info("[%s] CLU accepted its sessions again; state refreshed, entities available", clu_id)
            else:
                _LOGGER.debug("[%s] CLU state synced", clu_id)
            self._renewal_lost.discard(clu_id)
            self._async_connectivity_changed(clu_id)
        
        if restart is not None:
            self._announce_restart(clu_id, restart)
    
    async def _check_restart(self, clu_id: str) -> None:
        """Read the CLU's uptime and handle a restart since the last reading.
        
        A restarted CLU has lost its sessions and reset its User Features, so
        the cached state is stale: the CLU's entities become unavailable (as
        after an outage), and the restart is announced once the sessions are
        registered again and the new state was read (``_announce_restart``).
        The first reading only records when the CLU booted.
        """
        if clu_id in self._uptime_disabled:
            return
        answer = await self._apis[clu_id].read_uptime()
        if answer is None:
            _LOGGER.debug("[%s] No answer to the uptime query", clu_id)
            return
        if isinstance(answer, str):
            count = self._uptime_bad_answers.get(clu_id, 0) + 1
            self._uptime_bad_answers[clu_id] = count
            if count >= UPTIME_MAX_BAD_ANSWERS:
                self._uptime_disabled.add(clu_id)
                _LOGGER.warning("[%s] CLU answered the uptime query with %r, not a number of seconds; "
                                "restart detection is off until Home Assistant restarts", clu_id, answer)
            return
        self._uptime_bad_answers[clu_id] = 0
        
        boot_time = dt_util.utcnow() - timedelta(seconds=answer)
        restarted = self._uptime[clu_id].observe(answer, self._clock())
        if clu_id not in self._boot_time:
            self._boot_time[clu_id] = boot_time
            _LOGGER.debug("[%s] CLU booted at %s (uptime %d s)", clu_id, boot_time.isoformat(), answer)
            self._notify_boot_listeners(clu_id)
            return
        if not restarted:
            return
        
        self._pending_restart[clu_id] = RestartInfo(boot_time=boot_time, uptime=answer)
        _LOGGER.warning("[%s] CLU restarted at %s (uptime %d s): it lost its subscriptions and reset its "
                        "User Features; re-reading its state", clu_id, boot_time.isoformat(), answer)
        if self._connectivity[clu_id].mark_unsynced():
            self._async_connectivity_changed(clu_id)
    
    @callback
    def _announce_restart(self, clu_id: str, restart: RestartInfo) -> None:
        """Publish a restart: entities are available and show the new state."""
        self._boot_time[clu_id] = restart.boot_time
        self._notify_boot_listeners(clu_id)
        api = self._apis[clu_id]
        _LOGGER.info("[%s] State re-read after the CLU restart; entities available", clu_id)
        self.hass.bus.async_fire(
            EVENT_CLU_RESTARTED,
            {
                "clu_id": clu_id,
                "serial_number": api.clu.serial_number,
                "boot_time": restart.boot_time.isoformat(),
                "uptime": int(restart.uptime),
            },
        )
    
    def get_boot_time(self, clu_id: str) -> datetime | None:
        """When the CLU last booted, or None before its uptime was read."""
        return self._boot_time.get(clu_id)
    
    @callback
    def async_add_boot_listener(self, clu_id: str, callback_fn: Callable[[], None]) -> Callable[[], None]:
        """Register a callback for changes of the CLU's boot time."""
        listeners = self._boot_listeners.setdefault(clu_id, [])
        listeners.append(callback_fn)
        
        @callback
        def remove_listener() -> None:
            try:
                listeners.remove(callback_fn)
            except ValueError:
                pass
        
        return remove_listener
    
    @callback
    def _notify_boot_listeners(self, clu_id: str) -> None:
        for callback_fn in list(self._boot_listeners.get(clu_id, [])):
            try:
                callback_fn()
            except Exception as e:  # pragma: no cover - defensive
                _LOGGER.error("[%s] Error in boot time listener: %s", clu_id, e)
    
    @callback
    def _async_connectivity_changed(self, clu_id: str) -> None:
        """Push availability changes to all entities and connectivity sensors."""
        self.async_update_listeners()
        self._notify_connectivity_listeners(clu_id)
    
    @callback
    def _notify_connectivity_listeners(self, clu_id: str) -> None:
        for callback_fn in list(self._connectivity_listeners.get(clu_id, [])):
            try:
                callback_fn()
            except Exception as e:  # pragma: no cover - defensive
                _LOGGER.error("[%s] Error in connectivity listener: %s", clu_id, e)
    
    @callback
    def async_add_connectivity_listener(self, clu_id: str, callback_fn: Callable[[], None]) -> Callable[[], None]:
        """Register a callback for connectivity changes and contact with a CLU.
        
        Called on every contact as well, so it must be cheap and decide itself
        whether to write state.
        """
        listeners = self._connectivity_listeners.setdefault(clu_id, [])
        listeners.append(callback_fn)
        
        @callback
        def remove_listener() -> None:
            try:
                listeners.remove(callback_fn)
            except ValueError:
                pass
        
        return remove_listener
    
    def get_connectivity(self, clu_id: str) -> CluConnectivity | None:
        """Return the connectivity status of a CLU."""
        return self._connectivity.get(clu_id)
    
    def is_clu_available(self, clu_id: str) -> bool:
        """True when entities of the CLU may show their state."""
        connectivity = self._connectivity.get(clu_id)
        # Unknown CLU ids are not tracked, so they never make an entity unavailable.
        return connectivity is None or connectivity.available
    
    async def _ping_loop(self) -> None:
        while True:
            try:
                # Ping every 5 seconds
                ping_interval = 5
                await asyncio.sleep(ping_interval)
                
                tasks = [self._send_ping(clu_id) for clu_id in self._apis.keys()]
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                _LOGGER.debug("Ping loop cancelled")
                break
            except Exception as e:
                _LOGGER.error("Unexpected error in ping loop: %s", e)
    
    def _get_sessions(self, clu_id: str) -> list[RegistrationSession]:
        """The clientRegister sessions of a CLU, planned on first use.
        
        Planned again if keys were subscribed after the last plan.
        """
        keys = self.state.clus[clu_id].get_subscription_order()
        if self._planned_key_count.get(clu_id) != len(keys):
            sessions = plan_sessions(list(keys), self._apis[clu_id].first_session_id)
            self._sessions[clu_id] = sessions
            self._planned_key_count[clu_id] = len(keys)
            _LOGGER.debug("[%s] Subscribing %d keys in %d session(s): %s", clu_id, len(keys),
                          len(sessions), ", ".join(f"{s.session_id} ({len(s.keys)} keys)" for s in sessions))
        return self._sessions.get(clu_id, [])
    
    def _find_session(self, clu_id: str, session_id: int) -> RegistrationSession | None:
        for session in self._sessions.get(clu_id, []):
            if session.session_id == session_id:
                return session
        return None
    
    async def _register_sessions(
        self, clu_id: str, sessions: list[RegistrationSession]
    ) -> list[RegistrationSession]:
        """Register (or renew) sessions one after another. Returns the failed ones.
        
        The values of each answer are applied the moment it arrives, in order
        with pushed reports (see ``_apply_register_values``).
        """
        api = self._apis.get(clu_id)
        if not api:
            _LOGGER.warning("[%s] No API found for CLU during state registration", clu_id)
            return list(sessions)
        
        failed: list[RegistrationSession] = []
        for session in sessions:
            try:
                values = await api.register_session(
                    session,
                    on_values=lambda values, keys=session.keys, sid=session.session_id:
                        self._apply_register_values(clu_id, keys, values, sid),
                )
            except Exception as e:
                _LOGGER.error("[%s] Error during registration: %s", clu_id, e)
                values = None
            if values is None:
                failed.append(session)
        return failed
    
    async def _send_register(self, clu_id: str) -> bool:
        """Register every session of a CLU and store the values from the answers.
        
        Returns True when the whole state was refreshed from the CLU.
        """
        if clu_id not in self._apis:
            _LOGGER.warning("[%s] No API found for CLU during state registration", clu_id)
            return False
        
        if not self.state.clus[clu_id].has_states_to_register():
            _LOGGER.debug("[%s] No component states to register", clu_id)
            return True
        
        failed = await self._register_sessions(clu_id, self._get_sessions(clu_id))
        return not failed
    
    @callback
    def _apply_register_values(
        self,
        clu_id: str,
        keys: tuple[GrentonCluStateVariableKey | GrentonCluStateAttributeKey, ...],
        values: list[GrentonValue],
        session_id: int,
    ) -> bool:
        """Store the values of a register answer. Returns False to reject them."""
        if not values:
            return False
        matched = self._matched_values(clu_id, list(keys), values, f"Register answer of session {session_id}")
        if matched is None:
            return False
        self.state.clus[clu_id].apply_values(keys, matched)
        self.async_set_updated_data(self.state.__dict__)
        # Values arrived in a register response -> resync origin.
        self._notify_gesture_listeners(clu_id, "resync")
        return True
    
    async def _renew_loop(self) -> None:
        """Renew the sessions of every available CLU every RENEW_INTERVAL.
        
        CLUs that are not available are resynced on contact instead.
        """
        while True:
            try:
                await asyncio.sleep(RENEW_INTERVAL)
                tasks = [
                    self._run_sync(clu_id, "renew") for clu_id in self._apis
                    if self.is_clu_available(clu_id)
                ]
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                _LOGGER.debug("Renew loop cancelled")
                break
            except Exception as e:
                _LOGGER.error("Unexpected error in renew loop: %s", e)
    
    async def execute_action(self, action: GrentonAction) -> None:
        api = self._apis.get(action.clu_id)
        if not api:
            _LOGGER.warning("[%s] No API found for CLU during action execution", action.clu_id)
            return
        
        # Fail fast while the CLU is not responding, instead of queueing the
        # request behind timeouts and retries. Pings keep running and detect
        # the reconnect.
        connectivity = self._connectivity.get(action.clu_id)
        if connectivity is not None and not connectivity.connected:
            _LOGGER.debug("[%s] CLU not connected, rejecting action: %s",
                          action.clu_id, GrentonCluApiActionRequest.from_action(action).payload)
            raise HomeAssistantError(f"Grenton CLU {action.clu_id} is not responding")
        
        # State is never set optimistically here. Entity state only changes
        # when the CLU reports a new value, so after a failed action the entity
        # keeps showing the real (previous) state.
        #
        # A command the CLU never confirmed raises, so the service call that
        # sent it fails visibly (an error in the UI, a stopped automation run)
        # instead of looking successful. The CLU may still have executed it:
        # only its answer is known to be missing.
        # Capture the payload now: entities mutate their action objects,
        # and the request may wait in the CLU queue before it is sent.
        payload = GrentonCluApiActionRequest.from_action(action).payload
        try:
            result = await api.execute_action(action)
        except Exception as e:
            _LOGGER.error("[%s] Error executing action: %s", action.clu_id, e)
            raise HomeAssistantError(
                f"Grenton CLU {action.clu_id}: error sending {payload}: {e}"
            ) from e
        if result is ActionResult.FAILED:
            raise HomeAssistantError(
                f"Grenton CLU {action.clu_id} did not confirm {payload}"
            )
    
    @callback
    def _process_report(self, clu_id: str, session_id: int, values: list[GrentonValue]) -> None:
        """Process a report pushed by a CLU for one of its sessions.
        
        Args:
            clu_id: The CLU identifier
            session_id: The session the report belongs to
            values: Parsed values, in the order of the session's keys
        """
        session = self._find_session(clu_id, session_id)
        if session is None:
            # E.g. a session of an earlier run that has not expired yet.
            if (clu_id, session_id) not in self._unknown_sessions_logged:
                self._unknown_sessions_logged.add((clu_id, session_id))
                _LOGGER.debug("[%s] Ignoring report of unknown session %d", clu_id, session_id)
            return
        matched = self._matched_values(clu_id, list(session.keys), values, f"Report of session {session_id}")
        if matched is None:
            return
        self.state.clus[clu_id].apply_values(session.keys, matched)

        self.async_set_updated_data(self.state.__dict__)
        # Values arrived in a clientReport -> push origin. Notified only after
        # the new values are stored above.
        self._notify_gesture_listeners(clu_id, "push")
        _LOGGER.debug("[%s] Processed report of session %d with %d values", clu_id, session_id, len(values))

    def _matched_values(
        self,
        clu_id: str,
        keys: list[GrentonCluStateVariableKey | GrentonCluStateAttributeKey],
        values: list[GrentonValue],
        origin: str,
    ) -> list[GrentonValue] | None:
        """Return the values to apply to ``keys``, or None to ignore them.

        A report with more values than keys cannot be matched to the keys
        safely and is ignored. With fewer values (the CLU may leave out
        trailing nils), the values present are applied to the first keys.
        Each kind of mismatch is logged once.
        """
        if len(values) == len(keys):
            return values
        too_many = len(values) > len(keys)
        log_key = (clu_id, origin, len(keys), len(values))
        if log_key not in self._count_mismatch_logged:
            self._count_mismatch_logged.add(log_key)
            _LOGGER.warning(
                "[%s] %s has %d values for %d subscribed keys; %s",
                clu_id, origin, len(values), len(keys),
                "ignored" if too_many else f"applied to the first {len(values)} keys",
            )
        return None if too_many else values

    def register_component_state(self, state: GrentonStateObject) -> None:
        self.state.register_state(state)

    @callback
    def async_add_gesture_listener(
        self,
        state: GrentonStateObject,
        callback_fn: Callable[[GrentonValue, str], None],
    ) -> Callable[[], None]:
        """Register a gesture listener and return a function to remove it.

        The callback is invoked on the event loop with (value, origin) whenever
        this state's CLU stores new values, where value is the current value of
        this state's key only.
        """
        entry = (state, callback_fn)
        self._gesture_listeners.append(entry)

        @callback
        def remove_listener() -> None:
            try:
                self._gesture_listeners.remove(entry)
            except ValueError:
                pass

        return remove_listener

    @callback
    def _notify_gesture_listeners(self, clu_id: str, origin: str) -> None:
        """Notify gesture listeners for a CLU with their key's current value."""
        for state, callback_fn in list(self._gesture_listeners):
            if state.clu_id != clu_id:
                continue
            value = self.state.get_value_for_component(state)
            try:
                callback_fn(value, origin)
            except Exception as e:  # pragma: no cover - defensive
                _LOGGER.error("[%s] Error in gesture listener: %s", clu_id, e)
    
    async def async_setup(self) -> None:
        # Connect all APIs
        connection_tasks: list[Any] = []
        for clu in self.clus:
            api = self._apis[clu.id]
            connection_tasks.append(self._connect_api(api))
        
        # Wait for all connections to complete
        await asyncio.gather(*connection_tasks, return_exceptions=True)
        
        # Send initial pings to establish sessions
        ping_tasks = [self._send_ping(clu_id) for clu_id in self._apis.keys()]
        if ping_tasks:
            await asyncio.gather(*ping_tasks, return_exceptions=True)
        
        # Send initial registrations. This also marks each responding CLU as
        # synced, so its entities start available.
        register_tasks = [self._async_sync(clu_id, "startup") for clu_id in self.state.clus.keys()]
        if register_tasks:
            await asyncio.gather(*register_tasks, return_exceptions=True)
        self._started = True
        
        # Start background tasks
        self._ping_task = asyncio.create_task(self._ping_loop())
        self._renew_task = asyncio.create_task(self._renew_loop())
    
    async def _connect_api(self, api: GrentonCluApi) -> None:
        """Connect a single API instance."""
        try:
            success = await api.connect()
            if not success:
                _LOGGER.error("Failed to connect API for CLU %s", api.clu.id)
                return
            
            # Set subscription callback to handle reports
            if api.protocol:
                api.protocol.subscription_callback = (
                    lambda session_id, values: self._process_report(api.clu.id, session_id, values)
                )
                api.protocol.contact_callback = lambda: self._handle_contact(api.clu.id)
            else:
                _LOGGER.error("Failed to set subscription callback for CLU %s", api.clu.id)
        except Exception as e:
            _LOGGER.error("Error connecting API for CLU %s: %s", api.clu.id, e)
    
    async def async_shutdown(self) -> None:
        # Cancel background tasks
        if hasattr(self, '_ping_task'):
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass
            _LOGGER.debug("Cancelled ping task")
        
        if hasattr(self, '_renew_task'):
            self._renew_task.cancel()
            try:
                await self._renew_task
            except asyncio.CancelledError:
                pass
            _LOGGER.debug("Cancelled renew task")
        
        for task in self._sync_tasks.values():
            task.cancel()
        await asyncio.gather(*self._sync_tasks.values(), return_exceptions=True)
        self._sync_tasks.clear()
        
        # Disconnect all APIs
        disconnect_tasks: list[Any] = []
        for api in self._apis.values():
            disconnect_tasks.append(self._disconnect_api(api))
        
        await asyncio.gather(*disconnect_tasks, return_exceptions=True)
        self._apis.clear()
    
    async def _disconnect_api(self, api: GrentonCluApi) -> None:
        """Disconnect a single API instance."""
        try:
            await api.disconnect()
        except Exception as e:
            _LOGGER.error("Error disconnecting API for CLU %s: %s", api.clu.id, e)
    
    def get_value_for_component(self, state: GrentonStateObject) -> GrentonValue | None:
        """Get the value for a component from the state."""
        return self.state.get_value_for_component(state)
