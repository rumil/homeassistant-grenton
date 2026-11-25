from __future__ import annotations
from typing import Any, AsyncIterator, Optional, Dict, Callable
from contextlib import asynccontextmanager
from enum import Enum
import logging
import asyncio
import secrets

from ..clu import GrentonClu
from ..encryption import GrentonEncryption
from ..action import GrentonAction
from ...state import GrentonValue
from ..cipher import GrentonCipher
from ..registration import MAX_REQUEST_BYTES, RegistrationSession
from .clu_messages import (
    GrentonCluApiMessageParser,
    GrentonCluApiPingRequest,
    GrentonCluApiClientRegisterRequest,
    GrentonCluApiClientRegisterResponse,
    GrentonCluApiClientReportNotification,
    GrentonCluApiActionRequest,
)

_LOGGER = logging.getLogger(__name__)

# Maximum number of requests in flight per CLU at the same time. The CLU drops
# requests when it receives a burst of parallel datagrams, so by default
# requests are sent one at a time. Pings, registrations and actions all share
# this limit.
MAX_IN_FLIGHT_REQUESTS = 1

# Seconds to wait for a response, per kind of request. Counted from the
# moment the datagram is sent, not from the moment the request was queued.
# A CLU on the local network answers in tens of milliseconds, so these leave
# a wide margin for a CLU busy running a script.
#
# Pings keep 5 s: hang detection (3 failed pings in a row) is built on it.
PING_TIMEOUT = 5.0
# A registration answer carries the values of every key in the session.
REGISTER_TIMEOUT = 5.0
# One attempt of an action. An action runs inside the service call that
# sent it, so a short attempt keeps automations and the UI responsive:
# 3 attempts take at most 6 s.
ACTION_TIMEOUT = 2.0
# Any other request.
RESPONSE_TIMEOUT = 5.0

# Extra attempts for an idempotent action whose response timed out
# (2 retries = 3 attempts in total). Non-idempotent actions are never retried.
ACTION_RETRIES = 2

# Seconds a request slot stays blocked after a request completes before the
# next queued request may be sent. 0 disables the gap. If the CLU still drops
# requests with MAX_IN_FLIGHT_REQUESTS = 1, try 0.02 instead of raising
# RESPONSE_TIMEOUT.
REQUEST_GAP = 0.0

class ActionResult(Enum):
    """Outcome of an action sent to the CLU."""

    # The CLU answered the action.
    OK = "ok"
    # The action got no answer, and a newer write to the same target was
    # queued meanwhile. Retrying it would only delay the newer write, which
    # decides the final value.
    SUPERSEDED = "superseded"
    # No answer after every attempt.
    FAILED = "failed"


class GrentonCluApi:
    """Handles all communication with Grenton CLUs via UDP."""
    
    def __init__(self, clu: GrentonClu, encryption: GrentonEncryption):
        self.clu = clu
        self.encryption = encryption
        self.cipher = GrentonCipher(encryption)
        
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.protocol: Optional[GrentonCluApiProtocol] = None
        self.keep_alive_id = secrets.token_hex(4)
        # Session IDs of this client's clientRegister sessions start here.
        # Random per socket, so sessions of an earlier run (still alive on
        # the CLU until they expire) never share an ID with the new ones.
        self.first_session_id = 10000 + secrets.randbelow(50000)
        # Sequence number of the newest action per write target, so a retry
        # can tell that a newer write to the same target is waiting.
        self._action_counter = 0
        self._latest_write: dict[tuple[str, ...], int] = {}
        
    async def connect(self) -> bool:
        """Establish UDP connection to the CLU."""
        try:
            loop = asyncio.get_event_loop()
            
            # Create protocol instance first
            protocol = GrentonCluApiProtocol(self)
            self.protocol = protocol
            
            # Create UDP transport with the protocol instance
            self.transport, _ = await loop.create_datagram_endpoint(
                lambda: protocol,
                local_addr=('0.0.0.0', 0),  # Bind to any available port
            )
            
            _LOGGER.debug("[%s] Opened UDP socket at %s:%d", self.clu.id, self.clu.ip, self.clu.port)
            return True
            
        except Exception as e:
            _LOGGER.error("[%s] Failed to create UDP socket at %s:%d: %s", 
                         self.clu.id, self.clu.ip, self.clu.port, e)
            return False
    
    async def disconnect(self) -> None:
        """Close the UDP connection."""
        if self.transport:
            self.transport.close()
            _LOGGER.debug("[%s] Closed UDP socket", self.clu.id)
            self.transport = None
        
    async def ping(self) -> bool:
        """Send a keep-alive ping to the CLU."""
        if not self.protocol:
            _LOGGER.warning("[%s] No protocol available for ping", self.clu.id)
            return False
        
        request = GrentonCluApiPingRequest(self.keep_alive_id)
        # The coordinator counts failed pings and logs the outcome, so a ping
        # timeout is not logged as a warning here.
        wire_message = await self.protocol.send_request(request, timeout_log_level=logging.DEBUG)
        
        if wire_message is None:
            return False
        
        return True
    
    async def register_session(
        self,
        session: RegistrationSession,
        on_values: Optional[Callable[[list[GrentonValue]], bool]] = None,
    ) -> list[GrentonValue] | None:
        """Register (or renew) one clientRegister session with the CLU.
        
        ``on_values`` is called with the values of the answer as soon as the
        answer is received, before any datagram that arrived after it is
        handled. Applying them there keeps the answer and pushed reports in
        the order the CLU sent them. It returns False to reject the values,
        which makes the registration count as failed.
        
        Returns the values of the answer, or None when the registration
        failed.
        """
        if not session.keys:
            _LOGGER.debug("[%s] No state keys to register", self.clu.id)
            return None
        
        if not self.protocol:
            _LOGGER.warning("[%s] No protocol available for registration", self.clu.id)
            return None
        
        def handle_answer(wire_message: str) -> list[GrentonValue] | None:
            try:
                response = GrentonCluApiClientRegisterResponse(wire_message)
            except ValueError as e:
                _LOGGER.warning("[%s] Failed to parse register response: %s", self.clu.id, e)
                return None
            if response.session_id != session.session_id:
                _LOGGER.warning("[%s] Register answer for session %d, expected %d; ignored",
                                self.clu.id, response.session_id, session.session_id)
                return None
            if on_values is not None and not on_values(response.values):
                return None
            return response.values
        
        request = GrentonCluApiClientRegisterRequest(list(session.keys), session.session_id)
        request.on_response = handle_answer
        return await self.protocol.send_request(request)
    
    async def execute_action(self, action: GrentonAction) -> ActionResult:
        """Execute an action on the CLU.
        
        An idempotent write (setVar, set) whose answer times out is retried,
        all attempts in one request slot: no other request is sent between
        them, so a retry can never overtake a newer command. A retry is
        dropped when a newer write to the same target is already waiting:
        that write decides the final value.
        """
        if not self.protocol:
            _LOGGER.warning("[%s] No protocol available for action execution", self.clu.id)
            return ActionResult.FAILED
        
        # Build the payload before the first await: entities reuse and mutate
        # their action objects, so the value must be captured now.
        request = GrentonCluApiActionRequest.from_action(action)
        attempts = 1 + ACTION_RETRIES if request.idempotent else 1
        
        self._action_counter += 1
        sequence = self._action_counter
        if request.target is not None:
            self._latest_write[request.target] = sequence
        
        try:
            async with self.protocol.hold_slot():
                for attempt in range(1, attempts + 1):
                    if attempt > 1:
                        if self._latest_write.get(request.target) != sequence:  # type: ignore[arg-type]
                            _LOGGER.debug("[%s] Not retrying, a newer write to the same target is queued: %s",
                                          self.clu.name, request.payload)
                            return ActionResult.SUPERSEDED
                        request = request.with_new_msg_id()
                    
                    is_last_attempt = attempt == attempts
                    _LOGGER.debug("[%s][%s] Action attempt %d/%d: %s",
                                  self.clu.name, request.msg_id, attempt, attempts, request.payload)
                    
                    wire_message = await self.protocol.send_in_held_slot(
                        request,
                        timeout_log_level=logging.WARNING if is_last_attempt else logging.DEBUG,
                    )
                    
                    if wire_message is not None:
                        if attempt > 1:
                            _LOGGER.info("[%s][%s] Action succeeded on attempt %d/%d: %s",
                                         self.clu.name, request.msg_id, attempt, attempts, request.payload)
                        return ActionResult.OK
        finally:
            if request.target is not None and self._latest_write.get(request.target) == sequence:
                del self._latest_write[request.target]
        
        return ActionResult.FAILED


class GrentonCluApiProtocol(asyncio.DatagramProtocol):
    """UDP protocol handler for Grenton CLU API communication."""
    
    def __init__(self, api: GrentonCluApi):
        self.api = api
        # Futures of requests waiting for their answer, with the request.
        # Everything runs on the event loop, so no lock is needed.
        self._pending: Dict[str, tuple[asyncio.Future[Any], Any]] = {}
        self._response_timeout = RESPONSE_TIMEOUT
        self._timeouts: dict[str, float] = {
            "ping": PING_TIMEOUT,
            "register": REGISTER_TIMEOUT,
            "action": ACTION_TIMEOUT,
        }
        self._request_gap = REQUEST_GAP
        self._max_in_flight = MAX_IN_FLIGHT_REQUESTS
        self._slots = asyncio.Semaphore(MAX_IN_FLIGHT_REQUESTS)
        self._waiting = 0
        self._in_flight = 0
        # Called with the session ID and values of every pushed clientReport,
        # in the order the reports arrive.
        self.subscription_callback: Optional[Callable[[int, list[GrentonValue]], None]] = None
        # Called for every valid message received from the CLU (responses,
        # including late ones, and client reports): proof that it is alive.
        self.contact_callback: Optional[Callable[[], None]] = None
    
    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        _LOGGER.debug("[%s] UDP connection established", self.api.clu.name)
    
    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._handle_incoming_data(data)
    
    def _handle_incoming_data(self, data: bytes) -> None:
        """Handle incoming UDP data from the CLU."""
        decrypted = self.api.cipher.decrypt(data)
        if decrypted is None:
            _LOGGER.warning("[%s] Failed to decrypt message: %s", 
                          self.api.clu.name, data.hex())
            return
        
        try:
            plaintext = decrypted.decode().strip()
        except Exception:
            _LOGGER.warning("[%s] Failed to decode decrypted message: %s", 
                          self.api.clu.name, decrypted.hex())
            return
        
        # Check if it's a valid response
        if not GrentonCluApiMessageParser.is_valid_response(plaintext):
            _LOGGER.debug("[%s] Received unsupported message: %s", 
                        self.api.clu.name, plaintext)
            return
        
        self._process_response(plaintext)
    
    def _process_response(self, wire_message: str) -> None:
        """Process a message from the CLU.
        
        Messages are handled synchronously, one datagram at a time, so an
        answer and the reports around it are applied in the order the CLU
        sent them. (Handling each one in its own task let a report that
        arrived right after a register answer be applied first, and the
        older answer then overwrote it.)
        """
        # Extract message_id from wire format
        parts = wire_message.split(":", 3)
        if len(parts) != 4:
            _LOGGER.warning("[%s] Invalid wire message format", self.api.clu.name)
            return
        
        message_id = parts[2].lower()
        _LOGGER.debug("[%s][%s] Received: %s", self.api.clu.name, message_id, wire_message)
        
        if self.contact_callback:
            try:
                self.contact_callback()
            except Exception as e:  # pragma: no cover - defensive
                _LOGGER.error("[%s] Error in contact callback: %s", self.api.clu.name, e)
        
        pending = self._pending.pop(message_id, None)
        if pending is not None:
            future, request = pending
            if future.done():
                return
            result: Any = wire_message
            on_response = getattr(request, "on_response", None)
            if on_response is not None:
                try:
                    result = on_response(wire_message)
                except Exception as e:  # pragma: no cover - defensive
                    _LOGGER.error("[%s][%s] Error handling response: %s", self.api.clu.name, message_id, e)
                    result = None
            future.set_result(result)
            return
        
        # Check if this is a notification (subscription update) that needs processing
        if GrentonCluApiMessageParser.is_client_report(wire_message):
            if self.subscription_callback:
                try:
                    notification = GrentonCluApiClientReportNotification(wire_message)
                except ValueError as e:
                    _LOGGER.error("[%s] Failed to parse client report: %s", self.api.clu.name, e)
                    return
                try:
                    self.subscription_callback(notification.session_id, notification.values)
                except Exception as e:  # pragma: no cover - defensive
                    _LOGGER.error("[%s] Error handling client report: %s", self.api.clu.name, e)
            else:
                _LOGGER.debug("[%s][%s] Received report but no callback registered", self.api.clu.name, message_id)
        else:
            _LOGGER.debug("[%s][%s] Response received with no pending request", self.api.clu.name, message_id)
    
    async def send_request(
        self,
        request: Any,
        message_id: Optional[str] = None,
        *,
        timeout_log_level: int = logging.WARNING,
    ) -> Optional[str]:
        """Send a request to the CLU and wait for response.
        
        At most MAX_IN_FLIGHT_REQUESTS requests are in flight at once; other
        requests wait in a queue. The response timeout starts when the
        datagram is sent.
        
        Args:
            request: Request object implementing GrentonCluApiRequest protocol
            message_id: Deprecated - msg_id is now in the request itself
            timeout_log_level: Log level for the "Request timeout" message
            
        Returns:
            Response wire format string or None if failed
        """
        if not self.api.transport:
            _LOGGER.error("[%s] Transport not ready", self.api.clu.name)
            return None
        
        msg_id = request.msg_id
        encrypted = self._encrypt(request)
        if encrypted is None:
            return None
        
        _LOGGER.debug("[%s][%s] Queued (waiting: %d, in flight: %d/%d)",
                      self.api.clu.name, msg_id, self._waiting + 1, self._in_flight, self._max_in_flight)
        async with self.hold_slot():
            return await self._send_and_wait(request, msg_id, encrypted, timeout_log_level)
    
    @asynccontextmanager
    async def hold_slot(self) -> AsyncIterator[None]:
        """Hold one request slot for several requests sent one after another."""
        self._waiting += 1
        try:
            await self._slots.acquire()
        finally:
            self._waiting -= 1
        
        self._in_flight += 1
        try:
            yield
        finally:
            self._in_flight -= 1
            self._release_slot()
    
    async def send_in_held_slot(
        self,
        request: Any,
        *,
        timeout_log_level: int = logging.WARNING,
    ) -> Optional[str]:
        """Send a request from inside ``hold_slot`` and wait for its response."""
        if not self.api.transport:
            _LOGGER.error("[%s] Transport not ready", self.api.clu.name)
            return None
        encrypted = self._encrypt(request)
        if encrypted is None:
            return None
        return await self._send_and_wait(request, request.msg_id, encrypted, timeout_log_level)
    
    def _encrypt(self, request: Any) -> Optional[bytes]:
        """Encrypt a request, refusing one too long for the CLU.
        
        The CLU drops a request longer than one Ethernet frame without an
        answer, so sending it would only end in a timeout.
        """
        encrypted = self.api.cipher.encrypt(request.raw.encode())
        if encrypted is None:
            _LOGGER.error("[%s] Failed to encrypt request", self.api.clu.name)
            return None
        if len(encrypted) > MAX_REQUEST_BYTES:
            _LOGGER.error("[%s][%s] Request too long for the CLU (%d bytes, at most %d); not sent: %s",
                          self.api.clu.name, request.msg_id, len(encrypted), MAX_REQUEST_BYTES,
                          request.payload)
            return None
        return encrypted
    
    async def _send_and_wait(
        self,
        request: Any,
        msg_id: str,
        encrypted: bytes,
        timeout_log_level: int,
    ) -> Optional[str]:
        """Send one datagram and wait for its response. Caller holds a slot."""
        # The transport may have been closed while the request was queued.
        if not self.api.transport:
            _LOGGER.error("[%s] Transport not ready", self.api.clu.name)
            return None
        
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = (future, request)
        
        try:
            self.api.transport.sendto(encrypted, (self.api.clu.ip, self.api.clu.port))
            _LOGGER.debug("[%s][%s] Sent (waiting: %d, in flight: %d/%d): %s",
                          self.api.clu.name, msg_id, self._waiting, self._in_flight,
                          self._max_in_flight, request.raw)
        except Exception as e:
            self._pending.pop(msg_id, None)
            _LOGGER.error("[%s][%s] Failed to send request: %s", 
                        self.api.clu.name, msg_id, e)
            return None
        
        timeout = self._timeouts.get(getattr(request, "kind", ""), self._response_timeout)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            _LOGGER.log(timeout_log_level, "[%s][%s] Request timeout", self.api.clu.name, msg_id)
            return None
        except BaseException:
            self._pending.pop(msg_id, None)
            raise
    
    def _release_slot(self) -> None:
        """Free a request slot, after the configured gap if there is one."""
        if self._request_gap > 0:
            asyncio.get_running_loop().call_later(self._request_gap, self._slots.release)
        else:
            self._slots.release()
    
    def error_received(self, exc: Exception) -> None:
        _LOGGER.error("[%s] UDP error: %s", self.api.clu.name, exc)
    
    def connection_lost(self, exc: Optional[Exception]) -> None:
        if exc:
            _LOGGER.error("[%s] UDP connection lost: %s", self.api.clu.name, exc)
        else:
            _LOGGER.debug("[%s] UDP connection closed", self.api.clu.name)
        
        pending = list(self._pending.values())
        self._pending.clear()
        for fut, _ in pending:
            if not fut.done():
                fut.set_exception(ConnectionError(
                    f"UDP connection closed for CLU {self.api.clu.name}"))
