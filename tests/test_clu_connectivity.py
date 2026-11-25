"""Tests for CLU connectivity tracking, availability and fail-fast actions.

The coordinator tests need Home Assistant installed. The CLU is simulated by
a fake UDP transport that answers pings, clientRegister and actions like a
real CLU, or drops everything while it is "hung".
"""

import asyncio
import base64
import logging
import os
import re
import sys
import types
from pathlib import Path

import pytest

_PACKAGE_DIR = (
    Path(__file__).resolve().parent.parent / "custom_components" / "homeassistant_grenton"
)

# Stub the package modules so the integration's __init__ is not executed.
for _name, _path in (
    ("custom_components", _PACKAGE_DIR.parent),
    ("custom_components.homeassistant_grenton", _PACKAGE_DIR),
):
    if _name not in sys.modules:
        _module = types.ModuleType(_name)
        _module.__path__ = [str(_path)]
        sys.modules[_name] = _module

from custom_components.homeassistant_grenton.domain.connectivity import (  # noqa: E402
    CluConnectivity,
    ConnectivityTransition,
)

CLU_ID = "CLU221010475"
CLU_IP = "192.168.1.10"
NOW = __import__("datetime").datetime(2026, 10, 2, 13, 41)


# --- Pure state machine -------------------------------------------------------


def test_mark_unsynced_makes_entities_unavailable_until_synced():
    conn = CluConnectivity()
    conn.record_contact(NOW)
    conn.mark_synced()
    assert conn.mark_unsynced()
    assert conn.connected is True
    assert not conn.available
    assert not conn.mark_unsynced()
    assert conn.mark_synced()
    assert conn.available


def test_three_failed_pings_disconnect():
    conn = CluConnectivity()
    conn.record_contact(NOW)
    assert conn.mark_synced()

    assert conn.record_ping_failure() is ConnectivityTransition.NONE
    assert conn.record_ping_failure() is ConnectivityTransition.NONE
    assert conn.available
    assert conn.record_ping_failure() is ConnectivityTransition.DISCONNECTED
    assert conn.connected is False
    assert not conn.available
    # Further failures do not report the transition again.
    assert conn.record_ping_failure() is ConnectivityTransition.NONE


def test_contact_resets_failed_ping_count():
    conn = CluConnectivity()
    conn.record_contact(NOW)
    conn.mark_synced()
    conn.record_ping_failure()
    conn.record_ping_failure()
    conn.record_contact(NOW)
    assert conn.failed_pings == 0
    assert conn.record_ping_failure() is ConnectivityTransition.NONE
    assert conn.connected is True


def test_reconnect_needs_sync_before_available():
    conn = CluConnectivity()
    conn.record_contact(NOW)
    conn.mark_synced()
    for _ in range(3):
        conn.record_ping_failure()

    assert conn.record_contact(NOW) is ConnectivityTransition.RECONNECTED
    assert conn.connected is True
    assert not conn.available
    assert conn.mark_synced()
    assert conn.available


def test_mark_synced_ignored_while_disconnected():
    conn = CluConnectivity()
    for _ in range(3):
        conn.record_ping_failure()
    assert not conn.mark_synced()
    assert not conn.available


# --- Coordinator and entities (need Home Assistant) ---------------------------

ha_core = pytest.importorskip("homeassistant.core")

from homeassistant.exceptions import HomeAssistantError  # noqa: E402
from homeassistant.helpers import frame  # noqa: E402

from custom_components.homeassistant_grenton import coordinator as coordinator_module  # noqa: E402
from custom_components.homeassistant_grenton.domain.action import GrentonActionAttribute  # noqa: E402
from custom_components.homeassistant_grenton.domain.api import clu as clu_api  # noqa: E402
from custom_components.homeassistant_grenton.domain.clu import GrentonClu  # noqa: E402
from custom_components.homeassistant_grenton.domain.encryption import GrentonEncryption  # noqa: E402
from custom_components.homeassistant_grenton.domain.entities.bistable_switch import (  # noqa: E402
    GrentonEntityBistableSwitch,
)
from custom_components.homeassistant_grenton.domain.entities.clu_connectivity import (  # noqa: E402
    GrentonEntityCluConnectivity,
)
from custom_components.homeassistant_grenton.domain.enums import (  # noqa: E402
    GrentonActionEventType,
    GrentonUnit,
)
from custom_components.homeassistant_grenton.domain.state_object import (  # noqa: E402
    GrentonAttributeValueObject,
)
from custom_components.homeassistant_grenton.mappers.device_clu import DeviceCluMapper  # noqa: E402
from custom_components.homeassistant_grenton.domain import registration  # noqa: E402
from custom_components.homeassistant_grenton.const import EVENT_CLU_RESTARTED  # noqa: E402
from homeassistant.util import dt as dt_util  # noqa: E402


REGISTER_RE = re.compile(r"^SYSTEM:clientRegister\(0,0,(\d+),\{(.*)\}\)$")


class FakeClu:
    """Fake UDP transport answering like a CLU with two digital outputs.

    Like a real CLU it keeps the registered sessions and repeats their
    session ID in every report.
    """

    def __init__(self, protocol, cipher):
        self.protocol = protocol
        self.cipher = cipher
        self.hung = False
        # Answers checkAlive() with "emergency" and nothing else.
        self.emergency = False
        self.outputs = {"DOU0001": 1, "DOU0002": 0}
        # Seconds since the CLU booted, answered to CLU...:get(0)
        self.uptime: object = 100000
        # False: uptime queries get no answer
        self.answer_uptime = True
        self.sent: list[str] = []
        # session ID -> object names, in registration order
        self.sessions: dict[int, list[str]] = {}

    def sendto(self, data, addr):
        plaintext = self.cipher.decrypt(data).decode()
        _, _, msg_id, payload = plaintext.split(":", 3)
        self.sent.append(payload)
        if self.hung:
            return
        if self.emergency:
            if payload == "checkAlive()":
                asyncio.get_running_loop().call_soon(self._respond, msg_id, "emergency")
            return
        match = REGISTER_RE.match(payload)
        if match:
            session_id = int(match.group(1))
            self.sessions[session_id] = re.findall(r"\{(\w+),\d+\}", match.group(2))
            answer = self._report(session_id)
        elif payload == f"{CLU_ID}:get(0)":
            if not self.answer_uptime:
                return
            answer = str(self.uptime)
        else:
            answer = "0"
        asyncio.get_running_loop().call_soon(self._respond, msg_id, answer)

    def _report(self, session_id):
        values = ",".join(str(self.outputs[name]) for name in self.sessions[session_id])
        return f"clientReport:{session_id}:{{{values}}}"

    def reboot(self):
        """Restart: sessions are lost and outputs start off."""
        self.uptime = 5
        self.sessions.clear()
        self.outputs = {name: 0 for name in self.outputs}

    def push_report(self):
        for session_id in self.sessions:
            self._respond("00000000", self._report(session_id))

    def push_raw(self, table, session_id=None):
        if session_id is None:
            session_id = next(iter(self.sessions))
        self._respond("00000000", f"clientReport:{session_id}:{table}")

    def _respond(self, msg_id, payload):
        wire = f"resp:{CLU_IP}:{msg_id}:{payload}".encode()
        self.protocol.datagram_received(self.cipher.encrypt(wire), (CLU_IP, 1234))

    def close(self):
        pass


def make_switch(coordinator, object_name):
    def action(event, value):
        return GrentonActionAttribute(
            clu_id=CLU_ID, object_name=object_name, event=event, value=value, index="0"
        )

    return GrentonEntityBistableSwitch(
        coordinator=coordinator,
        id=f"switch_{object_name}",
        unit=GrentonUnit.UNKNOWN,
        state_object=GrentonAttributeValueObject(clu_id=CLU_ID, object_name=object_name, index="0"),
        action_on=action(GrentonActionEventType.ON, "1"),
        action_off=action(GrentonActionEventType.OFF, "0"),
    )


class Harness:
    def __init__(self, hass, coordinator, fake, switches, sensor, boot_sensor):
        self.hass = hass
        self.coordinator = coordinator
        self.fake = fake
        self.switches = switches
        self.sensor = sensor
        self.boot_sensor = boot_sensor
        self.sensor_writes = 0
        self.boot_sensor_writes = 0
        # (event data, switch availability, switch states) per restart event
        self.restarts: list[tuple[dict, list[bool], list[bool | None]]] = []
        # Switch availability at every coordinator update
        self.availability: list[list[bool]] = []
        # Every state write as HA would get it, in order, with its time:
        # ("switch", unique_id, available, is_on, time)
        # ("last_boot", boot time, restart_detected_at, time)
        # ("event", data, time)
        self.timeline: list[tuple] = []

    async def ping(self):
        await self.coordinator._send_ping(CLU_ID)
        await self.settle()

    async def settle(self):
        # Let response handling and sync tasks (resync, restart checks) run.
        for _ in range(20):
            await asyncio.sleep(0)
        running = [t for t in self.coordinator._sync_tasks.values() if not t.done()]
        if running:
            await asyncio.wait(running, timeout=2)
        for _ in range(5):
            await asyncio.sleep(0)


async def setup_harness(tmp_path, monkeypatch, start_hung=False, uptime=100000):
    # Short timeouts so requests to a hung CLU fail quickly.
    for name in ("RESPONSE_TIMEOUT", "PING_TIMEOUT", "REGISTER_TIMEOUT", "ACTION_TIMEOUT", "QUERY_TIMEOUT", "DESTROY_TIMEOUT"):
        monkeypatch.setattr(clu_api, name, 0.05)
    monkeypatch.setattr(coordinator_module, "RENEW_RETRY_DELAY", 0.01)

    hass = ha_core.HomeAssistant(str(tmp_path))
    frame.async_setup(hass)
    encryption = GrentonEncryption(
        key=base64.b64encode(os.urandom(16)).decode(),
        iv=base64.b64encode(os.urandom(16)).decode(),
    )
    clu = GrentonClu(id=CLU_ID, serial_number="221010475", name=CLU_ID, ip=CLU_IP, port=1234)
    coordinator = coordinator_module.GrentonCoordinator(hass, None, [clu], encryption)

    api = coordinator._apis[CLU_ID]
    holder = {}

    async def fake_connect():
        api.protocol = clu_api.GrentonCluApiProtocol(api)
        api.transport = holder["fake"] = FakeClu(api.protocol, api.cipher)
        holder["fake"].hung = start_hung
        holder["fake"].uptime = uptime
        return True

    api.connect = fake_connect

    switches = [make_switch(coordinator, "DOU0001"), make_switch(coordinator, "DOU0002")]
    [device] = DeviceCluMapper.to_domain([clu], coordinator)
    sensor, boot_sensor = device.entities

    restarts = []

    timeline: list[tuple] = []

    @ha_core.callback
    def on_restart(event):
        restarts.append((dict(event.data), [s.available for s in switches], [s.is_on for s in switches]))
        timeline.append(("event", dict(event.data), dt_util.utcnow()))

    hass.bus.async_listen(EVENT_CLU_RESTARTED, on_restart)

    await coordinator.async_setup()
    harness = Harness(hass, coordinator, holder["fake"], switches, sensor, boot_sensor)
    harness.restarts = restarts
    harness.timeline = timeline
    coordinator.async_add_listener(lambda: harness.availability.append([s.available for s in switches]))

    # The switches write their state on every coordinator update, as
    # CoordinatorEntity does once added to HA.
    for switch in switches:
        def record_switch(switch=switch):
            timeline.append(("switch", switch.unique_id, switch.available, switch.is_on, dt_util.utcnow()))

        switch.async_write_ha_state = record_switch
        coordinator.async_add_listener(switch._handle_coordinator_update)

    # Count sensor state writes without a full entity platform.
    def count_write():
        harness.sensor_writes += 1

    def count_boot_write():
        harness.boot_sensor_writes += 1
        timeline.append(("last_boot", boot_sensor.native_value,
                         boot_sensor.extra_state_attributes["restart_detected_at"], dt_util.utcnow()))

    sensor.async_write_ha_state = count_write
    sensor._remember_written()
    sensor.async_on_remove(
        coordinator.async_add_connectivity_listener(CLU_ID, sensor._handle_connectivity_update)
    )
    boot_sensor.async_write_ha_state = count_boot_write
    boot_sensor.async_on_remove(
        coordinator.async_add_boot_listener(CLU_ID, boot_sensor._handle_boot_time_update)
    )
    return harness


async def teardown(harness):
    await harness.coordinator.async_shutdown()


def run(coro):
    return asyncio.run(coro)


def test_startup_connected_and_entities_available(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            assert h.sensor.is_on is True
            assert h.sensor.available is True
            assert all(s.available for s in h.switches)
            assert h.switches[0].is_on is True
            assert h.switches[1].is_on is False
            assert h.sensor.extra_state_attributes["last_contact"] is not None
        finally:
            await teardown(h)

    run(scenario())


def test_three_failed_pings_make_entities_unavailable(tmp_path, monkeypatch, caplog):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            last_contact = h.coordinator.get_connectivity(CLU_ID).last_contact
            h.fake.hung = True

            await h.ping()
            await h.ping()
            assert h.sensor.is_on is True
            assert all(s.available for s in h.switches)

            await h.ping()
            assert h.sensor.is_on is False
            assert h.sensor.available is True  # the sensor itself stays available
            assert h.sensor_writes == 1
            assert not any(s.available for s in h.switches)
            assert h.sensor.extra_state_attributes["last_contact"] == last_contact

            # More failed pings while disconnected: no more state writes.
            await h.ping()
            await h.ping()
            assert h.sensor_writes == 1
        finally:
            await teardown(h)

    with caplog.at_level(logging.DEBUG):
        run(scenario())

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "not responding" in warnings[0].getMessage()


def test_reconnect_refreshes_state_from_clu_before_available(tmp_path, monkeypatch, caplog):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            assert h.switches[0].is_on is True
            h.fake.hung = True
            for _ in range(3):
                await h.ping()
            assert not any(s.available for s in h.switches)

            # Power cycle: all outputs start off and the subscription is gone.
            h.fake.outputs = {"DOU0001": 0, "DOU0002": 0}
            h.fake.hung = False
            h.fake.sent.clear()
            writes_before = h.sensor_writes

            await h.ping()

            assert any(p.startswith("SYSTEM:clientRegister") for p in h.fake.sent)
            assert h.sensor.is_on is True
            assert h.sensor_writes > writes_before
            assert all(s.available for s in h.switches)
            # State comes from the CLU, not from the pre-outage cache.
            assert h.switches[0].is_on is False
            assert h.switches[1].is_on is False
        finally:
            await teardown(h)

    with caplog.at_level(logging.DEBUG):
        run(scenario())

    infos = [
        r for r in caplog.records
        if r.levelno == logging.INFO and "responding again" in r.getMessage()
    ]
    assert len(infos) == 1


def test_entities_stay_unavailable_until_refresh_succeeds(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            h.fake.hung = True
            for _ in range(3):
                await h.ping()

            # The CLU answers pings but drops clientRegister (still booting).
            original_sendto = h.fake.sendto

            def drop_register(data, addr):
                plaintext = h.fake.cipher.decrypt(data).decode()
                if "clientRegister" in plaintext:
                    h.fake.sent.append(plaintext.split(":", 3)[3])
                    return
                original_sendto(data, addr)

            h.fake.hung = False
            h.fake.sendto = drop_register
            await h.ping()
            await asyncio.sleep(0.1)  # register request times out
            await h.settle()
            assert h.sensor.is_on is True
            assert not any(s.available for s in h.switches)

            # Next ping response triggers another refresh, which succeeds.
            h.fake.sendto = original_sendto
            await h.ping()
            assert all(s.available for s in h.switches)
        finally:
            await teardown(h)

    run(scenario())


def test_client_report_push_counts_as_contact(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            h.fake.hung = True
            await h.ping()
            await h.ping()
            assert h.coordinator.get_connectivity(CLU_ID).failed_pings == 2

            h.fake.push_report()
            await h.settle()
            assert h.coordinator.get_connectivity(CLU_ID).failed_pings == 0

            await h.ping()
            assert h.sensor.is_on is True
        finally:
            await teardown(h)

    run(scenario())


def test_actions_fail_fast_while_disconnected(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            h.fake.hung = True
            for _ in range(3):
                await h.ping()

            protocol = h.coordinator._apis[CLU_ID].protocol
            calls = []
            original_send_request = protocol.send_request

            async def tracking_send_request(*args, **kwargs):
                calls.append(args)
                return await original_send_request(*args, **kwargs)

            protocol.send_request = tracking_send_request
            h.fake.sent.clear()

            loop = asyncio.get_running_loop()
            start = loop.time()
            with pytest.raises(HomeAssistantError):
                await h.switches[0].async_turn_on()
            assert loop.time() - start < 0.01
            assert calls == []
            assert h.fake.sent == []
            assert protocol._waiting == 0 and protocol._in_flight == 0

            # Pings still go out, so the reconnect is detected.
            await h.ping()
            assert len(calls) == 1
        finally:
            await teardown(h)

    run(scenario())


def test_actions_work_while_connected(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            h.fake.sent.clear()
            await h.switches[1].async_turn_on()
            assert h.fake.sent == ['DOU0002:set(0,"1")']
        finally:
            await teardown(h)

    run(scenario())


def test_ping_responses_do_not_write_sensor_state_every_time(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            for _ in range(10):
                await h.ping()
            assert h.sensor_writes == 0
        finally:
            await teardown(h)

    run(scenario())


def test_connectivity_sensor_definition(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            sensor: GrentonEntityCluConnectivity = h.sensor
            assert sensor.device_class == "connectivity"
            assert sensor.entity_category == "diagnostic"
            assert sensor.has_entity_name
            assert not hasattr(sensor, "_attr_name")  # named by device class
            assert sensor.device_info["name"] == "Grenton CLU"
            assert sensor.unique_id == f"clu_{CLU_ID}_connectivity"
            # Suggested on first registration; HA automations reference it.
            assert sensor.entity_id == "binary_sensor.grenton_clu_connectivity"
        finally:
            await teardown(h)

    run(scenario())


def test_report_with_too_many_values_is_ignored(tmp_path, monkeypatch, caplog):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            assert h.switches[0].is_on is True
            assert h.switches[1].is_on is False
            for _ in range(2):
                h.fake.push_raw("{0,1,1}")
                await h.settle()
            assert h.switches[0].is_on is True
            assert h.switches[1].is_on is False
        finally:
            await teardown(h)

    with caplog.at_level(logging.WARNING):
        run(scenario())

    warnings = [r.getMessage() for r in caplog.records if "values for 2 subscribed keys" in r.getMessage()]
    assert len(warnings) == 1
    assert re.fullmatch(r"\[CLU221010475\] Report of session \d+ has 3 values for 2 subscribed keys; ignored",
                        warnings[0])


def test_report_with_fewer_values_updates_first_keys(tmp_path, monkeypatch, caplog):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            h.fake.push_raw("{0}")
            await h.settle()
            assert h.switches[0].is_on is False
            assert h.switches[1].is_on is False
        finally:
            await teardown(h)

    with caplog.at_level(logging.WARNING):
        run(scenario())

    assert any("applied to the first 1 keys" in r.getMessage() for r in caplog.records)


def test_string_with_comma_does_not_shift_later_values(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            h.fake.push_raw('{"a,b",1}')
            await h.settle()
            assert h.coordinator.get_value_for_component(h.switches[0].state_object) == "a,b"
            assert h.switches[1].is_on is True
        finally:
            await teardown(h)

    run(scenario())


def test_nil_in_register_answer_shows_unknown_not_off(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            h.fake.outputs = {"DOU0001": "nil", "DOU0002": 1}
            assert await h.coordinator._run_sync(CLU_ID, "renew") is True
            await h.settle()
            assert h.coordinator.get_value_for_component(h.switches[0].state_object) is None
            assert h.switches[0].is_on is None
            assert h.switches[1].is_on is True
        finally:
            await teardown(h)

    run(scenario())


def test_unconfirmed_action_raises(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            original_sendto = h.fake.sendto

            def drop_actions(data, addr):
                plaintext = h.fake.cipher.decrypt(data).decode()
                if ":set(" in plaintext:
                    h.fake.sent.append(plaintext.split(":", 3)[3])
                    return
                original_sendto(data, addr)

            h.fake.sendto = drop_actions
            h.fake.sent.clear()
            with pytest.raises(HomeAssistantError, match="did not confirm"):
                await h.switches[1].async_turn_on()
            assert h.fake.sent == ['DOU0002:set(0,"1")'] * 3
            # The CLU still answers pings: the CLU stays connected.
            await h.ping()
            assert h.sensor.is_on is True
        finally:
            await teardown(h)

    run(scenario())


def test_each_session_is_registered_and_reports_only_its_keys(tmp_path, monkeypatch):
    # One entry per session, so the two switches are in two sessions.
    monkeypatch.setattr(registration, "MAX_ENTRIES_PER_SESSION", 1)

    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            sessions = h.coordinator._get_sessions(CLU_ID)
            assert [len(s.keys) for s in sessions] == [1, 1]
            assert sorted(h.fake.sessions) == [s.session_id for s in sessions]
            assert h.switches[0].is_on is True
            assert h.switches[1].is_on is False

            # A report of the second session updates only the second switch.
            h.fake.push_raw("{1}", session_id=sessions[1].session_id)
            await h.settle()
            assert h.switches[0].is_on is True
            assert h.switches[1].is_on is True

            # A report of a session this client does not have is ignored.
            h.fake.push_raw("{0}", session_id=1)
            await h.settle()
            assert h.switches[0].is_on is True
            assert h.switches[1].is_on is True
        finally:
            await teardown(h)

    run(scenario())


def test_register_answer_for_another_session_fails_the_registration(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            original_report = h.fake._report
            h.fake._report = lambda session_id: original_report(session_id).replace(
                f"clientReport:{session_id}:", "clientReport:1:"
            )
            h.fake.outputs = {"DOU0001": 0, "DOU0002": 1}
            sessions = h.coordinator._get_sessions(CLU_ID)
            assert await h.coordinator._register_sessions(CLU_ID, sessions) == sessions
            # Values meant for another session were not applied.
            assert h.switches[0].is_on is True
            assert h.switches[1].is_on is False
        finally:
            await teardown(h)

    run(scenario())


def drop_next_registers(fake, count):
    """Make the fake CLU drop the next ``count`` clientRegister requests."""
    original_sendto = fake.sendto
    state = {"left": count}

    def sendto(data, addr):
        plaintext = fake.cipher.decrypt(data).decode()
        if "clientRegister" in plaintext and state["left"] > 0:
            state["left"] -= 1
            fake.sent.append(plaintext.split(":", 3)[3])
            return
        original_sendto(data, addr)

    fake.sendto = sendto


def test_renewal_interval_survives_one_lost_renewal():
    # vclu: a session not renewed within 60 s expires.
    assert coordinator_module.RENEW_INTERVAL * 2 <= 60


def test_renew_loop_renews_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(coordinator_module, "RENEW_INTERVAL", 0.02)

    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            h.fake.sent.clear()
            await asyncio.sleep(0.1)
            registers = [p for p in h.fake.sent if p.startswith("SYSTEM:clientRegister")]
            assert len(registers) >= 2
        finally:
            await teardown(h)

    run(scenario())


def test_failed_renewal_is_retried_after_a_short_delay(tmp_path, monkeypatch):
    monkeypatch.setattr(coordinator_module, "RENEW_RETRY_DELAY", 0.01)

    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            drop_next_registers(h.fake, 1)
            h.fake.sent.clear()
            h.fake.outputs = {"DOU0001": 0, "DOU0002": 1}
            assert await h.coordinator._run_sync(CLU_ID, "renew") is True
            registers = [p for p in h.fake.sent if p.startswith("SYSTEM:clientRegister")]
            assert len(registers) == 2 and registers[0] == registers[1]
            # The retry re-read the state.
            assert h.switches[0].is_on is False
            assert h.switches[1].is_on is True
            assert all(s.available for s in h.switches)
        finally:
            await teardown(h)

    run(scenario())


def test_emergency_mode_counts_as_not_responding(tmp_path, monkeypatch, caplog):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            h.fake.emergency = True
            for _ in range(3):
                await h.ping()
            assert h.sensor.is_on is False
            assert not any(s.available for s in h.switches)

            # Out of emergency mode: reconnect and refresh as after an outage.
            h.fake.emergency = False
            h.fake.outputs = {"DOU0001": 0, "DOU0002": 0}
            await h.ping()
            assert h.sensor.is_on is True
            assert all(s.available for s in h.switches)
            assert h.switches[0].is_on is False
        finally:
            await teardown(h)

    with caplog.at_level(logging.INFO):
        run(scenario())

    messages = [r.getMessage() for r in caplog.records]
    assert sum("emergency mode: its project is not running" in m for m in messages) == 1
    assert sum("no longer in emergency mode" in m for m in messages) == 1


def test_renewal_failing_twice_makes_entities_unavailable(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(coordinator_module, "RENEW_RETRY_DELAY", 0.01)

    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            drop_next_registers(h.fake, 2)
            assert await h.coordinator._run_sync(CLU_ID, "renew") is False
            assert h.sensor.is_on is True  # still answering pings
            assert not any(s.available for s in h.switches)

            # The next contact registers again; entities come back.
            h.fake.outputs = {"DOU0001": 0, "DOU0002": 1}
            await h.ping()
            await asyncio.sleep(0.05)
            await h.settle()
            assert all(s.available for s in h.switches)
            assert h.switches[1].is_on is True
        finally:
            await teardown(h)

    with caplog.at_level(logging.INFO):
        run(scenario())

    messages = [r.getMessage() for r in caplog.records]
    assert sum("did not accept 1 of its session(s) twice in a row" in m for m in messages) == 1
    assert sum("accepted its sessions again" in m for m in messages) == 1



# --- CLU restart detection ------------------------------------------------------


def assert_boot_time(h, uptime):
    expected = dt_util.utcnow().timestamp() - uptime
    assert abs(h.boot_sensor.native_value.timestamp() - expected) < 5


def test_startup_reads_boot_time_without_restart_event(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            assert_boot_time(h, 100000)
            assert h.boot_sensor.device_class == "timestamp"
            assert h.boot_sensor.entity_category == "diagnostic"
            assert h.boot_sensor.entity_id == "sensor.grenton_clu_last_boot"
            assert h.boot_sensor.unique_id == f"clu_{CLU_ID}_last_boot"
            assert h.restarts == []
            # Ordinary renewals do not change it.
            for _ in range(3):
                assert await h.coordinator._run_sync(CLU_ID, "renew") is True
            assert h.boot_sensor_writes == 0
            assert h.restarts == []
        finally:
            await teardown(h)

    run(scenario())


def test_restart_without_failed_pings_is_detected_at_renewal(tmp_path, monkeypatch, caplog):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            assert h.switches[0].is_on is True
            h.fake.reboot()
            h.availability.clear()

            assert await h.coordinator._run_sync(CLU_ID, "renew") is True

            # Entities went unavailable, then came back with the new state.
            assert [False, False] in h.availability
            assert h.availability[-1] == [True, True]
            # One event, fired when entities already show the new state.
            assert len(h.restarts) == 1
            data, available, states = h.restarts[0]
            assert available == [True, True]
            assert states == [False, False]
            assert data["clu_id"] == CLU_ID
            assert data["serial_number"] == "221010475"
            assert data["uptime"] == 5
            assert abs(dt_util.parse_datetime(data["boot_time"]).timestamp()
                       - (dt_util.utcnow().timestamp() - 5)) < 5
            # The last-boot sensor changed with it.
            assert h.boot_sensor_writes == 1
            assert_boot_time(h, 5)
            # The CLU never stopped answering pings.
            assert h.sensor.is_on is True
            assert h.sensor_writes == 0
        finally:
            await teardown(h)

    with caplog.at_level(logging.INFO):
        run(scenario())

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1 and "CLU restarted at" in warnings[0]


def test_restart_with_a_failed_ping_is_handled_on_the_next_answer(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            h.fake.hung = True
            await h.ping()
            h.fake.reboot()
            h.fake.hung = False
            h.fake.sent.clear()

            # No renewal needed: the answer to the next ping starts the check.
            await h.ping()

            assert h.fake.sent[1:3] == [f"{CLU_ID}:get(0)", h.fake.sent[2]]
            assert h.fake.sent[2].startswith("SYSTEM:clientRegister")
            assert len(h.restarts) == 1
            assert all(s.available for s in h.switches)
            assert h.switches[0].is_on is False
            assert h.sensor.is_on is True
        finally:
            await teardown(h)

    run(scenario())


def test_failed_ping_without_restart_renews_without_event(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            h.fake.hung = True
            await h.ping()
            h.fake.hung = False
            h.fake.sent.clear()
            await h.ping()

            assert any(p.startswith("SYSTEM:clientRegister") for p in h.fake.sent)
            assert h.restarts == []
            assert h.boot_sensor_writes == 0
            assert all(s.available for s in h.switches)
        finally:
            await teardown(h)

    run(scenario())


def test_long_outage_with_restart_fires_event_after_reconnect(tmp_path, monkeypatch, caplog):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            h.fake.hung = True
            for _ in range(3):
                await h.ping()
            assert not any(s.available for s in h.switches)
            h.fake.reboot()
            h.fake.hung = False
            await h.ping()

            assert len(h.restarts) == 1
            _, available, states = h.restarts[0]
            assert available == [True, True]
            assert states == [False, False]
        finally:
            await teardown(h)

    with caplog.at_level(logging.INFO):
        run(scenario())

    messages = [r.getMessage() for r in caplog.records]
    assert sum("responding again" in m for m in messages) == 1
    assert sum("CLU restarted at" in m for m in messages) == 1


def test_long_outage_without_restart_fires_no_event(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            h.fake.hung = True
            for _ in range(3):
                await h.ping()
            h.fake.hung = False
            await h.ping()
            assert all(s.available for s in h.switches)
            assert h.restarts == []
        finally:
            await teardown(h)

    run(scenario())


def test_unload_destroys_every_session(tmp_path, monkeypatch):
    monkeypatch.setattr(registration, "MAX_ENTRIES_PER_SESSION", 1)

    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        sessions = [s.session_id for s in h.coordinator._get_sessions(CLU_ID)]
        h.fake.sent.clear()
        await teardown(h)
        return h, sessions

    h, sessions = run(scenario())
    assert h.fake.sent == [f"SYSTEM:clientDestroy(0,0,{sid})" for sid in sessions]


def test_unload_skips_destroy_while_clu_is_not_responding(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        h.fake.hung = True
        for _ in range(3):
            await h.ping()
        h.fake.sent.clear()
        await teardown(h)
        return h

    h = run(scenario())
    assert h.fake.sent == []


# --- Ordering: last_boot before any post-restart value ----------------------------
#
# What a blueprint relies on (README, "CLU restart signal"): when an entity goes
# unavailable -> value after a CLU restart, sensor.grenton_clu_last_boot already
# holds the new boot time, and its restart_detected_at attribute is not earlier
# than the moment the entity went unavailable. After an outage without a
# restart, last_boot is not written at all.


def writes(h, kind, start=0):
    return [(i, entry) for i, entry in enumerate(h.timeline) if entry[0] == kind and i >= start]


def assert_restart_ordering(h, start, uptime=5):
    """The writes after ``start`` follow the restart contract."""
    boots = writes(h, "last_boot", start)
    assert len(boots) == 1, boots
    b, (_, boot_time, detected_at, _) = boots[0]
    assert abs(boot_time.timestamp() - (dt_util.utcnow().timestamp() - uptime)) < 5
    assert detected_at is not None

    switch_writes = writes(h, "switch", start)
    # Each switch went unavailable (in this window) before last_boot changed...
    went_unavailable = {}
    for i, (_, uid, available, _, at) in switch_writes:
        if not available and uid not in went_unavailable:
            went_unavailable[uid] = (i, at)
    assert set(went_unavailable) == {s.unique_id for s in h.switches}
    assert all(i < b for i, _ in went_unavailable.values())
    # ...no switch showed a value from then until last_boot changed...
    first_unavailable = min(i for i, _ in went_unavailable.values())
    assert not [i for i, entry in switch_writes if first_unavailable < i < b and entry[2]]
    # ...and every value shown after it comes after last_boot.
    shown = [(i, entry) for i, entry in switch_writes if i > first_unavailable and entry[2]]
    assert shown and all(i > b for i, _ in shown)
    # The blueprint rule: restart_detected_at >= the moment each entity went unavailable.
    assert all(detected_at >= at for _, at in went_unavailable.values())
    # The event comes after the values, with the same detection time.
    events = writes(h, "event", start)
    assert len(events) == 1 and events[0][0] > shown[0][0]
    assert events[0][1][1]["detected_at"] == detected_at.isoformat()
    return detected_at


def mark(h):
    return len(h.timeline)


def test_order_restart_detected_at_renewal(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            start = mark(h)
            h.fake.reboot()
            assert await h.coordinator._run_sync(CLU_ID, "renew") is True
            assert_restart_ordering(h, start)
            assert [s.is_on for s in h.switches] == [False, False]
        finally:
            await teardown(h)

    run(scenario())


def test_order_restart_detected_after_a_failed_ping(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            start = mark(h)
            h.fake.hung = True
            await h.ping()
            h.fake.reboot()
            h.fake.hung = False
            await h.ping()
            assert_restart_ordering(h, start)
        finally:
            await teardown(h)

    run(scenario())


def test_order_restart_during_an_outage(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            start = mark(h)
            h.fake.hung = True
            for _ in range(3):
                await h.ping()
            assert not any(s.available for s in h.switches)
            h.fake.reboot()
            h.fake.hung = False
            await h.ping()
            assert_restart_ordering(h, start)
        finally:
            await teardown(h)

    run(scenario())


def test_order_restart_after_two_failed_renewals(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            start = mark(h)
            drop_next_registers(h.fake, 2)
            assert await h.coordinator._run_sync(CLU_ID, "renew") is False
            assert not any(s.available for s in h.switches)
            h.fake.reboot()
            await h.ping()
            assert_restart_ordering(h, start)
        finally:
            await teardown(h)

    run(scenario())


def test_order_restart_whose_first_refresh_fails(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            start = mark(h)
            h.fake.reboot()
            drop_next_registers(h.fake, 2)
            assert await h.coordinator._run_sync(CLU_ID, "renew") is False
            # last_boot already changed; no value shown, no event yet.
            assert len(writes(h, "last_boot", start)) == 1
            assert h.restarts == []
            await h.ping()
            assert_restart_ordering(h, start)
        finally:
            await teardown(h)

    run(scenario())


def test_order_restart_during_outage_with_uptime_unreadable_at_first(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            start = mark(h)
            h.fake.hung = True
            for _ in range(3):
                await h.ping()
            h.fake.reboot()
            h.fake.answer_uptime = False
            h.fake.hung = False
            # The CLU answers pings and registrations, not the uptime query:
            # its entities must not come back yet.
            await h.ping()
            await h.ping()
            assert not any(s.available for s in h.switches)
            assert writes(h, "last_boot", start) == []
            assert not [e for _, e in writes(h, "switch", start) if e[2]]

            h.fake.answer_uptime = True
            await h.ping()
            assert_restart_ordering(h, start)
        finally:
            await teardown(h)

    run(scenario())


def test_order_restart_at_renewal_with_uptime_unreadable_at_first(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            start = mark(h)
            h.fake.reboot()
            h.fake.answer_uptime = False
            # Not known whether it restarted: no registration is shown as a
            # change, the entities become unavailable instead.
            assert await h.coordinator._run_sync(CLU_ID, "renew") is False
            assert not any(s.available for s in h.switches)
            assert not [e for _, e in writes(h, "switch", start) if e[2]]

            h.fake.answer_uptime = True
            await h.ping()
            assert_restart_ordering(h, start)
        finally:
            await teardown(h)

    run(scenario())


def test_one_lost_uptime_read_does_not_disturb_anything(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            start = mark(h)
            original_sendto = h.fake.sendto
            dropped = []

            def drop_one_uptime_query(data, addr):
                plaintext = h.fake.cipher.decrypt(data).decode()
                if plaintext.endswith(f"{CLU_ID}:get(0)") and not dropped:
                    dropped.append(1)
                    return
                original_sendto(data, addr)

            h.fake.sendto = drop_one_uptime_query
            assert await h.coordinator._run_sync(CLU_ID, "renew") is True
            assert dropped == [1]
            assert all(e[2] for _, e in writes(h, "switch", start))
            assert writes(h, "last_boot", start) == []
        finally:
            await teardown(h)

    run(scenario())


def test_unreadable_uptime_without_restart_leaves_last_boot_alone(tmp_path, monkeypatch, caplog):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            start = mark(h)
            h.fake.answer_uptime = False
            assert await h.coordinator._run_sync(CLU_ID, "renew") is False
            assert not any(s.available for s in h.switches)
            h.fake.answer_uptime = True
            await h.ping()
            assert all(s.available for s in h.switches)
            assert writes(h, "last_boot", start) == []
            assert h.restarts == []
        finally:
            await teardown(h)

    with caplog.at_level(logging.INFO):
        run(scenario())

    messages = [r.getMessage() for r in caplog.records]
    assert sum("could not be read twice in a row" in m for m in messages) == 1
    assert sum("uptime read again, no restart" in m for m in messages) == 1


def test_outage_without_restart_does_not_touch_last_boot(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            start = mark(h)
            h.fake.hung = True
            for _ in range(3):
                await h.ping()
            h.fake.outputs = {"DOU0001": 0, "DOU0002": 1}  # wall presses during the outage
            h.fake.hung = False
            await h.ping()
            for _ in range(3):
                assert await h.coordinator._run_sync(CLU_ID, "renew") is True
            assert all(s.available for s in h.switches)
            assert [s.is_on for s in h.switches] == [False, True]
            assert writes(h, "last_boot", start) == []
            assert h.boot_sensor.extra_state_attributes["restart_detected_at"] is None
        finally:
            await teardown(h)

    run(scenario())


def test_outage_after_a_restart_is_told_apart_by_restart_detected_at(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            start = mark(h)
            h.fake.reboot()
            assert await h.coordinator._run_sync(CLU_ID, "renew") is True
            detected_at = assert_restart_ordering(h, start)

            # Later an outage without a restart: restart_detected_at stays
            # earlier than the moment the entities went unavailable.
            start = mark(h)
            h.fake.hung = True
            for _ in range(3):
                await h.ping()
            h.fake.hung = False
            await h.ping()
            assert writes(h, "last_boot", start) == []
            went_unavailable = [e[4] for _, e in writes(h, "switch", start) if not e[2]]
            assert went_unavailable and all(detected_at < at for at in went_unavailable)
            assert h.boot_sensor.extra_state_attributes["restart_detected_at"] == detected_at
        finally:
            await teardown(h)

    run(scenario())


def test_startup_sets_last_boot_without_restart_detected_at(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            assert_boot_time(h, 100000)
            assert h.boot_sensor.extra_state_attributes == {"restart_detected_at": None}
        finally:
            await teardown(h)

    run(scenario())


def test_startup_while_clu_is_down_reads_boot_time_without_a_restart(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch, start_hung=True)
        try:
            assert h.boot_sensor.native_value is None
            h.fake.hung = False
            await h.ping()
            assert all(s.available for s in h.switches)
            # First reading: a boot time, but no restart.
            assert_boot_time(h, 100000)
            assert h.boot_sensor.extra_state_attributes["restart_detected_at"] is None
            assert h.restarts == []
        finally:
            await teardown(h)

    run(scenario())


def test_clu_that_never_reports_uptime_turns_detection_off_without_a_gap(tmp_path, monkeypatch, caplog):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch, uptime="nil")
        try:
            start = mark(h)
            # Startup (1) and one renewal with its re-read (2, 3): detection
            # is off, and the entities never became unavailable.
            assert await h.coordinator._run_sync(CLU_ID, "renew") is True
            assert all(e[2] for _, e in writes(h, "switch", start))
            h.fake.sent.clear()
            assert await h.coordinator._run_sync(CLU_ID, "renew") is True
            assert f"{CLU_ID}:get(0)" not in h.fake.sent
            assert h.boot_sensor.native_value is None
            assert h.restarts == []
        finally:
            await teardown(h)

    with caplog.at_level(logging.WARNING):
        run(scenario())

    warnings = [r.getMessage() for r in caplog.records if "restart detection is off" in r.getMessage()]
    assert len(warnings) == 1 and "'nil'" in warnings[0]


def test_uptime_that_stops_answering_is_given_up_on_eventually(tmp_path, monkeypatch, caplog):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            h.fake.answer_uptime = False
            assert await h.coordinator._run_sync(CLU_ID, "renew") is False
            assert not any(s.available for s in h.switches)
            for _ in range(coordinator_module.UPTIME_MAX_FAILURES):
                await h.ping()
                if all(s.available for s in h.switches):
                    break
            assert all(s.available for s in h.switches)
        finally:
            await teardown(h)

    with caplog.at_level(logging.WARNING):
        run(scenario())

    warnings = [r.getMessage() for r in caplog.records if "restart detection is off" in r.getMessage()]
    assert len(warnings) == 1 and "no answer" in warnings[0]
