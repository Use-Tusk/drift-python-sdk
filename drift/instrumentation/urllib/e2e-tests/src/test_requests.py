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

    # Additional edge case tests
    # Partial read with read(amt)
    make_request("GET", "/test/partial-read")

    # Response iteration using for loop
    make_request("GET", "/test/response-iteration")

    # readline() method
    make_request("GET", "/test/readline")

    # readlines() method
    make_request("GET", "/test/readlines")

    # Multiple reads from same response
    make_request("GET", "/test/multiple-reads")

    # getheaders() method
    make_request("GET", "/test/getheaders")

    # getheader() method
    make_request("GET", "/test/getheader")

    # getcode() method
    make_request("GET", "/test/getcode")

    # urlretrieve function
    make_request("GET", "/test/urlretrieve")

    # Response without context manager
    make_request("GET", "/test/no-context-manager")

    # SSL context parameter
    make_request("GET", "/test/ssl-context")

    # Empty response body (204 No Content)
    make_request("GET", "/test/empty-response")

    # HEAD request
    make_request("GET", "/test/head-request")

    # OPTIONS request
    make_request("GET", "/test/options-request")

    # Binary request body
    make_request("POST", "/test/binary-request-body")

    # HTTP 500 error
    make_request("GET", "/test/http-500-error")

    # Large query string
    make_request("GET", "/test/large-query-string")

    print_request_summary()
