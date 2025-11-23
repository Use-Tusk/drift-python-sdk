"""E2E tests for mock request/response flow.

These tests validate the full mock request flow:
1. SDK detects REPLAY mode
2. SDK connects to CLI via socket
3. On outbound request, SDK requests mock from CLI
4. CLI returns matching mock response
5. SDK returns mock response to caller instead of making real request

These tests guide the implementation of the mock system.
"""

import asyncio
import json
import os
import socket
import struct
import tempfile
import threading
import time
import unittest
from dataclasses import asdict
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
    """Mock CLI server that simulates the tusk CLI.

    This server implements the CLI side of the communication protocol
    to test the SDK's ability to:
    1. Connect and perform handshake
    2. Request mocks for outbound spans
    3. Handle various response scenarios
    """

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self._server_socket: socket.socket | None = None
        self._client_socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._mock_database: dict[str, dict] = {}
        self._session_id = "test-session-123"
        self._received_messages: list[dict] = []

    def add_mock(self, operation: str, input_pattern: dict, response: dict) -> None:
        """Add a mock response to the database.

        Args:
            operation: The operation type (e.g., "http", "database")
            input_pattern: Pattern to match against incoming span input
            response: The mock response to return
        """
        key = json.dumps({"operation": operation, "input": input_pattern}, sort_keys=True)
        self._mock_database[key] = response

    def find_mock(self, operation: str, span_input: dict) -> dict | None:
        """Find a matching mock response.

        This implements simple pattern matching - in a real implementation,
        the CLI would use more sophisticated matching.
        """
        for key, response in self._mock_database.items():
            pattern = json.loads(key)
            if pattern["operation"] == operation:
                # Simple subset matching
                if self._matches_pattern(pattern["input"], span_input):
                    return response
        return None

    def _matches_pattern(self, pattern: dict, actual: dict) -> bool:
        """Check if actual data matches the pattern."""
        for key, value in pattern.items():
            if key not in actual:
                return False
            if isinstance(value, dict) and isinstance(actual[key], dict):
                if not self._matches_pattern(value, actual[key]):
                    return False
            elif actual[key] != value:
                return False
        return True

    def start(self) -> None:
        """Start the mock CLI server."""
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

        self._server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server_socket.bind(self.socket_path)
        self._server_socket.listen(1)
        self._server_socket.settimeout(10.0)
        self._running = True

        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        """Main server loop."""
        try:
            self._client_socket, _ = self._server_socket.accept()
            self._client_socket.settimeout(5.0)

            while self._running:
                try:
                    # Read length prefix
                    length_data = self._recv_exact(4)
                    if not length_data:
                        break

                    length = struct.unpack(">I", length_data)[0]

                    # Read message
                    data = self._recv_exact(length)
                    if not data:
                        break

                    # Parse and handle message
                    message = json.loads(data.decode("utf-8"))
                    self._received_messages.append(message)
                    response = self._handle_message(message)

                    if response:
                        # Send response
                        response_data = json.dumps(response).encode("utf-8")
                        length_prefix = struct.pack(">I", len(response_data))
                        self._client_socket.sendall(length_prefix + response_data)

                except socket.timeout:
                    continue
                except Exception as e:
                    print(f"Server error: {e}")
                    break

        except socket.timeout:
            pass
        except Exception as e:
            print(f"Accept error: {e}")

    def _recv_exact(self, n: int) -> bytes | None:
        """Receive exactly n bytes."""
        data = b""
        while len(data) < n:
            chunk = self._client_socket.recv(n - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    def _handle_message(self, message: dict) -> dict | None:
        """Handle an incoming message and return response."""
        msg_type = message.get("type")
        payload = message.get("payload", {})

        if msg_type == SDKMessageType.SDK_CONNECT.value:
            return self._handle_connect(payload)
        elif msg_type == SDKMessageType.MOCK_REQUEST.value:
            return self._handle_mock_request(payload)
        elif msg_type == SDKMessageType.ENV_VAR_REQUEST.value:
            return self._handle_env_var_request(payload)

        return None

    def _handle_connect(self, payload: dict) -> dict:
        """Handle SDK_CONNECT message."""
        return {
            "type": CLIMessageType.CONNECT_RESPONSE.value,
            "payload": {
                "success": True,
                "cli_version": "0.1.0",
                "session_id": self._session_id,
            },
        }

    def _handle_mock_request(self, payload: dict) -> dict:
        """Handle MOCK_REQUEST message."""
        operation = payload.get("operation", "")
        outbound_span = payload.get("outbound_span", {})
        span_input = outbound_span.get("input_value", {})

        mock = self.find_mock(operation, span_input)

        if mock:
            return {
                "type": CLIMessageType.MOCK_RESPONSE.value,
                "payload": {
                    "request_id": payload.get("request_id", ""),
                    "found": True,
                    "response_data": mock,
                    "matched_span_id": "mock-span-123",
                },
            }
        else:
            return {
                "type": CLIMessageType.MOCK_RESPONSE.value,
                "payload": {
                    "request_id": payload.get("request_id", ""),
                    "found": False,
                    "error": "No matching mock found",
                },
            }

    def _handle_env_var_request(self, payload: dict) -> dict:
        """Handle ENV_VAR_REQUEST message."""
        names = payload.get("names", [])
        values = {name: os.environ.get(name) for name in names}
        return {
            "type": CLIMessageType.ENV_VAR_RESPONSE.value,
            "payload": {
                "request_id": payload.get("request_id", ""),
                "values": values,
            },
        }

    def stop(self) -> None:
        """Stop the server."""
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

    @property
    def received_messages(self) -> list[dict]:
        """Get all received messages."""
        return self._received_messages


class TestMockFlowIntegration(unittest.TestCase):
    """Integration tests for the full mock request flow."""

    def setUp(self):
        """Set up test fixtures."""
        self.socket_path = tempfile.mktemp(suffix=".sock")
        self.mock_server = MockCLIServer(self.socket_path)

    def tearDown(self):
        """Clean up test fixtures."""
        self.mock_server.stop()

    def test_connect_and_request_mock(self):
        """Test connecting and requesting a mock."""
        # Set up mock response
        self.mock_server.add_mock(
            operation="http",
            input_pattern={"method": "GET", "url": "https://api.example.com/users"},
            response={"status": 200, "body": {"users": [{"id": 1, "name": "Test"}]}},
        )

        # Start server
        self.mock_server.start()
        time.sleep(0.1)  # Give server time to start

        # Connect SDK
        config = CommunicatorConfig(socket_path=self.socket_path)
        communicator = ProtobufCommunicator(config)

        connect_request = ConnectRequest(
            service_id="test-service",
            sdk_version="0.1.0",
            min_cli_version="0.1.0",
            sdk_language="python",
        )

        response = asyncio.run(communicator.connect(connect_request))
        self.assertTrue(response.success)
        self.assertEqual(communicator.session_id, "test-session-123")

        # Request mock
        mock_request = GetMockRequest(
            request_id="req-001",
            test_id="test-001",
            outbound_span={
                "name": "HTTP GET",
                "input_value": {"method": "GET", "url": "https://api.example.com/users"},
            },
            operation="http",
        )

        mock_response = asyncio.run(communicator.request_mock(mock_request))
        self.assertTrue(mock_response.found)
        self.assertEqual(mock_response.response_data["status"], 200)
        self.assertEqual(mock_response.response_data["body"]["users"][0]["name"], "Test")

        # Cleanup
        asyncio.run(communicator.disconnect())

    def test_mock_not_found(self):
        """Test requesting a mock that doesn't exist."""
        self.mock_server.start()
        time.sleep(0.1)

        config = CommunicatorConfig(socket_path=self.socket_path)
        communicator = ProtobufCommunicator(config)

        connect_request = ConnectRequest(
            service_id="test-service",
            sdk_version="0.1.0",
            min_cli_version="0.1.0",
        )

        asyncio.run(communicator.connect(connect_request))

        # Request non-existent mock
        mock_request = GetMockRequest(
            request_id="req-002",
            test_id="test-001",
            outbound_span={
                "name": "HTTP GET",
                "input_value": {"method": "GET", "url": "https://unknown.com"},
            },
            operation="http",
        )

        mock_response = asyncio.run(communicator.request_mock(mock_request))
        self.assertFalse(mock_response.found)
        self.assertEqual(mock_response.error, "No matching mock found")

        asyncio.run(communicator.disconnect())

    def test_multiple_mock_requests(self):
        """Test making multiple mock requests in sequence."""
        # Set up multiple mocks
        self.mock_server.add_mock(
            operation="http",
            input_pattern={"method": "GET", "url": "https://api.example.com/posts"},
            response={"status": 200, "body": []},
        )
        self.mock_server.add_mock(
            operation="http",
            input_pattern={"method": "POST", "url": "https://api.example.com/posts"},
            response={"status": 201, "body": {"id": 1}},
        )
        self.mock_server.add_mock(
            operation="database",
            input_pattern={"query": "SELECT * FROM users"},
            response={"rows": [{"id": 1}]},
        )

        self.mock_server.start()
        time.sleep(0.1)

        config = CommunicatorConfig(socket_path=self.socket_path)
        communicator = ProtobufCommunicator(config)

        asyncio.run(communicator.connect(ConnectRequest(
            service_id="test",
            sdk_version="0.1.0",
            min_cli_version="0.1.0",
        )))

        # Request HTTP GET mock
        response1 = asyncio.run(communicator.request_mock(GetMockRequest(
            request_id="req-1",
            test_id="test-1",
            outbound_span={"input_value": {"method": "GET", "url": "https://api.example.com/posts"}},
            operation="http",
        )))
        self.assertTrue(response1.found)
        self.assertEqual(response1.response_data["status"], 200)

        # Request HTTP POST mock
        response2 = asyncio.run(communicator.request_mock(GetMockRequest(
            request_id="req-2",
            test_id="test-1",
            outbound_span={"input_value": {"method": "POST", "url": "https://api.example.com/posts"}},
            operation="http",
        )))
        self.assertTrue(response2.found)
        self.assertEqual(response2.response_data["status"], 201)

        # Request database mock
        response3 = asyncio.run(communicator.request_mock(GetMockRequest(
            request_id="req-3",
            test_id="test-1",
            outbound_span={"input_value": {"query": "SELECT * FROM users"}},
            operation="database",
        )))
        self.assertTrue(response3.found)
        self.assertEqual(response3.response_data["rows"][0]["id"], 1)

        asyncio.run(communicator.disconnect())

    def test_server_tracks_received_messages(self):
        """Test that server correctly tracks all received messages."""
        self.mock_server.start()
        time.sleep(0.1)

        config = CommunicatorConfig(socket_path=self.socket_path)
        communicator = ProtobufCommunicator(config)

        asyncio.run(communicator.connect(ConnectRequest(
            service_id="test-service",
            sdk_version="0.1.0",
            min_cli_version="0.1.0",
            sdk_language="python",
        )))

        asyncio.run(communicator.request_mock(GetMockRequest(
            request_id="req-track",
            test_id="test-track",
            outbound_span={"input_value": {}},
            operation="test",
        )))

        asyncio.run(communicator.disconnect())

        # Verify messages were received
        messages = self.mock_server.received_messages
        self.assertGreaterEqual(len(messages), 2)

        # First message should be connect
        self.assertEqual(messages[0]["type"], SDKMessageType.SDK_CONNECT.value)
        self.assertEqual(messages[0]["payload"]["service_id"], "test-service")

        # Second message should be mock request
        self.assertEqual(messages[1]["type"], SDKMessageType.MOCK_REQUEST.value)
        self.assertEqual(messages[1]["payload"]["request_id"], "req-track")


class TestMockPatternMatching(unittest.TestCase):
    """Tests for mock pattern matching logic."""

    def setUp(self):
        """Set up test fixtures."""
        self.socket_path = tempfile.mktemp(suffix=".sock")
        self.mock_server = MockCLIServer(self.socket_path)

    def tearDown(self):
        """Clean up test fixtures."""
        self.mock_server.stop()

    def test_exact_match(self):
        """Test exact pattern matching."""
        self.mock_server.add_mock(
            operation="http",
            input_pattern={"method": "GET", "url": "https://api.example.com/exact"},
            response={"matched": "exact"},
        )

        self.mock_server.start()
        time.sleep(0.1)

        config = CommunicatorConfig(socket_path=self.socket_path)
        communicator = ProtobufCommunicator(config)

        asyncio.run(communicator.connect(ConnectRequest(
            service_id="test",
            sdk_version="0.1.0",
            min_cli_version="0.1.0",
        )))

        response = asyncio.run(communicator.request_mock(GetMockRequest(
            request_id="req-exact",
            test_id="test-1",
            outbound_span={"input_value": {"method": "GET", "url": "https://api.example.com/exact"}},
            operation="http",
        )))

        self.assertTrue(response.found)
        self.assertEqual(response.response_data["matched"], "exact")

        asyncio.run(communicator.disconnect())

    def test_partial_match(self):
        """Test partial pattern matching (subset of fields)."""
        self.mock_server.add_mock(
            operation="http",
            input_pattern={"method": "GET"},  # Only match method
            response={"matched": "partial"},
        )

        self.mock_server.start()
        time.sleep(0.1)

        config = CommunicatorConfig(socket_path=self.socket_path)
        communicator = ProtobufCommunicator(config)

        asyncio.run(communicator.connect(ConnectRequest(
            service_id="test",
            sdk_version="0.1.0",
            min_cli_version="0.1.0",
        )))

        # Should match even with extra fields in actual request
        response = asyncio.run(communicator.request_mock(GetMockRequest(
            request_id="req-partial",
            test_id="test-1",
            outbound_span={"input_value": {
                "method": "GET",
                "url": "https://any.url.com",
                "headers": {"extra": "field"},
            }},
            operation="http",
        )))

        self.assertTrue(response.found)
        self.assertEqual(response.response_data["matched"], "partial")

        asyncio.run(communicator.disconnect())


if __name__ == "__main__":
    unittest.main()
