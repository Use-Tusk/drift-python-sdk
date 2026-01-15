"""Flask test app for e2e tests - aiohttp instrumentation testing."""

import asyncio

import aiohttp
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
# Basic HTTP Methods (GET, POST, PUT, PATCH, DELETE)
# =============================================================================


@app.route("/api/get-json", methods=["GET"])
def get_json():
    """Test GET request returning JSON."""

    async def fetch():
        async with aiohttp.ClientSession() as session:
            async with session.get("https://jsonplaceholder.typicode.com/posts/1") as response:
                return await response.json()

    try:
        result = asyncio.run(fetch())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/get-with-params", methods=["GET"])
def get_with_params():
    """Test GET request with query parameters."""

    async def fetch():
        async with aiohttp.ClientSession() as session:
            params = {"postId": 1}
            async with session.get(
                "https://jsonplaceholder.typicode.com/comments",
                params=params,
            ) as response:
                return await response.json()

    try:
        result = asyncio.run(fetch())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/get-with-headers", methods=["GET"])
def get_with_headers():
    """Test GET request with custom headers."""

    async def fetch():
        async with aiohttp.ClientSession() as session:
            headers = {
                "X-Custom-Header": "test-value",
                "Accept": "application/json",
            }
            async with session.get(
                "https://jsonplaceholder.typicode.com/posts/1",
                headers=headers,
            ) as response:
                return await response.json()

    try:
        result = asyncio.run(fetch())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/post-json", methods=["POST"])
def post_json():
    """Test POST request with JSON body."""

    async def fetch():
        data = request.get_json() or {}
        async with aiohttp.ClientSession() as session:
            payload = {
                "title": data.get("title", "Test Title"),
                "body": data.get("body", "Test Body"),
                "userId": data.get("userId", 1),
            }
            async with session.post(
                "https://jsonplaceholder.typicode.com/posts",
                json=payload,
            ) as response:
                return await response.json()

    try:
        result = asyncio.run(fetch())
        return jsonify(result), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/post-form", methods=["POST"])
def post_form():
    """Test POST request with form-encoded data."""

    async def fetch():
        async with aiohttp.ClientSession() as session:
            form_data = {
                "title": "Form Title",
                "body": "Form Body",
                "userId": "1",
            }
            async with session.post(
                "https://jsonplaceholder.typicode.com/posts",
                data=form_data,
            ) as response:
                return await response.json()

    try:
        result = asyncio.run(fetch())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/put-json", methods=["PUT"])
def put_json():
    """Test PUT request with JSON body."""

    async def fetch():
        data = request.get_json() or {}
        async with aiohttp.ClientSession() as session:
            payload = {
                "id": 1,
                "title": data.get("title", "Updated Title"),
                "body": data.get("body", "Updated Body"),
                "userId": data.get("userId", 1),
            }
            async with session.put(
                "https://jsonplaceholder.typicode.com/posts/1",
                json=payload,
            ) as response:
                return await response.json()

    try:
        result = asyncio.run(fetch())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/patch-json", methods=["PATCH"])
def patch_json():
    """Test PATCH request with partial JSON body."""

    async def fetch():
        data = request.get_json() or {}
        async with aiohttp.ClientSession() as session:
            payload = {"title": data.get("title", "Patched Title")}
            async with session.patch(
                "https://jsonplaceholder.typicode.com/posts/1",
                json=payload,
            ) as response:
                return await response.json()

    try:
        result = asyncio.run(fetch())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/delete", methods=["DELETE"])
def delete():
    """Test DELETE request."""

    async def fetch():
        async with aiohttp.ClientSession() as session:
            async with session.delete("https://jsonplaceholder.typicode.com/posts/1") as response:
                return {"status": "deleted", "status_code": response.status}

    try:
        result = asyncio.run(fetch())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =============================================================================
# Chained Requests
# =============================================================================


@app.route("/api/chain", methods=["GET"])
def chain():
    """Test sequential chained requests."""

    async def fetch():
        async with aiohttp.ClientSession() as session:
            # First request: get a user
            async with session.get("https://jsonplaceholder.typicode.com/users/1") as response:
                user = await response.json()

            # Second request: get posts by that user
            async with session.get(
                "https://jsonplaceholder.typicode.com/posts",
                params={"userId": user["id"]},
            ) as response:
                posts = await response.json()

            # Third request: get comments on the first post
            if posts:
                async with session.get(
                    f"https://jsonplaceholder.typicode.com/posts/{posts[0]['id']}/comments"
                ) as response:
                    comments = await response.json()
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


# =============================================================================
# Parallel Requests
# =============================================================================


@app.route("/api/parallel", methods=["GET"])
def parallel():
    """Test parallel requests using asyncio.gather."""

    async def fetch():
        async with aiohttp.ClientSession() as session:
            # Define tasks
            async def get_post():
                async with session.get("https://jsonplaceholder.typicode.com/posts/1") as response:
                    return await response.json()

            async def get_user():
                async with session.get("https://jsonplaceholder.typicode.com/users/1") as response:
                    return await response.json()

            async def get_comment():
                async with session.get("https://jsonplaceholder.typicode.com/comments/1") as response:
                    return await response.json()

            # Run requests in parallel
            post, user, comment = await asyncio.gather(get_post(), get_user(), get_comment())

            return {
                "post": post,
                "user": user,
                "comment": comment,
            }

    try:
        result = asyncio.run(fetch())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =============================================================================
# Additional Test Cases
# =============================================================================


@app.route("/test/timeout", methods=["GET"])
def test_timeout():
    """Test request with explicit timeout."""

    async def fetch():
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get("https://jsonplaceholder.typicode.com/posts/3") as response:
                return await response.json()

    try:
        result = asyncio.run(fetch())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/binary-response", methods=["GET"])
def test_binary_response():
    """Test handling of binary response (should be handled gracefully)."""

    async def fetch():
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://httpbin.org/image/png",
                headers={"Accept": "image/png"},
            ) as response:
                content = await response.read()
                return {
                    "status": response.status,
                    "content_type": response.content_type,
                    "content_length": len(content),
                }

    try:
        result = asyncio.run(fetch())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/redirect", methods=["GET"])
def test_redirect():
    """Test following redirects."""

    async def fetch():
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://httpbin.org/redirect/2",
                allow_redirects=True,
            ) as response:
                return {
                    "status": response.status,
                    "final_url": str(response.url),
                    "redirect_count": len(response.history),
                }

    try:
        result = asyncio.run(fetch())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/basic-auth", methods=["GET"])
def test_basic_auth():
    """Test request with basic authentication."""

    async def fetch():
        auth = aiohttp.BasicAuth("testuser", "testpass")
        async with aiohttp.ClientSession(auth=auth) as session:
            async with session.get("https://httpbin.org/basic-auth/testuser/testpass") as response:
                return await response.json()

    try:
        result = asyncio.run(fetch())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/multiple-requests", methods=["GET"])
def test_multiple_requests():
    """Test multiple requests in a single session."""

    async def fetch():
        async with aiohttp.ClientSession() as session:
            results = []
            for i in range(1, 4):
                async with session.get(f"https://jsonplaceholder.typicode.com/posts/{i}") as response:
                    data = await response.json()
                    results.append({"id": data["id"], "title": data["title"]})
            return {"posts": results}

    try:
        result = asyncio.run(fetch())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/streaming", methods=["GET"])
def test_streaming():
    """Test reading response in chunks."""

    async def fetch():
        async with aiohttp.ClientSession() as session:
            async with session.get("https://jsonplaceholder.typicode.com/posts/6") as response:
                # Read in chunks
                chunks = []
                async for chunk in response.content.iter_chunked(32):
                    chunks.append(chunk)
                content = b"".join(chunks)
                return {"status": response.status, "content_length": len(content)}

    try:
        result = asyncio.run(fetch())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/custom-connector", methods=["GET"])
def test_custom_connector():
    """Test with custom connector (connection pool settings)."""

    async def fetch():
        connector = aiohttp.TCPConnector(limit=10)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get("https://jsonplaceholder.typicode.com/posts/7") as response:
                return await response.json()

    try:
        result = asyncio.run(fetch())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/read-text", methods=["GET"])
def test_read_text():
    """Test reading response as text."""

    async def fetch():
        async with aiohttp.ClientSession() as session:
            async with session.get("https://jsonplaceholder.typicode.com/posts/8") as response:
                text = await response.text()
                return {"status": response.status, "text_length": len(text)}

    try:
        result = asyncio.run(fetch())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/post-bytes", methods=["POST"])
def test_post_bytes():
    """Test POST request with raw bytes body."""

    async def fetch():
        async with aiohttp.ClientSession() as session:
            body = b'{"title": "Bytes Title", "body": "Bytes Body", "userId": 1}'
            async with session.post(
                "https://httpbin.org/post",
                data=body,
                headers={"Content-Type": "application/json"},
            ) as response:
                result = await response.json()
                return {
                    "posted_data": result.get("data", ""),
                    "content_type": result.get("headers", {}).get("Content-Type", ""),
                }

    try:
        result = asyncio.run(fetch())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    sdk.mark_app_as_ready()
    app.run(host="0.0.0.0", port=8000, debug=False)
