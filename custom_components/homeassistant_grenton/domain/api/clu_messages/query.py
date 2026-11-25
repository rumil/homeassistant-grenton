from __future__ import annotations

from .base import GrentonCluApiRequest


class GrentonCluApiQueryRequest(GrentonCluApiRequest):
    """Request reading a value, e.g. ``CLU221010475:get(0)``.

    The CLU evaluates the expression and answers with its value; it puts
    ``return`` in front of it itself.
    """

    kind = "query"

    def __init__(self, expression: str, msg_id: str | None = None):
        super().__init__(expression, msg_id)
