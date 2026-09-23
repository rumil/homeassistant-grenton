"""Pure noise filter for numeric readings (no Home Assistant imports).

Grenton sensors that sit on a rounding boundary flicker by one step many times
a minute (e.g. 22.9 <-> 23.0 °C), flooding the state machine and recorder. The
filter publishes a raw reading only when it is clearly a real change:

- a change of at least ``deadband`` from the published value is published
  immediately (fast reaction to real jumps);
- a smaller change is published only after the new value has held steady for
  ``settle_seconds`` (one-step flicker that reverts sooner is dropped).

Non-numeric readings (None, strings, booleans) bypass the filter unchanged.
Time is passed in by the caller, so the logic is deterministic and testable;
scheduling the settle deadline is the caller's job (see ``pending_deadline``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_EPSILON = 1e-9


@dataclass(frozen=True)
class NoiseFilterParams:
    """Tuning for one filtered reading."""

    deadband: float
    settle_seconds: float


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


class NoiseFilter:
    """Hysteresis + debounce filter over a stream of raw readings."""

    def __init__(self, params: NoiseFilterParams) -> None:
        self.params = params
        self.published: Any = None
        self._candidate: float | None = None
        self._candidate_since: float | None = None

    @property
    def pending_deadline(self) -> float | None:
        """Time at which the pending small change would be published, if any."""
        if self._candidate_since is None:
            return None
        return self._candidate_since + self.params.settle_seconds

    def offer(self, raw: Any, now: float) -> bool:
        """Feed the current raw reading; return True if ``published`` changed."""
        number = _as_number(raw)
        current = _as_number(self.published)
        if number is None or current is None:
            return self._publish(raw)

        delta = abs(number - current)
        if delta < _EPSILON:
            # Back at the published value: the flicker reverted.
            self._clear_candidate()
            return False
        if delta >= self.params.deadband - _EPSILON:
            return self._publish(raw)

        # Small change: (re)start the settle clock whenever the candidate moves.
        if self._candidate is None or abs(self._candidate - number) >= _EPSILON:
            self._candidate = number
            self._candidate_since = now
        deadline = self.pending_deadline
        if deadline is not None and now >= deadline - _EPSILON:
            return self._publish(raw)
        return False

    def _publish(self, raw: Any) -> bool:
        self._clear_candidate()
        changed = raw != self.published
        self.published = raw
        return changed

    def _clear_candidate(self) -> None:
        self._candidate = None
        self._candidate_since = None
