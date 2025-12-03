"""Basic Flask integration tests for span capture.

These tests verify that the Flask instrumentation correctly captures
HTTP request/response data as spans.
"""

import os
import sys
import time
import unittest
from pathlib import Path

# Set up environment before importing drift
os.environ["TUSK_DRIFT_MODE"] = "RECORD"

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import requests

from drift import TuskDrift
from drift.core.types import SpanKind, StatusCode


class TestFlaskBasicSpanCapture(unittest.TestCase):
    """Test basic Flask request/response span capture."""

    @classmethod
    def setUpClass(cls):
        """Set up the SDK and Flask app once for all tests."""
        from drift.tracing.adapters import InMemorySpanAdapter, register_in_memory_adapter

        cls.sdk = TuskDrift.initialize()
        cls.adapter = InMemorySpanAdapter()
        register_in_memory_adapter(cls.adapter)

        # Flask is auto-instrumented by SDK initialization
        # Import Flask after SDK is set up
        from flask import Flask, jsonify, request

        cls.app = Flask(__name__)

        @cls.app.route("/health")
        def health():
            return jsonify({"status": "healthy", "timestamp": time.time()})

        @cls.app.route("/greet/<name>")
        def greet(name: str):
            greeting = request.args.get("greeting", "Hello")
            return jsonify({"message": f"{greeting}, {name}!", "name": name})

        @cls.app.route("/echo", methods=["POST"])
        def echo():
            data = request.get_json()
            return jsonify({"echoed": data, "received_at": time.time()})

        @cls.app.route("/error")
        def error():
            return jsonify({"error": "Something went wrong"}), 500

        @cls.app.route("/headers")
        def headers():
            # Echo back some headers for testing
            return jsonify({
                "user_agent": request.headers.get("User-Agent"),
                "custom_header": request.headers.get("X-Custom-Header"),
            })

        cls.sdk.mark_app_as_ready()

        # Start Flask server in background
        from tests.utils.flask_test_server import FlaskTestServer

        cls.server = FlaskTestServer(app=cls.app)
        cls.server.start()
        cls.base_url = cls.server.base_url

    @classmethod
    def tearDownClass(cls):
        """Clean up after all tests."""
        cls.server.stop()

    def setUp(self):
        """Clear spans before each test."""
        self.adapter.clear()

    def wait_for_spans(self, timeout: float = 0.5):
        """Wait for spans to be processed."""
        time.sleep(timeout)

    def test_captures_get_request_span(self):
        """Test that GET requests create spans."""
        response = requests.get(f"{self.base_url}/health")

        self.assertEqual(response.status_code, 200)
        self.wait_for_spans()

        spans = self.adapter.get_all_spans()
        self.assertGreaterEqual(len(spans), 1)

        # Find the server span (inbound request)
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]
        self.assertGreaterEqual(len(server_spans), 1)

        span = server_spans[0]
        self.assertIn("/health", span.name)
        self.assertEqual(span.status.code, StatusCode.OK)

    def test_captures_get_with_path_params(self):
        """Test that path parameters are captured."""
        response = requests.get(f"{self.base_url}/greet/World")

        self.assertEqual(response.status_code, 200)
        self.wait_for_spans()

        spans = self.adapter.get_all_spans()
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]
        self.assertGreaterEqual(len(server_spans), 1)

        span = server_spans[0]
        # Check input value contains the path
        input_val = span.input_value
        self.assertIsInstance(input_val, dict)
        # Path should contain the actual value
        target = input_val.get("target") or input_val.get("path") or input_val.get("url")
        self.assertIn("World", str(target))

    def test_captures_get_with_query_params(self):
        """Test that query parameters are captured."""
        response = requests.get(f"{self.base_url}/greet/World?greeting=Hi")

        self.assertEqual(response.status_code, 200)
        self.wait_for_spans()

        spans = self.adapter.get_all_spans()
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]
        self.assertGreaterEqual(len(server_spans), 1)

    def test_captures_post_request_span(self):
        """Test that POST requests create spans."""
        payload = {"message": "Hello", "count": 42}
        response = requests.post(f"{self.base_url}/echo", json=payload)

        self.assertEqual(response.status_code, 200)
        self.wait_for_spans()

        spans = self.adapter.get_all_spans()
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]
        self.assertGreaterEqual(len(server_spans), 1)

        span = server_spans[0]
        input_val = span.input_value
        self.assertIsInstance(input_val, dict)
        self.assertEqual(input_val.get("method"), "POST")

    def test_captures_error_response(self):
        """Test that error responses are captured correctly."""
        response = requests.get(f"{self.base_url}/error")

        self.assertEqual(response.status_code, 500)
        self.wait_for_spans()

        spans = self.adapter.get_all_spans()
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]
        self.assertGreaterEqual(len(server_spans), 1)

        span = server_spans[0]
        output_val = span.output_value
        self.assertIsInstance(output_val, dict)
        # Status code should be 500
        status_code = output_val.get("statusCode") or output_val.get("status_code")
        self.assertEqual(status_code, 500)

    def test_captures_request_headers(self):
        """Test that request headers are captured."""
        headers = {"X-Custom-Header": "custom-value"}
        response = requests.get(f"{self.base_url}/headers", headers=headers)

        self.assertEqual(response.status_code, 200)
        self.wait_for_spans()

        spans = self.adapter.get_all_spans()
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]
        self.assertGreaterEqual(len(server_spans), 1)

        span = server_spans[0]
        input_val = span.input_value
        self.assertIsInstance(input_val, dict)
        # Headers should be captured
        self.assertIn("headers", input_val)

    def test_span_has_trace_id(self):
        """Test that spans have valid trace IDs."""
        response = requests.get(f"{self.base_url}/health")

        self.assertEqual(response.status_code, 200)
        self.wait_for_spans()

        spans = self.adapter.get_all_spans()
        self.assertGreaterEqual(len(spans), 1)

        span = spans[0]
        self.assertIsNotNone(span.trace_id)
        self.assertEqual(len(span.trace_id), 32)  # 32 hex chars

    def test_span_has_span_id(self):
        """Test that spans have valid span IDs."""
        response = requests.get(f"{self.base_url}/health")

        self.assertEqual(response.status_code, 200)
        self.wait_for_spans()

        spans = self.adapter.get_all_spans()
        self.assertGreaterEqual(len(spans), 1)

        span = spans[0]
        self.assertIsNotNone(span.span_id)
        self.assertEqual(len(span.span_id), 16)  # 16 hex chars

    def test_span_has_timing_info(self):
        """Test that spans have timing information."""
        response = requests.get(f"{self.base_url}/health")

        self.assertEqual(response.status_code, 200)
        self.wait_for_spans()

        spans = self.adapter.get_all_spans()
        self.assertGreaterEqual(len(spans), 1)

        span = spans[0]
        self.assertIsNotNone(span.timestamp)
        self.assertIsNotNone(span.duration)
        # Duration should be positive
        total_nanos = span.duration.seconds * 1_000_000_000 + span.duration.nanos
        self.assertGreater(total_nanos, 0)


class TestFlaskMultipleRequests(unittest.TestCase):
    """Test multiple Flask requests create separate spans."""

    @classmethod
    def setUpClass(cls):
        """Set up the SDK and Flask app once for all tests."""
        from drift.tracing.adapters import InMemorySpanAdapter, register_in_memory_adapter

        cls.sdk = TuskDrift.get_instance()
        cls.adapter = InMemorySpanAdapter()
        register_in_memory_adapter(cls.adapter)

        # Import Flask after instrumentation is set up
        from flask import Flask, jsonify

        cls.app = Flask(__name__)

        @cls.app.route("/endpoint1")
        def endpoint1():
            return jsonify({"endpoint": 1})

        @cls.app.route("/endpoint2")
        def endpoint2():
            return jsonify({"endpoint": 2})

        # Start Flask server in background
        from tests.utils.flask_test_server import FlaskTestServer

        cls.server = FlaskTestServer(app=cls.app)
        cls.server.start()
        cls.base_url = cls.server.base_url

    @classmethod
    def tearDownClass(cls):
        """Clean up after all tests."""
        cls.server.stop()

    def setUp(self):
        """Clear spans before each test."""
        self.adapter.clear()

    def wait_for_spans(self, timeout: float = 0.5):
        """Wait for spans to be processed."""
        time.sleep(timeout)

    def test_multiple_requests_create_separate_spans(self):
        """Test that multiple requests create separate spans."""
        response1 = requests.get(f"{self.base_url}/endpoint1")
        response2 = requests.get(f"{self.base_url}/endpoint2")

        self.assertEqual(response1.status_code, 200)
        self.assertEqual(response2.status_code, 200)
        self.wait_for_spans()

        spans = self.adapter.get_all_spans()
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]

        # Should have at least 2 server spans
        self.assertGreaterEqual(len(server_spans), 2)

        # Spans should have different span IDs
        span_ids = [s.span_id for s in server_spans]
        self.assertEqual(len(span_ids), len(set(span_ids)))

    def test_multiple_requests_have_different_trace_ids(self):
        """Test that independent requests have different trace IDs."""
        response1 = requests.get(f"{self.base_url}/endpoint1")
        response2 = requests.get(f"{self.base_url}/endpoint2")

        self.assertEqual(response1.status_code, 200)
        self.assertEqual(response2.status_code, 200)
        self.wait_for_spans()

        spans = self.adapter.get_all_spans()
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]

        self.assertGreaterEqual(len(server_spans), 2)

        # Separate requests should have different trace IDs
        trace_ids = [s.trace_id for s in server_spans]
        self.assertEqual(len(trace_ids), len(set(trace_ids)))


if __name__ == "__main__":
    unittest.main()
