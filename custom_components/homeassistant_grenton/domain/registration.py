"""Splitting the subscribed keys into clientRegister sessions.

Pure logic without Home Assistant dependencies.

A CLU takes a request of at most 1472 bytes encrypted: one Ethernet frame
without IP fragments (1500 - 20 IP - 8 UDP). It drops a fragmented request
without answering (measured on a real CLU by the vclu project: 1472 bytes
answered, 1488 not). It also answers at most about 2047 characters. One
clientRegister with every key of a large installation crosses those limits,
and then the CLU never answers it.

The keys are therefore packed, in subscription order, into sessions of at
most MAX_ENTRIES_PER_SESSION entries (as many as Object Manager itself puts
in one clientRegister) whose request stays within REGISTER_BUDGET_BYTES.
Each session has its own session ID, which the CLU repeats in every report.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..state import GrentonCluStateAttributeKey, GrentonCluStateVariableKey

StateKey = GrentonCluStateVariableKey | GrentonCluStateAttributeKey

# Largest encrypted request a CLU answers.
MAX_REQUEST_BYTES = 1472
# Encrypted size a registration request is kept within, leaving a margin
# below MAX_REQUEST_BYTES. Small answers also stay well below the CLU's
# answer limit.
REGISTER_BUDGET_BYTES = 1300
# Entries per clientRegister; Object Manager registers 28 at most.
MAX_ENTRIES_PER_SESSION = 28
# Length of a message ID in the request header.
_MSG_ID_LENGTH = 8
_AES_BLOCK = 16


@dataclass(frozen=True)
class RegistrationSession:
    """One clientRegister session: its ID and the keys it reports, in order."""

    session_id: int
    keys: tuple[StateKey, ...]


def entry_text(key: StateKey) -> str:
    """The clientRegister entry for a key: ``"name"`` or ``{OBJECT,index}``."""
    if isinstance(key, GrentonCluStateVariableKey):
        name = key.name.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{name}"'
    return f"{{{key.object_name},{key.name}}}"


def register_payload(session_id: int, keys: tuple[StateKey, ...] | list[StateKey]) -> str:
    """The Lua call registering ``keys`` under ``session_id``.

    Address and port 0 make the CLU send its reports to the address and port
    the request came from.
    """
    entries = ",".join(entry_text(key) for key in keys)
    return f"SYSTEM:clientRegister(0,0,{session_id},{{{entries}}})"


def destroy_payload(session_id: int) -> str:
    """The Lua call ending the session ``session_id`` of this client."""
    return f"SYSTEM:clientDestroy(0,0,{session_id})"


def encrypted_size(plaintext_bytes: int) -> int:
    """Size of an AES-CBC datagram with PKCS7 padding for a plaintext size."""
    return (plaintext_bytes // _AES_BLOCK + 1) * _AES_BLOCK


def request_size(payload: str) -> int:
    """Encrypted size of a request carrying ``payload``."""
    header = len(f"req::{'0' * _MSG_ID_LENGTH}:".encode())
    return encrypted_size(header + len(payload.encode()))


def plan_sessions(
    keys: list[StateKey],
    first_session_id: int,
    *,
    budget_bytes: int | None = None,
    max_entries: int | None = None,
) -> list[RegistrationSession]:
    """Pack ``keys`` in order into sessions within the size and entry limits.

    Session IDs are consecutive from ``first_session_id``. A key whose entry
    alone exceeds the budget gets a session of its own. The limits default
    to REGISTER_BUDGET_BYTES and MAX_ENTRIES_PER_SESSION.
    """
    if budget_bytes is None:
        budget_bytes = REGISTER_BUDGET_BYTES
    if max_entries is None:
        max_entries = MAX_ENTRIES_PER_SESSION
    sessions: list[RegistrationSession] = []
    current: list[StateKey] = []

    def session_id() -> int:
        return first_session_id + len(sessions)

    for key in keys:
        candidate = current + [key]
        fits = (
            len(candidate) <= max_entries
            and request_size(register_payload(session_id(), candidate)) <= budget_bytes
        )
        if fits or not current:
            current = candidate
            continue
        sessions.append(RegistrationSession(session_id(), tuple(current)))
        current = [key]

    if current:
        sessions.append(RegistrationSession(session_id(), tuple(current)))
    return sessions
