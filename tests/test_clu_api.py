"""Unit tests for request queueing and retries in the CLU API.

The integration package's ``__init__`` pulls in Home Assistant, so a stub
package module is registered for it here. Its submodules (domain, dto, state)
do not need Home Assistant and are imported normally through the stub.
"""

import asyncio
import base64
import logging
import os
import sys
import types
from pathlib import Path

_PACKAGE_DIR = (
    Path(__file__).resolve().parent.parent / "custom_components" / "homeassistant_grenton"
)

for _name, _path in (
    ("custom_components", _PACKAGE_DIR.parent),
    ("custom_components.homeassistant_grenton", _PACKAGE_DIR),
):
    if _name not in sys.modules:
        _module = types.ModuleType(_name)
        _module.__path__ = [str(_path)]
        sys.modules[_name] = _module

from custom_components.homeassistant_grenton.domain import action as action_module  # noqa: E402
from custom_components.homeassistant_grenton.domain.api import clu as clu_api  # noqa: E402
from custom_components.homeassistant_grenton.domain.clu import GrentonClu  # noqa: E402
from custom_components.homeassistant_grenton.domain.encryption import GrentonEncryption  # noqa: E402
from custom_components.homeassistant_grenton.domain.enums import GrentonActionEventType  # noqa: E402

GrentonActionAttribute = action_module.GrentonActionAttribute
GrentonActionMethod = action_module.GrentonActionMethod
GrentonActionVariable = action_module.GrentonActionVariable
OK = clu_api.ActionResult.OK

CLU_IP = "192.168.1.10"
RESPONSE_DELAY = 0.02


class FakeCluTransport:
    """Fake UDP transport that answers requests like a CLU.

    Each request is answered after RESPONSE_DELAY seconds unless the
    ``drop`` callback returns True for it. Responses can also be delayed
    per request through the ``delay`` callback.
    """

    def __init__(self, protocol, cipher, drop=None, delay=None):
        self.protocol = protocol
        self.cipher = cipher
        self.drop = drop or (lambda msg_id, payload, attempt: False)
        self.delay = delay or (lambda msg_id, payload, attempt: RESPONSE_DELAY)
        self.sent: list[tuple[str, str]] = []
        self.send_times: list[float] = []
        self.answer_times: list[float] = []
        self.outstanding: set[str] = set()
        self.max_outstanding = 0
        self.max_in_flight_seen = 0
        self.attempts_per_payload: dict[str, int] = {}

    def sendto(self, data, addr):
        loop = asyncio.get_running_loop()
        plaintext = self.cipher.decrypt(data).decode()
        _, _, msg_id, payload = plaintext.split(":", 3)
        self.sent.append((msg_id, payload))
        self.send_times.append(loop.time())
        self.max_in_flight_seen = max(self.max_in_flight_seen, getattr(self.protocol, "_in_flight", 0))

        attempt = self.attempts_per_payload.get(payload, 0) + 1
        self.attempts_per_payload[payload] = attempt

        if self.drop(msg_id, payload, attempt):
            return

        self.outstanding.add(msg_id)
        self.max_outstanding = max(self.max_outstanding, len(self.outstanding))
        loop.call_later(self.delay(msg_id, payload, attempt), self._respond, msg_id)

    def _respond(self, msg_id):
        self.outstanding.discard(msg_id)
        self.answer_times.append(asyncio.get_running_loop().time())
        response = f"resp:{CLU_IP}:{msg_id}:0".encode()
        self.protocol.datagram_received(self.cipher.encrypt(response), (CLU_IP, 1234))

    def close(self):
        pass


def make_api(response_timeout=1.0, request_gap=0.0, **transport_kwargs):
    encryption = GrentonEncryption(
        key=base64.b64encode(os.urandom(16)).decode(),
        iv=base64.b64encode(os.urandom(16)).decode(),
    )
    clu = GrentonClu(id="CLU221010475", serial_number="221010475", name="CLU221010475", ip=CLU_IP, port=1234)
    api = clu_api.GrentonCluApi(clu, encryption)
    protocol = clu_api.GrentonCluApiProtocol(api)
    protocol._response_timeout = response_timeout
    protocol._timeouts = {kind: response_timeout for kind in protocol._timeouts}
    protocol._request_gap = request_gap
    api.protocol = protocol
    transport = FakeCluTransport(protocol, api.cipher, **transport_kwargs)
    api.transport = transport
    return api, transport


def set_action(object_name, value="1"):
    return GrentonActionAttribute(
        clu_id="CLU221010475",
        object_name=object_name,
        event=GrentonActionEventType.ON,
        value=value,
        index="0",
    )


def test_concurrent_actions_never_exceed_in_flight_limit():
    async def run():
        # 15 requests * 20 ms queueing is well above the 200 ms timeout, so
        # this also checks that the timeout starts when the datagram is sent.
        api, transport = make_api(response_timeout=0.2)
        actions = [set_action(f"DOU{i:04d}") for i in range(15)]
        results = await asyncio.gather(*(api.execute_action(a) for a in actions))
        return results, transport

    results, transport = asyncio.run(run())

    assert results == [OK] * 15
    assert len(transport.sent) == 15
    assert transport.max_outstanding == clu_api.MAX_IN_FLIGHT_REQUESTS == 1
    assert transport.max_in_flight_seen == 1
    assert sorted(payload for _, payload in transport.sent) == sorted(
        f'DOU{i:04d}:set(0,"1")' for i in range(15)
    )


def test_ping_and_register_share_the_in_flight_limit():
    async def run():
        api, transport = make_api()
        calls = [api.execute_action(set_action(f"DOU{i:04d}")) for i in range(5)]
        calls += [api.ping() for _ in range(5)]
        results = await asyncio.gather(*calls)
        return results, transport

    results, transport = asyncio.run(run())

    assert results == [OK] * 5 + [True] * 5
    assert transport.max_outstanding == 1
    assert transport.max_in_flight_seen == 1


def test_dropped_response_is_retried_with_fresh_msg_id(caplog):
    target = 'DOU3408:set(0,"1")'

    def drop(msg_id, payload, attempt):
        return payload == target and attempt == 1

    async def run():
        api, transport = make_api(response_timeout=0.1, drop=drop)
        actions = [set_action("DOU3408")] + [set_action(f"DOU{i:04d}") for i in range(5)]
        results = await asyncio.gather(*(api.execute_action(a) for a in actions))
        return results, transport

    with caplog.at_level(logging.DEBUG, logger=clu_api.__name__):
        results, transport = asyncio.run(run())

    assert results == [OK] * 6
    target_ids = [msg_id for msg_id, payload in transport.sent if payload == target]
    assert len(target_ids) == 2
    assert target_ids[0] != target_ids[1]

    # Intermediate timeouts are debug only; a successful retry is info.
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(
        r.levelno == logging.INFO and "succeeded on attempt 2/3" in r.getMessage()
        for r in caplog.records
    )


def test_gives_up_after_three_attempts_with_single_warning(caplog):
    async def run():
        api, transport = make_api(response_timeout=0.05, drop=lambda *_: True)
        result = await api.execute_action(set_action("DOU3408"))
        return result, transport

    with caplog.at_level(logging.DEBUG, logger=clu_api.__name__):
        result, transport = asyncio.run(run())

    assert result is clu_api.ActionResult.FAILED
    assert len(transport.sent) == 1 + clu_api.ACTION_RETRIES == 3
    assert len({msg_id for msg_id, _ in transport.sent}) == 3
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "Request timeout" in warnings[0].getMessage()


def test_non_idempotent_action_is_never_retried():
    async def run():
        api, transport = make_api(response_timeout=0.05, drop=lambda *_: True)
        method = GrentonActionMethod(
            clu_id="CLU221010475",
            object_name="DOU3408",
            event=GrentonActionEventType.CLICK,
            value="0",
            index="1",
        )
        result = await api.execute_action(method)
        return result, transport

    result, transport = asyncio.run(run())

    assert result is clu_api.ActionResult.FAILED
    assert len(transport.sent) == 1


def test_late_response_to_abandoned_attempt_is_not_matched():
    # Attempt 1 is answered after its timeout, while attempt 2 is pending.
    # Attempt 2 is dropped, so it must still time out; attempt 3 succeeds.
    def drop(msg_id, payload, attempt):
        return attempt == 2

    def delay(msg_id, payload, attempt):
        return 0.15 if attempt == 1 else RESPONSE_DELAY

    async def run():
        api, transport = make_api(response_timeout=0.1, drop=drop, delay=delay)
        result = await api.execute_action(set_action("DOU3408"))
        return result, transport

    result, transport = asyncio.run(run())

    assert result is OK
    assert len(transport.sent) == 3


def test_request_gap_delays_next_send():
    gap = 0.05

    async def run():
        api, transport = make_api(request_gap=gap)
        actions = [set_action(f"DOU{i:04d}") for i in range(4)]
        results = await asyncio.gather(*(api.execute_action(a) for a in actions))
        return results, transport

    results, transport = asyncio.run(run())

    assert results == [OK] * 4
    for answered, next_send in zip(transport.answer_times, transport.send_times[1:]):
        assert next_send - answered >= gap * 0.9


def var_action(name, value):
    return GrentonActionVariable(
        clu_id="CLU221010475",
        object_name="",
        event=GrentonActionEventType.ON,
        value=value,
        index=name,
    )


def test_retry_never_overtakes_a_newer_write_to_the_same_target():
    # The CLU executes "on" but its answer is lost. "off" is requested while
    # "on" waits for its answer. The CLU must end up off.
    def drop(msg_id, payload, attempt):
        return payload == 'setVar("light","1")' and attempt == 1

    async def run():
        api, transport = make_api(response_timeout=0.1, drop=drop)
        on = asyncio.create_task(api.execute_action(var_action("light", "1")))
        await asyncio.sleep(0.01)
        off = asyncio.create_task(api.execute_action(var_action("light", "0")))
        return await on, await off, transport

    on_result, off_result, transport = asyncio.run(run())

    payloads = [payload for _, payload in transport.sent]
    assert payloads == ['setVar("light","1")', 'setVar("light","0")']
    assert on_result is clu_api.ActionResult.SUPERSEDED
    assert off_result is OK


def test_retry_of_other_target_keeps_order_and_runs_first():
    # "on" for one light loses its answer while a write to another light
    # waits. The retry goes out before the other write, never after it.
    def drop(msg_id, payload, attempt):
        return payload == 'setVar("light","1")' and attempt == 1

    async def run():
        api, transport = make_api(response_timeout=0.1, drop=drop)
        first = asyncio.create_task(api.execute_action(var_action("light", "1")))
        await asyncio.sleep(0.01)
        second = asyncio.create_task(api.execute_action(var_action("other", "0")))
        return await first, await second, transport

    first, second, transport = asyncio.run(run())

    payloads = [payload for _, payload in transport.sent]
    assert payloads == ['setVar("light","1")', 'setVar("light","1")', 'setVar("other","0")']
    assert first is OK and second is OK


def test_same_object_other_feature_is_not_a_newer_write():
    # A thermostat's set point and mode are separate features of one object:
    # a write to one must not cancel the retry of the other.
    def drop(msg_id, payload, attempt):
        return payload == 'THE0001:set(3,"22")' and attempt == 1

    def attr(index, value):
        return GrentonActionAttribute(
            clu_id="CLU221010475",
            object_name="THE0001",
            event=GrentonActionEventType.ON,
            value=value,
            index=index,
        )

    async def run():
        api, transport = make_api(response_timeout=0.1, drop=drop)
        set_point = asyncio.create_task(api.execute_action(attr("3", "22")))
        await asyncio.sleep(0.01)
        mode = asyncio.create_task(api.execute_action(attr("8", "0")))
        return await set_point, await mode, transport

    set_point, mode, transport = asyncio.run(run())

    payloads = [payload for _, payload in transport.sent]
    assert payloads == ['THE0001:set(3,"22")', 'THE0001:set(3,"22")', 'THE0001:set(8,"0")']
    assert set_point is OK and mode is OK


def test_each_kind_of_request_has_its_own_timeout():
    async def run():
        api, transport = make_api(drop=lambda *_: True)
        api.protocol._timeouts = {"ping": 0.15, "action": 0.05, "register": 0.1}
        loop = asyncio.get_running_loop()
        elapsed = {}
        start = loop.time()
        await api.ping()
        elapsed["ping"] = loop.time() - start
        start = loop.time()
        await api.execute_action(GrentonActionMethod(
            clu_id="CLU221010475", object_name="DOU0001",
            event=GrentonActionEventType.CLICK, value="0", index="1",
        ))
        elapsed["action"] = loop.time() - start
        return elapsed

    elapsed = asyncio.run(run())
    assert 0.14 <= elapsed["ping"] < 0.25
    assert 0.04 <= elapsed["action"] < 0.1


def test_default_timeouts():
    # Actions run inside the service call: 3 attempts must stay short.
    assert clu_api.ACTION_TIMEOUT * (1 + clu_api.ACTION_RETRIES) <= 6.0
    # Hang detection is built on 5 s pings; registrations get as long.
    assert clu_api.PING_TIMEOUT == 5.0
    assert clu_api.REGISTER_TIMEOUT >= clu_api.ACTION_TIMEOUT


def test_request_too_long_for_the_clu_is_not_sent(caplog):
    async def run():
        api, transport = make_api(response_timeout=0.05)
        result = await api.execute_action(set_action("DOU0001", value="x" * 1500))
        return result, transport

    with caplog.at_level(logging.ERROR, logger=clu_api.__name__):
        result, transport = asyncio.run(run())

    assert result is clu_api.ActionResult.FAILED
    assert transport.sent == []
    assert any("too long for the CLU" in r.getMessage() for r in caplog.records)
