"""Unit tests for the pure GestureDecoder.

The decoder module is loaded directly from its file so these tests run without
a Home Assistant installation and without importing the integration package
(whose ``__init__`` pulls in Home Assistant).
"""

import importlib.util
import logging
import sys
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "homeassistant_grenton"
    / "domain"
    / "gesture_decoder.py"
)

_spec = importlib.util.spec_from_file_location("grenton_gesture_decoder", _MODULE_PATH)
assert _spec and _spec.loader
gesture_decoder = importlib.util.module_from_spec(_spec)
# Register before exec so dataclass introspection can resolve the module.
sys.modules[_spec.name] = gesture_decoder
_spec.loader.exec_module(gesture_decoder)

GestureDecoder = gesture_decoder.GestureDecoder
DecodedGesture = gesture_decoder.DecodedGesture
ORIGIN_PUSH = gesture_decoder.ORIGIN_PUSH
ORIGIN_RESYNC = gesture_decoder.ORIGIN_RESYNC


def make_value(seq: int, code: int) -> int:
    """Build a raw channel value from a sequence and a code."""
    return seq * 10 + code


def test_first_value_is_baseline_only():
    """The first value ever seen is a baseline and never fires."""
    decoder = GestureDecoder(name="test")
    # seq=5, code=1 (single) but it's the first value -> baseline only.
    assert decoder.decode(make_value(5, 1), ORIGIN_PUSH) is None


def test_resync_never_fires():
    """A resync value only updates the baseline and never fires."""
    decoder = GestureDecoder(name="test")
    decoder.decode(make_value(5, 0), ORIGIN_PUSH)  # baseline
    # A changed resync value with a real gesture code must not fire.
    assert decoder.decode(make_value(6, 2), ORIGIN_RESYNC) is None


def test_push_fires_once_per_distinct_value():
    """A push fires only on a changed value; the repeat is silent."""
    decoder = GestureDecoder(name="test")
    decoder.decode(make_value(5, 0), ORIGIN_PUSH)  # baseline
    gesture = decoder.decode(make_value(6, 1), ORIGIN_PUSH)
    assert gesture == DecodedGesture(event_type="single", sequence=6)
    # Same value again (e.g. repeated in later reports) -> silent.
    assert decoder.decode(make_value(6, 1), ORIGIN_PUSH) is None


def test_code_zero_is_silent_baseline_update():
    """Code 0 (idle) updates the baseline silently."""
    decoder = GestureDecoder(name="test")
    decoder.decode(make_value(5, 1), ORIGIN_PUSH)  # baseline
    assert decoder.decode(make_value(5, 0), ORIGIN_PUSH) is None
    # A later gesture still fires against the updated baseline.
    assert decoder.decode(make_value(6, 2), ORIGIN_PUSH) == DecodedGesture("double", 6)


def test_duplicate_value_in_unrelated_report_is_silent():
    """An unchanged value in a report triggered by another key does not fire."""
    decoder = GestureDecoder(name="test")
    decoder.decode(make_value(7, 3), ORIGIN_PUSH)  # baseline (first value)
    # Report triggered by a different key repeats this key's value unchanged.
    assert decoder.decode(make_value(7, 3), ORIGIN_PUSH) is None
    assert decoder.decode(make_value(7, 3), ORIGIN_PUSH) is None


def test_seq_wrap_9999_to_0_is_gap_of_one(caplog):
    """Sequence wrap 9999 -> 0 is a normal step of 1, not a gap warning."""
    decoder = GestureDecoder(name="test")
    decoder.decode(make_value(9999, 0), ORIGIN_PUSH)  # baseline
    with caplog.at_level(logging.WARNING):
        gesture = decoder.decode(make_value(0, 1), ORIGIN_PUSH)
    assert gesture == DecodedGesture("single", 0)
    assert not [r for r in caplog.records if "seq gap" in r.getMessage()]


def test_seq_gap_warns_but_still_fires(caplog):
    """A jump of more than 1 warns about lost gestures but still fires."""
    decoder = GestureDecoder(name="my_entity")
    decoder.decode(make_value(1, 0), ORIGIN_PUSH)  # baseline seq=1
    with caplog.at_level(logging.WARNING):
        gesture = decoder.decode(make_value(4, 2), ORIGIN_PUSH)  # seq jumps 1 -> 4
    assert gesture == DecodedGesture("double", 4)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a seq gap warning"
    msg = warnings[0].getMessage()
    assert "my_entity" in msg
    assert "2" in msg  # gap - 1 == 2 missed gestures


def test_integral_float_accepted():
    """An integral float such as 51.0 is accepted and normalised."""
    decoder = GestureDecoder(name="test")
    decoder.decode(50.0, ORIGIN_PUSH)  # baseline seq=5 code=0
    assert decoder.decode(51.0, ORIGIN_PUSH) == DecodedGesture("single", 5)


def test_numeric_string_accepted():
    """A numeric string is accepted and normalised."""
    decoder = GestureDecoder(name="test")
    decoder.decode("50", ORIGIN_PUSH)  # baseline
    assert decoder.decode("61", ORIGIN_PUSH) == DecodedGesture("single", 6)


@pytest.mark.parametrize(
    "bad",
    [
        make_value(5, 5),   # code 5 invalid
        make_value(5, 9),   # code 9 invalid
        -10,                # negative
        123456,             # above 99999
        51.5,               # non-integral float
        "abc",              # non-numeric string
        True,               # boolean is not a gesture value
    ],
)
def test_invalid_values_never_fire_and_keep_baseline(bad, caplog):
    """Invalid values warn once, never fire, and leave the baseline intact."""
    decoder = GestureDecoder(name="test")
    decoder.decode(make_value(5, 0), ORIGIN_PUSH)  # baseline seq=5
    with caplog.at_level(logging.WARNING):
        assert decoder.decode(bad, ORIGIN_PUSH) is None
    # Baseline unchanged: a later valid gesture fires with the expected gap.
    assert decoder.decode(make_value(6, 1), ORIGIN_PUSH) == DecodedGesture("single", 6)


def test_invalid_value_warns_once_per_distinct_value(caplog):
    """The same invalid value is warned about only once."""
    decoder = GestureDecoder(name="test")
    decoder.decode(make_value(5, 0), ORIGIN_PUSH)  # baseline
    with caplog.at_level(logging.WARNING):
        decoder.decode(make_value(5, 6), ORIGIN_PUSH)
        decoder.decode(make_value(5, 6), ORIGIN_PUSH)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_none_and_empty_are_no_value_yet(caplog):
    """None and "" are 'no value yet': silent, no warning, baseline unset.

    GrentonCluStateVariable turns None into "", so a failed startup register
    must not look like an invalid value during seeding.
    """
    decoder = GestureDecoder(name="test")
    with caplog.at_level(logging.WARNING):
        assert decoder.decode(None, ORIGIN_RESYNC) is None
        assert decoder.decode("", ORIGIN_RESYNC) is None
        assert decoder.decode("   ", ORIGIN_PUSH) is None
    assert not caplog.records  # no warnings
    # Baseline is still unset: the next value is treated as first-seen baseline.
    assert decoder.decode(make_value(6, 1), ORIGIN_PUSH) is None
    # And the value after that fires normally.
    assert decoder.decode(make_value(7, 2), ORIGIN_PUSH) == DecodedGesture("double", 7)
