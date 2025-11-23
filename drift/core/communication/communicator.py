"""Protobuf communicator for CLI communication.

This module implements the communication layer between the SDK and CLI
using Protocol Buffers over Unix sockets or TCP.

The communicator supports:
- Async communication for most operations
- Sync communication for blocking operations (mocks, env vars)
- Automatic reconnection on connection loss
- Request tracking with timeout handling
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import struct
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable

from .types import (
    CLIMessageType,
    ConnectRequest,
    ConnectResponse,
    GetMockRequest,
    GetMockResponse,
    EnvVarRequest,
    EnvVarResponse,
    SDKMessageType,
)

logger = logging.getLogger(__name__)

# Default socket path
DEFAULT_SOCKET_PATH = "/tmp/tusk-connect.sock"

# Default timeouts (in seconds)
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_REQUEST_TIMEOUT = 10.0


@dataclass
class CommunicatorConfig:
    """Configuration for ProtobufCommunicator."""

    socket_path: str | None = None
    """Unix socket path. If None, uses TUSK_MOCK_SOCKET env var or default."""

    host: str | None = None
    """TCP host. If set with port, uses TCP instead of Unix socket."""

    port: int | None = None
    """TCP port. If set with host, uses TCP instead of Unix socket."""

    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    """Timeout for initial connection (seconds)."""

    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    """Timeout for individual requests (seconds)."""

    auto_reconnect: bool = True
    """Whether to automatically reconnect on connection loss."""

    @classmethod
    def from_env(cls) -> CommunicatorConfig:
        """Create config from environment variables.

        Environment variables:
        - TUSK_MOCK_SOCKET: Unix socket path
        - TUSK_MOCK_HOST: TCP host
        - TUSK_MOCK_PORT: TCP port
        """
        socket_path = os.environ.get("TUSK_MOCK_SOCKET")
        host = os.environ.get("TUSK_MOCK_HOST")
        port_str = os.environ.get("TUSK_MOCK_PORT")
        port = int(port_str) if port_str else None

        return cls(
            socket_path=socket_path,
            host=host,
            port=port,
        )


class ProtobufCommunicator:
    """Handles communication between SDK and CLI.

    This class manages the socket connection and message framing
    for bidirectional protobuf communication.

    Message format:
    - 4-byte big-endian length prefix
    - Serialized protobuf message

    Usage:
        config = CommunicatorConfig.from_env()
        communicator = ProtobufCommunicator(config)

        # Connect to CLI
        response = await communicator.connect(ConnectRequest(...))

        # Request mock data
        mock = await communicator.request_mock(GetMockRequest(...))
    """

    def __init__(self, config: CommunicatorConfig | None = None) -> None:
        self.config = config or CommunicatorConfig.from_env()
        self._socket: socket.socket | None = None
        self._connected = False
        self._session_id: str | None = None
        self._pending_requests: dict[str, asyncio.Future[Any]] = {}
        self._receive_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def is_connected(self) -> bool:
        """Check if currently connected to CLI."""
        return self._connected and self._socket is not None

    @property
    def session_id(self) -> str | None:
        """Get the current session ID."""
        return self._session_id

    def _get_socket_address(self) -> tuple[str, int] | str:
        """Determine the socket address to use.

        Returns Unix socket path or (host, port) tuple.
        """
        # TCP takes precedence if both host and port are set
        if self.config.host and self.config.port:
            return (self.config.host, self.config.port)

        # Fall back to Unix socket
        socket_path = self.config.socket_path or DEFAULT_SOCKET_PATH
        return socket_path

    async def connect(self, request: ConnectRequest) -> ConnectResponse:
        """Connect to the CLI and perform handshake.

        Args:
            request: Connection request with service info and version

        Returns:
            ConnectResponse from CLI

        Raises:
            ConnectionError: If connection fails
            TimeoutError: If connection times out
        """
        address = self._get_socket_address()

        try:
            # Create appropriate socket type
            if isinstance(address, str):
                # Unix socket
                self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                logger.debug(f"Connecting to Unix socket: {address}")
            else:
                # TCP socket
                self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                logger.debug(f"Connecting to TCP: {address}")

            self._socket.settimeout(self.config.connect_timeout)
            self._socket.connect(address)
            self._socket.setblocking(False)

            # Send connect request
            await self._send_message(SDKMessageType.SDK_CONNECT, request)

            # Wait for response
            response = await self._receive_message(ConnectResponse)

            if response.success:
                self._connected = True
                self._session_id = response.session_id
                logger.info(f"Connected to CLI (session: {self._session_id})")
            else:
                raise ConnectionError(f"CLI rejected connection: {response.error}")

            return response

        except socket.timeout as e:
            self._cleanup()
            raise TimeoutError(f"Connection timed out: {e}") from e
        except socket.error as e:
            self._cleanup()
            raise ConnectionError(f"Socket error: {e}") from e

    async def disconnect(self) -> None:
        """Disconnect from CLI."""
        self._cleanup()
        logger.info("Disconnected from CLI")

    async def request_mock(self, request: GetMockRequest) -> GetMockResponse:
        """Request mocked response data from CLI.

        Args:
            request: Mock request with outbound span info

        Returns:
            GetMockResponse with matched data or not found

        Raises:
            ConnectionError: If not connected
            TimeoutError: If request times out
        """
        if not self.is_connected:
            raise ConnectionError("Not connected to CLI")

        await self._send_message(SDKMessageType.MOCK_REQUEST, request)
        return await self._receive_message(GetMockResponse)

    async def request_env_vars(self, request: EnvVarRequest) -> EnvVarResponse:
        """Request environment variables from CLI.

        Args:
            request: Environment variable request

        Returns:
            EnvVarResponse with variable values

        Raises:
            ConnectionError: If not connected
            TimeoutError: If request times out
        """
        if not self.is_connected:
            raise ConnectionError("Not connected to CLI")

        await self._send_message(SDKMessageType.ENV_VAR_REQUEST, request)
        return await self._receive_message(EnvVarResponse)

    def request_mock_sync(self, request: GetMockRequest) -> GetMockResponse:
        """Synchronous version of request_mock.

        This is a blocking call that should be used when async is not available.
        It creates a temporary event loop to run the async operation.
        """
        try:
            loop = asyncio.get_running_loop()
            # If we're in an async context, we need to use a different approach
            # For now, raise an error - this needs special handling
            raise RuntimeError(
                "request_mock_sync cannot be called from async context. "
                "Use request_mock instead."
            )
        except RuntimeError:
            # No running loop, safe to create one
            return asyncio.run(self.request_mock(request))

    async def _send_message(self, msg_type: SDKMessageType, payload: Any) -> None:
        """Send a message to CLI.

        Message format:
        - 4 bytes: message length (big-endian)
        - N bytes: serialized protobuf message
        """
        if not self._socket:
            raise ConnectionError("Socket not initialized")

        # Serialize the message
        # TODO: Implement actual protobuf serialization
        # For now, use a placeholder that will be replaced
        data = self._serialize_message(msg_type, payload)

        # Add length prefix
        length_prefix = struct.pack(">I", len(data))
        message = length_prefix + data

        # Send asynchronously
        loop = asyncio.get_event_loop()
        await loop.sock_sendall(self._socket, message)

    async def _receive_message(self, response_type: type) -> Any:
        """Receive and parse a message from CLI."""
        if not self._socket:
            raise ConnectionError("Socket not initialized")

        loop = asyncio.get_event_loop()

        # Read length prefix
        length_data = await asyncio.wait_for(
            loop.sock_recv(self._socket, 4),
            timeout=self.config.request_timeout,
        )

        if len(length_data) < 4:
            raise ConnectionError("Connection closed by CLI")

        length = struct.unpack(">I", length_data)[0]

        # Read message data
        data = b""
        while len(data) < length:
            chunk = await asyncio.wait_for(
                loop.sock_recv(self._socket, length - len(data)),
                timeout=self.config.request_timeout,
            )
            if not chunk:
                raise ConnectionError("Connection closed by CLI")
            data += chunk

        # Deserialize the message
        return self._deserialize_message(data, response_type)

    def _serialize_message(self, msg_type: SDKMessageType, payload: Any) -> bytes:
        """Serialize a message to protobuf bytes.

        TODO: Implement actual protobuf serialization using tusk-drift-schemas.
        """
        # Placeholder implementation - will be replaced with actual protobuf
        import json
        from dataclasses import asdict

        message = {
            "type": msg_type.value,
            "payload": asdict(payload) if hasattr(payload, "__dataclass_fields__") else payload,
        }
        return json.dumps(message).encode("utf-8")

    def _deserialize_message(self, data: bytes, response_type: type) -> Any:
        """Deserialize a protobuf message.

        TODO: Implement actual protobuf deserialization using tusk-drift-schemas.
        """
        # Placeholder implementation - will be replaced with actual protobuf
        import json

        message = json.loads(data.decode("utf-8"))
        payload = message.get("payload", {})

        # Create response object from payload
        return response_type(**payload)

    def _cleanup(self) -> None:
        """Clean up resources."""
        self._connected = False
        self._session_id = None

        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None

        if self._receive_task:
            self._receive_task.cancel()
            self._receive_task = None

        self._pending_requests.clear()
