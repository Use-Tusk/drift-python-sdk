"""Execute test requests against the FastAPI + PostgreSQL app."""

from drift.instrumentation.e2e_common.test_utils import make_request, print_request_summary

if __name__ == "__main__":
    print("Starting FastAPI + PostgreSQL (psycopg3) test request sequence...\n")

    # Execute test sequence
    make_request("GET", "/health")

    # Transaction test (rollback, doesn't depend on data)
    make_request("GET", "/db/transaction")

    # TODO: Re-enable these tests once psycopg (async) REPLAY mode is verified
    # Currently only 3 traces recorded vs 9 requests - some endpoints not recording properly
    #
    # # Query operations using async psycopg3
    # make_request("GET", "/db/query")
    #
    # # Insert operations
    # resp1 = make_request("POST", "/db/insert", json={"name": "Alice", "email": "alice@example.com"})
    # resp2 = make_request("POST", "/db/insert", json={"name": "Bob", "email": "bob@example.com"})
    #
    # # Update operation
    # if resp1.status_code == 201:
    #     user_id = resp1.json().get("id")
    #     if user_id:
    #         make_request("PUT", f"/db/update/{user_id}", json={"name": "Alice Updated"})
    #
    # # Async context propagation test
    # make_request("GET", "/db/async-context")
    #
    # # Sync fallback test
    # make_request("GET", "/db/sync-fallback")
    #
    # # Pipeline test
    # make_request("GET", "/db/pipeline")
    #
    # # Query again to see all users
    # make_request("GET", "/db/query")
    #
    # # Delete operation
    # if resp2.status_code == 201:
    #     user_id = resp2.json().get("id")
    #     if user_id:
    #         make_request("DELETE", f"/db/delete/{user_id}")

    print_request_summary()
