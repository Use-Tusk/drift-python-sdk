from __future__ import annotations

import logging
import os
import secrets
import socket
import struct
import threading
import traceback
from dataclasses import dataclass
from typing import Any

from tusk.drift.core.v1 import GetMockRequest as ProtoGetMockRequest

from ...version import MIN_CLI_VERSION, SDK_VERSION
from ..types import CleanSpanData
from .types import (
    CliMessage,
    ConnectRequest,
    EnvVarRequest,
    GetMockRequest,
    InstrumentationVersionMismatchAlert,
    MessageType,
    MockRequestInput,
    MockResponseOutput,
    SdkMessage,
    SendAlertRequest,
    SendInboundSpanForReplayRequest,
    UnpatchedDependencyAlert,
    span_to_proto,
)

logger = logging.getLogger(__name__)

# Default socket path
DEFAULT_SOCKET_PATH = "/tmp/tusk-connect.sock"

# Default timeouts (in seconds)
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_REQUEST_TIMEOUT = 10.0
SYNC_REQUEST_TIMEOUT = 10.0


@dataclass
class CommunicatorConfig:
    """Configuration for ProtobufCommunicator."""

    socket_path: str | None = None
    host: str | None = None
    port: int | None = None
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    auto_reconnect: bool = True

    @classmethod
    def from_env(cls) -> CommunicatorConfig:
        """Create config from environment variables."""
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
    """Handles protobuf communication between SDK and CLI."""

    def __init__(self, config: CommunicatorConfig | None = None) -> None:
        self.config = config or CommunicatorConfig.from_env()
        self._socket: socket.socket | None = None
        self._connected = False
        self._session_id: str | None = None
        self._incoming_buffer = bytearray()
        self._pending_requests: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

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

    def _generate_request_id(self) -> str:
        """Generate a unique request ID."""
        return secrets.token_hex(6)

    def _get_stack_trace(self) -> str:
        """Get the current stack trace, excluding internal frames."""
        lines = traceback.format_stack()
        # Filter out internal frames
        filtered = [
            line
            for line in lines
            if "ProtobufCommunicator" not in line and "communicator.py" not in line
        ]
        return "".join(filtered[-20:])  # Limit to last 20 frames

    # ========== Connection Methods ==========

    async def connect(
        self,
        connection_info: dict[str, Any] | None = None,
        service_id: str = "",
    ) -> None:
        """Connect to the CLI and perform handshake.

        Args:
            connection_info: Dict with 'socketPath' or 'host'/'port'
            service_id: Service identifier for the connection

        Raises:
            ConnectionError: If connection fails
            TimeoutError: If connection times out
        """
        # Determine address
        if connection_info:
            if "socketPath" in connection_info:
                address: tuple[str, int] | str = connection_info["socketPath"]
            else:
                address = (connection_info["host"], connection_info["port"])
        else:
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

            conn_type = "Unix socket" if isinstance(address, str) else "TCP"
            logger.debug(f"Connected to CLI via protobuf ({conn_type})")

            # Send connect message
            await self._send_connect_message(service_id)

            self._connected = True

        except socket.timeout as e:
            self._cleanup()
            raise TimeoutError(f"Connection timed out: {e}") from e
        except (socket.error, OSError) as e:
            self._cleanup()
            raise ConnectionError(f"Socket error: {e}") from e

    async def _send_connect_message(self, service_id: str) -> None:
        """Send the initial connection message to CLI and wait for acknowledgement."""
        connect_request = ConnectRequest(
            service_id=service_id,
            sdk_version=SDK_VERSION,
            min_cli_version=MIN_CLI_VERSION,
        )

        request_id = self._generate_request_id()
        sdk_message = SdkMessage(
            type=MessageType.SDK_CONNECT,
            request_id=request_id,
            connect_request=connect_request.to_proto(),
        )

        await self._send_protobuf_message(sdk_message)

        # Wait for connect response from CLI
        await self._receive_connect_response(request_id)

    async def _receive_connect_response(self, request_id: str) -> None:
        """Wait for and handle the connect response from CLI."""
        if not self._socket:
            raise ConnectionError("Socket not initialized")

        self._socket.settimeout(self.config.connect_timeout)

        try:
            # Read length prefix
            length_data = self._recv_exact(4)
            if not length_data:
                raise ConnectionError("Connection closed by CLI")

            length = struct.unpack(">I", length_data)[0]

            # Read message data
            message_data = self._recv_exact(length)
            if not message_data:
                raise ConnectionError("Connection closed by CLI")

            cli_message = CliMessage().parse(message_data)

            logger.debug(
                f"Received connect response: type={cli_message.type}, requestId={cli_message.request_id}"
            )

            if cli_message.connect_response:
                response = cli_message.connect_response
                if response.success:
                    logger.debug("CLI acknowledged connection successfully")
                else:
                    error_msg = response.error or "Unknown error"
                    raise ConnectionError(f"CLI rejected connection: {error_msg}")
            else:
                raise ConnectionError(
                    f"Expected connect response but got message type: {cli_message.type}"
                )

        except socket.timeout as e:
            raise TimeoutError(f"Timeout waiting for connect response: {e}") from e

    def connect_sync(
        self,
        connection_info: dict[str, Any] | None = None,
        service_id: str = "",
    ) -> None:
        """Connect to the CLI synchronously and perform handshake.

        This is a synchronous version of connect() that doesn't use async/await.
        The socket will remain open after this method returns.

        Args:
            connection_info: Dict with 'socketPath' or 'host'/'port'
            service_id: Service identifier for the connection

        Raises:
            ConnectionError: If connection fails
            TimeoutError: If connection times out
        """
        logger.info("[CONNECT_SYNC] Starting synchronous connection")
        # Determine address
        if connection_info:
            if "socketPath" in connection_info:
                address: tuple[str, int] | str = connection_info["socketPath"]
            else:
                address = (connection_info["host"], connection_info["port"])
        else:
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

            conn_type = "Unix socket" if isinstance(address, str) else "TCP"
            logger.debug(f"Connected to CLI via protobuf ({conn_type})")

            # Send connect message synchronously
            connect_request = ConnectRequest(
                service_id=service_id,
                sdk_version=SDK_VERSION,
                min_cli_version=MIN_CLI_VERSION,
            )

            request_id = self._generate_request_id()
            sdk_message = SdkMessage(
                type=MessageType.SDK_CONNECT,
                request_id=request_id,
                connect_request=connect_request.to_proto(),
            )

            # Send message (synchronous socket operation)
            message_bytes = bytes(sdk_message)
            length_prefix = struct.pack(">I", len(message_bytes))
            self._socket.sendall(length_prefix + message_bytes)

            # Receive connect response synchronously
            # Read length prefix
            length_data = self._recv_exact(4)
            if not length_data:
                raise ConnectionError("Connection closed by CLI")

            length = struct.unpack(">I", length_data)[0]

            # Read message data
            message_data = self._recv_exact(length)
            if not message_data:
                raise ConnectionError("Connection closed by CLI")

            cli_message = CliMessage().parse(message_data)

            logger.debug(
                f"Received connect response: type={cli_message.type}, requestId={cli_message.request_id}"
            )

            if cli_message.connect_response:
                response = cli_message.connect_response
                if response.success:
                    logger.debug("CLI acknowledged connection successfully")
                    self._connected = True
                    logger.info(
                        f"[CONNECT_SYNC] Connection successful! Socket is: {self._socket}"
                    )
                    logger.info(
                        f"[CONNECT_SYNC] _connected={self._connected}, is_connected={self.is_connected}"
                    )
                else:
                    error_msg = response.error or "Unknown error"
                    raise ConnectionError(f"CLI rejected connection: {error_msg}")
            else:
                raise ConnectionError(
                    f"Expected connect response but got message type: {cli_message.type}"
                )

        except socket.timeout as e:
            self._cleanup()
            raise TimeoutError(f"Connection timed out: {e}") from e
        except (socket.error, OSError) as e:
            self._cleanup()
            raise ConnectionError(f"Socket error: {e}") from e

        logger.info(
            f"[CONNECT_SYNC] Exiting connect_sync(). Socket still open: {self._socket is not None}"
        )

    async def disconnect(self) -> None:
        """Disconnect from CLI."""
        self._cleanup()
        logger.debug("Disconnected from CLI")

    async def request_mock_async(
        self, mock_request: MockRequestInput
    ) -> MockResponseOutput:
        """Request mocked response data from CLI (async).

        Args:
            mock_request: Mock request with test_id and outbound_span

        Returns:
            MockResponseOutput with found status and response data

        Raises:
            ConnectionError: If not connected
            TimeoutError: If request times out
        """
        if not self.is_connected:
            raise ConnectionError("Not connected to CLI")

        request_id = self._generate_request_id()

        # Clean and convert span to protobuf
        clean_span = self._clean_span(mock_request.outbound_span)
        proto_span = span_to_proto(clean_span) if clean_span else None

        # Create protobuf mock request
        proto_mock_request = GetMockRequest(
            request_id=request_id,
            test_id=mock_request.test_id,
            outbound_span={"name": "placeholder"},  # Will be set via proto
            tags={},
            stack_trace=getattr(clean_span, "stack_trace", "") if clean_span else "",
        ).to_proto()

        # Override with actual proto span
        if proto_span:
            proto_mock_request.outbound_span = proto_span

        sdk_message = SdkMessage(
            type=MessageType.MOCK_REQUEST,
            request_id=request_id,
            get_mock_request=proto_mock_request,
        )

        logger.debug(
            f"[ProtobufCommunicator] Creating mock request with requestId: {request_id}, "
            f"testId: {mock_request.test_id}"
        )

        # Send and wait for response
        await self._send_protobuf_message(sdk_message)
        response = await self._receive_response(request_id)

        return response

    def request_mock_sync(self, mock_request: MockRequestInput) -> MockResponseOutput:
        """Request mocked response data from CLI (synchronous).

        This blocks the current thread. Use for instrumentations that
        require synchronous mock fetching.

        Args:
            mock_request: Mock request with test_id and outbound_span

        Returns:
            MockResponseOutput with found status and response data
        """
        request_id = self._generate_request_id()

        # Convert span to protobuf
        proto_span = mock_request.outbound_span.to_proto()

        # Create protobuf SDK message directly
        proto_mock_request = ProtoGetMockRequest(
            request_id=request_id,
            test_id=mock_request.test_id,
            outbound_span=proto_span,
            tags={},
            stack_trace=mock_request.outbound_span.stack_trace or "",
        )

        sdk_message = SdkMessage(
            type=MessageType.MOCK_REQUEST,
            request_id=request_id,
            get_mock_request=proto_mock_request,
        )

        logger.debug(
            f"Sending protobuf request to CLI (sync), testId: {mock_request.test_id}"
        )

        return self._execute_sync_request(sdk_message, self._handle_mock_response)

    def request_env_vars_sync(self, trace_test_server_span_id: str) -> dict[str, str]:
        """Request environment variables from CLI (synchronous).

        Args:
            trace_test_server_span_id: Span ID for trace context

        Returns:
            Dictionary of environment variable names to values
        """
        request_id = self._generate_request_id()

        env_var_request = EnvVarRequest(
            request_id=request_id,
            trace_test_server_span_id=trace_test_server_span_id,
        )

        sdk_message = SdkMessage(
            type=MessageType.ENV_VAR_REQUEST,
            request_id=request_id,
            env_var_request=env_var_request.to_proto(),
        )

        logger.debug(
            f"[ProtobufCommunicator] Requesting env vars (sync) for trace: {trace_test_server_span_id}"
        )

        return self._execute_sync_request(sdk_message, self._handle_env_var_response)

    async def send_inbound_span_for_replay(self, span: CleanSpanData) -> None:
        """Send an inbound span to CLI for replay validation.

        Args:
            span: The inbound span data to send
        """
        if not self._socket:
            return

        proto_span = span_to_proto(span)

        request = SendInboundSpanForReplayRequest(span=proto_span)

        sdk_message = SdkMessage(
            type=MessageType.INBOUND_SPAN,
            request_id=self._generate_request_id(),
            send_inbound_span_for_replay_request=request,
        )

        await self._send_protobuf_message(sdk_message)

    async def send_instrumentation_version_mismatch_alert(
        self,
        module_name: str,
        requested_version: str | None,
        supported_versions: list[str],
    ) -> None:
        """Send instrumentation version mismatch alert to CLI."""
        if not self._socket:
            logger.debug("[ProtobufCommunicator] Not connected to CLI, skipping alert")
            return

        alert = SendAlertRequest(
            version_mismatch=InstrumentationVersionMismatchAlert(
                module_name=module_name,
                requested_version=requested_version or "",
                supported_versions=supported_versions,
                sdk_version=SDK_VERSION,
            ),
        )

        sdk_message = SdkMessage(
            type=MessageType.ALERT,
            request_id=self._generate_request_id(),
            send_alert_request=alert,
        )

        # Fire-and-forget
        try:
            await self._send_protobuf_message(sdk_message)
            logger.debug("[ProtobufCommunicator] Alert sent to CLI")
        except Exception as e:
            logger.debug(f"[ProtobufCommunicator] Failed to send alert to CLI: {e}")

    async def send_unpatched_dependency_alert(
        self,
        stack_trace: str,
        trace_test_server_span_id: str,
    ) -> None:
        """Send unpatched dependency alert to CLI."""
        if not self._socket:
            return

        alert = SendAlertRequest(
            unpatched_dependency=UnpatchedDependencyAlert(
                stack_trace=stack_trace,
                trace_test_server_span_id=trace_test_server_span_id,
                sdk_version=SDK_VERSION,
            ),
        )

        sdk_message = SdkMessage(
            type=MessageType.ALERT,
            request_id=self._generate_request_id(),
            send_alert_request=alert,
        )

        try:
            await self._send_protobuf_message(sdk_message)
        except Exception:
            pass  # Alerts are non-critical

    async def _send_protobuf_message(self, message: SdkMessage) -> None:
        """Send a protobuf message to CLI."""
        if not self._socket:
            raise ConnectionError("Not connected to CLI")

        # Serialize to bytes using betterproto
        message_bytes = bytes(message)

        # Create length prefix (4 bytes, big-endian)
        length_prefix = struct.pack(">I", len(message_bytes))

        # Send length prefix + message
        full_message = length_prefix + message_bytes

        # Send synchronously (socket is blocking for sends)
        self._socket.sendall(full_message)

    async def _receive_response(self, request_id: str) -> MockResponseOutput:
        """Receive and parse a response for a specific request ID."""
        if not self._socket:
            raise ConnectionError("Socket not initialized")

        self._socket.settimeout(self.config.request_timeout)

        try:
            while True:
                # Read length prefix
                length_data = self._recv_exact(4)
                if not length_data:
                    raise ConnectionError("Connection closed by CLI")

                length = struct.unpack(">I", length_data)[0]

                # Read message data
                message_data = self._recv_exact(length)
                if not message_data:
                    raise ConnectionError("Connection closed by CLI")

                cli_message = CliMessage().parse(message_data)

                logger.debug(
                    f"Received CLI message type: {cli_message.type}, requestId: {cli_message.request_id}"
                )

                if cli_message.request_id == request_id:
                    return self._handle_cli_message(cli_message)

                if cli_message.connect_response:
                    response = cli_message.connect_response
                    if response.success:
                        logger.debug("CLI acknowledged connection")
                        self._session_id = response.session_id
                    else:
                        logger.error(f"CLI rejected connection: {response.error}")
                    continue

        except socket.timeout as e:
            raise TimeoutError(f"Request timed out: {e}") from e

    def _recv_exact(self, n: int) -> bytes | None:
        """Receive exactly n bytes from socket."""
        data = bytearray()
        while len(data) < n:
            chunk = self._socket.recv(n - len(data))
            if not chunk:
                return None
            data.extend(chunk)
        return bytes(data)

    def _execute_sync_request(
        self,
        sdk_message: SdkMessage,
        response_handler: Any,
    ) -> Any:
        """Execute a synchronous request using a dedicated connection.

        Creates a new socket for each sync request to avoid message buffering
        issues with the async connection.
        """
        # Always create a new connection for sync requests
        # This avoids reading buffered messages from the async connection
        address = self._get_socket_address()

        if isinstance(address, str):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        sock.settimeout(SYNC_REQUEST_TIMEOUT)
        sock.connect(address)

        try:
            # Serialize and send
            message_bytes = bytes(sdk_message)
            length_prefix = struct.pack(">I", len(message_bytes))
            sock.sendall(length_prefix + message_bytes)

            # Receive response
            sock.settimeout(SYNC_REQUEST_TIMEOUT)

            # Read length prefix
            length_data = b""
            while len(length_data) < 4:
                chunk = sock.recv(4 - len(length_data))
                if not chunk:
                    raise ConnectionError("Connection closed")
                length_data += chunk

            length = struct.unpack(">I", length_data)[0]

            # Read message
            message_data = b""
            while len(message_data) < length:
                chunk = sock.recv(length - len(message_data))
                if not chunk:
                    raise ConnectionError("Connection closed")
                message_data += chunk

            # Parse and handle
            cli_message = CliMessage().parse(message_data)
            return response_handler(cli_message)

        finally:
            # Always close the sync socket
            sock.close()

    def _handle_cli_message(self, message: CliMessage) -> MockResponseOutput:
        """Handle a CLI message and extract mock response."""
        if message.get_mock_response:
            return self._handle_mock_response(message)

        if message.connect_response:
            response = message.connect_response
            if response.success:
                self._session_id = response.session_id
            return MockResponseOutput(found=False, error="Unexpected connect response")

        return MockResponseOutput(found=False, error="Unknown message type")

    def _handle_mock_response(self, cli_message: CliMessage) -> MockResponseOutput:
        """Extract MockResponseOutput from CLI message."""
        mock_response = cli_message.get_mock_response
        if not mock_response:
            raise ValueError("No mock response in CLI message")

        if mock_response.found:
            response_data = self._extract_response_data(mock_response.response_data)
            return MockResponseOutput(
                found=True,
                response=response_data,
            )
        else:
            return MockResponseOutput(
                found=False,
                error=mock_response.error or "Mock not found",
            )

    def _handle_env_var_response(self, cli_message: CliMessage) -> dict[str, str]:
        """Extract environment variables from CLI message."""
        env_response = cli_message.env_var_response
        if not env_response:
            raise ValueError("No env var response in CLI message")

        return dict(env_response.env_vars) if env_response.env_vars else {}

    def _extract_response_data(self, struct: Any) -> dict[str, Any]:
        """Extract response data from protobuf Struct."""
        if not struct:
            return {}

        try:

            def value_to_python(value):
                """Convert protobuf Value to Python type."""
                if hasattr(value, "null_value"):
                    return None
                elif hasattr(value, "number_value"):
                    return value.number_value
                elif hasattr(value, "string_value"):
                    return value.string_value
                elif hasattr(value, "bool_value"):
                    return value.bool_value
                elif hasattr(value, "struct_value") and value.struct_value:
                    return struct_to_dict(value.struct_value)
                elif hasattr(value, "list_value") and value.list_value:
                    return [value_to_python(v) for v in value.list_value.values]
                return None

            def struct_to_dict(s):
                """Convert protobuf Struct to Python dict."""
                if not hasattr(s, "fields"):
                    return {}
                result = {}
                for key, value in s.fields.items():
                    result[key] = value_to_python(value)
                return result

            data = struct_to_dict(struct)

            if "response" in data:
                mock_interaction = data["response"]

                if (
                    isinstance(mock_interaction, dict)
                    and "response" in mock_interaction
                ):
                    response_obj = mock_interaction["response"]
                    if isinstance(response_obj, dict) and "body" in response_obj:
                        return response_obj["body"] or {}
                    elif isinstance(response_obj, dict):
                        return response_obj

                if (
                    isinstance(mock_interaction, dict)
                    and "Response" in mock_interaction
                ):
                    response_obj = mock_interaction["Response"]
                    if isinstance(response_obj, dict) and "Body" in response_obj:
                        return response_obj["Body"] or {}

                return mock_interaction

            return data

        except Exception as e:
            logger.error(f"Failed to extract response data: {e}")
            return {}

    def _clean_span(self, data: Any) -> Any:
        """Clean span data by removing None/undefined values."""
        if data is None:
            return None

        if isinstance(data, list):
            return [self._clean_span(item) for item in data if item is not None]

        if isinstance(data, dict):
            return {
                key: self._clean_span(value)
                for key, value in data.items()
                if value is not None
            }

        if hasattr(data, "__dict__"):
            # Handle dataclass/object
            return {
                key: self._clean_span(value)
                for key, value in data.__dict__.items()
                if value is not None
            }

        return data

    def _cleanup(self) -> None:
        """Clean up resources."""
        import traceback

        logger.warning("[CLEANUP] _cleanup() called! Stack trace:")
        logger.warning("".join(traceback.format_stack()))

        self._connected = False
        self._session_id = None
        self._incoming_buffer.clear()

        if self._socket:
            try:
                logger.warning("[CLEANUP] Closing socket")
                self._socket.close()
            except Exception:
                pass
            self._socket = None

        self._pending_requests.clear()
