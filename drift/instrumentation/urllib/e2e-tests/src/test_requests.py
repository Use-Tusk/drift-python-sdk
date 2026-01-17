"""Execute test requests against the Flask app to exercise the urllib instrumentation."""

from drift.instrumentation.e2e_common.test_utils import make_request, print_request_summary

if __name__ == "__main__":
    print("Starting test request sequence for urllib instrumentation...\n")

    # Health check
    make_request("GET", "/health")

    # Basic GET request - urlopen with string URL
    make_request("GET", "/api/get-json")

    # GET with Request object and custom headers
    make_request("GET", "/api/get-with-request-object")

    # GET with query parameters
    make_request("GET", "/api/get-with-params")

    # POST with JSON body
    make_request(
        "POST",
        "/api/post-json",
        json={"title": "Test Post", "body": "This is a test post body", "userId": 1},
    )

    # POST with form data
    make_request("POST", "/api/post-form")

    # PUT request
    make_request("PUT", "/api/put-json")

    # PATCH request
    make_request("PATCH", "/api/patch-json")

    # DELETE request
    make_request("DELETE", "/api/delete")

    # Sequential chained requests
    make_request("GET", "/api/chain")

    # Parallel requests with context propagation
    make_request("GET", "/api/parallel")

    # Request with explicit timeout
    make_request("GET", "/api/with-timeout")

    # Custom opener usage
    make_request("GET", "/api/custom-opener")

    # Text response handling
    make_request("GET", "/api/text-response")

    # urlopen with data parameter
    make_request("POST", "/api/urlopen-with-data")

    # Bug-exposing tests (these tests expose bugs in the instrumentation)
    # HTTP 404 error handling - tests HTTPError replay
    make_request("GET", "/test/http-404-error")

    # HTTP redirect handling - tests geturl() after redirects
    make_request("GET", "/test/http-redirect")

    print_request_summary()
