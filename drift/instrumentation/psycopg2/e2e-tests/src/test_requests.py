"""Execute test requests against the Psycopg Flask app."""

from drift.instrumentation.e2e_common.test_utils import make_request, print_request_summary

if __name__ == "__main__":
    print("Starting Psycopg test request sequence...\n")

    # Execute test sequence
    make_request("GET", "/health")

    # Test register_default_jsonb on InstrumentedConnection (Django compatibility)
    # This simulates what Django's PostgreSQL backend does after connect()
    make_request("GET", "/db/register-jsonb")

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

    # Regression coverage for cursor fetch semantics and error replay fidelity
    make_request("GET", "/db/fetchmany-arraysize")
    make_request("GET", "/db/error-then-query")

    # Delete operation
    if resp2.status_code == 201:
        user_id = resp2.json().get("id")
        if user_id:
            make_request("DELETE", f"/db/delete/{user_id}")

    print_request_summary()
