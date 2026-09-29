"""Pure noise filter for numeric readings (no Home Assistant imports).

Grenton sensors that sit on a rounding boundary flicker by one step many times
a minute (e.g. 22.9 <-> 23.0 °C), flooding the state machine and recorder. The
filter publishes a raw reading only when it is clearly a real change:

- a change of at least ``deadband`` from the published value is published
  immediately (fast reaction to real jumps);
- a smaller change is published only after the new value has held steady for
  ``settle_seconds`` (one-step flicker that reverts sooner is dropped).

Numeric readings listed in ``invalid_values`` are dropped: the published value
is kept for up to ``invalid_hold_seconds``; if the reading is still invalid
after that, None (unknown) is published. Grenton temperature sensors report
-255 while the CLU is being configured and for a moment after power returns,
so a short burst is ridden out while a sensor that stays broken shows up.

Non-numeric readings (None, strings, booleans) bypass the filter unchanged.
Time is passed in by the caller, so the logic is deterministic and testable;
scheduling the settle and hold deadlines is the caller's job (see
``pending_deadline``).
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
    invalid_values: frozenset[float] = frozenset()
    invalid_hold_seconds: float = 0


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
        self._invalid_since: float | None = None

    @property
    def pending_deadline(self) -> float | None:
        """Time at which ``published`` would change without a new reading, if any.

        Either the pending small change settles, or an invalid reading that is
        still held turns into None.
        """
        if self._candidate_since is not None:
            return self._candidate_since + self.params.settle_seconds
        if self._invalid_since is not None and self.published is not None:
            return self._invalid_since + self.params.invalid_hold_seconds
        return None

    def offer(self, raw: Any, now: float) -> bool:
        """Feed the current raw reading; return True if ``published`` changed."""
        number = _as_number(raw)
        if number is not None and self._is_invalid(number):
            # Hold the published value for a while; a bogus reading also
            # breaks any settle.
            self._clear_candidate()
            if self._invalid_since is None:
                self._invalid_since = now
            if now >= self._invalid_since + self.params.invalid_hold_seconds - _EPSILON:
                return self._publish_value(None)
            return False
        self._invalid_since = None
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

    def _is_invalid(self, number: float) -> bool:
        return any(abs(number - invalid) < _EPSILON for invalid in self.params.invalid_values)

    def _publish(self, raw: Any) -> bool:
        self._clear_candidate()
        return self._publish_value(raw)

    def _publish_value(self, raw: Any) -> bool:
        changed = raw != self.published
        self.published = raw
        return changed

    def _clear_candidate(self) -> None:
        self._candidate = None
        self._candidate_since = None
