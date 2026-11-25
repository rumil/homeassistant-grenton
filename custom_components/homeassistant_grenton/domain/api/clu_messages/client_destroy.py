from __future__ import annotations

from .base import GrentonCluApiRequest
from ...registration import destroy_payload


class GrentonCluApiClientDestroyRequest(GrentonCluApiRequest):
    """Request ending one of this client's clientRegister sessions.

    Sent from the socket that registered it: address and port 0 stand for
    the sender, as in clientRegister. The CLU answers with the session ID.
    """

    kind = "destroy"
    session_id: int

    def __init__(self, session_id: int, msg_id: str | None = None):
        self.session_id = session_id
        super().__init__(destroy_payload(session_id), msg_id)
