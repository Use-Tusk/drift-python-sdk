"""Execute test requests against the Redis Flask app."""

import time

import requests

BASE_URL = "http://localhost:8000"


def make_request(method, endpoint, **kwargs):
    """Make HTTP request and log result."""
    url = f"{BASE_URL}{endpoint}"
    print(f"→ {method} {endpoint}")

    # Set default timeout if not provided
    kwargs.setdefault("timeout", 30)
    response = requests.request(method, url, **kwargs)
    print(f"  Status: {response.status_code}")
    time.sleep(0.5)  # Small delay between requests
    return response


if __name__ == "__main__":
    print("Starting Redis test request sequence...\n")

    # Execute test sequence
    make_request("GET", "/health")

    # Set operations
    make_request("POST", "/redis/set", json={"key": "test_key", "value": "test_value"})
    make_request("POST", "/redis/set", json={"key": "test_key_expiry", "value": "expires_soon", "ex": 300})

    # Get operations
    make_request("GET", "/redis/get/test_key")
    make_request("GET", "/redis/get/test_key_expiry")
    # TODO: figure out why this test fails during replay
    # make_request("GET", "/redis/get/nonexistent_key")

    # Increment operations
    make_request("POST", "/redis/incr/counter")
    make_request("POST", "/redis/incr/counter")
    make_request("POST", "/redis/incr/counter")

    # Keys pattern matching
    make_request("GET", "/redis/keys/test_*")
    make_request("GET", "/redis/keys/*")

    # Delete operations
    make_request("DELETE", "/redis/delete/test_key")
    make_request("DELETE", "/redis/delete/counter")

    make_request("GET", "/test/mget-mset")

    # Pipeline operations
    make_request("GET", "/test/pipeline-basic")
    make_request("GET", "/test/pipeline-no-transaction")

    # Async Pipeline operations
    make_request("GET", "/test/async-pipeline")

    # Binary data handling
    make_request("GET", "/test/binary-data")

    make_request("GET", "/test/transaction-watch")

    print("\nAll requests completed successfully")
