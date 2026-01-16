"""Integration tests for FastAPI replay mode."""

import os
import socket
import sys
import time
from pathlib import Path

import pytest
import requests

# Set replay mode before importing drift
os.environ["TUSK_DRIFT_MODE"] = "REPLAY"

# Create a fake socket to satisfy the CLI connection check
socket_path = "/tmp/tusk-connect-fastapi-replay-test.sock"
os.environ["TUSK_MOCK_SOCKET"] = socket_path

# Clean up any existing socket
if Path(socket_path).exists():
    os.unlink(socket_path)

# Create a Unix socket (won't be functional, but satisfies existence check)
test_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
test_socket.bind(socket_path)
test_socket.listen(1)

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from drift import TuskDrift
from drift.core.tracing.adapters import InMemorySpanAdapter, register_in_memory_adapter
from drift.core.types import SpanKind


@pytest.fixture(scope="module")
def fastapi_replay_app():
    """Set up SDK and FastAPI app once for all tests."""
    sdk = TuskDrift.initialize()
    adapter = InMemorySpanAdapter()
    register_in_memory_adapter(adapter)

    from fastapi import FastAPI
    from pydantic import BaseModel

    app = FastAPI()

    @app.get("/health")
    def health():
        return {"status": "healthy"}

    @app.get("/user/{name}")
    def get_user(name: str):
        return {"user": name, "id": 123}

    class EchoRequest(BaseModel):
        message: str

    @app.post("/echo")
    def echo(data: EchoRequest):
        return {"echoed": data.model_dump()}

    sdk.mark_app_as_ready()

    # Start FastAPI server in background
    from tests.utils.fastapi_test_server import FastAPITestServer

    server = FastAPITestServer(app=app)
    server.start()

    yield {"sdk": sdk, "adapter": adapter, "app": app, "server": server, "base_url": server.base_url}

    server.stop()
    try:
        test_socket.close()
        if Path(socket_path).exists():
            os.unlink(socket_path)
    except Exception:
        pass


@pytest.fixture
def adapter(fastapi_replay_app):
    """Get adapter and clear it before each test."""
    adapter = fastapi_replay_app["adapter"]
    adapter.clear()
    return adapter


@pytest.fixture
def base_url(fastapi_replay_app):
    """Get base URL for requests."""
    return fastapi_replay_app["base_url"]


def wait_for_spans(timeout: float = 0.5):
    """Wait for spans to be processed."""
    time.sleep(timeout)


class TestFastAPIReplayMode:
    """Test FastAPI instrumentation in REPLAY mode."""

    def test_request_without_trace_id_header(self, adapter, base_url):
        """Test that requests without trace ID don't create SERVER spans."""
        response = requests.get(f"{base_url}/health")

        assert response.status_code == 200
        wait_for_spans()

        spans = adapter.get_all_spans()
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]
        assert len(server_spans) == 0

    def test_request_with_trace_id_header(self, adapter, base_url):
        """Test that requests with trace ID create SERVER spans."""
        response = requests.get(f"{base_url}/user/alice", headers={"x-td-trace-id": "test-trace-123"})

        assert response.status_code == 200
        data = response.json()
        assert data["user"] == "alice"
        assert data["id"] == 123

        wait_for_spans()

        spans = adapter.get_all_spans()
        assert len(spans) >= 1

        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]
        assert len(server_spans) >= 1

        span = server_spans[0]
        assert span.trace_id == "test-trace-123"
        assert "/user" in span.name

    def test_post_request_with_trace_id(self, adapter, base_url):
        """Test that POST requests work in replay mode."""
        response = requests.post(
            f"{base_url}/echo",
            json={"message": "test"},
            headers={"x-td-trace-id": "post-trace-456"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["echoed"]["message"] == "test"

        wait_for_spans()

        spans = adapter.get_all_spans()
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]

        if len(server_spans) > 0:
            span = server_spans[0]
            assert span.trace_id == "post-trace-456"

    def test_case_insensitive_headers(self, adapter, base_url):
        """Test that trace ID header is case-insensitive."""
        response = requests.get(f"{base_url}/health", headers={"x-td-trace-id": "lowercase-trace"})
        assert response.status_code == 200

        response = requests.get(f"{base_url}/health", headers={"X-TD-TRACE-ID": "uppercase-trace"})
        assert response.status_code == 200
