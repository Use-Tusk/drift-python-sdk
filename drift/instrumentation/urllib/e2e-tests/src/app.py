"""Flask test app for e2e tests - urllib.request instrumentation testing."""

import json
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request, build_opener, urlopen

from flask import Flask, jsonify
from flask import request as flask_request
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


# GET request - simple urlopen with string URL
@app.route("/api/get-json", methods=["GET"])
def get_json():
    """Test basic GET request using urlopen with string URL."""
    try:
        with urlopen("https://jsonplaceholder.typicode.com/posts/1", timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# GET request using Request object with custom headers
@app.route("/api/get-with-request-object", methods=["GET"])
def get_with_request_object():
    """Test GET request using Request object with custom headers."""
    try:
        req = Request(
            "https://jsonplaceholder.typicode.com/posts/1",
            headers={
                "Accept": "application/json",
                "User-Agent": "urllib-test/1.0",
                "X-Custom-Header": "test-value",
            },
        )
        with urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# GET request with query parameters in URL
@app.route("/api/get-with-params", methods=["GET"])
def get_with_params():
    """Test GET request with query parameters."""
    try:
        with urlopen("https://jsonplaceholder.typicode.com/comments?postId=1", timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            return jsonify({"count": len(data), "first": data[0] if data else None})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# POST request with JSON body
@app.route("/api/post-json", methods=["POST"])
def post_json():
    """Test POST request with JSON body."""
    try:
        post_data = flask_request.get_json() or {}
        body = json.dumps(
            {
                "title": post_data.get("title", "Test Title"),
                "body": post_data.get("body", "Test Body"),
                "userId": post_data.get("userId", 1),
            }
        ).encode("utf-8")

        req = Request(
            "https://jsonplaceholder.typicode.com/posts",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            return jsonify(data), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# POST request with form-encoded data
@app.route("/api/post-form", methods=["POST"])
def post_form():
    """Test POST request with form-encoded data."""
    try:
        from urllib.parse import urlencode

        body = urlencode({"field1": "value1", "field2": "value2"}).encode("utf-8")
        req = Request(
            "https://httpbin.org/post",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# PUT request
@app.route("/api/put-json", methods=["PUT"])
def put_json():
    """Test PUT request with JSON body."""
    try:
        body = json.dumps(
            {
                "id": 1,
                "title": "Updated Title",
                "body": "Updated Body",
                "userId": 1,
            }
        ).encode("utf-8")

        req = Request(
            "https://jsonplaceholder.typicode.com/posts/1",
            data=body,
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        with urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# PATCH request
@app.route("/api/patch-json", methods=["PATCH"])
def patch_json():
    """Test PATCH request with partial JSON body."""
    try:
        body = json.dumps({"title": "Patched Title"}).encode("utf-8")

        req = Request(
            "https://jsonplaceholder.typicode.com/posts/1",
            data=body,
            headers={"Content-Type": "application/json"},
            method="PATCH",
        )
        with urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# DELETE request
@app.route("/api/delete", methods=["DELETE"])
def delete_resource():
    """Test DELETE request."""
    try:
        req = Request(
            "https://jsonplaceholder.typicode.com/posts/1",
            method="DELETE",
        )
        with urlopen(req, timeout=10) as response:
            return jsonify({"status": "deleted", "status_code": response.status})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Sequential chained requests
@app.route("/api/chain", methods=["GET"])
def chain_requests():
    """Test sequential chained requests."""
    try:
        # First request: get a user
        with urlopen("https://jsonplaceholder.typicode.com/users/1", timeout=10) as response:
            user = json.loads(response.read().decode("utf-8"))

        # Second request: get posts by that user
        with urlopen(f"https://jsonplaceholder.typicode.com/posts?userId={user['id']}", timeout=10) as response:
            posts = json.loads(response.read().decode("utf-8"))

        # Third request: get comments on the first post
        if posts:
            with urlopen(
                f"https://jsonplaceholder.typicode.com/posts/{posts[0]['id']}/comments", timeout=10
            ) as response:
                comments = json.loads(response.read().decode("utf-8"))
        else:
            comments = []

        return jsonify(
            {
                "user": user["name"],
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

    def fetch_url(url):
        with urlopen(url, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    with ThreadPoolExecutor(max_workers=3) as executor:
        # Run three requests in parallel with context propagation
        posts_future = executor.submit(
            _run_with_context,
            ctx,
            fetch_url,
            "https://jsonplaceholder.typicode.com/posts/1",
        )
        users_future = executor.submit(
            _run_with_context,
            ctx,
            fetch_url,
            "https://jsonplaceholder.typicode.com/users/1",
        )
        comments_future = executor.submit(
            _run_with_context,
            ctx,
            fetch_url,
            "https://jsonplaceholder.typicode.com/comments/1",
        )

        post = posts_future.result()
        user = users_future.result()
        comment = comments_future.result()

    return jsonify(
        {
            "post": post,
            "user": user,
            "comment": comment,
        }
    )


# Request with explicit timeout
@app.route("/api/with-timeout", methods=["GET"])
def with_timeout():
    """Test request with explicit timeout."""
    try:
        with urlopen("https://jsonplaceholder.typicode.com/posts/1", timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            return jsonify(data)
    except TimeoutError:
        return jsonify({"error": "Request timed out"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Custom opener usage via build_opener
@app.route("/api/custom-opener", methods=["GET"])
def custom_opener():
    """Test custom opener created via build_opener()."""
    try:
        from urllib.request import HTTPHandler

        opener = build_opener(HTTPHandler())
        with opener.open("https://jsonplaceholder.typicode.com/posts/1", timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Text response handling
@app.route("/api/text-response", methods=["GET"])
def text_response():
    """Test request that returns text/plain."""
    try:
        with urlopen("https://httpbin.org/robots.txt", timeout=10) as response:
            content = response.read().decode("utf-8")
            headers = dict(response.info().items())
            return jsonify(
                {
                    "content": content,
                    "content_type": headers.get("Content-Type"),
                }
            )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Request with urlopen data parameter (implicit POST)
@app.route("/api/urlopen-with-data", methods=["POST"])
def urlopen_with_data():
    """Test urlopen with data parameter (creates implicit POST)."""
    try:
        body = json.dumps({"test": "value"}).encode("utf-8")
        # Using urlopen with data parameter makes it a POST request
        req = Request(
            "https://httpbin.org/post",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(req, data=body, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    sdk.mark_app_as_ready()
    app.run(host="0.0.0.0", port=8000, debug=False)
