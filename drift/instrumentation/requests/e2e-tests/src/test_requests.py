"""Execute test requests against the Flask app to exercise the requests instrumentation."""

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
    print("Starting test request sequence for requests instrumentation...\n")

    # Health check
    make_request("GET", "/health")

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

    # Sequential chained requests
    make_request("GET", "/api/chain")

    # Parallel requests with context propagation
    make_request("GET", "/api/parallel")

    # Request with timeout
    make_request("GET", "/api/with-timeout")

    # Text response handling
    make_request("GET", "/api/text-response")

    make_request("GET", "/test/session-send-direct")

    print("\nAll requests completed successfully")
