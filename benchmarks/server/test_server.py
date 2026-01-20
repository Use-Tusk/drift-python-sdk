"""Test server for benchmarks - provides various endpoint types for measuring SDK overhead."""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any

from flask import Flask, Response, request


class TestServer:
    """Flask-based test server for benchmarks."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.host = host
        self.port = port
        self.app = Flask(__name__)
        self._server_thread: threading.Thread | None = None
        self._actual_port: int | None = None
        self._setup_routes()

    def _setup_routes(self) -> None:
        """Set up all benchmark endpoints."""

        @self.app.route("/health")
        def health() -> Response:
            return Response(
                json.dumps({"status": "ok"}),
                mimetype="application/json",
            )

        @self.app.route("/api/simple")
        def simple() -> Response:
            """Minimal processing endpoint."""
            import time

            return Response(
                json.dumps({"message": "Hello World", "timestamp": int(time.time() * 1000)}),
                mimetype="application/json",
            )

        @self.app.route("/api/simple-post", methods=["POST"])
        def simple_post() -> Response:
            """Minimal POST endpoint."""
            import time

            return Response(
                json.dumps({"message": "Hello World", "timestamp": int(time.time() * 1000)}),
                mimetype="application/json",
            )

        @self.app.route("/api/echo", methods=["POST"])
        def echo() -> Response:
            """Echo back request body."""
            data = request.get_json(force=True, silent=True) or {}
            return Response(json.dumps(data), mimetype="application/json")

        @self.app.route("/api/small")
        def small() -> Response:
            """Return ~100KB payload."""
            import time

            data = {
                "id": "small-123",
                "data": "x" * (100 * 1024),
                "timestamp": int(time.time() * 1000),
            }
            return Response(json.dumps(data), mimetype="application/json")

        @self.app.route("/api/small-post", methods=["POST"])
        def small_post() -> Response:
            """Accept and return ~100KB payload."""
            import time

            data = {
                "id": "small-post-123",
                "data": "x" * (100 * 1024),
                "timestamp": int(time.time() * 1000),
            }
            return Response(json.dumps(data), mimetype="application/json")

        @self.app.route("/api/medium")
        def medium() -> Response:
            """Return ~1MB payload."""
            import time

            data = {
                "id": "medium-456",
                "data": "x" * (1024 * 1024),
                "timestamp": int(time.time() * 1000),
            }
            return Response(json.dumps(data), mimetype="application/json")

        @self.app.route("/api/medium-post", methods=["POST"])
        def medium_post() -> Response:
            """Accept and return ~1MB payload."""
            import time

            data = {
                "id": "medium-post-456",
                "data": "x" * (1024 * 1024),
                "timestamp": int(time.time() * 1000),
            }
            return Response(json.dumps(data), mimetype="application/json")

        @self.app.route("/api/large")
        def large() -> Response:
            """Return ~2MB payload."""
            import time

            data = {
                "id": "large-789",
                "data": "x" * (2 * 1024 * 1024),
                "timestamp": int(time.time() * 1000),
            }
            return Response(json.dumps(data), mimetype="application/json")

        @self.app.route("/api/large-post", methods=["POST"])
        def large_post() -> Response:
            """Accept and return ~2MB payload."""
            import time

            data = {
                "id": "large-post-789",
                "data": "x" * (2 * 1024 * 1024),
                "timestamp": int(time.time() * 1000),
            }
            return Response(json.dumps(data), mimetype="application/json")

        @self.app.route("/api/compute-hash", methods=["POST"])
        def compute_hash() -> Response:
            """CPU-intensive endpoint - iterative hashing."""
            data = request.get_json(force=True, silent=True) or {}
            input_data = data.get("data", "default-data")
            iterations = data.get("iterations", 1000)

            hash_val = input_data
            for _ in range(iterations):
                hash_val = hashlib.sha256(hash_val.encode()).hexdigest()

            return Response(
                json.dumps({"hash": hash_val, "iterations": iterations}),
                mimetype="application/json",
            )

        @self.app.route("/api/compute-json", methods=["POST"])
        def compute_json() -> Response:
            """CPU-intensive endpoint - JSON parsing/stringifying."""
            req_data = request.get_json(force=True, silent=True) or {}
            iterations = req_data.get("iterations", 100)
            data = req_data.get("data", {"test": "data", "nested": {"value": 123}})

            result = data
            for i in range(iterations):
                str_data = json.dumps(result)
                result = json.loads(str_data)
                result["iteration"] = i

            return Response(json.dumps(result), mimetype="application/json")

        @self.app.route("/api/auth/login", methods=["POST"])
        def auth_login() -> Response:
            """Endpoint with sensitive data for transform testing."""
            data = request.get_json(force=True, silent=True) or {}
            return Response(
                json.dumps(
                    {
                        "success": True,
                        "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
                        "user": {
                            "id": 123,
                            "email": data.get("email", "user@example.com"),
                            "role": "user",
                        },
                    }
                ),
                mimetype="application/json",
            )

        @self.app.route("/api/users", methods=["POST"])
        def create_user() -> Response:
            """Endpoint with nested sensitive data."""
            import time

            data = request.get_json(force=True, silent=True) or {}
            return Response(
                json.dumps(
                    {
                        "id": int(time.time() * 1000),
                        "username": data.get("username", "testuser"),
                        "profile": {
                            "email": data.get("email", "test@example.com"),
                            "ssn": data.get("ssn", "123-45-6789"),
                            "creditCard": data.get("creditCard", "4111-1111-1111-1111"),
                        },
                        "createdAt": "2025-01-14T00:00:00Z",
                    }
                ),
                mimetype="application/json",
            )

        @self.app.route("/api/io-bound", methods=["POST"])
        def io_bound() -> Response:
            """High IO, low CPU endpoint - simulates IO-bound work."""
            import time

            data = request.get_json(force=True, silent=True) or {}
            jobs = data.get("jobs", 5)
            delay_ms = data.get("delayMs", 5)

            results = []
            for i in range(jobs):
                time.sleep(delay_ms / 1000)
                results.append(
                    {
                        "jobId": i,
                        "timestamp": int(time.time() * 1000),
                        "status": "completed",
                    }
                )

            return Response(
                json.dumps(
                    {
                        "totalJobs": jobs,
                        "results": results,
                        "completedAt": int(time.time() * 1000),
                    }
                ),
                mimetype="application/json",
            )

        @self.app.route("/api/slow")
        def slow() -> Response:
            """Slow endpoint - simulates slow processing."""
            import time

            time.sleep(0.1)
            return Response(
                json.dumps({"message": "Slow response", "timestamp": int(time.time() * 1000)}),
                mimetype="application/json",
            )

        @self.app.route("/api/error")
        def error() -> Response:
            """Error endpoint."""
            return Response(
                json.dumps({"error": "Internal Server Error"}),
                status=500,
                mimetype="application/json",
            )

        @self.app.route("/api/realistic", methods=["POST"])
        def realistic() -> Response:
            """Realistic API endpoint - simulates typical production workload.

            This endpoint simulates a real API that:
            1. Validates input (light CPU)
            2. Queries a database (I/O wait - simulated with sleep)
            3. Processes results (moderate CPU)
            4. Returns response

            Target: ~10-20ms response time (typical for production APIs)
            """
            import re
            import time

            data = request.get_json(force=True, silent=True) or {}

            # 1. Input validation (light CPU work)
            user_id = data.get("userId", "user-123")
            query = data.get("query", "default query")

            # Validate email format if provided
            email = data.get("email", "")
            email_valid = bool(re.match(r"[^@]+@[^@]+\.[^@]+", email)) if email else True

            # 2. Simulate database query (I/O wait ~10ms)
            time.sleep(0.010)

            # 3. Process results (moderate CPU - JSON manipulation)
            results = []
            for i in range(10):
                item = {
                    "id": f"item-{i}",
                    "userId": user_id,
                    "data": {"index": i, "query": query[:50]},
                    "score": (i + 1) * 0.1,
                }
                # Serialize and deserialize (simulates data transformation)
                item_str = json.dumps(item)
                results.append(json.loads(item_str))

            # 4. Build response
            response_data = {
                "success": True,
                "userId": user_id,
                "emailValid": email_valid,
                "resultCount": len(results),
                "results": results,
                "processingTimeMs": 10,  # Approximate
                "timestamp": int(time.time() * 1000),
            }

            return Response(json.dumps(response_data), mimetype="application/json")

        @self.app.route("/api/typical-read", methods=["GET"])
        def typical_read() -> Response:
            """Typical read endpoint - simulates fetching data.

            Simulates: Auth check + DB read + response serialization
            Target: ~5-10ms response time
            """
            import time

            # Simulate auth token validation (light CPU)
            auth_header = request.headers.get("Authorization", "")
            is_authenticated = len(auth_header) > 10

            # Simulate database read (~5ms)
            time.sleep(0.005)

            # Build response
            data = {
                "id": "resource-123",
                "name": "Example Resource",
                "description": "This is a typical API response",
                "metadata": {
                    "createdAt": "2025-01-14T00:00:00Z",
                    "updatedAt": "2025-01-14T12:00:00Z",
                    "version": 1,
                },
                "authenticated": is_authenticated,
                "timestamp": int(time.time() * 1000),
            }

            return Response(json.dumps(data), mimetype="application/json")

        @self.app.route("/api/typical-write", methods=["POST"])
        def typical_write() -> Response:
            """Typical write endpoint - simulates creating/updating data.

            Simulates: Validation + DB write + response
            Target: ~15-25ms response time
            """
            import time

            data = request.get_json(force=True, silent=True) or {}

            # Input validation (light CPU)
            name = data.get("name", "")
            if len(name) < 1:
                name = "default"

            # Simulate database write (~15ms - writes are slower than reads)
            time.sleep(0.015)

            # Build response
            response_data = {
                "id": f"new-{int(time.time() * 1000)}",
                "name": name,
                "status": "created",
                "timestamp": int(time.time() * 1000),
            }

            return Response(json.dumps(response_data), mimetype="application/json")

    def start(self) -> dict[str, Any]:
        """Start the test server in a background thread."""
        from werkzeug.serving import make_server

        # Let make_server bind to port 0 to get an ephemeral port
        # This avoids TOCTOU race where a separate socket finds a free port
        # but another process binds to it before we can
        self._server = make_server(self.host, self.port, self.app, threaded=True)
        self._actual_port = self._server.server_port

        def run_server():
            self._server.serve_forever()

        self._server_thread = threading.Thread(target=run_server, daemon=True)
        self._server_thread.start()

        url = f"http://{self.host}:{self._actual_port}"
        print(f"Test server started at {url}")

        return {
            "port": self._actual_port,
            "host": self.host,
            "url": url,
        }

    def stop(self) -> None:
        """Stop the test server."""
        if hasattr(self, "_server"):
            self._server.shutdown()
        if self._server_thread:
            self._server_thread.join(timeout=5)
        print("Test server stopped")

    def get_url(self) -> str:
        """Get the server URL."""
        return f"http://{self.host}:{self._actual_port}"


if __name__ == "__main__":
    # Quick test
    server = TestServer()
    info = server.start()
    print(f"Server running at {info['url']}")
    input("Press Enter to stop...")
    server.stop()
