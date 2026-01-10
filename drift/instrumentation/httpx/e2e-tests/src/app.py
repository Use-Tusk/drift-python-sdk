"""Flask test app for e2e tests - HTTPX instrumentation testing (sync and async)."""

import asyncio

import httpx
from flask import Flask, jsonify, request

from drift import TuskDrift

# Initialize SDK
sdk = TuskDrift.initialize(
    api_key="tusk-test-key",
    log_level="debug",
)

app = Flask(__name__)


# =============================================================================
# Health Check
# =============================================================================


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy"})


# =============================================================================
# Sync Client Tests (httpx.Client)
# =============================================================================


@app.route("/api/sync/get-json", methods=["GET"])
def sync_get_json():
    """Test sync GET request returning JSON."""
    try:
        with httpx.Client() as client:
            response = client.get("https://jsonplaceholder.typicode.com/posts/1")
            return jsonify(response.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sync/get-with-params", methods=["GET"])
def sync_get_with_params():
    """Test sync GET request with query parameters."""
    try:
        with httpx.Client() as client:
            response = client.get(
                "https://jsonplaceholder.typicode.com/comments",
                params={"postId": 1},
            )
            return jsonify(response.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sync/get-with-headers", methods=["GET"])
def sync_get_with_headers():
    """Test sync GET request with custom headers."""
    try:
        with httpx.Client() as client:
            response = client.get(
                "https://jsonplaceholder.typicode.com/posts/1",
                headers={
                    "X-Custom-Header": "test-value",
                    "Accept": "application/json",
                },
            )
            return jsonify(response.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sync/post-json", methods=["POST"])
def sync_post_json():
    """Test sync POST request with JSON body."""
    try:
        data = request.get_json() or {}
        with httpx.Client() as client:
            response = client.post(
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


@app.route("/api/sync/post-form", methods=["POST"])
def sync_post_form():
    """Test sync POST request with form-encoded data (using content parameter)."""
    try:
        with httpx.Client() as client:
            # Use jsonplaceholder which returns deterministic responses
            # Send form data as content with explicit content-type
            response = client.post(
                "https://jsonplaceholder.typicode.com/posts",
                content="title=Form+Title&body=Form+Body&userId=1",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            return jsonify(response.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sync/put-json", methods=["PUT"])
def sync_put_json():
    """Test sync PUT request with JSON body."""
    try:
        data = request.get_json() or {}
        with httpx.Client() as client:
            response = client.put(
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


@app.route("/api/sync/patch-json", methods=["PATCH"])
def sync_patch_json():
    """Test sync PATCH request with partial JSON body."""
    try:
        data = request.get_json() or {}
        with httpx.Client() as client:
            response = client.patch(
                "https://jsonplaceholder.typicode.com/posts/1",
                json={"title": data.get("title", "Patched Title")},
            )
            return jsonify(response.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sync/delete", methods=["DELETE"])
def sync_delete():
    """Test sync DELETE request."""
    try:
        with httpx.Client() as client:
            response = client.delete("https://jsonplaceholder.typicode.com/posts/1")
            return jsonify({"status": "deleted", "status_code": response.status_code})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sync/chain", methods=["GET"])
def sync_chain():
    """Test sync sequential chained requests."""
    try:
        with httpx.Client() as client:
            # First request: get a user
            user_response = client.get("https://jsonplaceholder.typicode.com/users/1")
            user = user_response.json()

            # Second request: get posts by that user
            posts_response = client.get(
                "https://jsonplaceholder.typicode.com/posts",
                params={"userId": user["id"]},
            )
            posts = posts_response.json()

            # Third request: get comments on the first post
            if posts:
                comments_response = client.get(f"https://jsonplaceholder.typicode.com/posts/{posts[0]['id']}/comments")
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


# =============================================================================
# Async Client Tests (httpx.AsyncClient)
# =============================================================================


@app.route("/api/async/get-json", methods=["GET"])
def async_get_json():
    """Test async GET request returning JSON."""

    async def fetch():
        async with httpx.AsyncClient() as client:
            response = await client.get("https://jsonplaceholder.typicode.com/posts/2")
            return response.json()

    try:
        result = asyncio.run(fetch())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/async/get-with-params", methods=["GET"])
def async_get_with_params():
    """Test async GET request with query parameters."""

    async def fetch():
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://jsonplaceholder.typicode.com/comments",
                params={"postId": 2},
            )
            return response.json()

    try:
        result = asyncio.run(fetch())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/async/post-json", methods=["POST"])
def async_post_json():
    """Test async POST request with JSON body."""

    async def fetch():
        data = request.get_json() or {}
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://jsonplaceholder.typicode.com/posts",
                json={
                    "title": data.get("title", "Async Test Title"),
                    "body": data.get("body", "Async Test Body"),
                    "userId": data.get("userId", 2),
                },
            )
            return response.json()

    try:
        result = asyncio.run(fetch())
        return jsonify(result), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/async/put-json", methods=["PUT"])
def async_put_json():
    """Test async PUT request with JSON body."""

    async def fetch():
        data = request.get_json() or {}
        async with httpx.AsyncClient() as client:
            response = await client.put(
                "https://jsonplaceholder.typicode.com/posts/2",
                json={
                    "id": 2,
                    "title": data.get("title", "Async Updated Title"),
                    "body": data.get("body", "Async Updated Body"),
                    "userId": data.get("userId", 2),
                },
            )
            return response.json()

    try:
        result = asyncio.run(fetch())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/async/delete", methods=["DELETE"])
def async_delete():
    """Test async DELETE request."""

    async def fetch():
        async with httpx.AsyncClient() as client:
            response = await client.delete("https://jsonplaceholder.typicode.com/posts/2")
            return {"status": "deleted", "status_code": response.status_code}

    try:
        result = asyncio.run(fetch())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/async/parallel", methods=["GET"])
def async_parallel():
    """Test parallel async requests using asyncio.gather."""

    async def fetch():
        async with httpx.AsyncClient() as client:
            # Run three requests in parallel
            posts_task = client.get("https://jsonplaceholder.typicode.com/posts/1")
            users_task = client.get("https://jsonplaceholder.typicode.com/users/1")
            comments_task = client.get("https://jsonplaceholder.typicode.com/comments/1")

            posts_response, users_response, comments_response = await asyncio.gather(
                posts_task, users_task, comments_task
            )

            return {
                "post": posts_response.json(),
                "user": users_response.json(),
                "comment": comments_response.json(),
            }

    try:
        result = asyncio.run(fetch())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/async/chain", methods=["GET"])
def async_chain():
    """Test async sequential chained requests."""

    async def fetch():
        async with httpx.AsyncClient() as client:
            # First request: get a user
            user_response = await client.get("https://jsonplaceholder.typicode.com/users/2")
            user = user_response.json()

            # Second request: get posts by that user
            posts_response = await client.get(
                "https://jsonplaceholder.typicode.com/posts",
                params={"userId": user["id"]},
            )
            posts = posts_response.json()

            # Third request: get comments on the first post
            if posts:
                comments_response = await client.get(
                    f"https://jsonplaceholder.typicode.com/posts/{posts[0]['id']}/comments"
                )
                comments = comments_response.json()
            else:
                comments = []

            return {
                "user": user,
                "post_count": len(posts),
                "first_post_comments": len(comments),
            }

    try:
        result = asyncio.run(fetch())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/streaming", methods=["GET"])
def test_streaming():
    """Test 5: Streaming response using client.stream() context manager."""
    try:
        with httpx.Client() as client:
            with client.stream("GET", "https://jsonplaceholder.typicode.com/posts/6") as response:
                # Read the streaming response
                content = response.read()
                data = response.json()
                return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/toplevel-stream", methods=["GET"])
def test_toplevel_stream():
    """Test 6: Top-level httpx.stream() context manager."""
    try:
        with httpx.stream("GET", "https://jsonplaceholder.typicode.com/posts/7") as response:
            content = response.read()
            data = response.json()
            return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/multipart-files", methods=["POST"])
def test_multipart_files():
    """Test 7: Multipart file upload using files= parameter."""
    try:
        # Create in-memory file-like content
        files = {"file": ("test.txt", b"Hello, World!", "text/plain")}
        with httpx.Client() as client:
            # Use httpbin.org which echoes back file uploads
            response = client.post(
                "https://httpbin.org/post",
                files=files,
            )
            result = response.json()
            return jsonify({"uploaded": True, "files": result.get("files", {})})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/follow-redirects", methods=["GET"])
def test_follow_redirects():
    """Test 12: Following HTTP redirects."""
    try:
        with httpx.Client(follow_redirects=True) as client:
            # httpbin.org/redirect/2 will redirect twice before returning
            response = client.get("https://httpbin.org/redirect/2")
            return jsonify(
                {
                    "final_url": str(response.url),
                    "status_code": response.status_code,
                    "redirect_count": len(response.history),
                }
            )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/async-send", methods=["GET"])
def test_async_send():
    """Test 14: AsyncClient.send() method - bypasses AsyncClient.request()."""

    async def fetch():
        async with httpx.AsyncClient() as client:
            req = client.build_request("GET", "https://jsonplaceholder.typicode.com/posts/10")
            response = await client.send(req)
            return response.json()

    try:
        result = asyncio.run(fetch())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/async-stream", methods=["GET"])
def test_async_stream():
    """Test 15: Async streaming response using AsyncClient.stream()."""

    async def fetch():
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", "https://jsonplaceholder.typicode.com/posts/11") as response:
                await response.aread()
                return response.json()

    try:
        result = asyncio.run(fetch())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/basic-auth", methods=["GET"])
def test_basic_auth():
    try:
        with httpx.Client() as client:
            # httpbin.org/basic-auth/{user}/{passwd} returns 200 if auth succeeds
            response = client.get(
                "https://httpbin.org/basic-auth/testuser/testpass",
                auth=("testuser", "testpass"),
            )
            return jsonify(response.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/event-hooks", methods=["GET"])
def test_event_hooks():
    try:
        request_headers_captured = []
        response_headers_captured = []

        def log_request(request):
            request_headers_captured.append(dict(request.headers))
            request.headers["X-Hook-Added"] = "true"

        def log_response(response):
            response_headers_captured.append(dict(response.headers))

        with httpx.Client(event_hooks={"request": [log_request], "response": [log_response]}) as client:
            response = client.get("https://httpbin.org/headers")
            result = response.json()
            return jsonify(
                {
                    "hook_header_present": "X-Hook-Added" in result.get("headers", {}),
                    "request_captured": len(request_headers_captured) > 0,
                    "response_captured": len(response_headers_captured) > 0,
                }
            )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/response-hook-only", methods=["GET"])
def test_response_hook_only():
    try:
        response_data_captured = []

        def capture_response(response):
            response_data_captured.append(
                {
                    "status": response.status_code,
                    "url": str(response.url),
                }
            )

        with httpx.Client(event_hooks={"response": [capture_response]}) as client:
            response = client.get("https://httpbin.org/get")
            return jsonify(
                {
                    "captured": len(response_data_captured) > 0,
                    "response_status": response.status_code,
                }
            )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/request-hook-modify-url", methods=["GET"])
def test_request_hook_modify_url():
    try:

        def add_query_param(request):
            request.headers["X-Hook-Tried-Url-Modify"] = "true"
            return request

        with httpx.Client(event_hooks={"request": [add_query_param]}) as client:
            response = client.get("https://httpbin.org/get?original=param")
            result = response.json()
            return jsonify(
                {
                    "headers": result.get("headers", {}),
                    "args": result.get("args", {}),
                }
            )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/digest-auth", methods=["GET"])
def test_digest_auth():
    try:
        with httpx.Client() as client:
            auth = httpx.DigestAuth("digestuser", "digestpass")
            response = client.get(
                "https://httpbin.org/digest-auth/auth/digestuser/digestpass",
                auth=auth,
            )
            return jsonify(response.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/test/async-hooks", methods=["GET"])
def test_async_hooks():
    """Test: AsyncClient with async event hooks."""

    async def fetch():
        request_count = [0]
        response_count = [0]

        async def async_request_hook(request):
            request_count[0] += 1
            request.headers["X-Async-Hook"] = "true"

        async def async_response_hook(response):
            response_count[0] += 1

        async with httpx.AsyncClient(
            event_hooks={
                "request": [async_request_hook],
                "response": [async_response_hook],
            }
        ) as client:
            response = await client.get("https://httpbin.org/headers")
            result = response.json()
            return {
                "request_hook_called": request_count[0] > 0,
                "response_hook_called": response_count[0] > 0,
                "async_hook_header_present": "X-Async-Hook" in result.get("headers", {}),
            }

    try:
        result = asyncio.run(fetch())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# =============================================================================
# Bug Hunting Tests - Confirmed Instrumentation Bugs
# =============================================================================

@app.route("/test/file-like-body", methods=["POST"])
def test_file_like_body():
    """Test: Request body from file-like object (BytesIO)."""
    try:
        import io
        file_content = b'{"title": "File Body", "body": "From BytesIO", "userId": 1}'
        file_obj = io.BytesIO(file_content)

        with httpx.Client() as client:
            response = client.post(
                "https://httpbin.org/post",
                content=file_obj,
                headers={"Content-Type": "application/json"},
            )
            result = response.json()
            return jsonify({
                "posted_data": result.get("data", ""),
                "content_type": result.get("headers", {}).get("Content-Type", ""),
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    sdk.mark_app_as_ready()
    app.run(host="0.0.0.0", port=8000, debug=False)
