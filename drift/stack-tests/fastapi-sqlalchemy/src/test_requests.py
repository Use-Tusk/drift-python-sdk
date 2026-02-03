"""Execute test requests against the FastAPI + SQLAlchemy app."""

from drift.instrumentation.e2e_common.test_utils import make_request, print_request_summary

if __name__ == "__main__":
    print("Starting FastAPI + SQLAlchemy test request sequence...\n")

    # Execute test sequence
    make_request("GET", "/health")

    # Query operations using SQLAlchemy ORM
    make_request("GET", "/db/query")

    # Raw SQL through SQLAlchemy
    make_request("GET", "/db/raw-sql")

    # Aggregation queries
    make_request("GET", "/db/aggregation")

    # Insert/Update/Delete operations via ORM
    resp1 = make_request("POST", "/db/insert", json={"name": "Alice", "email": "alice@example.com"})
    if resp1.status_code == 201:
        user_id = resp1.json().get("id")
        if user_id:
            make_request("PUT", f"/db/update/{user_id}", json={"name": "Alice Updated"})
            make_request("DELETE", f"/db/delete/{user_id}")

    # Transaction test
    make_request("GET", "/db/transaction")

    # Async context propagation test (3 concurrent queries)
    make_request("GET", "/db/async-context")

    # Multiple related queries
    make_request("GET", "/db/relationships")

    print_request_summary()
