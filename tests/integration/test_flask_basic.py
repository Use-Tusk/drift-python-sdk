"""Basic Flask integration tests for span capture.

These tests verify that the Flask instrumentation correctly captures
HTTP request/response data as spans.
"""

import os
import sys
import time
from pathlib import Path

import pytest
import requests

# Set up environment before importing drift
os.environ["TUSK_DRIFT_MODE"] = "RECORD"

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from drift import TuskDrift
from drift.core.types import SpanKind, StatusCode


@pytest.fixture(scope="module")
def flask_app_and_adapter():
    """Set up the SDK, Flask app, and adapter once for all tests in module."""
    from drift.core.tracing.adapters import InMemorySpanAdapter, register_in_memory_adapter

    sdk = TuskDrift.initialize()
    adapter = InMemorySpanAdapter()
    register_in_memory_adapter(adapter)

    # Flask is auto-instrumented by SDK initialization
    from flask import Flask, jsonify, request

    app = Flask(__name__)

    @app.route("/health")
    def health():
        return jsonify({"status": "healthy", "timestamp": time.time()})

    @app.route("/greet/<name>")
    def greet(name: str):
        greeting = request.args.get("greeting", "Hello")
        return jsonify({"message": f"{greeting}, {name}!", "name": name})

    @app.route("/echo", methods=["POST"])
    def echo():
        data = request.get_json()
        return jsonify({"echoed": data, "received_at": time.time()})

    @app.route("/error")
    def error():
        return jsonify({"error": "Something went wrong"}), 500

    @app.route("/headers")
    def headers():
        return jsonify(
            {
                "user_agent": request.headers.get("User-Agent"),
                "custom_header": request.headers.get("X-Custom-Header"),
            }
        )

    sdk.mark_app_as_ready()

    # Start Flask server in background
    from tests.utils.flask_test_server import FlaskTestServer

    server = FlaskTestServer(app=app)
    server.start()

    yield {"sdk": sdk, "adapter": adapter, "app": app, "server": server, "base_url": server.base_url}

    server.stop()


@pytest.fixture
def adapter(flask_app_and_adapter):
    """Get adapter and clear it before each test."""
    adapter = flask_app_and_adapter["adapter"]
    adapter.clear()
    return adapter


@pytest.fixture
def base_url(flask_app_and_adapter):
    """Get base URL for requests."""
    return flask_app_and_adapter["base_url"]


def wait_for_spans(timeout: float = 0.5):
    """Wait for spans to be processed."""
    time.sleep(timeout)


class TestFlaskBasicSpanCapture:
    """Test basic Flask request/response span capture."""

    def test_captures_get_request_span(self, adapter, base_url):
        """Test that GET requests create spans."""
        response = requests.get(f"{base_url}/health")

        assert response.status_code == 200
        wait_for_spans()

        spans = adapter.get_all_spans()
        assert len(spans) >= 1

        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]
        assert len(server_spans) >= 1

        span = server_spans[0]
        assert "/health" in span.name
        assert span.status.code == StatusCode.OK

    def test_captures_get_with_path_params(self, adapter, base_url):
        """Test that path parameters are captured."""
        response = requests.get(f"{base_url}/greet/World")

        assert response.status_code == 200
        wait_for_spans()

        spans = adapter.get_all_spans()
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]
        assert len(server_spans) >= 1

        span = server_spans[0]
        input_val = span.input_value
        assert isinstance(input_val, dict)
        target = input_val.get("target") or input_val.get("path") or input_val.get("url")
        assert "World" in str(target)

    def test_captures_get_with_query_params(self, adapter, base_url):
        """Test that query parameters are captured."""
        response = requests.get(f"{base_url}/greet/World?greeting=Hi")

        assert response.status_code == 200
        wait_for_spans()

        spans = adapter.get_all_spans()
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]
        assert len(server_spans) >= 1

    def test_captures_post_request_span(self, adapter, base_url):
        """Test that POST requests create spans."""
        payload = {"message": "Hello", "count": 42}
        response = requests.post(f"{base_url}/echo", json=payload)

        assert response.status_code == 200
        wait_for_spans()

        spans = adapter.get_all_spans()
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]
        assert len(server_spans) >= 1

        span = server_spans[0]
        input_val = span.input_value
        assert isinstance(input_val, dict)
        assert input_val.get("method") == "POST"

    def test_captures_error_response(self, adapter, base_url):
        """Test that error responses are captured correctly."""
        response = requests.get(f"{base_url}/error")

        assert response.status_code == 500
        wait_for_spans()

        spans = adapter.get_all_spans()
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]
        assert len(server_spans) >= 1

        span = server_spans[0]
        output_val = span.output_value
        assert isinstance(output_val, dict)
        status_code = output_val.get("statusCode") or output_val.get("status_code")
        assert status_code == 500

    def test_captures_request_headers(self, adapter, base_url):
        """Test that request headers are captured."""
        headers = {"X-Custom-Header": "custom-value"}
        response = requests.get(f"{base_url}/headers", headers=headers)

        assert response.status_code == 200
        wait_for_spans()

        spans = adapter.get_all_spans()
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]
        assert len(server_spans) >= 1

        span = server_spans[0]
        input_val = span.input_value
        assert isinstance(input_val, dict)
        assert "headers" in input_val

    def test_span_has_trace_id(self, adapter, base_url):
        """Test that spans have valid trace IDs."""
        response = requests.get(f"{base_url}/health")

        assert response.status_code == 200
        wait_for_spans()

        spans = adapter.get_all_spans()
        assert len(spans) >= 1

        span = spans[0]
        assert span.trace_id is not None
        assert len(span.trace_id) == 32

    def test_span_has_span_id(self, adapter, base_url):
        """Test that spans have valid span IDs."""
        response = requests.get(f"{base_url}/health")

        assert response.status_code == 200
        wait_for_spans()

        spans = adapter.get_all_spans()
        assert len(spans) >= 1

        span = spans[0]
        assert span.span_id is not None
        assert len(span.span_id) == 16

    def test_span_has_timing_info(self, adapter, base_url):
        """Test that spans have timing information."""
        response = requests.get(f"{base_url}/health")

        assert response.status_code == 200
        wait_for_spans()

        spans = adapter.get_all_spans()
        assert len(spans) >= 1

        span = spans[0]
        assert span.timestamp is not None
        assert span.duration is not None
        total_nanos = span.duration.seconds * 1_000_000_000 + span.duration.nanos
        assert total_nanos > 0


@pytest.fixture(scope="module")
def flask_multi_app_and_adapter():
    """Set up Flask app for multiple request tests."""
    from drift.core.tracing.adapters import InMemorySpanAdapter, register_in_memory_adapter

    sdk = TuskDrift.get_instance()
    adapter = InMemorySpanAdapter()
    register_in_memory_adapter(adapter)

    from flask import Flask, jsonify

    app = Flask(__name__)

    @app.route("/endpoint1")
    def endpoint1():
        return jsonify({"endpoint": 1})

    @app.route("/endpoint2")
    def endpoint2():
        return jsonify({"endpoint": 2})

    from tests.utils.flask_test_server import FlaskTestServer

    server = FlaskTestServer(app=app)
    server.start()

    yield {"sdk": sdk, "adapter": adapter, "app": app, "server": server, "base_url": server.base_url}

    server.stop()


@pytest.fixture
def multi_adapter(flask_multi_app_and_adapter):
    """Get adapter and clear it before each test."""
    adapter = flask_multi_app_and_adapter["adapter"]
    adapter.clear()
    return adapter


@pytest.fixture
def multi_base_url(flask_multi_app_and_adapter):
    """Get base URL for requests."""
    return flask_multi_app_and_adapter["base_url"]


class TestFlaskMultipleRequests:
    """Test multiple Flask requests create separate spans."""

    def test_multiple_requests_create_separate_spans(self, multi_adapter, multi_base_url):
        """Test that multiple requests create separate spans."""
        response1 = requests.get(f"{multi_base_url}/endpoint1")
        response2 = requests.get(f"{multi_base_url}/endpoint2")

        assert response1.status_code == 200
        assert response2.status_code == 200
        wait_for_spans()

        spans = multi_adapter.get_all_spans()
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]

        assert len(server_spans) >= 2

        span_ids = [s.span_id for s in server_spans]
        assert len(span_ids) == len(set(span_ids))

    def test_multiple_requests_have_different_trace_ids(self, multi_adapter, multi_base_url):
        """Test that independent requests have different trace IDs."""
        response1 = requests.get(f"{multi_base_url}/endpoint1")
        response2 = requests.get(f"{multi_base_url}/endpoint2")

        assert response1.status_code == 200
        assert response2.status_code == 200
        wait_for_spans()

        spans = multi_adapter.get_all_spans()
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]

        assert len(server_spans) >= 2

        trace_ids = [s.trace_id for s in server_spans]
        assert len(trace_ids) == len(set(trace_ids))
