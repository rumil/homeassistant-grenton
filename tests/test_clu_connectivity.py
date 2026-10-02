"""Tests for CLU connectivity tracking, availability and fail-fast actions.

The coordinator tests need Home Assistant installed. The CLU is simulated by
a fake UDP transport that answers pings, clientRegister and actions like a
real CLU, or drops everything while it is "hung".
"""

import asyncio
import base64
import logging
import os
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


class FakeClu:
    """Fake UDP transport answering like a CLU with two digital outputs."""

    def __init__(self, protocol, cipher):
        self.protocol = protocol
        self.cipher = cipher
        self.hung = False
        self.outputs = {"DOU0001": 1, "DOU0002": 0}
        self.sent: list[str] = []

    def sendto(self, data, addr):
        plaintext = self.cipher.decrypt(data).decode()
        _, _, msg_id, payload = plaintext.split(":", 3)
        self.sent.append(payload)
        if self.hung:
            return
        if payload.startswith("SYSTEM:clientRegister"):
            values = ",".join(str(self.outputs[name]) for name in ("DOU0001", "DOU0002"))
            answer = f"clientReport:abcd:{{{values}}}"
        else:
            answer = "0"
        asyncio.get_running_loop().call_soon(self._respond, msg_id, answer)

    def push_report(self):
        values = ",".join(str(self.outputs[name]) for name in ("DOU0001", "DOU0002"))
        self._respond("00000000", f"clientReport:abcd:{{{values}}}")

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
    def __init__(self, hass, coordinator, fake, switches, sensor):
        self.hass = hass
        self.coordinator = coordinator
        self.fake = fake
        self.switches = switches
        self.sensor = sensor
        self.sensor_writes = 0

    async def ping(self):
        await self.coordinator._send_ping(CLU_ID)
        await self.settle()

    async def settle(self):
        # Let response handling and resync tasks run.
        for _ in range(20):
            await asyncio.sleep(0)


async def setup_harness(tmp_path, monkeypatch):
    # Short timeout so pings to a hung CLU fail quickly.
    monkeypatch.setattr(clu_api, "RESPONSE_TIMEOUT", 0.05)

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
        return True

    api.connect = fake_connect

    switches = [make_switch(coordinator, "DOU0001"), make_switch(coordinator, "DOU0002")]
    [device] = DeviceCluMapper.to_domain([clu], coordinator)
    [sensor] = device.entities

    await coordinator.async_setup()
    harness = Harness(hass, coordinator, holder["fake"], switches, sensor)

    # Count connectivity sensor state writes without a full entity platform.
    def count_write():
        harness.sensor_writes += 1

    sensor.async_write_ha_state = count_write
    sensor._remember_written()
    sensor.async_on_remove(
        coordinator.async_add_connectivity_listener(CLU_ID, sensor._handle_connectivity_update)
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
