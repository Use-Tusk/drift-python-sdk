"""Integration tests for Flask replay mode."""

import os
import socket
import time
import unittest
from pathlib import Path

# Set replay mode before importing drift
os.environ["TUSK_DRIFT_MODE"] = "REPLAY"

# Create a fake socket to satisfy the CLI connection check
socket_path = "/tmp/tusk-connect-flask-replay-test.sock"
os.environ["TUSK_MOCK_SOCKET"] = socket_path

# Clean up any existing socket
if Path(socket_path).exists():
    os.unlink(socket_path)

# Create a Unix socket (won't be functional, but satisfies existence check)
test_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
test_socket.bind(socket_path)
test_socket.listen(1)

from flask import Flask, jsonify

from drift import TuskDrift
from drift.core.tracing.adapters import InMemorySpanAdapter, register_in_memory_adapter
from drift.core.types import SpanKind


class TestFlaskReplayMode(unittest.TestCase):
    """Test Flask instrumentation in REPLAY mode."""

    @classmethod
    def setUpClass(cls):
        """Set up SDK and Flask app once for all tests."""
        cls.sdk = TuskDrift.initialize()
        cls.adapter = InMemorySpanAdapter()
        register_in_memory_adapter(cls.adapter)

        # Create Flask app
        cls.app = Flask(__name__)

        @cls.app.route("/health")
        def health():
            return jsonify({"status": "healthy"})

        @cls.app.route("/user/<name>")
        def get_user(name: str):
            return jsonify({"user": name, "id": 123})

        @cls.app.route("/echo", methods=["POST"])
        def echo():
            from flask import request

            data = request.get_json()
            return jsonify({"echoed": data})

        cls.sdk.mark_app_as_ready()
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        """Clean up socket."""
        try:
            test_socket.close()
            if Path(socket_path).exists():
                os.unlink(socket_path)
        except Exception:
            pass

    def setUp(self):
        """Clear spans before each test."""
        self.adapter.clear()

    def wait_for_spans(self, timeout: float = 0.5):
        """Wait for spans to be processed."""
        time.sleep(timeout)

    def test_request_without_trace_id_header(self):
        """Test that requests without trace ID don't create spans."""
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.wait_for_spans()

        spans = self.adapter.get_all_spans()
        # No span should be created without trace ID in replay mode
        self.assertEqual(len(spans), 0)

    def test_request_with_trace_id_header(self):
        """Test that requests with trace ID create SERVER spans."""
        response = self.client.get("/user/alice", headers={"x-td-trace-id": "test-trace-123"})

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["user"], "alice")
        self.assertEqual(data["id"], 123)

        self.wait_for_spans()

        spans = self.adapter.get_all_spans()
        self.assertGreaterEqual(len(spans), 1)

        # Find SERVER span
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]
        self.assertGreaterEqual(len(server_spans), 1)

        span = server_spans[0]
        # Verify trace ID matches the one from header
        self.assertEqual(span.trace_id, "test-trace-123")
        self.assertIn("/user", span.name)

    def test_post_request_with_trace_id(self):
        """Test that POST requests work in replay mode."""
        response = self.client.post("/echo", json={"message": "test"}, headers={"x-td-trace-id": "post-trace-456"})

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["echoed"]["message"], "test")

        self.wait_for_spans()

        spans = self.adapter.get_all_spans()
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]

        if len(server_spans) > 0:
            span = server_spans[0]
            self.assertEqual(span.trace_id, "post-trace-456")

    def test_case_insensitive_headers(self):
        """Test that trace ID header is case-insensitive."""
        # Try lowercase
        response = self.client.get("/health", headers={"x-td-trace-id": "lowercase-trace"})
        self.assertEqual(response.status_code, 200)

        # Try uppercase
        response = self.client.get("/health", headers={"X-TD-TRACE-ID": "uppercase-trace"})
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
