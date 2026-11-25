"""Tests for CLU restart detection from its uptime."""

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

from custom_components.homeassistant_grenton.domain import uptime  # noqa: E402
from custom_components.homeassistant_grenton.domain.clu import GrentonClu  # noqa: E402

UptimeTracker = uptime.UptimeTracker


def clu(id, serial="221010475"):
    return GrentonClu(id=id, serial_number=serial, name="x", ip="192.168.1.10", port=1234)


def test_first_reading_is_not_a_restart():
    assert UptimeTracker().observe(100000, 0.0) is False


def test_uptime_growing_with_time_is_not_a_restart():
    tracker = UptimeTracker()
    tracker.observe(100000, 0.0)
    assert tracker.observe(100030, 30.0) is False
    # Whole-second resolution and request delays.
    assert tracker.observe(100058, 60.4) is False


def test_uptime_going_back_is_a_restart():
    tracker = UptimeTracker()
    tracker.observe(100000, 0.0)
    assert tracker.observe(12, 30.0) is True
    # The new reading is the new baseline.
    assert tracker.observe(42, 60.0) is False


def test_restart_right_after_a_reading_is_detected():
    tracker = UptimeTracker()
    tracker.observe(3600, 0.0)
    # Rebooted 1 s after the reading, read again 25 s later.
    assert tracker.observe(24, 25.0) is True


def test_slow_clu_clock_over_a_long_gap_is_not_a_restart():
    tracker = UptimeTracker()
    tracker.observe(100000, 0.0)
    ten_hours = 36000.0
    # The CLU's clock ran 0.2 % slow during a 10 h network outage.
    assert tracker.observe(100000 + ten_hours * 0.998, ten_hours) is False


def test_clu_object_name():
    assert uptime.clu_object_name(clu("CLU221010475")) == "CLU221010475"
    assert uptime.clu_object_name(clu("some-uuid", "221010475")) == "CLU221010475"
    assert uptime.clu_object_name(clu("some-uuid", "CLU221010475")) == "CLU221010475"
    # Never something that is not a plain Lua name.
    assert uptime.clu_object_name(clu("x); os.exit(", "1 2")) is None


def test_parse_uptime():
    assert uptime.parse_uptime("12345") == 12345
    assert uptime.parse_uptime(' "12345" ') == 12345
    assert uptime.parse_uptime("12.5") == 12.5
    assert uptime.parse_uptime("nil") is None
    assert uptime.parse_uptime("") is None
    assert uptime.parse_uptime("-5") is None
    assert uptime.parse_uptime("nan") is None
