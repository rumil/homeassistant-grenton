from __future__ import annotations

from .base import GrentonCluApiRequest, GrentonCluApiResponse


def is_emergency_payload(payload: str) -> bool:
    """True for the answer of a CLU in emergency mode.

    In emergency mode a CLU answers checkAlive() with ``emergency`` (Object
    Manager capture in the opengr8on docs): it is reachable, but its project
    is not running, so subscriptions and actions do not work.
    """
    return payload.strip().strip('"').lower() == "emergency"


class GrentonCluApiPingRequest(GrentonCluApiRequest):
    """Ping request to check if CLU is alive."""
    
    kind = "ping"
    
    def __init__(self, msg_id: str | None = None):
        super().__init__("checkAlive()", msg_id)


class GrentonCluApiPingResponse(GrentonCluApiResponse):
    """Ping response from CLU."""

    def __init__(self, wire_message: str):
        """Initialize from wire format message."""
        super().__init__(wire_message)
