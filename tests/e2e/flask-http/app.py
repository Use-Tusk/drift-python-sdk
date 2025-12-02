import os
import sys
import time

sys.path.insert(
    0,
    os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    ),
)

from drift import TuskDrift

sdk = TuskDrift.initialize()

import requests as http_requests
from flask import Flask, jsonify, request

app = Flask(__name__)


@app.route("/health")
def health():
    """Health check endpoint."""
    return jsonify({"status": "healthy", "timestamp": time.time()})


@app.route("/test-http-get")
def test_http_get():
    """Test outbound HTTP GET request."""
    response = http_requests.get("https://jsonplaceholder.typicode.com/posts/1")
    return jsonify(
        {
            "endpoint": "/test-http-get",
            "result": response.json(),
        }
    )


@app.route("/test-http-post", methods=["POST"])
def test_http_post():
    """Test outbound HTTP POST request."""
    payload = request.get_json() or {"title": "test", "body": "test body", "userId": 1}
    response = http_requests.post(
        "https://jsonplaceholder.typicode.com/posts",
        json=payload,
    )
    return jsonify(
        {
            "endpoint": "/test-http-post",
            "result": response.json(),
        }
    )


@app.route("/test-http-put", methods=["PUT"])
def test_http_put():
    """Test outbound HTTP PUT request."""
    payload = request.get_json() or {
        "id": 1,
        "title": "updated",
        "body": "updated body",
        "userId": 1,
    }
    response = http_requests.put(
        "https://jsonplaceholder.typicode.com/posts/1",
        json=payload,
    )
    return jsonify(
        {
            "endpoint": "/test-http-put",
            "result": response.json(),
        }
    )


@app.route("/test-http-delete", methods=["DELETE"])
def test_http_delete():
    """Test outbound HTTP DELETE request."""
    response = http_requests.delete("https://jsonplaceholder.typicode.com/posts/1")
    return jsonify(
        {
            "endpoint": "/test-http-delete",
            "status_code": response.status_code,
        }
    )


@app.route("/test-http-headers")
def test_http_headers():
    """Test outbound HTTP request with custom headers."""
    response = http_requests.get(
        "https://httpbin.org/headers",
        headers={
            "X-Custom-Header": "test-value",
            "X-Request-Id": "e2e-test-123",
        },
    )
    return jsonify(
        {
            "endpoint": "/test-http-headers",
            "result": response.json(),
        }
    )


@app.route("/test-http-query-params")
def test_http_query_params():
    """Test outbound HTTP request with query parameters."""
    response = http_requests.get(
        "https://jsonplaceholder.typicode.com/posts",
        params={"userId": 1, "_limit": 3},
    )
    return jsonify(
        {
            "endpoint": "/test-http-query-params",
            "result": response.json(),
        }
    )


@app.route("/test-http-error")
def test_http_error():
    """Test outbound HTTP request that returns an error."""
    try:
        response = http_requests.get("https://httpbin.org/status/404")
        return jsonify(
            {
                "endpoint": "/test-http-error",
                "status_code": response.status_code,
                "error": True,
            }
        )
    except Exception as e:
        return jsonify(
            {
                "endpoint": "/test-http-error",
                "error": str(e),
            }
        ), 500


@app.route("/test-chained-requests")
def test_chained_requests():
    """Test multiple chained outbound requests."""
    # First request: get a user
    user_response = http_requests.get("https://jsonplaceholder.typicode.com/users/1")
    user = user_response.json()

    # Second request: get posts by that user
    posts_response = http_requests.get(
        "https://jsonplaceholder.typicode.com/posts",
        params={"userId": user["id"], "_limit": 2},
    )
    posts = posts_response.json()

    return jsonify(
        {
            "endpoint": "/test-chained-requests",
            "user": user,
            "posts": posts,
        }
    )


@app.route("/greet/<name>")
def greet(name: str):
    """Greet endpoint with path parameter."""
    greeting = request.args.get("greeting", "Hello")
    return jsonify(
        {
            "message": f"{greeting}, {name}!",
            "name": name,
        }
    )


@app.route("/echo", methods=["POST"])
def echo():
    """Echo back the request body."""
    data = request.get_json()
    return jsonify(
        {
            "echoed": data,
            "received_at": time.time(),
        }
    )


def main():
    port = int(os.environ.get("PORT", 5000))
    sdk.mark_app_as_ready()
    print(f"Server running on port {port}")
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
