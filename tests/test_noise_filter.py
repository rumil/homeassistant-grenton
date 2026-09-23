"""Unit tests for the pure NoiseFilter.

The filter module is loaded directly from its file so these tests run without
a Home Assistant installation and without importing the integration package
(whose ``__init__`` pulls in Home Assistant).
"""

import importlib.util
import sys
from pathlib import Path

_MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "homeassistant_grenton"
    / "domain"
    / "noise_filter.py"
)

_spec = importlib.util.spec_from_file_location("grenton_noise_filter", _MODULE_PATH)
assert _spec and _spec.loader
noise_filter = importlib.util.module_from_spec(_spec)
# Register before exec so dataclass introspection can resolve the module.
sys.modules[_spec.name] = noise_filter
_spec.loader.exec_module(noise_filter)

NoiseFilter = noise_filter.NoiseFilter
NoiseFilterParams = noise_filter.NoiseFilterParams

PARAMS = NoiseFilterParams(deadband=0.2, settle_seconds=60)


def make(initial=23.0):
    f = NoiseFilter(PARAMS)
    assert f.offer(initial, 0.0) is True
    return f


def test_first_value_is_published():
    f = NoiseFilter(PARAMS)
    assert f.offer(22.9, 0.0) is True
    assert f.published == 22.9
    assert f.pending_deadline is None


def test_one_step_flicker_is_suppressed():
    f = make(23)
    # Real-world pattern: 23 -> 23.1 for about a second -> back to 23.
    for t in range(0, 600, 10):
        assert f.offer(23.1, t) is False
        assert f.offer(23, t + 1) is False
    assert f.published == 23
    assert f.pending_deadline is None


def test_small_change_published_after_settle_time():
    f = make(23.0)
    assert f.offer(23.1, 100.0) is False
    assert f.pending_deadline == 160.0
    assert f.offer(23.1, 159.0) is False
    assert f.offer(23.1, 160.0) is True
    assert f.published == 23.1
    assert f.pending_deadline is None


def test_settle_clock_restarts_when_candidate_moves():
    f = make(23.0)
    assert f.offer(23.1, 0.0) is False
    assert f.offer(22.9, 30.0) is False
    assert f.pending_deadline == 90.0
    assert f.offer(22.9, 89.0) is False
    assert f.offer(22.9, 90.0) is True
    assert f.published == 22.9


def test_large_change_published_immediately():
    f = make(23.0)
    assert f.offer(23.2, 5.0) is True
    assert f.published == 23.2
    assert f.offer(22.8, 6.0) is True
    assert f.published == 22.8


def test_deadband_boundary_tolerates_float_error():
    f = make(22.9)
    # 23.1 - 22.9 == 0.20000000000000284; 22.9 - 22.7 == 0.1999999999999993
    assert f.offer(23.1, 1.0) is True
    f = make(22.9)
    assert f.offer(22.7, 1.0) is True


def test_non_numeric_values_bypass_filter():
    f = make(23.0)
    assert f.offer(None, 1.0) is True
    assert f.published is None
    assert f.offer(23.1, 2.0) is True
    assert f.published == 23.1
    assert f.offer("error", 3.0) is True
    assert f.offer("error", 4.0) is False


def test_booleans_are_not_treated_as_numbers():
    f = make(1)
    assert f.offer(True, 1.0) is False  # True == 1, so no visible change
    assert f.published is True
    assert f.offer(1.1, 2.0) is True


def test_int_and_float_equal_values_do_not_count_as_change():
    f = make(23)
    assert f.offer(23.0, 1.0) is False
    assert f.pending_deadline is None
