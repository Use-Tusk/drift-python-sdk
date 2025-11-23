"""E2E tests for CLI communication.

These tests validate the communication protocol between the SDK and CLI.
They are designed to guide the implementation of CLI communication features.

Test categories:
1. Connection handshake tests
2. Mock request/response tests
3. Environment variable tests
4. Error handling tests
5. Protocol conformance tests
"""

import asyncio
import os
import socket
import struct
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

# Set up environment
os.environ["TUSK_DRIFT_MODE"] = "REPLAY"

from drift.core.communication import (
    CommunicatorConfig,
    ProtobufCommunicator,
    ConnectRequest,
    ConnectResponse,
    GetMockRequest,
    GetMockResponse,
    SDKMessageType,
    CLIMessageType,
)


class MockCLIServer:
    """Mock CLI server for testing SDK communication.

    This simulates the CLI side of the socket communication
    to test the SDK's ProtobufCommunicator.
    """

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self._server_socket: socket.socket | None = None
        self._client_socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._responses: list[bytes] = []

    def queue_response(self, response: bytes) -> None:
        """Queue a response to send when a request is received."""
        self._responses.append(response)

    def start(self) -> None:
        """Start the mock CLI server."""
        # Remove existing socket file
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

        self._server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server_socket.bind(self.socket_path)
        self._server_socket.listen(1)
        self._server_socket.settimeout(5.0)
        self._running = True

        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        """Serve incoming connections."""
        try:
            self._client_socket, _ = self._server_socket.accept()
            self._client_socket.settimeout(5.0)

            while self._running:
                try:
                    # Read length prefix
                    length_data = self._client_socket.recv(4)
                    if not length_data:
                        break

                    length = struct.unpack(">I", length_data)[0]

                    # Read message
                    data = b""
                    while len(data) < length:
                        chunk = self._client_socket.recv(length - len(data))
                        if not chunk:
                            break
                        data += chunk

                    # Send queued response if available
                    if self._responses:
                        response = self._responses.pop(0)
                        length_prefix = struct.pack(">I", len(response))
                        self._client_socket.sendall(length_prefix + response)

                except socket.timeout:
                    continue
                except Exception:
                    break

        except Exception:
            pass

    def stop(self) -> None:
        """Stop the mock CLI server."""
        self._running = False

        if self._client_socket:
            try:
                self._client_socket.close()
            except Exception:
                pass

        if self._server_socket:
            try:
                self._server_socket.close()
            except Exception:
                pass

        if self._thread:
            self._thread.join(timeout=2.0)

        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)


class TestCommunicatorConfig(unittest.TestCase):
    """Tests for CommunicatorConfig."""

    def test_default_config(self):
        """Default config should have sensible defaults."""
        config = CommunicatorConfig()

        self.assertIsNone(config.socket_path)
        self.assertIsNone(config.host)
        self.assertIsNone(config.port)
        self.assertEqual(config.connect_timeout, 5.0)
        self.assertEqual(config.request_timeout, 10.0)
        self.assertTrue(config.auto_reconnect)

    def test_config_from_env_socket(self):
        """Config should read Unix socket from environment."""
        with patch.dict(os.environ, {"TUSK_MOCK_SOCKET": "/tmp/test.sock"}):
            config = CommunicatorConfig.from_env()
            self.assertEqual(config.socket_path, "/tmp/test.sock")

    def test_config_from_env_tcp(self):
        """Config should read TCP host/port from environment."""
        with patch.dict(os.environ, {
            "TUSK_MOCK_HOST": "localhost",
            "TUSK_MOCK_PORT": "9000",
        }):
            config = CommunicatorConfig.from_env()
            self.assertEqual(config.host, "localhost")
            self.assertEqual(config.port, 9000)


class TestConnectionHandshake(unittest.TestCase):
    """Tests for SDK-CLI connection handshake."""

    def setUp(self):
        """Set up test fixtures."""
        self.socket_path = tempfile.mktemp(suffix=".sock")
        self.mock_server = MockCLIServer(self.socket_path)

    def tearDown(self):
        """Clean up test fixtures."""
        self.mock_server.stop()

    def test_connect_request_structure(self):
        """ConnectRequest should have required fields."""
        request = ConnectRequest(
            service_id="test-service",
            sdk_version="0.1.0",
            min_cli_version="0.1.0",
            sdk_language="python",
        )

        self.assertEqual(request.service_id, "test-service")
        self.assertEqual(request.sdk_version, "0.1.0")
        self.assertEqual(request.min_cli_version, "0.1.0")
        self.assertEqual(request.sdk_language, "python")

    def test_connect_response_success(self):
        """ConnectResponse should handle success case."""
        response = ConnectResponse(
            success=True,
            cli_version="0.2.0",
            session_id="session-123",
        )

        self.assertTrue(response.success)
        self.assertEqual(response.cli_version, "0.2.0")
        self.assertEqual(response.session_id, "session-123")
        self.assertIsNone(response.error)

    def test_connect_response_failure(self):
        """ConnectResponse should handle failure case."""
        response = ConnectResponse(
            success=False,
            error="Version mismatch",
        )

        self.assertFalse(response.success)
        self.assertEqual(response.error, "Version mismatch")

    def test_communicator_initial_state(self):
        """Communicator should start disconnected."""
        config = CommunicatorConfig(socket_path=self.socket_path)
        communicator = ProtobufCommunicator(config)

        self.assertFalse(communicator.is_connected)
        self.assertIsNone(communicator.session_id)

    @unittest.skip("Requires mock server implementation")
    def test_successful_connection(self):
        """SDK should successfully connect to CLI."""
        # Start mock server
        self.mock_server.start()

        # Queue successful response
        import json
        response = json.dumps({
            "type": CLIMessageType.CONNECT_RESPONSE.value,
            "payload": {
                "success": True,
                "cli_version": "0.2.0",
                "session_id": "session-123",
            },
        }).encode()
        self.mock_server.queue_response(response)

        # Connect
        config = CommunicatorConfig(socket_path=self.socket_path)
        communicator = ProtobufCommunicator(config)

        request = ConnectRequest(
            service_id="test-service",
            sdk_version="0.1.0",
            min_cli_version="0.1.0",
        )

        response = asyncio.run(communicator.connect(request))

        self.assertTrue(response.success)
        self.assertTrue(communicator.is_connected)
        self.assertEqual(communicator.session_id, "session-123")

    def test_connection_timeout(self):
        """SDK should timeout if CLI doesn't respond."""
        # Don't start server - connection should fail
        config = CommunicatorConfig(
            socket_path=self.socket_path,
            connect_timeout=0.5,
        )
        communicator = ProtobufCommunicator(config)

        request = ConnectRequest(
            service_id="test-service",
            sdk_version="0.1.0",
            min_cli_version="0.1.0",
        )

        with self.assertRaises((ConnectionError, TimeoutError, FileNotFoundError)):
            asyncio.run(communicator.connect(request))


class TestMockRequests(unittest.TestCase):
    """Tests for mock request/response flow."""

    def test_mock_request_structure(self):
        """GetMockRequest should have required fields."""
        request = GetMockRequest(
            request_id="req-123",
            test_id="test-456",
            outbound_span={
                "name": "HTTP GET",
                "input_value": {"method": "GET", "url": "https://api.example.com"},
            },
            operation="http",
            tags={"env": "test"},
        )

        self.assertEqual(request.request_id, "req-123")
        self.assertEqual(request.test_id, "test-456")
        self.assertEqual(request.outbound_span["name"], "HTTP GET")
        self.assertEqual(request.operation, "http")
        self.assertEqual(request.tags["env"], "test")

    def test_mock_response_found(self):
        """GetMockResponse should handle found case."""
        response = GetMockResponse(
            request_id="req-123",
            found=True,
            response_data={"status": 200, "body": {"message": "OK"}},
            matched_span_id="span-789",
        )

        self.assertTrue(response.found)
        self.assertEqual(response.response_data["status"], 200)
        self.assertEqual(response.matched_span_id, "span-789")
        self.assertIsNone(response.error)

    def test_mock_response_not_found(self):
        """GetMockResponse should handle not found case."""
        response = GetMockResponse(
            request_id="req-123",
            found=False,
            error="No matching span found",
        )

        self.assertFalse(response.found)
        self.assertIsNone(response.response_data)
        self.assertEqual(response.error, "No matching span found")

    def test_request_mock_requires_connection(self):
        """request_mock should fail if not connected."""
        config = CommunicatorConfig()
        communicator = ProtobufCommunicator(config)

        request = GetMockRequest(
            request_id="req-123",
            test_id="test-456",
            outbound_span={},
        )

        with self.assertRaises(ConnectionError):
            asyncio.run(communicator.request_mock(request))


class TestMessageFraming(unittest.TestCase):
    """Tests for message framing protocol."""

    def test_length_prefix_format(self):
        """Length prefix should be 4-byte big-endian."""
        # Test encoding
        length = 256
        prefix = struct.pack(">I", length)
        self.assertEqual(len(prefix), 4)
        self.assertEqual(prefix, b"\x00\x00\x01\x00")

        # Test decoding
        decoded = struct.unpack(">I", prefix)[0]
        self.assertEqual(decoded, 256)

    def test_large_message_length(self):
        """Should handle large message lengths."""
        # Max uint32
        length = 2**32 - 1
        prefix = struct.pack(">I", length)
        decoded = struct.unpack(">I", prefix)[0]
        self.assertEqual(decoded, length)


class TestProtocolConformance(unittest.TestCase):
    """Tests for protocol conformance with Node.js SDK."""

    def test_sdk_message_types_match_nodejs(self):
        """SDK message types should match Node.js SDK."""
        # These values must match the protobuf schema
        self.assertEqual(SDKMessageType.UNSPECIFIED.value, 0)
        self.assertEqual(SDKMessageType.SDK_CONNECT.value, 1)
        self.assertEqual(SDKMessageType.MOCK_REQUEST.value, 2)
        self.assertEqual(SDKMessageType.ENV_VAR_REQUEST.value, 3)
        self.assertEqual(SDKMessageType.INBOUND_SPAN.value, 4)
        self.assertEqual(SDKMessageType.ALERT.value, 5)

    def test_cli_message_types_match_nodejs(self):
        """CLI message types should match Node.js SDK."""
        self.assertEqual(CLIMessageType.UNSPECIFIED.value, 0)
        self.assertEqual(CLIMessageType.CONNECT_RESPONSE.value, 1)
        self.assertEqual(CLIMessageType.MOCK_RESPONSE.value, 2)
        self.assertEqual(CLIMessageType.ENV_VAR_RESPONSE.value, 3)
        self.assertEqual(CLIMessageType.INBOUND_SPAN_RESPONSE.value, 4)


class TestErrorHandling(unittest.TestCase):
    """Tests for error handling in CLI communication."""

    def test_connection_error_cleanup(self):
        """Communicator should clean up on connection error."""
        config = CommunicatorConfig(socket_path="/nonexistent/path.sock")
        communicator = ProtobufCommunicator(config)

        request = ConnectRequest(
            service_id="test",
            sdk_version="0.1.0",
            min_cli_version="0.1.0",
        )

        try:
            asyncio.run(communicator.connect(request))
        except (ConnectionError, FileNotFoundError):
            pass

        self.assertFalse(communicator.is_connected)
        self.assertIsNone(communicator.session_id)

    def test_disconnect_is_idempotent(self):
        """Calling disconnect multiple times should be safe."""
        config = CommunicatorConfig()
        communicator = ProtobufCommunicator(config)

        # Should not raise
        asyncio.run(communicator.disconnect())
        asyncio.run(communicator.disconnect())


if __name__ == "__main__":
    unittest.main()
