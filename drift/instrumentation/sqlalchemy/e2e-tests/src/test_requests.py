"""Execute test requests against the SQLAlchemy Flask app."""

from drift.instrumentation.e2e_common.test_utils import make_request, print_request_summary

if __name__ == "__main__":
    print("Starting SQLAlchemy test request sequence...\n")

    make_request("GET", "/health")
    make_request("GET", "/db/query")

    create_resp = make_request("POST", "/db/insert", json={"name": "Alice", "email": "alice@example.com"})
    make_request(
        "POST",
        "/db/batch-insert",
        json={
            "users": [
                {"name": "Bob", "email": "bob@example.com"},
                {"name": "Charlie", "email": "charlie@example.com"},
            ]
        },
    )
    make_request("GET", "/db/query")

    if create_resp.status_code == 201:
        user_id = create_resp.json().get("id")
        if user_id:
            make_request("PUT", f"/db/update/{user_id}", json={"name": "Alice Updated"})
            make_request("DELETE", f"/db/delete/{user_id}")

    make_request("GET", "/db/query")

    print_request_summary()
