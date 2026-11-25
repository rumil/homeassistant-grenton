"""Tests for splitting subscribed keys into clientRegister sessions."""

import base64
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

from custom_components.homeassistant_grenton.domain import registration  # noqa: E402
from custom_components.homeassistant_grenton.domain.api.clu_messages import (  # noqa: E402
    GrentonCluApiClientRegisterRequest,
)
from custom_components.homeassistant_grenton.domain.cipher import GrentonCipher  # noqa: E402
from custom_components.homeassistant_grenton.domain.encryption import GrentonEncryption  # noqa: E402
from custom_components.homeassistant_grenton.state import (  # noqa: E402
    GrentonCluStateAttributeKey as Attr,
    GrentonCluStateVariableKey as Var,
)

plan_sessions = registration.plan_sessions


def house_keys():
    """Roughly the shape of a real installation: 8 thermostats with 7
    features each, 27 User Feature switches, 7 contacts, 6 temperatures."""
    keys = []
    for t in range(8):
        keys += [Attr(f"THE{4100 + t}", str(i)) for i in (3, 6, 8, 10, 11, 12, 14)]
    keys += [Var(f"hallwayLedSwitch{i:02d}") for i in range(27)]
    keys += [Attr(f"DIN{2200 + i}", "0") for i in range(7)]
    keys += [Attr(f"PAN{3300 + i}", "6") for i in range(6)]
    return keys


def assert_valid_plan(keys, sessions, first_id):
    assert [key for session in sessions for key in session.keys] == keys
    assert [s.session_id for s in sessions] == list(range(first_id, first_id + len(sessions)))
    for session in sessions:
        assert 1 <= len(session.keys) <= registration.MAX_ENTRIES_PER_SESSION
        size = registration.request_size(registration.register_payload(session.session_id, session.keys))
        assert size <= registration.REGISTER_BUDGET_BYTES <= registration.MAX_REQUEST_BYTES


def test_house_is_split_into_small_sessions():
    keys = house_keys()
    sessions = plan_sessions(keys, 41234)
    assert_valid_plan(keys, sessions, 41234)
    assert len(sessions) == 4  # 96 keys, at most 28 each


def test_long_names_split_by_bytes_before_28_entries():
    keys = [Var("a" * 80 + str(i)) for i in range(40)]
    sessions = plan_sessions(keys, 10000)
    assert_valid_plan(keys, sessions, 10000)
    assert all(len(s.keys) < 28 for s in sessions)


def test_single_entry_over_budget_gets_its_own_session():
    huge = Var("x" * 2000)
    keys = [Attr("DOU0001", "0"), huge, Attr("DOU0002", "0")]
    sessions = plan_sessions(keys, 10000)
    assert [s.keys for s in sessions] == [(keys[0],), (huge,), (keys[2],)]


def test_no_keys_no_sessions():
    assert plan_sessions([], 10000) == []


def test_entries_and_payload():
    assert registration.entry_text(Var("gesture")) == '"gesture"'
    assert registration.entry_text(Var('a"b\\c')) == '"a\\"b\\\\c"'
    assert registration.entry_text(Attr("DOU0001", "0")) == "{DOU0001,0}"
    assert (
        registration.register_payload(12345, [Var("x"), Attr("DOU0001", "0")])
        == 'SYSTEM:clientRegister(0,0,12345,{"x",{DOU0001,0}})'
    )
    assert registration.destroy_payload(12345) == "SYSTEM:clientDestroy(0,0,12345)"


def test_request_size_matches_the_encrypted_datagram():
    cipher = GrentonCipher(GrentonEncryption(
        key=base64.b64encode(os.urandom(16)).decode(),
        iv=base64.b64encode(os.urandom(16)).decode(),
    ))
    for session in plan_sessions(house_keys(), 54321):
        request = GrentonCluApiClientRegisterRequest(list(session.keys), session.session_id)
        assert len(cipher.encrypt(request.raw.encode())) == registration.request_size(request.payload)
