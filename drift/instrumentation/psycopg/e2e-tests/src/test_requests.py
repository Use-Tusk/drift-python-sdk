"""Execute test requests against the Psycopg Flask app."""

from drift.instrumentation.e2e_common.test_utils import make_request, print_request_summary

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

    make_request("GET", "/test/cursor-stream")
    make_request("GET", "/test/server-cursor")
    make_request("GET", "/test/copy-to")
    make_request("GET", "/test/multiple-queries")
    make_request("GET", "/test/pipeline-mode")
    make_request("GET", "/test/dict-row-factory")
    make_request("GET", "/test/namedtuple-row-factory")
    make_request("GET", "/test/cursor-iteration")
    make_request("GET", "/test/executemany-returning")
    make_request("GET", "/test/rownumber")
    make_request("GET", "/test/statusmessage")
    make_request("GET", "/test/nextset")
    make_request("GET", "/test/server-cursor-scroll")
    make_request("GET", "/test/cursor-scroll")
    make_request("GET", "/test/cursor-reuse")
    make_request("GET", "/test/sql-composed")
    make_request("GET", "/test/binary-uuid")
    make_request("GET", "/test/binary-bytea")
    make_request("GET", "/test/class-row-factory")
    make_request("GET", "/test/kwargs-row-factory")
    make_request("GET", "/test/scalar-row-factory")
    make_request("GET", "/test/binary-format")

    # Test: NULL values handling (integrated into E2E suite)
    make_request("GET", "/test/null-values")

    # Test: Transaction context manager
    make_request("GET", "/test/transaction-context")

    # JSON/JSONB and array types tests
    make_request("GET", "/test/json-jsonb")
    make_request("GET", "/test/array-types")
    make_request("GET", "/test/cursor-set-result")

    # These tests expose hash mismatch bugs with Decimal and date/time types
    make_request("GET", "/test/decimal-types")
    make_request("GET", "/test/date-time-types")

    # These tests expose serialization bugs with inet/cidr and range types
    make_request("GET", "/test/inet-cidr-types")
    make_request("GET", "/test/range-types")

    # Regression coverage for cursor fetch semantics and error replay fidelity
    make_request("GET", "/db/fetchmany-arraysize")
    make_request("GET", "/db/error-then-query")

    print_request_summary()
