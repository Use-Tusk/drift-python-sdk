"""Execute test requests against the Flask app to exercise the aiohttp instrumentation."""

from drift.instrumentation.e2e_common.test_utils import make_request, print_request_summary

if __name__ == "__main__":
    print("Starting test request sequence for aiohttp instrumentation...\n")

    # Health check
    make_request("GET", "/health")

    # ==========================================================================
    # Basic HTTP Methods
    # ==========================================================================
    print("\n--- Basic HTTP Methods ---\n")

    # Basic GET request - JSON response
    make_request("GET", "/api/get-json")

    # GET with query parameters
    make_request("GET", "/api/get-with-params")

    # GET with custom headers
    make_request("GET", "/api/get-with-headers")

    # POST with JSON body
    make_request(
        "POST",
        "/api/post-json",
        json={"title": "Test Post", "body": "This is a test post body", "userId": 1},
    )

    # POST with form data
    make_request("POST", "/api/post-form")

    # PUT request
    make_request(
        "PUT",
        "/api/put-json",
        json={"title": "Updated Post", "body": "This is an updated post body", "userId": 1},
    )

    # PATCH request
    make_request("PATCH", "/api/patch-json", json={"title": "Patched Title"})

    # DELETE request
    make_request("DELETE", "/api/delete")

    # ==========================================================================
    # Chained and Parallel Requests
    # ==========================================================================
    print("\n--- Chained and Parallel Requests ---\n")

    # Sequential chained requests
    make_request("GET", "/api/chain")

    # Parallel requests
    make_request("GET", "/api/parallel")

    # ==========================================================================
    # Additional Test Cases
    # ==========================================================================
    print("\n--- Additional Test Cases ---\n")

    # Timeout configuration
    make_request("GET", "/test/timeout")

    # Binary response
    make_request("GET", "/test/binary-response")

    # Following redirects
    make_request("GET", "/test/redirect")

    # Basic authentication
    make_request("GET", "/test/basic-auth")

    # Multiple requests in sequence
    make_request("GET", "/test/multiple-requests")

    # Streaming response
    make_request("GET", "/test/streaming")

    # Custom connector
    make_request("GET", "/test/custom-connector")

    # Read as text
    make_request("GET", "/test/read-text")

    # POST with bytes body
    make_request("POST", "/test/post-bytes")

    print_request_summary()
