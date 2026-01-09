"""Execute test requests against the Flask app to exercise the HTTPX instrumentation."""

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
    print("Starting test request sequence for HTTPX instrumentation...\n")

    # Health check
    make_request("GET", "/health")

    # ==========================================================================
    # Sync Client Tests
    # ==========================================================================
    print("\n--- Sync Client Tests ---\n")

    # Basic GET request - JSON response
    make_request("GET", "/api/sync/get-json")

    # GET with query parameters
    make_request("GET", "/api/sync/get-with-params")

    # GET with custom headers
    make_request("GET", "/api/sync/get-with-headers")

    # POST with JSON body
    make_request(
        "POST",
        "/api/sync/post-json",
        json={"title": "Sync Test Post", "body": "This is a sync test post body", "userId": 1},
    )

    # POST with form data
    make_request("POST", "/api/sync/post-form")

    # PUT request
    make_request(
        "PUT",
        "/api/sync/put-json",
        json={"title": "Sync Updated Post", "body": "This is a sync updated post body", "userId": 1},
    )

    # PATCH request
    make_request("PATCH", "/api/sync/patch-json", json={"title": "Sync Patched Title"})

    # DELETE request
    make_request("DELETE", "/api/sync/delete")

    # Sequential chained requests
    make_request("GET", "/api/sync/chain")

    # ==========================================================================
    # Async Client Tests
    # ==========================================================================
    print("\n--- Async Client Tests ---\n")

    # Async GET request - JSON response
    make_request("GET", "/api/async/get-json")

    # Async GET with query parameters
    make_request("GET", "/api/async/get-with-params")

    # Async POST with JSON body
    make_request(
        "POST",
        "/api/async/post-json",
        json={"title": "Async Test Post", "body": "This is an async test post body", "userId": 2},
    )

    # Async PUT request
    make_request(
        "PUT",
        "/api/async/put-json",
        json={"title": "Async Updated Post", "body": "This is an async updated post body", "userId": 2},
    )

    # Async DELETE request
    make_request("DELETE", "/api/async/delete")

    # Parallel async requests
    make_request("GET", "/api/async/parallel")

    # Async sequential chained requests
    make_request("GET", "/api/async/chain")

    print("\nAll requests completed successfully")
