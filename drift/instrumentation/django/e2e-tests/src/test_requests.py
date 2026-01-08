"""Execute test requests against the Django app."""

import os
import time

import requests

PORT = os.getenv("PORT", "8000")
BASE_URL = f"http://localhost:{PORT}"


def make_request(method: str, endpoint: str, **kwargs):
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
    print("Starting Django test request sequence...\n")

    # Execute test sequence
    make_request("GET", "/health")
    make_request("GET", "/api/weather")
    make_request("GET", "/api/user/test123")
    make_request("GET", "/api/activity")
    make_request("GET", "/api/post/1")
    make_request(
        "POST",
        "/api/post",
        json={
            "title": "Test Post",
            "body": "This is a test post body",
            "userId": 1,
        },
    )
    make_request("DELETE", "/api/post/1/delete")

    print("\nAll requests completed successfully")
