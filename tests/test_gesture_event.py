"""Tests for the gesture event entity across CLU outages.

The entity writes state only for gestures and for availability changes. The
CLU is simulated by a fake UDP transport with a single gesture variable, as
in test_clu_connectivity. Needs Home Assistant installed.
"""

import asyncio
import base64
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

ha_core = pytest.importorskip("homeassistant.core")

from homeassistant.helpers import frame  # noqa: E402

from custom_components.homeassistant_grenton import coordinator as coordinator_module  # noqa: E402
from custom_components.homeassistant_grenton.domain.api import clu as clu_api  # noqa: E402
from custom_components.homeassistant_grenton.domain.clu import GrentonClu  # noqa: E402
from custom_components.homeassistant_grenton.domain.encryption import GrentonEncryption  # noqa: E402
from custom_components.homeassistant_grenton.domain.entities.gesture_event import (  # noqa: E402
    GrentonEntityGestureEvent,
)
from custom_components.homeassistant_grenton.domain.state_object import (  # noqa: E402
    GrentonVariableValueObject,
)

CLU_ID = "CLU221010475"
CLU_IP = "192.168.1.10"


class FakeClu:
    """Fake UDP transport answering like a CLU with one gesture variable."""

    def __init__(self, protocol, cipher, hung):
        self.protocol = protocol
        self.cipher = cipher
        self.hung = hung
        # value = seq * 10 + code, see gesture_decoder
        self.gesture = 0

    def sendto(self, data, addr):
        plaintext = self.cipher.decrypt(data).decode()
        _, _, msg_id, payload = plaintext.split(":", 3)
        if self.hung:
            return
        if payload.startswith("SYSTEM:clientRegister"):
            answer = f"clientReport:abcd:{{{self.gesture}}}"
        else:
            answer = "0"
        asyncio.get_running_loop().call_soon(self._respond, msg_id, answer)

    def push_report(self):
        self._respond("00000000", f"clientReport:abcd:{{{self.gesture}}}")

    def _respond(self, msg_id, payload):
        wire = f"resp:{CLU_IP}:{msg_id}:{payload}".encode()
        self.protocol.datagram_received(self.cipher.encrypt(wire), (CLU_IP, 1234))

    def close(self):
        pass


class Harness:
    def __init__(self, coordinator, fake, entity):
        self.coordinator = coordinator
        self.fake = fake
        self.entity = entity
        # (available, state) of every state write
        self.writes: list[tuple[bool, str | None]] = []
        # (event_type, attributes) of every fired event
        self.events: list[tuple[str, dict | None]] = []

    async def ping(self):
        await self.coordinator._send_ping(CLU_ID)
        await self.settle()

    async def push(self, value):
        self.fake.gesture = value
        self.fake.push_report()
        await self.settle()

    async def disconnect(self):
        self.fake.hung = True
        for _ in range(3):
            await self.ping()

    async def settle(self):
        # Let response handling and resync tasks run.
        for _ in range(20):
            await asyncio.sleep(0)


async def setup_harness(tmp_path, monkeypatch, start_hung=False):
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
        api.transport = holder["fake"] = FakeClu(api.protocol, api.cipher, start_hung)
        return True

    api.connect = fake_connect

    entity = GrentonEntityGestureEvent(
        coordinator=coordinator,
        id="hallway_buttons_gesture",
        state_object=GrentonVariableValueObject(clu_id=CLU_ID, object_name="", index="gestureHallway"),
    )
    entity.hass = hass
    entity.entity_id = "event.hallway_buttons"

    await coordinator.async_setup()
    h = Harness(coordinator, holder["fake"], entity)

    # Record state writes and fired events without a full entity platform.
    def record_write():
        h.writes.append((entity.available, entity.state))

    trigger_event = entity._trigger_event

    def record_event(event_type, event_attributes=None):
        h.events.append((event_type, event_attributes))
        trigger_event(event_type, event_attributes)

    entity.async_write_ha_state = record_write
    entity._trigger_event = record_event

    # What the entity platform does when adding the entity.
    await entity.async_added_to_hass()
    entity.async_write_ha_state()
    return h


async def teardown(h):
    await h.coordinator.async_shutdown()


def run(coro):
    return asyncio.run(coro)


def test_disconnect_writes_unavailable_once(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            await h.push(11)
            assert h.events == [("single", {"sequence": 1})]
            last_event = h.entity.state
            h.writes.clear()

            await h.disconnect()
            assert h.writes == [(False, last_event)]
            assert not h.entity.available

            # More failed pings while disconnected: no more state writes.
            await h.ping()
            await h.ping()
            assert len(h.writes) == 1
            assert len(h.events) == 1
        finally:
            await teardown(h)

    run(scenario())


def test_reconnect_writes_available_once_without_event(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            await h.push(11)
            last_event = h.entity.state
            await h.disconnect()
            h.writes.clear()

            # Power cycle: the CLU restarts with its gesture value reset.
            h.fake.gesture = 0
            h.fake.hung = False
            await h.ping()

            assert h.entity.available
            assert h.writes == [(True, last_event)]
            assert h.entity.state == last_event
            assert len(h.events) == 1
        finally:
            await teardown(h)

    run(scenario())


def test_resync_after_reconnect_does_not_fire_changed_value(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            await h.push(11)
            last_event = h.entity.state
            await h.disconnect()
            h.writes.clear()

            # A gesture during the outage arrives only in the resync: no event.
            h.fake.gesture = 22
            h.fake.hung = False
            await h.ping()

            assert h.writes == [(True, last_event)]
            assert len(h.events) == 1
        finally:
            await teardown(h)

    run(scenario())


def test_startup_while_clu_unreachable_becomes_available(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch, start_hung=True)
        try:
            # Initial state written by the platform while the CLU is down.
            assert h.writes == [(False, None)]

            h.fake.hung = False
            await h.ping()

            assert h.entity.available
            assert h.writes == [(False, None), (True, None)]
            assert h.events == []
        finally:
            await teardown(h)

    run(scenario())


def test_ordinary_updates_do_not_write_state(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            await h.push(11)
            await h.push(10)  # idle clear
            h.writes.clear()
            h.events.clear()

            # Report caused by another key, periodic resync, pings.
            await h.push(10)
            await h.coordinator._send_register(CLU_ID)
            await h.settle()
            h.coordinator.async_update_listeners()
            for _ in range(3):
                await h.ping()

            assert h.writes == []
            assert h.events == []
        finally:
            await teardown(h)

    run(scenario())


def test_gesture_after_reconnect_fires_one_event(tmp_path, monkeypatch):
    async def scenario():
        h = await setup_harness(tmp_path, monkeypatch)
        try:
            await h.push(11)
            await h.disconnect()
            h.fake.gesture = 0
            h.fake.hung = False
            await h.ping()
            h.writes.clear()
            h.events.clear()

            await h.push(12)

            assert h.events == [("double", {"sequence": 1})]
            assert len(h.writes) == 1
            available, state = h.writes[0]
            assert available
            assert state == h.entity.state
        finally:
            await teardown(h)

    run(scenario())
