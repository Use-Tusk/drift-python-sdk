#!/usr/bin/env python3
"""Standalone mock upstream server for e2e/benchmark HTTP dependencies."""

from __future__ import annotations

import gzip
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


def _json(handler: BaseHTTPRequestHandler, payload: Any, status: int = 200):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _json_gzip(handler: BaseHTTPRequestHandler, payload: Any, status: int = 200):
    """Serve JSON compressed with gzip, setting Content-Encoding: gzip."""
    body = gzip.compress(json.dumps(payload).encode("utf-8"))
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Encoding", "gzip")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _text(handler: BaseHTTPRequestHandler, payload: str, status: int = 200):
    body = payload.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _bytes(handler: BaseHTTPRequestHandler, payload: bytes, content_type: str, status: int = 200):
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _status(handler: BaseHTTPRequestHandler, status: int):
    handler.send_response(status)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def _normalize_path(path: str) -> str:
    # Legacy endpoints used by the original django/fastapi/flask mocks.
    if path.startswith("/api/posts"):
        return path.replace("/api/posts", "/posts", 1)
    if path == "/api/random-activity":
        return "/random"
    if path == "/api/ip-api/json":
        return "/json/"
    if path == "/api/httpbin/get":
        return "/get"
    if path in {"/api/randomuser", "/api/randomuser/"}:
        return "/api/"
    return path


def _extract_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    raw_body = handler.rfile.read(length).decode("utf-8") if length else "{}"
    try:
        return json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        return {}


def _mock_post(post_id: int) -> dict[str, Any]:
    return {
        "id": post_id,
        "title": f"Mock Post {post_id}",
        "body": f"Body for post {post_id}",
        "userId": ((post_id - 1) % 10) + 1,
    }


def _mock_user(user_id: int) -> dict[str, Any]:
    return {
        "id": user_id,
        "name": f"User {user_id}",
        "username": f"user{user_id}",
        "email": f"user{user_id}@example.com",
    }


_SAMPLE_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc````\x00\x00"
    b"\x00\x04\x00\x01\xf6\x177U\x00\x00\x00\x00IEND\xaeB`\x82"
)


class MockHandler(BaseHTTPRequestHandler):
    server_version = "mock-upstream/1.0"

    def log_message(self, format: str, *args):
        # Keep benchmark output clean.
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        path = _normalize_path(parsed.path)
        query = parse_qs(parsed.query)

        if path == "/health":
            return _json(self, {"status": "ok"})

        if path == "/v1/forecast":
            return _json(
                self,
                {
                    "latitude": 40.7128,
                    "longitude": -74.0060,
                    "current_weather": {
                        "temperature": 22.1,
                        "windspeed": 9.8,
                        "weathercode": 1,
                        "time": "2024-01-01T12:00",
                    },
                },
            )

        if path in {"/api/", "/api/randomuser", "/api/randomuser/"}:
            seed = query.get("seed", ["default-seed"])[0]
            return _json(
                self,
                {
                    "results": [
                        {
                            "login": {"uuid": f"uuid-{seed}"},
                            "name": {"first": "Test", "last": "User"},
                            "email": f"{seed}@example.com",
                        }
                    ],
                    "info": {"seed": seed, "results": 1, "version": "1.0"},
                },
            )

        if path == "/random":
            return _json(
                self,
                {
                    "activity": "Take a short walk",
                    "type": "relaxation",
                    "participants": 1,
                },
            )

        if path == "/gzip":
            return _json_gzip(
                self,
                {"gzipped": True, "method": "GET", "origin": "mock"},
            )

        if path in {"/json", "/json/"}:
            return _json(
                self,
                {
                    "city": "New York",
                    "lat": 40.7128,
                    "lon": -74.0060,
                    "country": "United States",
                },
            )

        if path == "/get":
            flat_query = {k: v[0] if v else "" for k, v in query.items()}
            return _json(
                self,
                {
                    "args": flat_query,
                    "url": f"http://mock-upstream:8081{path}" + (f"?{parsed.query}" if parsed.query else ""),
                    "headers": dict(self.headers.items()),
                },
            )

        if path == "/headers":
            return _json(self, {"headers": dict(self.headers.items())})

        if path.startswith("/stream/"):
            count = int(path.split("/")[-1] or "5")
            lines = [json.dumps({"id": i, "message": f"line-{i}"}) for i in range(1, count + 1)]
            return _text(self, "\n".join(lines))

        if path == "/robots.txt":
            return _text(self, "User-agent: *\nDisallow: /deny\n")

        if path.startswith("/redirect/"):
            remaining = int(path.split("/")[-1] or "0")
            next_location = "/get" if remaining <= 1 else f"/redirect/{remaining - 1}"
            self.send_response(302)
            self.send_header("Location", next_location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path.startswith("/status/"):
            status_code = int(path.split("/")[-1] or "200")
            return _status(self, status_code)

        if path == "/image/png":
            return _bytes(self, _SAMPLE_PNG, content_type="image/png")

        if path.startswith("/basic-auth/") or path.startswith("/digest-auth/"):
            return _json(self, {"authenticated": True, "user": "testuser"})

        if path.startswith("/posts/") and path.endswith("/comments"):
            post_id = path.split("/")[2]
            return _json(
                self,
                [
                    {"id": 1, "postId": int(post_id), "body": f"Comment A for {post_id}"},
                    {"id": 2, "postId": int(post_id), "body": f"Comment B for {post_id}"},
                ],
            )

        if path.startswith("/posts/"):
            post_id = int(path.split("/")[2])
            return _json(self, _mock_post(post_id))

        if path == "/posts":
            user_id = int(query.get("userId", ["1"])[0])
            posts = [_mock_post(i) for i in range(1, 6) if _mock_post(i)["userId"] == user_id]
            return _json(self, posts)

        if path.startswith("/comments/"):
            comment_id = int(path.split("/")[2])
            return _json(self, {"id": comment_id, "postId": 1, "body": f"Comment {comment_id}"})

        if path == "/comments":
            post_id = int(query.get("postId", ["1"])[0])
            comments = [{"id": i, "postId": post_id, "body": f"Comment {i} for post {post_id}"} for i in range(1, 6)]
            return _json(self, comments)

        if path.startswith("/users/"):
            user_id = int(path.split("/")[2])
            return _json(self, _mock_user(user_id))

        return _json(self, {"error": f"No mock route for {path}"}, status=404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = _normalize_path(parsed.path)

        if path == "/posts":
            payload = _extract_json_body(self)
            response = {
                "id": 101,
                "title": payload.get("title", ""),
                "body": payload.get("body", ""),
                "userId": payload.get("userId", 1),
            }
            return _json(self, response, status=201)

        if path == "/post":
            # Mimic enough of httpbin's response format for tests.
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length else b""
            return _json(
                self,
                {
                    "args": {},
                    "data": raw_body.decode("utf-8", errors="replace"),
                    "files": {"file": "mock-upload-content"},
                    "form": {},
                    "headers": dict(self.headers.items()),
                    "json": None,
                    "url": "http://mock-upstream:8081/post",
                },
            )

        return _json(self, {"error": f"No mock route for {path}"}, status=404)

    def do_PUT(self):
        parsed = urlparse(self.path)
        path = _normalize_path(parsed.path)
        if path.startswith("/posts/"):
            post_id = int(path.split("/")[2])
            payload = _extract_json_body(self)
            payload.setdefault("id", post_id)
            payload.setdefault("userId", 1)
            return _json(self, payload)
        return _json(self, {"error": f"No mock route for {path}"}, status=404)

    def do_PATCH(self):
        parsed = urlparse(self.path)
        path = _normalize_path(parsed.path)
        if path.startswith("/posts/"):
            post_id = int(path.split("/")[2])
            payload = _extract_json_body(self)
            payload["id"] = post_id
            payload.setdefault("userId", 1)
            return _json(self, payload)
        return _json(self, {"error": f"No mock route for {path}"}, status=404)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = _normalize_path(parsed.path)
        if path.startswith("/posts/"):
            post_id = path.split("/")[2]
            return _json(self, {"message": f"Post {post_id} deleted"})
        return _json(self, {"error": f"No mock route for {path}"}, status=404)

    def do_OPTIONS(self):
        parsed = urlparse(self.path)
        path = _normalize_path(parsed.path)
        if path in {"/get", "/posts", "/post"}:
            self.send_response(200)
            self.send_header("Allow", "GET,POST,PUT,PATCH,DELETE,HEAD,OPTIONS")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return _json(self, {"error": f"No mock route for {path}"}, status=404)

    def do_HEAD(self):
        parsed = urlparse(self.path)
        path = _normalize_path(parsed.path)
        if path.startswith("/posts/"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        _status(self, 404)


def main():
    port = int(os.getenv("MOCK_UPSTREAM_PORT", "8081"))
    server = ThreadingHTTPServer(("0.0.0.0", port), MockHandler)
    print(f"Mock upstream listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
