"""Pure gesture decoding logic for Grenton gesture channels.

This module is intentionally free of any Home Assistant imports so it can be
unit tested without a Home Assistant installation. It turns the numeric value
of a gesture "channel" User Feature into discrete gesture events.

Value encoding (produced by the CLU, fixed contract)::

    value = seq * 10 + code
    seq  : 0..9999, incremented (mod 10000) by the CLU for every emitted gesture
    code : 0 = idle (CLU clears the code ~2 s after a gesture, seq unchanged)
           1 = single, 2 = double, 3 = hold_start, 4 = hold_release

Codes 5..9, negative numbers, non-integral numbers and values above 99999 are
invalid. The CLU computes the value with Lua division, so it may arrive as a
float with an integral value (e.g. ``51.0``) or as a numeric string; those are
accepted and normalised to int.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

# origin values
ORIGIN_PUSH = "push"
ORIGIN_RESYNC = "resync"

# code -> event type
_CODE_TO_TYPE = {
    1: "single",
    2: "double",
    3: "hold_start",
    4: "hold_release",
}

_SEQ_MOD = 10000
_MAX_VALUE = 99999


@dataclass(frozen=True)
class DecodedGesture:
    """A decoded gesture ready to be fired as an event."""

    event_type: str
    sequence: int


class GestureDecoder:
    """Stateful decoder for a single gesture channel.

    A decoder instance tracks the last seen value for one key and decides,
    given a new value and its origin, whether a gesture event should fire.
    """

    def __init__(self, name: str = "gesture", logger: Optional[logging.Logger] = None) -> None:
        self._name = name
        self._logger = logger if logger is not None else logging.getLogger(__name__)
        self._last_value: Optional[int] = None
        self._invalid_seen: set[Any] = set()

    def decode(self, raw_value: Any, origin: str) -> Optional[DecodedGesture]:
        """Decode a raw value and return a gesture to fire, or ``None``.

        Args:
            raw_value: The value of this channel's key (int, integral float,
                numeric string, ``None`` or ``""``).
            origin: ``"push"`` if the value arrived in a clientReport, or
                ``"resync"`` if it arrived in a register response (including the
                first one at startup).
        """
        # "No value yet": None or empty string. Silent, no warning, baseline
        # stays unset. GrentonCluStateVariable turns None into "", so a failed
        # startup register would otherwise look like an invalid value.
        if raw_value is None or (isinstance(raw_value, str) and raw_value.strip() == ""):
            return None

        value = self._parse(raw_value)
        if value is None or value % 10 > 4:
            # Invalid value: warn once per distinct raw value, treat as baseline
            # (never fire), and keep the numeric baseline unchanged so seq math
            # is not corrupted.
            if raw_value not in self._invalid_seen:
                self._invalid_seen.add(raw_value)
                self._logger.warning(
                    "[%s] Ignoring invalid gesture value: %r", self._name, raw_value
                )
            return None

        last = self._last_value

        # The first value ever seen is a baseline only. Never fire on it.
        if last is None:
            self._last_value = value
            return None

        # A resync value never fires. It only updates the baseline. This is
        # essential: if a push packet was lost, the periodic resync must not
        # deliver a stale gesture long after the press.
        if origin == ORIGIN_RESYNC:
            self._last_value = value
            return None

        # push origin from here on.

        # Every clientReport carries all subscribed keys, so an unchanged value
        # in a report triggered by another key must not fire.
        if value == last:
            return None

        code = value % 10
        seq = value // 10

        # Update the baseline for every changed value we observe, including the
        # idle clear, so the next comparison is against the latest value.
        self._last_value = value

        # Code 0 (idle) updates the baseline silently.
        if code == 0:
            return None

        # Warn (but still fire) if we appear to have missed gestures.
        last_seq = last // 10
        gap = (seq - last_seq) % _SEQ_MOD
        if gap > 1:
            self._logger.warning(
                "[%s] Missed %d gesture(s) (seq gap), firing latest", self._name, gap - 1
            )

        return DecodedGesture(event_type=_CODE_TO_TYPE[code], sequence=seq)

    @staticmethod
    def _parse(raw_value: Any) -> Optional[int]:
        """Normalise a raw value to an int in ``0..99999`` or ``None``.

        Accepts ints, integral floats and numeric strings. Rejects booleans,
        non-integral numbers, negatives and values above 99999.
        """
        # bool is a subclass of int; a gesture value is never a boolean.
        if isinstance(raw_value, bool):
            return None

        if isinstance(raw_value, int):
            value = raw_value
        elif isinstance(raw_value, float):
            if not raw_value.is_integer():
                return None
            value = int(raw_value)
        elif isinstance(raw_value, str):
            stripped = raw_value.strip()
            try:
                as_float = float(stripped)
            except ValueError:
                return None
            if not as_float.is_integer():
                return None
            value = int(as_float)
        else:
            return None

        if value < 0 or value > _MAX_VALUE:
            return None

        return value
