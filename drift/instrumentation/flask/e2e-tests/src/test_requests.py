"""Execute test requests against the Flask app."""

import requests
import time

BASE_URL = "http://localhost:8000"


def make_request(method, endpoint, **kwargs):
    """Make HTTP request and log result."""
    url = f"{BASE_URL}{endpoint}"
    print(f"→ {method} {endpoint}")

    response = requests.request(method, url, **kwargs)
    print(f"  Status: {response.status_code}")
    time.sleep(0.5)  # Small delay between requests
    return response


if __name__ == "__main__":
    print("Starting test request sequence...\n")

    # Execute test sequence
    make_request("GET", "/health")
    make_request("GET", "/api/weather-activity")
    make_request("GET", "/api/user/test123")
    make_request("POST", "/api/user")
    make_request("GET", "/api/post/1")
    make_request("POST", "/api/post", json={
        "title": "Test Post",
        "body": "This is a test post",
        "userId": 1
    })
    make_request("DELETE", "/api/post/1")

    print("\nAll requests completed successfully")
