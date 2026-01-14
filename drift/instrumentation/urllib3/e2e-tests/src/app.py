"""Flask test app for e2e tests - urllib3 instrumentation testing."""

import json

import urllib3
from flask import Flask, jsonify, request

from drift import TuskDrift

# Initialize SDK
sdk = TuskDrift.initialize(
    api_key="tusk-test-key",
    log_level="debug",
)

app = Flask(__name__)

# Create a shared PoolManager for connection reuse
http = urllib3.PoolManager()


# =============================================================================
# Health Check
# =============================================================================


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy"})


# =============================================================================
# PoolManager Tests (high-level API)
# =============================================================================


@app.route("/api/poolmanager/get-json", methods=["GET"])
def poolmanager_get_json():
    """Test GET request returning JSON using PoolManager."""
    try:
        response = http.request("GET", "https://jsonplaceholder.typicode.com/posts/1")
        data = json.loads(response.data.decode("utf-8"))
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/poolmanager/get-with-params", methods=["GET"])
def poolmanager_get_with_params():
    """Test GET request with query parameters using PoolManager."""
    try:
        response = http.request(
            "GET",
            "https://jsonplaceholder.typicode.com/comments",
            fields={"postId": "1"},
        )
        data = json.loads(response.data.decode("utf-8"))
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/poolmanager/get-with-headers", methods=["GET"])
def poolmanager_get_with_headers():
    """Test GET request with custom headers using PoolManager."""
    try:
        response = http.request(
            "GET",
            "https://jsonplaceholder.typicode.com/posts/1",
            headers={
                "X-Custom-Header": "test-value",
                "Accept": "application/json",
            },
        )
        data = json.loads(response.data.decode("utf-8"))
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/poolmanager/post-json", methods=["POST"])
def poolmanager_post_json():
    """Test POST request with JSON body using PoolManager."""
    try:
        req_data = request.get_json() or {}
        body = json.dumps(
            {
                "title": req_data.get("title", "Test Title"),
                "body": req_data.get("body", "Test Body"),
                "userId": req_data.get("userId", 1),
            }
        )
        response = http.request(
            "POST",
            "https://jsonplaceholder.typicode.com/posts",
            body=body.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        data = json.loads(response.data.decode("utf-8"))
        return jsonify(data), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/poolmanager/post-form", methods=["POST"])
def poolmanager_post_form():
    """Test POST request with form-encoded data using PoolManager."""
    try:
        response = http.request(
            "POST",
            "https://jsonplaceholder.typicode.com/posts",
            fields={
                "title": "Form Title",
                "body": "Form Body",
                "userId": "1",
            },
        )
        data = json.loads(response.data.decode("utf-8"))
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/poolmanager/put-json", methods=["PUT"])
def poolmanager_put_json():
    """Test PUT request with JSON body using PoolManager."""
    try:
        req_data = request.get_json() or {}
        body = json.dumps(
            {
                "id": 1,
                "title": req_data.get("title", "Updated Title"),
                "body": req_data.get("body", "Updated Body"),
                "userId": req_data.get("userId", 1),
            }
        )
        response = http.request(
            "PUT",
            "https://jsonplaceholder.typicode.com/posts/1",
            body=body.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        data = json.loads(response.data.decode("utf-8"))
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/poolmanager/patch-json", methods=["PATCH"])
def poolmanager_patch_json():
    """Test PATCH request with partial JSON body using PoolManager."""
    try:
        req_data = request.get_json() or {}
        body = json.dumps(
            {
                "title": req_data.get("title", "Patched Title"),
            }
        )
        response = http.request(
            "PATCH",
            "https://jsonplaceholder.typicode.com/posts/1",
            body=body.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        data = json.loads(response.data.decode("utf-8"))
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/poolmanager/delete", methods=["DELETE"])
def poolmanager_delete():
    """Test DELETE request using PoolManager."""
    try:
        response = http.request("DELETE", "https://jsonplaceholder.typicode.com/posts/1")
        return jsonify({"status": "deleted", "status_code": response.status})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/poolmanager/chain", methods=["GET"])
def poolmanager_chain():
    """Test sequential chained requests using PoolManager."""
    try:
        # First request: get a user
        user_response = http.request("GET", "https://jsonplaceholder.typicode.com/users/1")
        user = json.loads(user_response.data.decode("utf-8"))

        # Second request: get posts by that user
        posts_response = http.request(
            "GET",
            "https://jsonplaceholder.typicode.com/posts",
            fields={"userId": str(user["id"])},
        )
        posts = json.loads(posts_response.data.decode("utf-8"))

        # Third request: get comments on the first post
        if posts:
            comments_response = http.request(
                "GET",
                f"https://jsonplaceholder.typicode.com/posts/{posts[0]['id']}/comments",
            )
            comments = json.loads(comments_response.data.decode("utf-8"))
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


# =============================================================================
# HTTPConnectionPool Tests (low-level API)
# =============================================================================


@app.route("/api/connectionpool/get-json", methods=["GET"])
def connectionpool_get_json():
    """Test GET request using HTTPConnectionPool directly."""
    pool = None
    try:
        pool = urllib3.HTTPSConnectionPool("jsonplaceholder.typicode.com", port=443)
        response = pool.request("GET", "/posts/2")
        data = json.loads(response.data.decode("utf-8"))
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if pool is not None:
            pool.close()


@app.route("/api/connectionpool/post-json", methods=["POST"])
def connectionpool_post_json():
    """Test POST request using HTTPConnectionPool directly."""
    pool = None
    try:
        req_data = request.get_json() or {}
        body = json.dumps(
            {
                "title": req_data.get("title", "Pool Test Title"),
                "body": req_data.get("body", "Pool Test Body"),
                "userId": req_data.get("userId", 2),
            }
        )
        pool = urllib3.HTTPSConnectionPool("jsonplaceholder.typicode.com", port=443)
        response = pool.request(
            "POST",
            "/posts",
            body=body.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        data = json.loads(response.data.decode("utf-8"))
        return jsonify(data), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if pool is not None:
            pool.close()


# =============================================================================
# Additional Test Cases
# =============================================================================


@app.route("/test/timeout", methods=["GET"])
def test_timeout():
    """Test request with explicit timeout."""
    try:
        response = http.request(
            "GET",
            "https://jsonplaceholder.typicode.com/posts/3",
            timeout=urllib3.Timeout(connect=5.0, read=10.0),
        )
        data = json.loads(response.data.decode("utf-8"))
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/retries", methods=["GET"])
def test_retries():
    """Test request with retry configuration."""
    try:
        response = http.request(
            "GET",
            "https://jsonplaceholder.typicode.com/posts/4",
            retries=urllib3.Retry(total=3, backoff_factor=0.1),
        )
        data = json.loads(response.data.decode("utf-8"))
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/binary-response", methods=["GET"])
def test_binary_response():
    """Test handling of binary response (should be handled gracefully)."""
    try:
        # Fetch a small image
        response = http.request(
            "GET",
            "https://httpbin.org/image/png",
            headers={"Accept": "image/png"},
        )
        return jsonify(
            {
                "status": response.status,
                "content_type": response.headers.get("Content-Type", ""),
                "content_length": len(response.data),
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/redirect", methods=["GET"])
def test_redirect():
    """Test following redirects."""
    try:
        response = http.request(
            "GET",
            "https://httpbin.org/redirect/2",
            redirect=True,
        )
        return jsonify(
            {
                "status": response.status,
                "final_url": response.geturl() if hasattr(response, "geturl") else "unknown",
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/new-poolmanager", methods=["GET"])
def test_new_poolmanager():
    """Test with a fresh PoolManager instance per request."""
    try:
        local_http = urllib3.PoolManager()
        response = local_http.request("GET", "https://jsonplaceholder.typicode.com/posts/5")
        data = json.loads(response.data.decode("utf-8"))
        local_http.clear()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/basic-auth", methods=["GET"])
def test_basic_auth():
    """Test request with basic authentication."""
    try:
        # Create authorization header for basic auth
        import base64

        credentials = base64.b64encode(b"testuser:testpass").decode("ascii")
        headers = urllib3.make_headers(basic_auth="testuser:testpass")

        response = http.request(
            "GET",
            "https://httpbin.org/basic-auth/testuser/testpass",
            headers=headers,
        )
        data = json.loads(response.data.decode("utf-8"))
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/multiple-requests", methods=["GET"])
def test_multiple_requests():
    """Test multiple requests in a single endpoint."""
    try:
        results = []

        # Make three sequential requests
        for i in range(1, 4):
            response = http.request("GET", f"https://jsonplaceholder.typicode.com/posts/{i}")
            data = json.loads(response.data.decode("utf-8"))
            results.append({"id": data["id"], "title": data["title"]})

        return jsonify({"posts": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/requests-lib", methods=["GET"])
def test_requests_lib():
    """Test using requests library (which uses urllib3 internally).

    This test verifies that we don't create double spans when requests
    library is used, since requests uses urllib3 under the hood.
    """
    import requests as requests_lib

    try:
        response = requests_lib.get("https://jsonplaceholder.typicode.com/posts/10")
        return jsonify(response.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =============================================================================
# Bug Detection Tests - Confirmed bugs that expose instrumentation issues
# =============================================================================


@app.route("/test/bug/preload-content-false", methods=["GET"])
def test_bug_preload_content_false():
    """CONFIRMED BUG: preload_content=False parameter breaks response reading.

    When preload_content=False, the response body is not preloaded into memory.
    The instrumentation reads .data in _finalize_span which consumes the body
    before the application can read it.

    Root cause: instrumentation.py line 839 accesses response.data unconditionally
    """
    try:
        response = http.request(
            "GET",
            "https://jsonplaceholder.typicode.com/posts/21",
            preload_content=False,
        )
        # Manually read the data after the response
        data_bytes = response.read()
        response.release_conn()
        data = json.loads(data_bytes.decode("utf-8"))
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/bug/streaming-response", methods=["GET"])
def test_bug_streaming_response():
    """CONFIRMED BUG: Streaming response body is consumed before iteration.

    When using response.stream() to iterate over chunks, the instrumentation
    has already consumed the body by accessing response.data in _finalize_span.

    Root cause: Same as preload-content-false - instrumentation.py line 839
    """
    try:
        response = http.request(
            "GET",
            "https://jsonplaceholder.typicode.com/posts/27",
            preload_content=False,
        )

        # Try to read response in chunks using stream()
        chunks = []
        for chunk in response.stream(32):  # Read 32 bytes at a time
            chunks.append(chunk)

        response.release_conn()
        full_data = b"".join(chunks)
        data = json.loads(full_data.decode("utf-8"))
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    sdk.mark_app_as_ready()
    app.run(host="0.0.0.0", port=8000, debug=False)
