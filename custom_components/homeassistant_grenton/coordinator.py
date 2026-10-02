from __future__ import annotations
from typing import Any, Callable

import logging
import asyncio
from datetime import datetime

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .domain.clu import GrentonClu
from .domain.encryption import GrentonEncryption
from .domain.state_object import GrentonStateObject
from .domain.action import GrentonAction
from .domain.api.clu import GrentonCluApi
from .domain.connectivity import CluConnectivity, ConnectivityTransition
from .state import GrentonState, GrentonCluState, GrentonCluStateVariableKey, GrentonCluStateAttributeKey, GrentonValue
from .domain.api.clu_messages import GrentonCluApiActionRequest

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

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
        self._resync_tasks: dict[str, asyncio.Task[None]] = {}
        # Last contact before the CLU went disconnected, for the reconnect log.
        self._offline_since: dict[str, datetime | None] = {}
        # Set once async_setup has done the initial sync. Before that, contact
        # does not start a resync because setup registers states itself.
        self._started = False

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
            success = await api.ping()
        except Exception as e:
            _LOGGER.debug("[%s] Error during ping: %s", clu_id, e)
            success = False
        
        # A successful ping is recorded as contact when its response arrives.
        if not success:
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
        transition = connectivity.record_contact(dt_util.utcnow())
        
        if transition is ConnectivityTransition.RECONNECTED:
            _LOGGER.debug("[%s] CLU responded, refreshing its state before marking entities available", clu_id)
            # Only the connectivity sensor changes here: entities stay
            # unavailable until the state refresh completes.
        
        if self._started and not connectivity.synced:
            self._schedule_resync(clu_id)
        
        self._notify_connectivity_listeners(clu_id)
    
    @callback
    def _schedule_resync(self, clu_id: str) -> None:
        task = self._resync_tasks.get(clu_id)
        if task is not None and not task.done():
            return
        self._resync_tasks[clu_id] = asyncio.create_task(self._async_resync(clu_id))
    
    async def _async_resync(self, clu_id: str) -> None:
        """Re-register the client report and refresh all states from the CLU.
        
        Entities become available only after this succeeds. On failure the
        next contact (e.g. the next ping response) starts another attempt.
        """
        if not await self._send_register(clu_id):
            _LOGGER.debug("[%s] State refresh failed, retrying on next contact", clu_id)
            return
        
        connectivity = self._connectivity[clu_id]
        if not connectivity.mark_synced():
            return
        
        if clu_id in self._offline_since:
            last_contact = self._offline_since.pop(clu_id)
            _LOGGER.info("[%s] CLU is responding again (last contact before the outage: %s); "
                         "state refreshed from the CLU, entities available",
                         clu_id, last_contact.isoformat() if last_contact else "never")
        else:
            _LOGGER.debug("[%s] CLU state synced", clu_id)
        self._async_connectivity_changed(clu_id)
    
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
    
    async def _send_register(self, clu_id: str) -> bool:
        """Register the client report and store the values from the response.
        
        Returns True when the state was refreshed from the CLU.
        """
        api = self._apis.get(clu_id)
        if not api:
            _LOGGER.warning("[%s] No API found for CLU during state registration", clu_id)
            return False
        
        clu_state = self.state.clus[clu_id]
        
        # Check if there are any states to register
        if not clu_state.has_states_to_register():
            _LOGGER.debug("[%s] No component states to register", clu_id)
            return True
        
        try:
            keys = clu_state.get_subscription_order()
            values = await api.register_component_states(keys)
            if values:
                # Update state by zipping keys with values
                for key, value in zip(keys, values):
                    if isinstance(key, GrentonCluStateVariableKey):
                        clu_state.set_variable(key, value)
                    elif isinstance(key, GrentonCluStateAttributeKey): # type: ignore
                        clu_state.set_attribute(key, value)
                self.async_set_updated_data(self.state.__dict__)
                # Values arrived in a register response -> resync origin.
                self._notify_gesture_listeners(clu_id, "resync")
                return True
        except Exception as e:
            _LOGGER.error("[%s] Error during registration: %s", clu_id, e)
        return False
    
    async def _register_loop(self) -> None:
        while True:
            try:
                # Only register if there are actual states to register.
                # CLUs that are not available are resynced on contact instead.
                clu_ids_to_register = [
                    clu_id for clu_id, clu_state in self.state.clus.items()
                    if clu_state.has_states_to_register() and self.is_clu_available(clu_id)
                ]
                
                if not clu_ids_to_register:
                    # No states to register, wait longer before checking again
                    await asyncio.sleep(45)
                    continue
                
                # Registration interval
                registration_interval = 45
                await asyncio.sleep(registration_interval)
                
                tasks = [
                    self._send_register(clu_id) for clu_id in clu_ids_to_register
                    if self.is_clu_available(clu_id)
                ]
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                _LOGGER.debug("Register loop cancelled")
                break
            except Exception as e:
                _LOGGER.error("Unexpected error in register loop: %s", e)
    
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
        try:
            # Capture the payload now: entities mutate their action objects,
            # and the request may wait in the CLU queue before it is sent.
            payload = GrentonCluApiActionRequest.from_action(action).payload
            success = await api.execute_action(action)
            if not success:
                _LOGGER.warning("[%s] Action execution failed for payload: %s", action.clu_id, payload)
        except Exception as e:
            _LOGGER.error("[%s] Error executing action: %s", action.clu_id, e)
    
    async def _process_report(self, clu_id: str, values: list[GrentonValue]) -> None:
        """Process a report from a CLU and update state values.
        
        Args:
            clu_id: The CLU identifier
            values: Parsed values from the report
        """
        clu_state = self.state.clus[clu_id]
        clu_state.update_state(values)

        self.async_set_updated_data(self.state.__dict__)
        # Values arrived in a clientReport -> push origin. Notified only after
        # the new values are stored above.
        self._notify_gesture_listeners(clu_id, "push")
        _LOGGER.debug("[%s] Processed report with %d values", clu_id, len(values))

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
        register_tasks = [self._async_resync(clu_id) for clu_id in self.state.clus.keys()]
        if register_tasks:
            await asyncio.gather(*register_tasks, return_exceptions=True)
        self._started = True
        
        # Start background tasks
        self._ping_task = asyncio.create_task(self._ping_loop())
        self._register_task = asyncio.create_task(self._register_loop())
    
    async def _connect_api(self, api: GrentonCluApi) -> None:
        """Connect a single API instance."""
        try:
            success = await api.connect()
            if not success:
                _LOGGER.error("Failed to connect API for CLU %s", api.clu.id)
                return
            
            # Set subscription callback to handle reports
            if api.protocol:
                async def handle_subscription(values: list[GrentonValue]) -> None:
                    await self._process_report(api.clu.id, values)
                api.protocol.subscription_callback = handle_subscription
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
        
        if hasattr(self, '_register_task'):
            self._register_task.cancel()
            try:
                await self._register_task
            except asyncio.CancelledError:
                pass
            _LOGGER.debug("Cancelled register task")
        
        for task in self._resync_tasks.values():
            task.cancel()
        await asyncio.gather(*self._resync_tasks.values(), return_exceptions=True)
        self._resync_tasks.clear()
        
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
