"""Execute test requests against the Flask app to exercise the urllib3 instrumentation."""

import time

import requests

BASE_URL = "http://localhost:8000"


def make_request(method, endpoint, **kwargs):
    """Make HTTP request and log result."""
    url = f"{BASE_URL}{endpoint}"
    print(f"-> {method} {endpoint}")

    # Set default timeout if not provided
    kwargs.setdefault("timeout", 30)
    response = requests.request(method, url, **kwargs)
    print(f"   Status: {response.status_code}")
    time.sleep(0.5)  # Small delay between requests
    return response


if __name__ == "__main__":
    print("Starting test request sequence for urllib3 instrumentation...\n")

    # Health check
    make_request("GET", "/health")

    # ==========================================================================
    # PoolManager Tests (high-level API)
    # ==========================================================================
    print("\n--- PoolManager Tests ---\n")

    # Basic GET request - JSON response
    make_request("GET", "/api/poolmanager/get-json")

    # GET with query parameters
    make_request("GET", "/api/poolmanager/get-with-params")

    # GET with custom headers
    make_request("GET", "/api/poolmanager/get-with-headers")

    # POST with JSON body
    make_request(
        "POST",
        "/api/poolmanager/post-json",
        json={"title": "Test Post", "body": "This is a test post body", "userId": 1},
    )

    # POST with form data
    make_request("POST", "/api/poolmanager/post-form")

    # PUT request
    make_request(
        "PUT",
        "/api/poolmanager/put-json",
        json={"title": "Updated Post", "body": "This is an updated post body", "userId": 1},
    )

    # PATCH request
    make_request("PATCH", "/api/poolmanager/patch-json", json={"title": "Patched Title"})

    # DELETE request
    make_request("DELETE", "/api/poolmanager/delete")

    # Sequential chained requests
    make_request("GET", "/api/poolmanager/chain")

    # ==========================================================================
    # HTTPConnectionPool Tests (low-level API)
    # ==========================================================================
    print("\n--- HTTPConnectionPool Tests ---\n")

    # GET using connection pool directly
    make_request("GET", "/api/connectionpool/get-json")

    # POST using connection pool directly
    make_request(
        "POST",
        "/api/connectionpool/post-json",
        json={"title": "Pool Test", "body": "Connection pool test", "userId": 2},
    )

    # ==========================================================================
    # Additional Tests
    # ==========================================================================
    print("\n--- Additional Tests ---\n")

    # Request with timeout
    make_request("GET", "/test/timeout")

    # Request with retries
    make_request("GET", "/test/retries")

    # Redirect handling
    make_request("GET", "/test/redirect")

    # New PoolManager per request
    make_request("GET", "/test/new-poolmanager")

    # Basic authentication
    make_request("GET", "/test/basic-auth")

    # Multiple requests in sequence
    make_request("GET", "/test/multiple-requests")

    # ==========================================================================
    # Avoid Double-span Test (requests library)
    # ==========================================================================
    print("\n--- Double-span Test (requests library) ---\n")

    # Test that uses requests library (which internally uses urllib3)
    # This should NOT create double spans
    make_request("GET", "/test/requests-lib")

    # ==========================================================================
    # Note: Bug detection tests for preload_content=False and streaming responses
    # are NOT included in e2e tests because these patterns are incompatible with
    # replay mode - we can't capture the response body without consuming the stream.
    #
    # The instrumentation now correctly handles these patterns by NOT capturing
    # the response body, which allows the application to read/stream normally.
    # However, this means there's no body to replay in REPLAY mode.
    #
    # To verify the fix works, run the manual test script in RECORD mode:
    #   curl http://localhost:8000/test/bug/preload-content-false
    #   curl http://localhost:8000/test/bug/streaming-response
    # ==========================================================================

    print("\nAll requests completed successfully")
