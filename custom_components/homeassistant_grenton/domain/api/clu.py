from __future__ import annotations
from typing import Any, Optional, Dict, Callable, Awaitable
import logging
import asyncio
import secrets

from ..clu import GrentonClu
from ..encryption import GrentonEncryption
from ..action import GrentonAction
from ...state import GrentonCluStateVariableKey, GrentonCluStateAttributeKey, GrentonValue
from ..cipher import GrentonCipher
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

# Seconds to wait for a response. Counted from the moment the datagram is
# sent, not from the moment the request was queued.
RESPONSE_TIMEOUT = 5.0

# Extra attempts for an idempotent action whose response timed out
# (2 retries = 3 attempts in total). Non-idempotent actions are never retried.
ACTION_RETRIES = 2

# Seconds a request slot stays blocked after a request completes before the
# next queued request may be sent. 0 disables the gap. If the CLU still drops
# requests with MAX_IN_FLIGHT_REQUESTS = 1, try 0.02 instead of raising
# RESPONSE_TIMEOUT.
REQUEST_GAP = 0.0

class GrentonCluApi:
    """Handles all communication with Grenton CLUs via UDP."""
    
    def __init__(self, clu: GrentonClu, encryption: GrentonEncryption):
        self.clu = clu
        self.encryption = encryption
        self.cipher = GrentonCipher(encryption)
        
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.protocol: Optional[GrentonCluApiProtocol] = None
        self.keep_alive_id = secrets.token_hex(4)
        self.subscription_id = secrets.token_hex(4)
        
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
    
    async def register_component_states(self, keys: list[GrentonCluStateVariableKey | GrentonCluStateAttributeKey]) -> list[GrentonValue] | None:
        """Register component state keys with the CLU."""
        if not keys:
            _LOGGER.debug("[%s] No state keys to register", self.clu.id)
            return None
        
        if not self.protocol:
            _LOGGER.warning("[%s] No protocol available for registration", self.clu.id)
            return None
        
        request = GrentonCluApiClientRegisterRequest(keys, self.subscription_id)
        wire_message = await self.protocol.send_request(request)
        
        if wire_message is None:
            return None
        
        try:
            response = GrentonCluApiClientRegisterResponse(wire_message)
            return response.values
        except ValueError as e:
            _LOGGER.error("[%s] Failed to parse register response: %s", self.clu.id, e)
            return None
    
    async def execute_action(self, action: GrentonAction) -> bool:
        """Execute an action on the CLU."""
        if not self.protocol:
            _LOGGER.warning("[%s] No protocol available for action execution", self.clu.id)
            return False
        
        # Build the payload before the first await: entities reuse and mutate
        # their action objects, so the value must be captured now.
        request = GrentonCluApiActionRequest.from_action(action)
        attempts = 1 + ACTION_RETRIES if request.idempotent else 1
        
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                request = request.with_new_msg_id()
            
            is_last_attempt = attempt == attempts
            _LOGGER.debug("[%s][%s] Action attempt %d/%d: %s",
                          self.clu.name, request.msg_id, attempt, attempts, request.payload)
            
            wire_message = await self.protocol.send_request(
                request,
                timeout_log_level=logging.WARNING if is_last_attempt else logging.DEBUG,
            )
            
            if wire_message is not None:
                if attempt > 1:
                    _LOGGER.info("[%s][%s] Action succeeded on attempt %d/%d: %s",
                                 self.clu.name, request.msg_id, attempt, attempts, request.payload)
                return True
        
        return False


class GrentonCluApiProtocol(asyncio.DatagramProtocol):
    """UDP protocol handler for Grenton CLU API communication."""
    
    def __init__(self, api: GrentonCluApi):
        self.api = api
        self._pending: Dict[str, asyncio.Future[str]] = {}
        self._pending_lock = asyncio.Lock()
        self._response_timeout = RESPONSE_TIMEOUT
        self._request_gap = REQUEST_GAP
        self._max_in_flight = MAX_IN_FLIGHT_REQUESTS
        self._slots = asyncio.Semaphore(MAX_IN_FLIGHT_REQUESTS)
        self._waiting = 0
        self._in_flight = 0
        self.subscription_callback: Optional[Callable[[list[GrentonValue]], Awaitable[None]]] = None
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
        """Process a response message from the CLU."""
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
        
        async def _complete() -> None:
            async with self._pending_lock:
                fut = self._pending.pop(message_id, None)
            
            if fut and not fut.done():
                fut.set_result(wire_message)
            else:
                # Check if this is a notification (subscription update) that needs processing
                if GrentonCluApiMessageParser.is_client_report(wire_message):
                    if self.subscription_callback:
                        try:
                            notification = GrentonCluApiClientReportNotification(wire_message)
                            await self.subscription_callback(notification.values)
                        except ValueError as e:
                            _LOGGER.error("[%s] Failed to parse client report: %s", self.api.clu.name, e)
                    else:
                        _LOGGER.debug("[%s][%s] Received report but no callback registered", self.api.clu.name, message_id)
                else:
                    _LOGGER.debug("[%s][%s] Response received with no pending request", self.api.clu.name, message_id)
        
        asyncio.create_task(_complete())
    
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
        payload = request.raw.encode()
        encrypted = self.api.cipher.encrypt(payload)
        
        if encrypted is None:
            _LOGGER.error("[%s] Failed to encrypt request", self.api.clu.name)
            return None
        
        self._waiting += 1
        _LOGGER.debug("[%s][%s] Queued (waiting: %d, in flight: %d/%d)",
                      self.api.clu.name, msg_id, self._waiting, self._in_flight, self._max_in_flight)
        try:
            await self._slots.acquire()
        finally:
            self._waiting -= 1
        
        self._in_flight += 1
        try:
            return await self._send_and_wait(request, msg_id, encrypted, timeout_log_level)
        finally:
            self._in_flight -= 1
            self._release_slot()
    
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
        
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        
        async with self._pending_lock:
            self._pending[msg_id] = future
        
        try:
            self.api.transport.sendto(encrypted, (self.api.clu.ip, self.api.clu.port))
            _LOGGER.debug("[%s][%s] Sent (waiting: %d, in flight: %d/%d): %s",
                          self.api.clu.name, msg_id, self._waiting, self._in_flight,
                          self._max_in_flight, request.raw)
        except Exception as e:
            async with self._pending_lock:
                self._pending.pop(msg_id, None)
            _LOGGER.error("[%s][%s] Failed to send request: %s", 
                        self.api.clu.name, msg_id, e)
            return None
        
        try:
            return await asyncio.wait_for(future, timeout=self._response_timeout)
        except asyncio.TimeoutError:
            async with self._pending_lock:
                self._pending.pop(msg_id, None)
            _LOGGER.log(timeout_log_level, "[%s][%s] Request timeout", self.api.clu.name, msg_id)
            return None
        except Exception:
            async with self._pending_lock:
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
        
        async def _fail_pending() -> None:
            async with self._pending_lock:
                futures = list(self._pending.values())
                self._pending.clear()
            
            for fut in futures:
                if not fut.done():
                    fut.set_exception(ConnectionError(
                        f"UDP connection closed for CLU {self.api.clu.name}"))
        
        asyncio.create_task(_fail_pending())
