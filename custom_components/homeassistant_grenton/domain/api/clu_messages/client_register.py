from __future__ import annotations

from ....state import (
    GrentonCluStateVariableKey,
    GrentonCluStateAttributeKey,
    GrentonValue,
)
from .base import GrentonCluApiRequest, GrentonCluApiResponse, GrentonCluApiNotification
from .lua_literal import parse_lua_table
from ...registration import register_payload


def parse_client_report_payload(payload: str) -> tuple[int, list[GrentonValue]]:
    """Parse a ``clientReport:<session>:{...}`` payload.

    Returns the session ID and the values.

    Raises:
        ValueError: when the payload is not a clientReport or its table
            cannot be parsed. A report that cannot be parsed must not be
            applied: guessing would put values on the wrong keys.
    """
    if not payload.startswith("clientReport:"):
        raise ValueError(f"Not a clientReport: {payload!r}")
    
    report_parts = payload.split(":", 2)
    if len(report_parts) != 3:
        raise ValueError(f"Invalid clientReport: {payload!r}")
    
    try:
        session_id = int(report_parts[1])
    except ValueError:
        raise ValueError(f"Invalid session ID in clientReport: {payload!r}") from None
    
    return session_id, parse_lua_table(report_parts[2])


class GrentonCluApiClientRegisterRequest(GrentonCluApiRequest):
    """Request to register component states for subscription.
    
    Registers one session: its keys are reported under ``session_id``.
    """
    
    kind = "register"
    keys: list[GrentonCluStateVariableKey | GrentonCluStateAttributeKey]
    session_id: int
    
    def __init__(
        self,
        keys: list[GrentonCluStateVariableKey | GrentonCluStateAttributeKey],
        session_id: int,
        msg_id: str | None = None,
    ):
        """Initialize with the session's state keys and its session ID."""
        if not keys:
            raise ValueError("Cannot create registration request with no keys")
        
        self.keys = list(keys)
        self.session_id = session_id
        super().__init__(register_payload(session_id, self.keys), msg_id)


class GrentonCluApiClientRegisterResponse(GrentonCluApiResponse):
    """Response from client registration with initial values."""
    
    session_id: int
    values: list[GrentonValue]
    
    def __init__(self, wire_message: str):
        """Initialize from wire format message and parse values."""
        super().__init__(wire_message)
        self.session_id, self.values = parse_client_report_payload(self.payload)


class GrentonCluApiClientReportNotification(GrentonCluApiNotification):
    """Notification with subscription report and updated values."""
    
    session_id: int
    values: list[GrentonValue]
    
    def __init__(self, wire_message: str):
        """Initialize from wire format message and parse values."""
        super().__init__(wire_message)
        self.session_id, self.values = parse_client_report_payload(self.payload)
