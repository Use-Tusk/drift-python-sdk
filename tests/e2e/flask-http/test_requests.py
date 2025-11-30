#!/usr/bin/env python3
"""Script to hit all Flask e2e test endpoints.

This script makes HTTP requests to all test endpoints to generate
traces during RECORD mode.
"""

import sys
import time
import requests


def hit_endpoints(base_url: str = "http://localhost:5000") -> bool:
    """Hit all test endpoints.

    Args:
        base_url: Base URL of the Flask server

    Returns:
        True if all requests succeeded, False otherwise
    """
    endpoints = [
        ("GET", "/health", None, None),
        ("GET", "/test-http-get", None, None),
        ("POST", "/test-http-post", {"title": "test", "body": "test body"}, None),
        ("PUT", "/test-http-put", {"id": 1, "title": "updated"}, None),
        ("DELETE", "/test-http-delete", None, None),
        ("GET", "/test-http-headers", None, None),
        ("GET", "/test-http-query-params", None, None),
        ("GET", "/test-http-error", None, None),  # Expected to return error
        ("GET", "/test-chained-requests", None, None),
        ("GET", "/greet/World", None, {"greeting": "Hi"}),
        ("POST", "/echo", {"message": "hello"}, None),
    ]

    success_count = 0
    failure_count = 0

    print(f"Hitting all Flask HTTP endpoints at {base_url}...")

    for method, path, json_body, params in endpoints:
        url = f"{base_url}{path}"
        print(f"  - {method} {path}")

        try:
            if method == "GET":
                response = requests.get(url, params=params, timeout=10)
            elif method == "POST":
                response = requests.post(url, json=json_body, timeout=10)
            elif method == "PUT":
                response = requests.put(url, json=json_body, timeout=10)
            elif method == "DELETE":
                response = requests.delete(url, timeout=10)
            else:
                print(f"    ❌ Unknown method: {method}")
                failure_count += 1
                continue

            # Accept 404 for the error test endpoint
            if path == "/test-http-error" and response.status_code == 200:
                print(f"    ✓ Status: {response.status_code}")
                success_count += 1
            elif response.status_code < 400:
                print(f"    ✓ Status: {response.status_code}")
                success_count += 1
            else:
                print(f"    ❌ Status: {response.status_code}")
                failure_count += 1

        except Exception as e:
            print(f"    ❌ Error: {e}")
            failure_count += 1

    print(f"\nCompleted: {success_count} succeeded, {failure_count} failed")
    return failure_count == 0


if __name__ == "__main__":
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:5000"

    # Wait a bit to ensure server is fully ready
    time.sleep(2)

    success = hit_endpoints(base_url)
    sys.exit(0 if success else 1)
