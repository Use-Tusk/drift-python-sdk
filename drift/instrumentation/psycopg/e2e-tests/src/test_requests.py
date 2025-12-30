"""Execute test requests against the Psycopg Flask app."""

import time

import requests

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
    print("Starting Psycopg test request sequence...\n")

    # Execute test sequence
    make_request("GET", "/health")

    # Query operations
    make_request("GET", "/db/query")

    # Insert operations
    resp1 = make_request("POST", "/db/insert", json={"name": "Alice", "email": "alice@example.com"})
    resp2 = make_request("POST", "/db/insert", json={"name": "Bob", "email": "bob@example.com"})

    # Batch insert
    make_request(
        "POST",
        "/db/batch-insert",
        json={
            "users": [
                {"name": "Charlie", "email": "charlie@example.com"},
                {"name": "David", "email": "david@example.com"},
                {"name": "Eve", "email": "eve@example.com"},
            ]
        },
    )

    # Update operation
    if resp1.status_code == 201:
        user_id = resp1.json().get("id")
        if user_id:
            make_request("PUT", f"/db/update/{user_id}", json={"name": "Alice Updated"})

    # Transaction test
    make_request("POST", "/db/transaction")

    # Query again to see all users
    make_request("GET", "/db/query")

    # Delete operation
    if resp2.status_code == 201:
        user_id = resp2.json().get("id")
        if user_id:
            make_request("DELETE", f"/db/delete/{user_id}")

    print("\nAll requests completed successfully")
