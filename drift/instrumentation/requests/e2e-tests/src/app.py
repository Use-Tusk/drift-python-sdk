"""Flask test app for e2e tests - Requests instrumentation testing."""

from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Flask, jsonify, request
from opentelemetry import context as otel_context

from drift import TuskDrift

# Initialize SDK
sdk = TuskDrift.initialize(
    api_key="tusk-test-key",
    log_level="debug",
)

app = Flask(__name__)


def _run_with_context(ctx, fn, *args, **kwargs):
    """Helper to run a function with OpenTelemetry context in a thread pool."""
    token = otel_context.attach(ctx)
    try:
        return fn(*args, **kwargs)
    finally:
        otel_context.detach(token)


# Health check endpoint
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy"})


# GET request - simple JSON response
@app.route("/api/get-json", methods=["GET"])
def get_json():
    """Test GET request returning JSON."""
    try:
        response = requests.get("https://jsonplaceholder.typicode.com/posts/1")
        return jsonify(response.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# GET request with query parameters
@app.route("/api/get-with-params", methods=["GET"])
def get_with_params():
    """Test GET request with query parameters."""
    try:
        response = requests.get(
            "https://jsonplaceholder.typicode.com/comments",
            params={"postId": 1, "limit": 5},
        )
        return jsonify(response.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# GET request with custom headers
@app.route("/api/get-with-headers", methods=["GET"])
def get_with_headers():
    """Test GET request with custom headers."""
    try:
        response = requests.get(
            "https://httpbin.org/headers",
            headers={
                "X-Custom-Header": "test-value",
                "Accept": "application/json",
            },
        )
        return jsonify(response.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# POST request with JSON body
@app.route("/api/post-json", methods=["POST"])
def post_json():
    """Test POST request with JSON body."""
    try:
        data = request.get_json() or {}
        response = requests.post(
            "https://jsonplaceholder.typicode.com/posts",
            json={
                "title": data.get("title", "Test Title"),
                "body": data.get("body", "Test Body"),
                "userId": data.get("userId", 1),
            },
        )
        return jsonify(response.json()), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# POST request with form data
@app.route("/api/post-form", methods=["POST"])
def post_form():
    """Test POST request with form-encoded data."""
    try:
        response = requests.post(
            "https://httpbin.org/post",
            data={
                "field1": "value1",
                "field2": "value2",
            },
        )
        return jsonify(response.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# PUT request
@app.route("/api/put-json", methods=["PUT"])
def put_json():
    """Test PUT request with JSON body."""
    try:
        data = request.get_json() or {}
        response = requests.put(
            "https://jsonplaceholder.typicode.com/posts/1",
            json={
                "id": 1,
                "title": data.get("title", "Updated Title"),
                "body": data.get("body", "Updated Body"),
                "userId": data.get("userId", 1),
            },
        )
        return jsonify(response.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# PATCH request
@app.route("/api/patch-json", methods=["PATCH"])
def patch_json():
    """Test PATCH request with partial JSON body."""
    try:
        data = request.get_json() or {}
        response = requests.patch(
            "https://jsonplaceholder.typicode.com/posts/1",
            json={"title": data.get("title", "Patched Title")},
        )
        return jsonify(response.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# DELETE request
@app.route("/api/delete", methods=["DELETE"])
def delete_resource():
    """Test DELETE request."""
    try:
        response = requests.delete("https://jsonplaceholder.typicode.com/posts/1")
        return jsonify({"status": "deleted", "status_code": response.status_code})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Sequential chained requests
@app.route("/api/chain", methods=["GET"])
def chain_requests():
    """Test sequential chained requests."""
    try:
        # First request: get a user
        user_response = requests.get("https://jsonplaceholder.typicode.com/users/1")
        user = user_response.json()

        # Second request: get posts by that user
        posts_response = requests.get(
            "https://jsonplaceholder.typicode.com/posts",
            params={"userId": user["id"]},
        )
        posts = posts_response.json()

        # Third request: get comments on the first post
        if posts:
            comments_response = requests.get(f"https://jsonplaceholder.typicode.com/posts/{posts[0]['id']}/comments")
            comments = comments_response.json()
        else:
            comments = []

        return jsonify(
            {
                "user": user,
                "post_count": len(posts),
                "first_post_comments": len(comments),
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Parallel requests with ThreadPoolExecutor
@app.route("/api/parallel", methods=["GET"])
def parallel_requests():
    """Test parallel requests with context propagation."""
    ctx = otel_context.get_current()

    with ThreadPoolExecutor(max_workers=3) as executor:
        # Run three requests in parallel with context propagation
        posts_future = executor.submit(
            _run_with_context,
            ctx,
            requests.get,
            "https://jsonplaceholder.typicode.com/posts/1",
        )
        users_future = executor.submit(
            _run_with_context,
            ctx,
            requests.get,
            "https://jsonplaceholder.typicode.com/users/1",
        )
        comments_future = executor.submit(
            _run_with_context,
            ctx,
            requests.get,
            "https://jsonplaceholder.typicode.com/comments/1",
        )

        posts_response = posts_future.result()
        users_response = users_future.result()
        comments_response = comments_future.result()

    return jsonify(
        {
            "post": posts_response.json(),
            "user": users_response.json(),
            "comment": comments_response.json(),
        }
    )


# Request with timeout
@app.route("/api/with-timeout", methods=["GET"])
def with_timeout():
    """Test request with explicit timeout."""
    try:
        response = requests.get(
            "https://jsonplaceholder.typicode.com/posts/1",
            timeout=10,
        )
        return jsonify(response.json())
    except requests.Timeout:
        return jsonify({"error": "Request timed out"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Multiple content types
@app.route("/api/text-response", methods=["GET"])
def text_response():
    """Test request that returns text/plain."""
    try:
        response = requests.get("https://httpbin.org/robots.txt")
        return jsonify(
            {
                "content": response.text,
                "content_type": response.headers.get("Content-Type"),
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/session-send-direct", methods=["GET"])
def session_send_direct():
    try:
        session = requests.Session()

        # Create a PreparedRequest manually
        req = requests.Request("GET", "https://jsonplaceholder.typicode.com/posts/2")
        prepared = session.prepare_request(req)

        # Call send() directly - bypasses request() method
        response = session.send(prepared, timeout=10)

        return jsonify({"status_code": response.status_code, "data": response.json(), "test": "session-send-direct"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/streaming-iter-lines", methods=["GET"])
def test_streaming_iter_lines():
    """Test streaming response using iter_lines."""
    try:
        response = requests.get(
            "https://httpbin.org/stream/5",  # Returns 5 JSON lines
            stream=True,
            timeout=10,
        )
        # Consume the stream using iter_lines
        lines = []
        for line in response.iter_lines():
            if line:
                lines.append(line.decode("utf-8"))

        return jsonify(
            {"test": "streaming-iter-lines", "status_code": response.status_code, "lines_received": len(lines)}
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/response-hooks", methods=["GET"])
def test_response_hooks():
    """Test response hooks that modify response."""
    try:
        hook_called = {"value": False}
        hook_status = {"value": 0}

        def response_hook(response, *args, **kwargs):
            hook_called["value"] = True
            hook_status["value"] = response.status_code
            return response

        response = requests.get("https://httpbin.org/get", hooks={"response": response_hook}, timeout=10)

        return jsonify(
            {
                "test": "response-hooks",
                "status_code": response.status_code,
                "hook_called": hook_called["value"],
                "hook_status": hook_status["value"],
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    sdk.mark_app_as_ready()
    app.run(host="0.0.0.0", port=8000, debug=False)
