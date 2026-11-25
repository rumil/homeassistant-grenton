"""CLU restart detection from its uptime.

Pure logic without Home Assistant dependencies.

A CLU keeps its clientRegister sessions and its User Features in RAM, so a
restart loses the sessions and resets the User Features to their initial
values. A restart can be faster than the ping-based outage detection (3
failed pings), so it is detected from the CLU's own uptime instead: feature
0 of the CLU object, the time in seconds since its last reset (Object
Manager's CLU definition). An uptime lower than the previous reading plus
the time elapsed since means the CLU started again in between.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime

from .clu import GrentonClu

# Feature of the CLU object holding its uptime in seconds.
UPTIME_FEATURE = 0

# Seconds an uptime reading may fall behind the expected value before it
# counts as a restart: covers whole-second resolution and request delays.
# A restart drops the uptime to the time since the reset, far below it.
RESTART_TOLERANCE = 10.0
# Fraction of the time between readings added to the tolerance, so a CLU
# clock running slightly slow is not taken for a restart after a long gap.
RESTART_TOLERANCE_RATE = 0.01

_OBJECT_NAME = re.compile(r"^CLU[0-9A-Za-z_]+$")
_SERIAL = re.compile(r"^[0-9A-Za-z_]+$")


def clu_object_name(clu: GrentonClu) -> str | None:
    """The Lua name of the CLU object, e.g. ``CLU221010475``.

    Taken from the interface's CLU id, or built from the serial number. Only
    a plain Lua identifier is returned, as it is sent to the CLU as code.
    """
    if _OBJECT_NAME.match(clu.id):
        return clu.id
    serial = clu.serial_number
    if serial.upper().startswith("CLU"):
        serial = serial[3:]
    if _SERIAL.match(serial):
        return f"CLU{serial}"
    return None


def parse_uptime(payload: str) -> float | None:
    """Uptime in seconds from the CLU's answer, or None if it is not one."""
    text = payload.strip().strip('"').strip()
    try:
        value = float(text)
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


@dataclass
class RestartInfo:
    """A detected restart."""

    boot_time: datetime
    uptime: float


@dataclass
class UptimeTracker:
    """Last uptime reading of one CLU and the monotonic time it was taken."""

    uptime: float | None = None
    observed_at: float | None = None
    readings: int = field(default=0)

    def observe(self, uptime: float, now: float) -> bool:
        """Record a reading. Returns True if the CLU restarted since the last one."""
        restarted = False
        if self.uptime is not None and self.observed_at is not None:
            elapsed = max(0.0, now - self.observed_at)
            expected = self.uptime + elapsed
            tolerance = RESTART_TOLERANCE + RESTART_TOLERANCE_RATE * elapsed
            restarted = uptime + tolerance < expected
        self.uptime = uptime
        self.observed_at = now
        self.readings += 1
        return restarted
