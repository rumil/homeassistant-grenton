from __future__ import annotations

from ...action import (
    GrentonAction,
    GrentonActionVariable,
    GrentonActionAttribute,
    GrentonActionMethod,
    GrentonActionScript,
)
from .base import GrentonCluApiRequest, GrentonCluApiResponse


class GrentonCluApiActionRequest(GrentonCluApiRequest):
    """Request to execute an action on the CLU.
    
    Supports variable, attribute, method, and script actions.
    """
    
    idempotent: bool
    
    def __init__(self, payload: str, msg_id: str | None = None, idempotent: bool = False):
        """Initialize with payload, optional message ID and idempotency flag.
        
        Args:
            payload: The payload string to execute
            msg_id: Unique message ID for tracking (defaults to random hex)
            idempotent: True when executing the payload twice has the same
                effect as executing it once, so it is safe to retry
        """
        super().__init__(payload, msg_id)
        self.idempotent = idempotent
    
    @staticmethod
    def from_action(
        action: GrentonAction,
        msg_id: str | None = None
    ) -> 'GrentonCluApiActionRequest':
        """Create request from a GrentonAction.
        
        Args:
            action: GrentonAction instance (Variable, Attribute, Method, or Script)
            msg_id: Optional message ID
            
        Returns:
            GrentonCluApiActionRequest with appropriate payload
        """
        # setVar and set write an absolute value, so they are idempotent.
        # execute() calls an object method that may toggle or change a value
        # relatively (e.g. Switch, Start/Stop, Step), and a script can do
        # anything, so those are never treated as idempotent.
        if isinstance(action, GrentonActionVariable):
            payload = f'setVar("{action.index}","{action.value}")'
            idempotent = True
        elif isinstance(action, GrentonActionAttribute):
            payload = f'{action.object_name}:set({action.index},"{action.value}")'
            idempotent = True
        elif isinstance(action, GrentonActionMethod):
            payload = f'{action.object_name}:execute({action.index},"{action.value}")'
            idempotent = False
        elif isinstance(action, GrentonActionScript):
            payload = f'{action.object_name}({action.value})'
            idempotent = False
        else:
            raise ValueError(f"Unsupported action type: {type(action).__name__}")
        
        return GrentonCluApiActionRequest(payload, msg_id, idempotent)
    
    def with_new_msg_id(self) -> 'GrentonCluApiActionRequest':
        """Return a copy of this request with a fresh message ID.
        
        Used for retries, so a late response to an abandoned attempt cannot
        be matched to the new attempt.
        """
        while True:
            request = GrentonCluApiActionRequest(self.payload, None, self.idempotent)
            if request.msg_id != self.msg_id:
                return request


class GrentonCluApiActionResponse(GrentonCluApiResponse):
    """Response from action execution."""
    
    def __init__(self, wire_message: str):
        """Initialize from wire format message."""
        super().__init__(wire_message)
