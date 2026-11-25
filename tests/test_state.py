"""Tests for the CLU state cache."""

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

from custom_components.homeassistant_grenton.domain.api.clu_messages import (  # noqa: E402
    GrentonCluApiClientRegisterResponse,
    GrentonCluApiClientReportNotification,
)
from custom_components.homeassistant_grenton.state import (  # noqa: E402
    GrentonCluState,
    GrentonCluStateAttributeKey,
    GrentonCluStateVariableKey,
)


def make_state():
    state = GrentonCluState()
    state.add_variable("gesture")
    state.add_attribute("DOU0001", "0")
    return state, GrentonCluStateVariableKey("gesture"), GrentonCluStateAttributeKey("DOU0001", "0")


def test_nil_in_register_answer_is_stored_as_none():
    state, var_key, attr_key = make_state()
    answer = GrentonCluApiClientRegisterResponse("resp:192.168.1.10:12345678:clientReport:1:{nil,nil}")
    for key, value in zip(state.get_subscription_order(), answer.values):
        if isinstance(key, GrentonCluStateVariableKey):
            state.set_variable(key, value)
        else:
            state.set_attribute(key, value)
    assert state.get_variable(var_key) is None
    assert state.get_attribute(attr_key) is None


def test_nil_in_push_and_in_register_answer_match():
    pushed, var_key, attr_key = make_state()
    pushed.update_state(
        GrentonCluApiClientReportNotification("resp:192.168.1.10:00000000:clientReport:1:{nil,1}").values
    )
    registered, _, _ = make_state()
    registered.set_variable(var_key, None)
    registered.set_attribute(attr_key, 1)
    assert pushed.get_variable(var_key) == registered.get_variable(var_key) is None
    assert pushed.get_attribute(attr_key) == registered.get_attribute(attr_key) == 1


def test_value_before_first_answer_is_empty_string():
    # Unchanged: "no value yet" is "", which entities treat as not known.
    state, var_key, _ = make_state()
    assert state.get_variable(var_key) == ""
