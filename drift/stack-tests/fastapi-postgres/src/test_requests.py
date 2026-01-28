"""Execute test requests against the FastAPI + PostgreSQL app."""

from drift.instrumentation.e2e_common.test_utils import make_request, print_request_summary

if __name__ == "__main__":
    print("Starting FastAPI + PostgreSQL (psycopg3) test request sequence...\n")

    # Execute test sequence
    make_request("GET", "/health")

    # Sync fallback test - uses sync psycopg.connect() which is fully instrumented
    make_request("GET", "/db/sync-fallback")

    # Async psycopg tests using AsyncConnectionPool
    make_request("GET", "/db/transaction")
    make_request("GET", "/db/query")

    # Insert/Update/Delete operations
    resp1 = make_request("POST", "/db/insert", json={"name": "Alice", "email": "alice@example.com"})
    if resp1.status_code == 201:
        user_id = resp1.json().get("id")
        if user_id:
            make_request("PUT", f"/db/update/{user_id}", json={"name": "Alice Updated"})
            make_request("DELETE", f"/db/delete/{user_id}")

    # Async context propagation test (3 concurrent queries)
    make_request("GET", "/db/async-context")

    # Pipeline test (multiple cursors)
    make_request("GET", "/db/pipeline")

    print_request_summary()
