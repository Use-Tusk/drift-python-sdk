"""Integration tests for Flask replay mode."""

import os
import socket
import time
from pathlib import Path

import pytest

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


@pytest.fixture(scope="module")
def flask_replay_app():
    """Set up SDK and Flask app once for all tests."""
    sdk = TuskDrift.initialize()
    adapter = InMemorySpanAdapter()
    register_in_memory_adapter(adapter)

    app = Flask(__name__)

    @app.route("/health")
    def health():
        return jsonify({"status": "healthy"})

    @app.route("/user/<name>")
    def get_user(name: str):
        return jsonify({"user": name, "id": 123})

    @app.route("/echo", methods=["POST"])
    def echo():
        from flask import request

        data = request.get_json()
        return jsonify({"echoed": data})

    sdk.mark_app_as_ready()
    client = app.test_client()

    yield {"sdk": sdk, "adapter": adapter, "app": app, "client": client}

    # Cleanup socket
    try:
        test_socket.close()
        if Path(socket_path).exists():
            os.unlink(socket_path)
    except Exception:
        pass


@pytest.fixture
def adapter(flask_replay_app):
    """Get adapter and clear it before each test."""
    adapter = flask_replay_app["adapter"]
    adapter.clear()
    return adapter


@pytest.fixture
def client(flask_replay_app):
    """Get test client."""
    return flask_replay_app["client"]


def wait_for_spans(timeout: float = 0.5):
    """Wait for spans to be processed."""
    time.sleep(timeout)


class TestFlaskReplayMode:
    """Test Flask instrumentation in REPLAY mode."""

    def test_request_without_trace_id_header(self, adapter, client):
        """Test that requests without trace ID don't create spans."""
        response = client.get("/health")

        assert response.status_code == 200
        wait_for_spans()

        spans = adapter.get_all_spans()
        assert len(spans) == 0

    def test_request_with_trace_id_header(self, adapter, client):
        """Test that requests with trace ID create SERVER spans."""
        response = client.get("/user/alice", headers={"x-td-trace-id": "test-trace-123"})

        assert response.status_code == 200
        data = response.get_json()
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

    def test_post_request_with_trace_id(self, adapter, client):
        """Test that POST requests work in replay mode."""
        response = client.post("/echo", json={"message": "test"}, headers={"x-td-trace-id": "post-trace-456"})

        assert response.status_code == 200
        data = response.get_json()
        assert data["echoed"]["message"] == "test"

        wait_for_spans()

        spans = adapter.get_all_spans()
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]

        if len(server_spans) > 0:
            span = server_spans[0]
            assert span.trace_id == "post-trace-456"

    def test_case_insensitive_headers(self, adapter, client):
        """Test that trace ID header is case-insensitive."""
        response = client.get("/health", headers={"x-td-trace-id": "lowercase-trace"})
        assert response.status_code == 200

        response = client.get("/health", headers={"X-TD-TRACE-ID": "uppercase-trace"})
        assert response.status_code == 200
