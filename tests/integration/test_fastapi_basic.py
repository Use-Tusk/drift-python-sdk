"""Basic FastAPI integration tests for span capture.

These tests verify that the FastAPI instrumentation correctly captures
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
def fastapi_app_and_adapter():
    """Set up the SDK, FastAPI app, and adapter once for all tests in module."""
    from drift.core.tracing.adapters import InMemorySpanAdapter, register_in_memory_adapter

    sdk = TuskDrift.get_instance()
    if not TuskDrift._initialized:
        sdk = TuskDrift.initialize()

    adapter = InMemorySpanAdapter()
    register_in_memory_adapter(adapter)

    # FastAPI is auto-instrumented by SDK initialization
    from fastapi import FastAPI
    from pydantic import BaseModel

    app = FastAPI()

    class EchoRequest(BaseModel):
        message: str | None = None
        count: int | None = None

    @app.get("/health")
    def health():
        return {"status": "healthy", "timestamp": time.time()}

    @app.get("/greet/{name}")
    def greet(name: str, greeting: str = "Hello"):
        return {"message": f"{greeting}, {name}!", "name": name}

    @app.post("/echo")
    def echo(data: EchoRequest):
        return {"echoed": data.model_dump(), "received_at": time.time()}

    @app.get("/error")
    def error():
        return {"error": "Something went wrong"}, 500

    @app.get("/headers")
    def headers():
        return {"info": "Headers endpoint"}

    sdk.mark_app_as_ready()

    # Start FastAPI server in background
    from tests.utils.fastapi_test_server import FastAPITestServer

    server = FastAPITestServer(app=app)
    server.start()

    yield {"sdk": sdk, "adapter": adapter, "app": app, "server": server, "base_url": server.base_url}

    server.stop()


@pytest.fixture
def adapter(fastapi_app_and_adapter):
    """Get adapter and clear it before each test."""
    adapter = fastapi_app_and_adapter["adapter"]
    adapter.clear()
    return adapter


@pytest.fixture
def base_url(fastapi_app_and_adapter):
    """Get base URL for requests."""
    return fastapi_app_and_adapter["base_url"]


def wait_for_spans(timeout: float = 0.5):
    """Wait for spans to be processed."""
    time.sleep(timeout)


class TestFastAPIBasicSpanCapture:
    """Test basic FastAPI request/response span capture."""

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

    def test_captures_request_body(self, adapter, base_url):
        """Test that request body is captured for POST requests."""
        payload = {"test_key": "test_value"}
        response = requests.post(f"{base_url}/echo", json=payload)

        assert response.status_code == 200
        wait_for_spans()

        spans = adapter.get_all_spans()
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]
        assert len(server_spans) >= 1

        span = server_spans[0]
        input_val = span.input_value
        assert isinstance(input_val, dict)
        assert "body" in input_val


@pytest.fixture(scope="module")
def fastapi_multi_app_and_adapter():
    """Set up FastAPI app for multiple request tests."""
    from drift.core.tracing.adapters import InMemorySpanAdapter, register_in_memory_adapter

    sdk = TuskDrift.get_instance()
    adapter = InMemorySpanAdapter()
    register_in_memory_adapter(adapter)

    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/endpoint1")
    def endpoint1():
        return {"endpoint": 1}

    @app.get("/endpoint2")
    def endpoint2():
        return {"endpoint": 2}

    from tests.utils.fastapi_test_server import FastAPITestServer

    server = FastAPITestServer(app=app)
    server.start()

    yield {"sdk": sdk, "adapter": adapter, "app": app, "server": server, "base_url": server.base_url}

    server.stop()


@pytest.fixture
def multi_adapter(fastapi_multi_app_and_adapter):
    """Get adapter and clear it before each test."""
    adapter = fastapi_multi_app_and_adapter["adapter"]
    adapter.clear()
    return adapter


@pytest.fixture
def multi_base_url(fastapi_multi_app_and_adapter):
    """Get base URL for requests."""
    return fastapi_multi_app_and_adapter["base_url"]


class TestFastAPIMultipleRequests:
    """Test multiple FastAPI requests create separate spans."""

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
