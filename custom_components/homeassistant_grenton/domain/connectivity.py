"""Connectivity tracking for a single CLU.

Pure state machine without Home Assistant dependencies. The coordinator feeds
it ping results and every message received from the CLU, and acts on the
transitions it reports.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

# Consecutive failed pings after which a CLU is considered disconnected. With
# a 5 s ping interval and a 5 s response timeout, a failed ping cycle takes
# about 10 s, so this is about 30 s.
FAILED_PINGS_THRESHOLD = 3


class ConnectivityTransition(Enum):
    """Transition reported by CluConnectivity."""

    NONE = "none"
    DISCONNECTED = "disconnected"
    RECONNECTED = "reconnected"


@dataclass
class CluConnectivity:
    """Connectivity and state-sync status of one CLU.

    ``connected`` is None until the first contact or until enough pings fail.
    ``synced`` is True when the cached state was refreshed from the CLU after
    the most recent (re)connect. Entities are available only when both hold.
    """

    threshold: int = FAILED_PINGS_THRESHOLD
    connected: bool | None = None
    synced: bool = False
    failed_pings: int = 0
    last_contact: datetime | None = None

    @property
    def available(self) -> bool:
        """True when entities of this CLU may show their state."""
        return self.connected is True and self.synced

    def record_contact(self, now: datetime) -> ConnectivityTransition:
        """Record any message received from the CLU."""
        self.last_contact = now
        self.failed_pings = 0
        if self.connected is True:
            return ConnectivityTransition.NONE
        self.connected = True
        # The cached state may be stale: it must be refreshed before entities
        # become available again.
        self.synced = False
        return ConnectivityTransition.RECONNECTED

    def record_ping_failure(self) -> ConnectivityTransition:
        """Record a ping that got no response."""
        self.failed_pings += 1
        if self.connected is False or self.failed_pings < self.threshold:
            return ConnectivityTransition.NONE
        self.connected = False
        self.synced = False
        return ConnectivityTransition.DISCONNECTED

    def mark_synced(self) -> bool:
        """Mark the state as refreshed. Returns True if entities became available."""
        if self.connected is not True or self.synced:
            return False
        self.synced = True
        return True
