"""Flask app with Psycopg (v3) operations for e2e testing."""

import os

import psycopg
from flask import Flask, jsonify, request

from drift import TuskDrift

# Initialize Drift SDK
sdk = TuskDrift.initialize(
    api_key="tusk-test-key",
    log_level="debug",
)

app = Flask(__name__)


# Build connection string from environment variables
def get_conn_string():
    return (
        f"host={os.getenv('POSTGRES_HOST', 'postgres')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"dbname={os.getenv('POSTGRES_DB', 'testdb')} "
        f"user={os.getenv('POSTGRES_USER', 'testuser')} "
        f"password={os.getenv('POSTGRES_PASSWORD', 'testpass')}"
    )


@app.route("/health")
def health():
    """Health check endpoint."""
    return jsonify({"status": "healthy"})


@app.route("/db/query")
def db_query():
    """Test simple SELECT query."""
    try:
        with psycopg.connect(get_conn_string()) as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM users ORDER BY id LIMIT 10")
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
            results = [dict(zip(columns, row)) for row in rows]

        return jsonify({"count": len(results), "data": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/db/insert", methods=["POST"])
def db_insert():
    """Test INSERT operation."""
    try:
        data = request.get_json()
        name = data.get("name", "Test User")
        email = data.get("email", f"test{os.urandom(4).hex()}@example.com")

        with psycopg.connect(get_conn_string()) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (name, email) VALUES (%s, %s) RETURNING id, name, email, created_at", (name, email)
            )
            row = cur.fetchone()
            columns = [desc[0] for desc in cur.description]
            user = dict(zip(columns, row))
            conn.commit()

        return jsonify(user), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/db/update/<int:user_id>", methods=["PUT"])
def db_update(user_id):
    """Test UPDATE operation."""
    try:
        data = request.get_json()
        name = data.get("name")

        with psycopg.connect(get_conn_string()) as conn, conn.cursor() as cur:
            cur.execute("UPDATE users SET name = %s WHERE id = %s RETURNING id, name, email", (name, user_id))
            row = cur.fetchone()
            if row:
                columns = [desc[0] for desc in cur.description]
                user = dict(zip(columns, row))
                conn.commit()
                return jsonify(user)
            else:
                return jsonify({"error": "User not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/db/delete/<int:user_id>", methods=["DELETE"])
def db_delete(user_id):
    """Test DELETE operation."""
    try:
        with psycopg.connect(get_conn_string()) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE id = %s RETURNING id", (user_id,))
            row = cur.fetchone()
            conn.commit()

            if row:
                return jsonify({"id": row[0], "deleted": True})
            else:
                return jsonify({"error": "User not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/db/batch-insert", methods=["POST"])
def db_batch_insert():
    """Test batch INSERT with executemany."""
    try:
        data = request.get_json()
        users = data.get("users", [])

        with psycopg.connect(get_conn_string()) as conn, conn.cursor() as cur:
            cur.executemany("INSERT INTO users (name, email) VALUES (%s, %s)", [(u["name"], u["email"]) for u in users])
            conn.commit()

        return jsonify({"inserted": len(users)}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/db/fetchmany-arraysize")
def db_fetchmany_arraysize():
    """Test that fetchmany() respects runtime cursor.arraysize updates."""
    try:
        with psycopg.connect(get_conn_string()) as conn, conn.cursor() as cur:
            cur.execute("SELECT id, name FROM users ORDER BY id")
            cur.arraysize = 2
            batch = cur.fetchmany()

        return jsonify(
            {
                "arraysize": 2,
                "batch_len": len(batch),
                "ids": [row[0] for row in batch],
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/db/error-then-query")
def db_error_then_query():
    """Test error handling + follow-up query in same transaction context."""
    error_message = ""
    follow_up_count = 0
    try:
        with psycopg.connect(get_conn_string()) as conn, conn.cursor() as cur:
            try:
                cur.execute("SELECT * FROM users_missing_table")
                cur.fetchall()
            except Exception as exc:
                error_message = str(exc)
                conn.rollback()

            cur.execute("SELECT id FROM users ORDER BY id LIMIT 1")
            rows = cur.fetchall()
            follow_up_count = len(rows)

        return jsonify(
            {
                "had_error": bool(error_message),
                "error_contains_missing_table": "users_missing_table" in error_message,
                "follow_up_count": follow_up_count,
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/db/transaction", methods=["POST"])
def db_transaction():
    """Test transaction with rollback."""
    try:
        with psycopg.connect(get_conn_string()) as conn:
            with conn.cursor() as cur:
                # Start transaction
                cur.execute(
                    "INSERT INTO users (name, email) VALUES (%s, %s) RETURNING id", ("Temp User", "temp@example.com")
                )
                temp_id = cur.fetchone()[0]

                # Intentionally rollback
                conn.rollback()

        return jsonify({"temp_id": temp_id, "rolled_back": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/cursor-stream")
def test_cursor_stream():
    """Test cursor.stream() - generator-based result streaming.

    This tests whether the instrumentation captures streaming queries
    that return results as a generator.
    """
    try:
        with psycopg.connect(get_conn_string()) as conn, conn.cursor() as cur:
            # Stream results row-by-row instead of fetchall
            results = []
            for row in cur.stream("SELECT id, name, email FROM users ORDER BY id LIMIT 5"):
                results.append({"id": row[0], "name": row[1], "email": row[2]})
        return jsonify({"count": len(results), "data": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/server-cursor")
def test_server_cursor():
    """Test ServerCursor (named cursor) - server-side cursor.

    This tests whether the instrumentation captures server-side cursors
    which use DECLARE CURSOR on the database server.
    """
    try:
        with psycopg.connect(get_conn_string()) as conn:
            # Named cursor creates a server-side cursor
            with conn.cursor(name="test_server_cursor") as cur:
                cur.execute("SELECT id, name, email FROM users ORDER BY id LIMIT 5")
                rows = cur.fetchall()
                columns = [desc[0] for desc in cur.description] if cur.description else ["id", "name", "email"]
                results = [dict(zip(columns, row)) for row in rows]
        return jsonify({"count": len(results), "data": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/copy-to")
def test_copy_to():
    """Test cursor.copy() with COPY TO - bulk data export.

    This tests whether the instrumentation captures COPY operations.
    """
    try:
        with psycopg.connect(get_conn_string()) as conn, conn.cursor() as cur:
            # Use COPY to export data
            output = []
            with cur.copy("COPY (SELECT id, name, email FROM users ORDER BY id LIMIT 5) TO STDOUT") as copy:
                for row in copy:
                    # Handle both bytes and memoryview
                    if isinstance(row, memoryview):
                        row = bytes(row)
                    output.append(row.decode("utf-8").strip())
        return jsonify({"count": len(output), "data": output})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/multiple-queries")
def test_multiple_queries():
    """Test multiple queries in same connection.

    This tests whether multiple queries in the same connection
    are all captured and replayed correctly.
    """
    try:
        with psycopg.connect(get_conn_string()) as conn, conn.cursor() as cur:
            # Query 1
            cur.execute("SELECT COUNT(*) FROM users")
            count = cur.fetchone()[0]

            # Query 2
            cur.execute("SELECT MAX(id) FROM users")
            max_id = cur.fetchone()[0]

            # Query 3
            cur.execute("SELECT MIN(id) FROM users")
            min_id = cur.fetchone()[0]

        return jsonify({"count": count, "max_id": max_id, "min_id": min_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/pipeline-mode")
def test_pipeline_mode():
    """Test pipeline mode - batched operations.

    Pipeline mode allows sending multiple queries without waiting for results.
    This tests whether the instrumentation handles pipeline mode correctly.
    """
    try:
        with psycopg.connect(get_conn_string()) as conn:
            # Enter pipeline mode
            with conn.pipeline() as p:
                cur1 = conn.execute("SELECT id, name FROM users ORDER BY id LIMIT 3")
                cur2 = conn.execute("SELECT COUNT(*) FROM users")
                # Sync the pipeline to get results
                p.sync()

                rows1 = cur1.fetchall()
                count = cur2.fetchone()[0]

        return jsonify({"rows": [{"id": r[0], "name": r[1]} for r in rows1], "count": count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/dict-row-factory")
def test_dict_row_factory():
    """Test dict_row row factory.

    Tests whether the instrumentation correctly handles dict row factories
    which return dictionaries instead of tuples.
    """
    try:
        from psycopg.rows import dict_row

        with psycopg.connect(get_conn_string(), row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, name, email FROM users ORDER BY id LIMIT 3")
                rows = cur.fetchall()

        return jsonify(
            {
                "count": len(rows),
                "data": rows,  # Already dictionaries
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/namedtuple-row-factory")
def test_namedtuple_row_factory():
    """Test namedtuple_row row factory.

    Tests whether the instrumentation correctly handles namedtuple row factories.
    """
    try:
        from psycopg.rows import namedtuple_row

        with psycopg.connect(get_conn_string(), row_factory=namedtuple_row) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, name, email FROM users ORDER BY id LIMIT 3")
                rows = cur.fetchall()

        # Convert named tuples to dicts for JSON serialization
        return jsonify({"count": len(rows), "data": [{"id": r.id, "name": r.name, "email": r.email} for r in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/cursor-iteration")
def test_cursor_iteration():
    """Test direct cursor iteration (for row in cursor).

    Tests whether iterating over cursor directly works correctly.
    """
    try:
        with psycopg.connect(get_conn_string()) as conn, conn.cursor() as cur:
            cur.execute("SELECT id, name FROM users ORDER BY id LIMIT 5")

            # Iterate directly over cursor
            results = []
            for row in cur:
                results.append({"id": row[0], "name": row[1]})

        return jsonify({"count": len(results), "data": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/executemany-returning")
def test_executemany_returning():
    """Test executemany with returning=True.

    Tests whether the instrumentation correctly handles executemany with returning=True.
    """
    try:
        with psycopg.connect(get_conn_string()) as conn, conn.cursor() as cur:
            # Create temp table
            cur.execute("CREATE TEMP TABLE batch_test (id SERIAL, name TEXT)")

            # Use executemany with returning
            params = [("Batch User 1",), ("Batch User 2",), ("Batch User 3",)]
            cur.executemany("INSERT INTO batch_test (name) VALUES (%s) RETURNING id, name", params, returning=True)

            # Fetch results from each batch
            results = []
            for result in cur.results():
                row = result.fetchone()
                if row:
                    results.append({"id": row[0], "name": row[1]})

            conn.commit()

        return jsonify({"count": len(results), "data": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/rownumber")
def test_rownumber():
    """Test cursor.rownumber property.

    Tests whether the rownumber property is properly tracked during replay mode.
    """
    try:
        with psycopg.connect(get_conn_string()) as conn, conn.cursor() as cur:
            cur.execute("SELECT id, name FROM users ORDER BY id LIMIT 5")

            positions = []
            # Record rownumber at each fetch
            positions.append({"before": cur.rownumber})

            cur.fetchone()
            positions.append({"after_fetchone_1": cur.rownumber})

            cur.fetchone()
            positions.append({"after_fetchone_2": cur.rownumber})

            cur.fetchmany(2)
            positions.append({"after_fetchmany_2": cur.rownumber})

        return jsonify({"positions": positions})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/statusmessage")
def test_statusmessage():
    """Test cursor.statusmessage property."""
    try:
        with psycopg.connect(get_conn_string()) as conn, conn.cursor() as cur:
            # SELECT should return something like "SELECT 5"
            cur.execute("SELECT id FROM users LIMIT 5")
            select_status = cur.statusmessage
            cur.fetchall()

            # INSERT should return something like "INSERT 0 1"
            cur.execute(
                "INSERT INTO users (name, email) VALUES (%s, %s) RETURNING id", ("StatusTest", "status@test.com")
            )
            insert_status = cur.statusmessage
            cur.fetchone()

            conn.rollback()  # Don't actually insert

        return jsonify({"select_status": select_status, "insert_status": insert_status})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/nextset")
def test_nextset():
    """Test cursor.nextset() for multiple result sets.

    Tests whether the instrumentation correctly handles nextset() for multiple result sets.
    """
    try:
        with psycopg.connect(get_conn_string()) as conn, conn.cursor() as cur:
            # Create temp table
            cur.execute("CREATE TEMP TABLE nextset_test (id SERIAL, val TEXT)")

            # Insert multiple rows with returning
            cur.executemany(
                "INSERT INTO nextset_test (val) VALUES (%s) RETURNING id, val",
                [("First",), ("Second",), ("Third",)],
                returning=True,
            )

            # Use nextset to iterate through result sets
            results = []
            while True:
                row = cur.fetchone()
                if row:
                    results.append({"id": row[0], "val": row[1]})
                if cur.nextset() is None:
                    break

            conn.commit()

        return jsonify({"count": len(results), "data": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/cursor-scroll")
def test_cursor_scroll():
    """Test cursor.scroll() method.

    Tests whether the instrumentation correctly handles scroll() for cursor position tracking.
    """
    try:
        with psycopg.connect(get_conn_string()) as conn, conn.cursor() as cur:
            cur.execute("SELECT id, name FROM users ORDER BY id")

            # Fetch first row
            first = cur.fetchone()

            # Scroll back to start
            cur.scroll(0, mode="absolute")

            # Fetch first row again
            first_again = cur.fetchone()

        return jsonify(
            {
                "first": {"id": first[0], "name": first[1]} if first else None,
                "first_again": {"id": first_again[0], "name": first_again[1]} if first_again else None,
                "match": first == first_again,
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/server-cursor-scroll")
def test_server_cursor_scroll():
    """Test ServerCursor.scroll() method.

    Tests whether the instrumentation correctly handles scroll() for server-side cursors.
    """
    try:
        with psycopg.connect(get_conn_string()) as conn:
            # Named cursor with scrollable=True
            with conn.cursor(name="scroll_test", scrollable=True) as cur:
                cur.execute("SELECT id, name FROM users ORDER BY id")

                # Fetch first row
                first = cur.fetchone()

                # Scroll back to start
                cur.scroll(0, mode="absolute")

                # Fetch first row again
                first_again = cur.fetchone()

        return jsonify(
            {
                "first": {"id": first[0], "name": first[1]} if first else None,
                "first_again": {"id": first_again[0], "name": first_again[1]} if first_again else None,
                "match": first == first_again,
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/cursor-reuse")
def test_cursor_reuse():
    """Test reusing cursor for multiple queries.

    Tests whether the instrumentation correctly handles reusing a cursor for multiple execute() calls.
    """
    try:
        with psycopg.connect(get_conn_string()) as conn, conn.cursor() as cur:
            # First query
            cur.execute("SELECT id, name FROM users WHERE id = 1")
            row1 = cur.fetchone()

            # Second query on same cursor
            cur.execute("SELECT id, name FROM users WHERE id = 2")
            row2 = cur.fetchone()

            # Third query
            cur.execute("SELECT COUNT(*) FROM users")
            count = cur.fetchone()[0]

        return jsonify(
            {
                "row1": {"id": row1[0], "name": row1[1]} if row1 else None,
                "row2": {"id": row2[0], "name": row2[1]} if row2 else None,
                "count": count,
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/sql-composed")
def test_sql_composed():
    """Test psycopg.sql.SQL() composed queries."""
    try:
        from psycopg import sql

        with psycopg.connect(get_conn_string()) as conn, conn.cursor() as cur:
            table = sql.Identifier("users")
            columns = sql.SQL(", ").join([sql.Identifier("id"), sql.Identifier("name"), sql.Identifier("email")])

            query = sql.SQL("SELECT {} FROM {} ORDER BY id LIMIT 3").format(columns, table)
            cur.execute(query)
            rows = cur.fetchall()

        return jsonify({"count": len(rows), "data": [{"id": r[0], "name": r[1], "email": r[2]} for r in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/binary-uuid")
def test_binary_uuid():
    """Test binary UUID data type.

    Tests whether the instrumentation correctly handles binary UUID data types.
    """
    try:
        import uuid

        with psycopg.connect(get_conn_string()) as conn, conn.cursor() as cur:
            # Create a temp table with UUID column
            cur.execute("CREATE TEMP TABLE uuid_test (id UUID PRIMARY KEY, name TEXT)")

            # Insert a UUID
            test_uuid = uuid.uuid4()
            cur.execute("INSERT INTO uuid_test (id, name) VALUES (%s, %s) RETURNING id, name", (test_uuid, "UUID Test"))
            inserted = cur.fetchone()

            # Query it back
            cur.execute("SELECT id, name FROM uuid_test WHERE id = %s", (test_uuid,))
            queried = cur.fetchone()

            conn.commit()

        return jsonify(
            {
                "inserted_uuid": str(inserted[0]) if inserted else None,
                "queried_uuid": str(queried[0]) if queried else None,
                "match": str(inserted[0]) == str(queried[0]) if inserted and queried else False,
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/binary-bytea")
def test_binary_bytea():
    """Test binary bytea data type.

    Tests whether the instrumentation correctly handles binary bytea data types.
    """
    try:
        with psycopg.connect(get_conn_string()) as conn, conn.cursor() as cur:
            # Create a temp table with bytea column
            cur.execute("CREATE TEMP TABLE bytea_test (id SERIAL, data BYTEA)")

            # Insert binary data
            test_data = b"\x00\x01\x02\x03\xff\xfe\xfd"
            cur.execute("INSERT INTO bytea_test (data) VALUES (%s) RETURNING id, data", (test_data,))
            inserted = cur.fetchone()

            conn.commit()

        # Convert bytes to hex for JSON serialization
        return jsonify(
            {
                "inserted_id": inserted[0] if inserted else None,
                "data_hex": inserted[1].hex() if inserted and inserted[1] else None,
                "data_length": len(inserted[1]) if inserted and inserted[1] else 0,
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/class-row-factory")
def test_class_row_factory():
    """Test class_row row factory.

    Tests whether the instrumentation correctly handles class_row factories
    which return instances of a custom class.
    """
    try:
        from dataclasses import dataclass

        from psycopg.rows import class_row

        @dataclass
        class User:
            id: int
            name: str
            email: str

        with psycopg.connect(get_conn_string(), row_factory=class_row(User)) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, name, email FROM users ORDER BY id LIMIT 3")
                rows = cur.fetchall()

        return jsonify({"count": len(rows), "data": [{"id": r.id, "name": r.name, "email": r.email} for r in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/kwargs-row-factory")
def test_kwargs_row_factory():
    """Test kwargs_row row factory.

    Tests whether the instrumentation correctly handles kwargs_row factories
    which call a function with keyword arguments.
    """
    try:
        from psycopg.rows import kwargs_row

        def make_user_dict(**kwargs):
            return {"user_data": kwargs, "processed": True}

        with psycopg.connect(get_conn_string(), row_factory=kwargs_row(make_user_dict)) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, name, email FROM users ORDER BY id LIMIT 3")
                rows = cur.fetchall()

        return jsonify(
            {
                "count": len(rows),
                "data": rows,  # Already processed dictionaries
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/scalar-row-factory")
def test_scalar_row_factory():
    """Test scalar_row row factory.

    Tests whether the instrumentation correctly handles scalar_row factories
    which return just the first column value.
    """
    try:
        from psycopg.rows import scalar_row

        with psycopg.connect(get_conn_string(), row_factory=scalar_row) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT name FROM users ORDER BY id LIMIT 5")
                rows = cur.fetchall()

        return jsonify(
            {
                "count": len(rows),
                "data": rows,  # Just names as scalars
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/binary-format")
def test_binary_format():
    """Test execute with binary=True parameter.

    Tests whether the instrumentation correctly handles binary format transfers.
    """
    try:
        with psycopg.connect(get_conn_string()) as conn, conn.cursor() as cur:
            # Execute with binary=True
            cur.execute("SELECT id, name FROM users ORDER BY id LIMIT 3", binary=True)
            rows = cur.fetchall()

        return jsonify({"count": len(rows), "data": [{"id": r[0], "name": r[1]} for r in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/null-values")
def test_null_values():
    """Test handling of NULL values in results."""
    try:
        with psycopg.connect(get_conn_string()) as conn, conn.cursor() as cur:
            # Create temp table with nullable columns
            cur.execute("""
                CREATE TEMP TABLE null_test (
                    id INT,
                    nullable_text TEXT,
                    nullable_int INT,
                    nullable_bool BOOLEAN
                )
            """)

            # Insert rows with NULL values
            cur.execute("""
                INSERT INTO null_test VALUES
                    (1, 'has_value', 42, TRUE),
                    (2, NULL, NULL, NULL),
                    (3, 'another', NULL, FALSE)
            """)

            # Query rows
            cur.execute("SELECT * FROM null_test ORDER BY id")
            rows = cur.fetchall()
            conn.commit()

        return jsonify(
            {
                "count": len(rows),
                "data": [
                    {"id": r[0], "nullable_text": r[1], "nullable_int": r[2], "nullable_bool": r[3]} for r in rows
                ],
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/transaction-context")
def test_transaction_context():
    """Test conn.transaction() context manager."""
    try:
        results = []
        with psycopg.connect(get_conn_string()) as conn:
            # Use explicit transaction
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("CREATE TEMP TABLE tx_test (id INT, val TEXT)")
                    cur.execute("INSERT INTO tx_test VALUES (1, 'first')")
                    cur.execute("SELECT * FROM tx_test")
                    rows = cur.fetchall()
                    results.append({"phase": "inside_transaction", "rows": [list(r) for r in rows]})

            # After transaction commit
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM tx_test")
                rows = cur.fetchall()
                results.append({"phase": "after_commit", "rows": [list(r) for r in rows]})

        return jsonify({"results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/json-jsonb")
def test_json_jsonb():
    """Test JSON and JSONB data types."""
    try:
        with psycopg.connect(get_conn_string()) as conn, conn.cursor() as cur:
            # Create temp table with JSON columns
            cur.execute("""
                CREATE TEMP TABLE json_test (
                    id INT,
                    json_col JSON,
                    jsonb_col JSONB
                )
            """)

            # Insert JSON data
            import json

            test_json = {"name": "test", "values": [1, 2, 3], "nested": {"key": "value"}}
            cur.execute("INSERT INTO json_test VALUES (%s, %s, %s)", (1, json.dumps(test_json), json.dumps(test_json)))

            # Query back
            cur.execute("SELECT * FROM json_test WHERE id = 1")
            row = cur.fetchone()
            conn.commit()

        return jsonify({"id": row[0], "json_col": row[1], "jsonb_col": row[2]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/array-types")
def test_array_types():
    """Test PostgreSQL array types."""
    try:
        with psycopg.connect(get_conn_string()) as conn, conn.cursor() as cur:
            # Create temp table with array columns
            cur.execute("""
                CREATE TEMP TABLE array_test (
                    id INT,
                    int_array INTEGER[],
                    text_array TEXT[]
                )
            """)

            # Insert array data
            cur.execute("INSERT INTO array_test VALUES (%s, %s, %s)", (1, [10, 20, 30], ["a", "b", "c"]))

            # Query back
            cur.execute("SELECT * FROM array_test WHERE id = 1")
            row = cur.fetchone()
            conn.commit()

        return jsonify(
            {
                "id": row[0],
                "int_array": list(row[1]) if row[1] else None,
                "text_array": list(row[2]) if row[2] else None,
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/cursor-set-result")
def test_cursor_set_result():
    """Test cursor.set_result() method.

    This tests whether the instrumentation correctly handles
    set_result() for navigating between result sets.
    """
    try:
        with psycopg.connect(get_conn_string()) as conn, conn.cursor() as cur:
            # Create temp table and insert with returning
            cur.execute("CREATE TEMP TABLE setresult_test (id SERIAL, val TEXT)")

            cur.executemany(
                "INSERT INTO setresult_test (val) VALUES (%s) RETURNING id, val",
                [("First",), ("Second",), ("Third",)],
                returning=True,
            )

            # Use set_result to navigate to specific result sets
            results = []

            # Go to the last result set
            cur.set_result(-1)
            row = cur.fetchone()
            results.append({"set": "last", "data": {"id": row[0], "val": row[1]} if row else None})

            # Go to the first result set
            cur.set_result(0)
            row = cur.fetchone()
            results.append({"set": "first", "data": {"id": row[0], "val": row[1]} if row else None})

            conn.commit()

        return jsonify({"results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/decimal-types")
def test_decimal_types():
    """Test Decimal/numeric types."""
    try:
        from decimal import Decimal

        with psycopg.connect(get_conn_string()) as conn, conn.cursor() as cur:
            # Create temp table with numeric columns
            cur.execute("""
                CREATE TEMP TABLE decimal_test (
                    id INT,
                    price DECIMAL(10, 2),
                    rate DECIMAL(18, 8)
                )
            """)

            # Insert decimal data
            cur.execute("INSERT INTO decimal_test VALUES (%s, %s, %s)", (1, Decimal("123.45"), Decimal("0.00000001")))

            # Query back
            cur.execute("SELECT * FROM decimal_test WHERE id = 1")
            row = cur.fetchone()
            conn.commit()

        return jsonify(
            {"id": row[0], "price": str(row[1]) if row[1] else None, "rate": str(row[2]) if row[2] else None}
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/date-time-types")
def test_date_time_types():
    """Test date/time types."""
    try:
        from datetime import date, time, timedelta

        with psycopg.connect(get_conn_string()) as conn, conn.cursor() as cur:
            # Create temp table with date/time columns
            cur.execute("""
                CREATE TEMP TABLE datetime_test (
                    id INT,
                    birth_date DATE,
                    wake_time TIME,
                    duration INTERVAL
                )
            """)

            # Insert date/time data
            cur.execute(
                "INSERT INTO datetime_test VALUES (%s, %s, %s, %s)",
                (1, date(1990, 5, 15), time(8, 30, 0), timedelta(hours=2, minutes=30)),
            )

            # Query back
            cur.execute("SELECT * FROM datetime_test WHERE id = 1")
            row = cur.fetchone()
            conn.commit()

        return jsonify(
            {
                "id": row[0],
                "birth_date": str(row[1]) if row[1] else None,
                "wake_time": str(row[2]) if row[2] else None,
                "duration": str(row[3]) if row[3] else None,
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/inet-cidr-types")
def test_inet_cidr_types():
    """Test PostgreSQL inet/cidr network types."""
    try:
        from ipaddress import IPv4Address, IPv4Network

        with psycopg.connect(get_conn_string()) as conn, conn.cursor() as cur:
            # Create temp table with network columns
            cur.execute("""
                CREATE TEMP TABLE network_test (
                    id INT,
                    ip_addr INET,
                    network CIDR
                )
            """)

            # Insert network data
            cur.execute("INSERT INTO network_test VALUES (%s, %s, %s)", (1, "192.168.1.100", "10.0.0.0/8"))

            # Query back
            cur.execute("SELECT * FROM network_test WHERE id = 1")
            row = cur.fetchone()
            conn.commit()

        return jsonify(
            {"id": row[0], "ip_addr": str(row[1]) if row[1] else None, "network": str(row[2]) if row[2] else None}
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/range-types")
def test_range_types():
    """Test PostgreSQL range types."""
    try:
        from psycopg.types.range import Range

        with psycopg.connect(get_conn_string()) as conn, conn.cursor() as cur:
            # Create temp table with range columns
            cur.execute("""
                CREATE TEMP TABLE range_test (
                    id INT,
                    int_range INT4RANGE,
                    ts_range TSRANGE
                )
            """)

            # Insert range data
            from datetime import datetime

            int_range = Range(1, 10)
            ts_range = Range(datetime(2024, 1, 1, 0, 0), datetime(2024, 12, 31, 23, 59))

            cur.execute("INSERT INTO range_test VALUES (%s, %s, %s)", (1, int_range, ts_range))

            # Query back
            cur.execute("SELECT * FROM range_test WHERE id = 1")
            row = cur.fetchone()
            conn.commit()

        return jsonify(
            {"id": row[0], "int_range": str(row[1]) if row[1] else None, "ts_range": str(row[2]) if row[2] else None}
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    sdk.mark_app_as_ready()
    app.run(host="0.0.0.0", port=8000, debug=False)
