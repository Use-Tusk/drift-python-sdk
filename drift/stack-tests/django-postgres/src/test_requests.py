"""Execute test requests against the Django + PostgreSQL app."""

from drift.instrumentation.e2e_common.test_utils import make_request, print_request_summary

if __name__ == "__main__":
    print("Starting Django + PostgreSQL test request sequence...\n")

    # Execute test sequence
    make_request("GET", "/health")

    # Key integration test: register_default_jsonb on InstrumentedConnection
    # This is the main test for the bug fix
    make_request("GET", "/db/register-jsonb")

    # Transaction test (rollback, doesn't return data)
    make_request("POST", "/db/transaction")

    # TODO: Re-enable these tests once cursor.description REPLAY bug is fixed
    # The issue is that cursor.description is None in REPLAY mode when using
    # Django's cursor wrapper with INSERT/UPDATE RETURNING queries.
    #
    # # Query operations using Django's connection
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
    # # Raw connection test
    # make_request("GET", "/db/raw-connection")
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
