from __future__ import annotations

from ....state import (
    GrentonCluStateVariableKey,
    GrentonCluStateAttributeKey,
    GrentonValue,
)
from .base import GrentonCluApiRequest, GrentonCluApiResponse, GrentonCluApiNotification
from .lua_literal import parse_lua_table


def _parse_client_report_payload(payload: str) -> list[GrentonValue]:
    """Parse a ``clientReport:<session>:{...}`` payload into its values.

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
    
    return parse_lua_table(report_parts[2])


class GrentonCluApiClientRegisterRequest(GrentonCluApiRequest):
    """Request to register component states for subscription."""
    
    kind = "register"
    keys: list[GrentonCluStateVariableKey | GrentonCluStateAttributeKey]
    
    def __init__(self, keys: list[GrentonCluStateVariableKey | GrentonCluStateAttributeKey], msg_id: str | None = None):
        """Initialize with state keys to register."""
        self.keys = keys
        
        if not keys:
            raise ValueError("Cannot create registration request with no keys")
        
        mapped_keys: list[str] = []
        
        for key in keys:
            if isinstance(key, GrentonCluStateVariableKey):
                mapped_keys.append(f'"{key.name}"')
            elif isinstance(key, GrentonCluStateAttributeKey): # pyright: ignore[reportUnnecessaryIsInstance]
                mapped_keys.append(f'{{{key.object_name},{key.name}}}')
        
        payload = f'SYSTEM:clientRegister(0,0,1,{{{",".join(mapped_keys)}}})'
        super().__init__(payload, msg_id)


class GrentonCluApiClientRegisterResponse(GrentonCluApiResponse):
    """Response from client registration with initial values."""
    
    values: list[GrentonValue]
    
    def __init__(self, wire_message: str):
        """Initialize from wire format message and parse values."""
        super().__init__(wire_message)
        self.values = _parse_client_report_payload(self.payload)


class GrentonCluApiClientReportNotification(GrentonCluApiNotification):
    """Notification with subscription report and updated values."""
    
    values: list[GrentonValue]
    
    def __init__(self, wire_message: str):
        """Initialize from wire format message and parse values."""
        super().__init__(wire_message)
        self.values = _parse_client_report_payload(self.payload)
