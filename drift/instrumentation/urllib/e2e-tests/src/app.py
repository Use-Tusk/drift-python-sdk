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


# Test: HTTP 404 error handling (BUG-EXPOSING TEST)
@app.route("/test/http-404-error", methods=["GET"])
def test_http_404_error():
    """Test handling of HTTP 404 error responses.

    When urlopen receives a 4xx/5xx response, it raises HTTPError which is
    also a valid response object. This tests if the instrumentation correctly
    handles this case.
    """
    from urllib.error import HTTPError as UrllibHTTPError

    try:
        with urlopen("https://httpbin.org/status/404", timeout=10) as response:
            return jsonify({"status": response.status})
    except UrllibHTTPError as e:
        # HTTPError is also a response object - we can read its body
        body = e.read().decode("utf-8") if e.fp else ""
        return jsonify(
            {
                "error": True,
                "code": e.code,
                "reason": e.reason,
                "body": body,
            }
        ), 200  # Return 200 since we handled the error
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Test: HTTP redirect handling (BUG-EXPOSING TEST)
@app.route("/test/http-redirect", methods=["GET"])
def test_http_redirect():
    """Test HTTP redirect handling (301, 302, etc.).

    urllib follows redirects automatically. This tests if the instrumentation
    correctly handles redirect scenarios.
    """
    try:
        # httpbin.org/redirect/n redirects n times before returning 200
        with urlopen("https://httpbin.org/redirect/2", timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            return jsonify(
                {
                    "final_url": response.geturl(),
                    "status": response.status,
                    "data": data,
                }
            )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Test: Partial read with read(amt) parameter
@app.route("/test/partial-read", methods=["GET"])
def test_partial_read():
    """Test reading response body in chunks using read(amt).

    Tests if the ResponseWrapper correctly handles partial reads and caches
    the full body for instrumentation while still allowing incremental reading.
    """
    try:
        with urlopen("https://jsonplaceholder.typicode.com/posts/1", timeout=10) as response:
            # Read in small chunks
            chunks = []
            while True:
                chunk = response.read(50)
                if not chunk:
                    break
                chunks.append(chunk)
            full_body = b"".join(chunks)
            data = json.loads(full_body.decode("utf-8"))
            return jsonify(
                {
                    "chunk_count": len(chunks),
                    "total_bytes": len(full_body),
                    "data": data,
                }
            )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Test: Response iteration using for loop
@app.route("/test/response-iteration", methods=["GET"])
def test_response_iteration():
    """Test iterating over response lines using for loop.

    Tests if the ResponseWrapper correctly implements __iter__ and __next__
    for line-by-line iteration.
    """
    try:
        with urlopen("https://httpbin.org/robots.txt", timeout=10) as response:
            lines = []
            for line in response:
                lines.append(line.decode("utf-8").strip())
            return jsonify(
                {
                    "line_count": len(lines),
                    "lines": lines,
                }
            )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Test: readline() method
@app.route("/test/readline", methods=["GET"])
def test_readline():
    """Test reading response line by line using readline().

    Tests if the ResponseWrapper correctly implements readline().
    """
    try:
        with urlopen("https://httpbin.org/robots.txt", timeout=10) as response:
            lines = []
            while True:
                line = response.readline()
                if not line:
                    break
                lines.append(line.decode("utf-8").strip())
            return jsonify(
                {
                    "line_count": len(lines),
                    "lines": lines,
                }
            )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Test: readlines() method
@app.route("/test/readlines", methods=["GET"])
def test_readlines():
    """Test reading all response lines at once using readlines().

    Tests if the ResponseWrapper correctly implements readlines().
    """
    try:
        with urlopen("https://httpbin.org/robots.txt", timeout=10) as response:
            lines = response.readlines()
            decoded_lines = [line.decode("utf-8").strip() for line in lines]
            return jsonify(
                {
                    "line_count": len(decoded_lines),
                    "lines": decoded_lines,
                }
            )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Test: Multiple reads from the same response
@app.route("/test/multiple-reads", methods=["GET"])
def test_multiple_reads():
    """Test reading from response multiple times.

    The ResponseWrapper should cache the body and allow multiple reads.
    This tests if the first read() returns data and subsequent reads work correctly.
    """
    try:
        with urlopen("https://jsonplaceholder.typicode.com/posts/1", timeout=10) as response:
            # First read - should get all data
            first_read = response.read()
            # Second read - should return empty (standard file-like behavior after EOF)
            second_read = response.read()
            data = json.loads(first_read.decode("utf-8"))
            return jsonify(
                {
                    "first_read_bytes": len(first_read),
                    "second_read_bytes": len(second_read),
                    "data": data,
                }
            )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Test: getheaders() method
@app.route("/test/getheaders", methods=["GET"])
def test_getheaders():
    """Test getting response headers using getheaders().

    Tests if the ResponseWrapper correctly implements getheaders().
    """
    try:
        with urlopen("https://jsonplaceholder.typicode.com/posts/1", timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            headers = response.getheaders()
            # Only check that we got headers and specific expected ones exist
            # Don't include dynamic headers in the response
            has_content_type = any(h[0].lower() == "content-type" for h in headers)
            return jsonify(
                {
                    "data": data,
                    "has_headers": len(headers) > 0,
                    "has_content_type": has_content_type,
                }
            )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Test: getheader() method
@app.route("/test/getheader", methods=["GET"])
def test_getheader():
    """Test getting specific header using getheader().

    Tests if the ResponseWrapper correctly implements getheader().
    """
    try:
        with urlopen("https://jsonplaceholder.typicode.com/posts/1", timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            content_type = response.getheader("Content-Type")
            missing_header = response.getheader("X-Missing-Header", "default-value")
            return jsonify(
                {
                    "data": data,
                    "content_type": content_type,
                    "missing_header": missing_header,
                }
            )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Test: getcode() method
@app.route("/test/getcode", methods=["GET"])
def test_getcode():
    """Test getting status code using getcode().

    Tests if the ResponseWrapper correctly implements getcode().
    """
    try:
        with urlopen("https://jsonplaceholder.typicode.com/posts/1", timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            code = response.getcode()
            status = response.status
            return jsonify(
                {
                    "data": data,
                    "getcode": code,
                    "status": status,
                }
            )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Test: urlretrieve function
@app.route("/test/urlretrieve", methods=["GET"])
def test_urlretrieve():
    """Test the urlretrieve function.

    urlretrieve downloads a URL to a temporary file. Since it uses urlopen
    internally, it should be instrumented. This tests if the trace is captured.
    """
    import os
    import tempfile
    from urllib.request import urlretrieve

    try:
        # Create a temp file
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = tmp.name

        # Download to temp file
        filepath, headers = urlretrieve(
            "https://jsonplaceholder.typicode.com/posts/1",
            tmp_path,
        )

        # Read and parse the downloaded content
        with open(filepath) as f:
            content = f.read()
            data = json.loads(content)

        # Cleanup
        os.unlink(tmp_path)

        # Don't include filepath in response since it's non-deterministic
        return jsonify(
            {
                "downloaded": True,
                "data": data,
            }
        )
    except Exception as e:
        # Cleanup on error
        if "tmp_path" in locals() and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        return jsonify({"error": str(e)}), 500


# Test: Response without context manager (direct assignment)
@app.route("/test/no-context-manager", methods=["GET"])
def test_no_context_manager():
    """Test using urlopen without context manager.

    Some code may not use the 'with' statement. Tests if the response
    can be used directly without context manager issues.
    """
    response = None
    try:
        response = urlopen("https://jsonplaceholder.typicode.com/posts/1", timeout=10)
        data = json.loads(response.read().decode("utf-8"))
        response.close()
        return jsonify(
            {
                "data": data,
                "closed_manually": True,
            }
        )
    except Exception as e:
        if response:
            response.close()
        return jsonify({"error": str(e)}), 500


# Test: SSL context parameter
@app.route("/test/ssl-context", methods=["GET"])
def test_ssl_context():
    """Test urlopen with SSL context parameter.

    Tests if the instrumentation correctly handles requests made with
    explicit SSL context configuration.
    """
    import ssl

    try:
        context = ssl.create_default_context()
        # Relax verification for testing purposes
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED

        with urlopen("https://jsonplaceholder.typicode.com/posts/1", timeout=10, context=context) as response:
            data = json.loads(response.read().decode("utf-8"))
            return jsonify(
                {
                    "data": data,
                    "ssl_version": context.protocol,
                }
            )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Test: Empty response body (204 No Content)
@app.route("/test/empty-response", methods=["GET"])
def test_empty_response():
    """Test handling of empty response bodies.

    Tests if the instrumentation correctly handles 204 No Content
    or other empty response scenarios.
    """
    try:
        # httpbin.org/status/204 returns no content
        with urlopen("https://httpbin.org/status/204", timeout=10) as response:
            body = response.read()
            return jsonify(
                {
                    "status": response.status,
                    "body_length": len(body),
                    "is_empty": len(body) == 0,
                }
            )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Test: HEAD request (no body in response)
@app.route("/test/head-request", methods=["GET"])
def test_head_request():
    """Test HEAD request which returns no body.

    HEAD requests should not have a response body but should have headers.
    Tests if the instrumentation handles this correctly.
    """
    try:
        req = Request(
            "https://jsonplaceholder.typicode.com/posts/1",
            method="HEAD",
        )
        with urlopen(req, timeout=10) as response:
            body = response.read()
            return jsonify(
                {
                    "status": response.status,
                    "body_length": len(body),
                    "has_content_type": response.getheader("Content-Type") is not None,
                }
            )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Test: OPTIONS request
@app.route("/test/options-request", methods=["GET"])
def test_options_request():
    """Test OPTIONS request method.

    OPTIONS requests are used for CORS preflight checks.
    Tests if the instrumentation handles this HTTP method.
    """
    try:
        req = Request(
            "https://httpbin.org/get",
            method="OPTIONS",
        )
        with urlopen(req, timeout=10) as response:
            body = response.read()
            allow_header = response.getheader("Allow")
            return jsonify(
                {
                    "status": response.status,
                    "body_length": len(body),
                    "allow_header": allow_header,
                }
            )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Test: Binary request body (bytes)
@app.route("/test/binary-request-body", methods=["POST"])
def test_binary_request_body():
    """Test POST request with binary (non-JSON) request body.

    Tests if the instrumentation correctly captures and replays
    binary request bodies.
    """
    try:
        # Send raw bytes
        binary_data = b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09"
        req = Request(
            "https://httpbin.org/post",
            data=binary_data,
            headers={"Content-Type": "application/octet-stream"},
        )
        with urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            # httpbin returns the base64-encoded data
            return jsonify(
                {
                    "status": response.status,
                    "data_received": data.get("data", ""),
                }
            )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Test: HTTP 500 Internal Server Error
@app.route("/test/http-500-error", methods=["GET"])
def test_http_500_error():
    """Test handling of HTTP 500 Internal Server Error.

    Tests if the instrumentation correctly handles and replays
    server error responses.
    """
    from urllib.error import HTTPError as UrllibHTTPError

    try:
        with urlopen("https://httpbin.org/status/500", timeout=10) as response:
            return jsonify({"status": response.status})
    except UrllibHTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        return jsonify(
            {
                "error": True,
                "code": e.code,
                "reason": e.reason,
            }
        ), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Test: Large query string
@app.route("/test/large-query-string", methods=["GET"])
def test_large_query_string():
    """Test request with a large query string.

    Tests if the instrumentation correctly handles URLs with
    many query parameters.
    """
    try:
        # Build a URL with many query parameters
        params = "&".join([f"param{i}=value{i}" for i in range(20)])
        url = f"https://httpbin.org/get?{params}"

        with urlopen(url, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            return jsonify(
                {
                    "status": response.status,
                    "args_count": len(data.get("args", {})),
                }
            )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    sdk.mark_app_as_ready()
    app.run(host="0.0.0.0", port=8000, debug=False)
