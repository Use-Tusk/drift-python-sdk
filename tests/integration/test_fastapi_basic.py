"""Basic FastAPI integration tests for span capture.

These tests verify that the FastAPI instrumentation correctly captures
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

from drift import FastAPIInstrumentation, TuskDrift
from drift.core.types import SpanKind, StatusCode


class TestFastAPIBasicSpanCapture(unittest.TestCase):
    """Test basic FastAPI request/response span capture."""

    @classmethod
    def setUpClass(cls):
        """Set up the SDK and FastAPI app once for all tests."""
        cls.sdk = TuskDrift.get_instance()
        if not TuskDrift._initialized:
            cls.sdk = TuskDrift.initialize(use_batching=False)
        cls.fastapi_instrumentation = FastAPIInstrumentation()

        # Import FastAPI after instrumentation is set up
        from fastapi import FastAPI
        from pydantic import BaseModel

        cls.app = FastAPI()

        class EchoRequest(BaseModel):
            message: str | None = None
            count: int | None = None

        @cls.app.get("/health")
        def health():
            return {"status": "healthy", "timestamp": time.time()}

        @cls.app.get("/greet/{name}")
        def greet(name: str, greeting: str = "Hello"):
            return {"message": f"{greeting}, {name}!", "name": name}

        @cls.app.post("/echo")
        def echo(data: EchoRequest):
            return {"echoed": data.model_dump(), "received_at": time.time()}

        @cls.app.get("/error")
        def error():
            return {"error": "Something went wrong"}, 500

        @cls.app.get("/headers")
        def headers():
            return {"info": "Headers endpoint"}

        cls.sdk.mark_app_as_ready()

        # Start FastAPI server in background
        from tests.utils.fastapi_test_server import FastAPITestServer

        cls.server = FastAPITestServer(app=cls.app)
        cls.server.start()
        cls.base_url = cls.server.base_url

    @classmethod
    def tearDownClass(cls):
        """Clean up after all tests."""
        cls.server.stop()

    def setUp(self):
        """Clear spans before each test."""
        self.sdk.clear_in_memory_spans()

    def wait_for_spans(self, timeout: float = 0.5):
        """Wait for spans to be processed."""
        time.sleep(timeout)

    def test_captures_get_request_span(self):
        """Test that GET requests create spans."""
        response = requests.get(f"{self.base_url}/health")

        self.assertEqual(response.status_code, 200)
        self.wait_for_spans()

        spans = self.sdk.get_in_memory_spans()
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

        spans = self.sdk.get_in_memory_spans()
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]
        self.assertGreaterEqual(len(server_spans), 1)

        span = server_spans[0]
        input_val = span.input_value
        self.assertIsInstance(input_val, dict)

    def test_captures_get_with_query_params(self):
        """Test that query parameters are captured."""
        response = requests.get(f"{self.base_url}/greet/World?greeting=Hi")

        self.assertEqual(response.status_code, 200)
        self.wait_for_spans()

        spans = self.sdk.get_in_memory_spans()
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]
        self.assertGreaterEqual(len(server_spans), 1)

    def test_captures_post_request_span(self):
        """Test that POST requests create spans."""
        payload = {"message": "Hello", "count": 42}
        response = requests.post(f"{self.base_url}/echo", json=payload)

        self.assertEqual(response.status_code, 200)
        self.wait_for_spans()

        spans = self.sdk.get_in_memory_spans()
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]
        self.assertGreaterEqual(len(server_spans), 1)

        span = server_spans[0]
        input_val = span.input_value
        self.assertIsInstance(input_val, dict)
        self.assertEqual(input_val.get("method"), "POST")

    def test_span_has_trace_id(self):
        """Test that spans have valid trace IDs."""
        response = requests.get(f"{self.base_url}/health")

        self.assertEqual(response.status_code, 200)
        self.wait_for_spans()

        spans = self.sdk.get_in_memory_spans()
        self.assertGreaterEqual(len(spans), 1)

        span = spans[0]
        self.assertIsNotNone(span.trace_id)
        self.assertEqual(len(span.trace_id), 32)

    def test_span_has_span_id(self):
        """Test that spans have valid span IDs."""
        response = requests.get(f"{self.base_url}/health")

        self.assertEqual(response.status_code, 200)
        self.wait_for_spans()

        spans = self.sdk.get_in_memory_spans()
        self.assertGreaterEqual(len(spans), 1)

        span = spans[0]
        self.assertIsNotNone(span.span_id)
        self.assertEqual(len(span.span_id), 16)

    def test_span_has_timing_info(self):
        """Test that spans have timing information."""
        response = requests.get(f"{self.base_url}/health")

        self.assertEqual(response.status_code, 200)
        self.wait_for_spans()

        spans = self.sdk.get_in_memory_spans()
        self.assertGreaterEqual(len(spans), 1)

        span = spans[0]
        self.assertIsNotNone(span.timestamp)
        self.assertIsNotNone(span.duration)
        total_nanos = span.duration.seconds * 1_000_000_000 + span.duration.nanos
        self.assertGreater(total_nanos, 0)

    def test_captures_request_body(self):
        """Test that request body is captured for POST requests."""
        payload = {"test_key": "test_value"}
        response = requests.post(f"{self.base_url}/echo", json=payload)

        self.assertEqual(response.status_code, 200)
        self.wait_for_spans()

        spans = self.sdk.get_in_memory_spans()
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]
        self.assertGreaterEqual(len(server_spans), 1)

        span = server_spans[0]
        input_val = span.input_value
        self.assertIsInstance(input_val, dict)
        # Body should be captured (may be base64 encoded)
        self.assertIn("body", input_val)


class TestFastAPIMultipleRequests(unittest.TestCase):
    """Test multiple FastAPI requests create separate spans."""

    @classmethod
    def setUpClass(cls):
        """Set up the SDK and FastAPI app once for all tests."""
        cls.sdk = TuskDrift.get_instance()

        from fastapi import FastAPI

        cls.app = FastAPI()

        @cls.app.get("/endpoint1")
        def endpoint1():
            return {"endpoint": 1}

        @cls.app.get("/endpoint2")
        def endpoint2():
            return {"endpoint": 2}

        from tests.utils.fastapi_test_server import FastAPITestServer

        cls.server = FastAPITestServer(app=cls.app)
        cls.server.start()
        cls.base_url = cls.server.base_url

    @classmethod
    def tearDownClass(cls):
        """Clean up after all tests."""
        cls.server.stop()

    def setUp(self):
        """Clear spans before each test."""
        self.sdk.clear_in_memory_spans()

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

        spans = self.sdk.get_in_memory_spans()
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]

        self.assertGreaterEqual(len(server_spans), 2)

        span_ids = [s.span_id for s in server_spans]
        self.assertEqual(len(span_ids), len(set(span_ids)))

    def test_multiple_requests_have_different_trace_ids(self):
        """Test that independent requests have different trace IDs."""
        response1 = requests.get(f"{self.base_url}/endpoint1")
        response2 = requests.get(f"{self.base_url}/endpoint2")

        self.assertEqual(response1.status_code, 200)
        self.assertEqual(response2.status_code, 200)
        self.wait_for_spans()

        spans = self.sdk.get_in_memory_spans()
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]

        self.assertGreaterEqual(len(server_spans), 2)

        trace_ids = [s.trace_id for s in server_spans]
        self.assertEqual(len(trace_ids), len(set(trace_ids)))


if __name__ == "__main__":
    unittest.main()
