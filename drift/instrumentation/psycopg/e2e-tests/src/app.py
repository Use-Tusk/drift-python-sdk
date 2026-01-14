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
            results = [dict(zip(columns, row, strict=False)) for row in rows]

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
            user = dict(zip(columns, row, strict=False))
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
                user = dict(zip(columns, row, strict=False))
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
                results = [dict(zip(columns, row, strict=False)) for row in rows]
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
                    output.append(row.decode('utf-8').strip())
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

        return jsonify({
            "rows": [{"id": r[0], "name": r[1]} for r in rows1],
            "count": count
        })
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

        return jsonify({
            "count": len(rows),
            "data": rows  # Already dictionaries
        })
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
        return jsonify({
            "count": len(rows),
            "data": [{"id": r.id, "name": r.name, "email": r.email} for r in rows]
        })
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

        return jsonify({
            "count": len(results),
            "data": results
        })
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
            cur.executemany(
                "INSERT INTO batch_test (name) VALUES (%s) RETURNING id, name",
                params,
                returning=True
            )

            # Fetch results from each batch
            results = []
            for result in cur.results():
                row = result.fetchone()
                if row:
                    results.append({"id": row[0], "name": row[1]})

            conn.commit()

        return jsonify({
            "count": len(results),
            "data": results
        })
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

        return jsonify({
            "positions": positions
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/test/statusmessage")
def test_statusmessage():
    """Test cursor.statusmessage property.

    BUG: The statusmessage property is not captured during RECORD mode
    and not mocked during REPLAY mode. During RECORD, statusmessage
    returns the command status (e.g., "SELECT 5", "INSERT 0 1"), but
    during REPLAY it returns null because this property is not tracked.
    """
    try:
        with psycopg.connect(get_conn_string()) as conn, conn.cursor() as cur:
            # SELECT should return something like "SELECT 5"
            cur.execute("SELECT id FROM users LIMIT 5")
            select_status = cur.statusmessage
            cur.fetchall()

            # INSERT should return something like "INSERT 0 1"
            cur.execute(
                "INSERT INTO users (name, email) VALUES (%s, %s) RETURNING id",
                ("StatusTest", "status@test.com")
            )
            insert_status = cur.statusmessage
            cur.fetchone()

            conn.rollback()  # Don't actually insert

        return jsonify({
            "select_status": select_status,
            "insert_status": insert_status
        })
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
                returning=True
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

        return jsonify({
            "count": len(results),
            "data": results
        })
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
            cur.scroll(0, mode='absolute')

            # Fetch first row again
            first_again = cur.fetchone()

        return jsonify({
            "first": {"id": first[0], "name": first[1]} if first else None,
            "first_again": {"id": first_again[0], "name": first_again[1]} if first_again else None,
            "match": first == first_again
        })
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
                cur.scroll(0, mode='absolute')

                # Fetch first row again
                first_again = cur.fetchone()

        return jsonify({
            "first": {"id": first[0], "name": first[1]} if first else None,
            "first_again": {"id": first_again[0], "name": first_again[1]} if first_again else None,
            "match": first == first_again
        })
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

        return jsonify({
            "row1": {"id": row1[0], "name": row1[1]} if row1 else None,
            "row2": {"id": row2[0], "name": row2[1]} if row2 else None,
            "count": count
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/test/sql-composed")
def test_sql_composed():
    """Test psycopg.sql.SQL() composed queries."""
    try:
        from psycopg import sql

        with psycopg.connect(get_conn_string()) as conn, conn.cursor() as cur:
            table = sql.Identifier("users")
            columns = sql.SQL(", ").join([
                sql.Identifier("id"),
                sql.Identifier("name"),
                sql.Identifier("email")
            ])

            query = sql.SQL("SELECT {} FROM {} ORDER BY id LIMIT 3").format(columns, table)
            cur.execute(query)
            rows = cur.fetchall()

        return jsonify({
            "count": len(rows),
            "data": [{"id": r[0], "name": r[1], "email": r[2]} for r in rows]
        })
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
            cur.execute(
                "INSERT INTO uuid_test (id, name) VALUES (%s, %s) RETURNING id, name",
                (test_uuid, "UUID Test")
            )
            inserted = cur.fetchone()

            # Query it back
            cur.execute("SELECT id, name FROM uuid_test WHERE id = %s", (test_uuid,))
            queried = cur.fetchone()

            conn.commit()

        return jsonify({
            "inserted_uuid": str(inserted[0]) if inserted else None,
            "queried_uuid": str(queried[0]) if queried else None,
            "match": str(inserted[0]) == str(queried[0]) if inserted and queried else False
        })
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
            test_data = b'\x00\x01\x02\x03\xff\xfe\xfd'
            cur.execute(
                "INSERT INTO bytea_test (data) VALUES (%s) RETURNING id, data",
                (test_data,)
            )
            inserted = cur.fetchone()

            conn.commit()

        # Convert bytes to hex for JSON serialization
        return jsonify({
            "inserted_id": inserted[0] if inserted else None,
            "data_hex": inserted[1].hex() if inserted and inserted[1] else None,
            "data_length": len(inserted[1]) if inserted and inserted[1] else 0
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/test/class-row-factory")
def test_class_row_factory():
    """Test class_row row factory.

    Tests whether the instrumentation correctly handles class_row factories
    which return instances of a custom class.
    """
    try:
        from psycopg.rows import class_row
        from dataclasses import dataclass

        @dataclass
        class User:
            id: int
            name: str
            email: str

        with psycopg.connect(get_conn_string(), row_factory=class_row(User)) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, name, email FROM users ORDER BY id LIMIT 3")
                rows = cur.fetchall()

        return jsonify({
            "count": len(rows),
            "data": [{"id": r.id, "name": r.name, "email": r.email} for r in rows]
        })
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

        return jsonify({
            "count": len(rows),
            "data": rows  # Already processed dictionaries
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# BUG HUNTING TEST ENDPOINTS

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

        return jsonify({
            "count": len(rows),
            "data": rows  # Just names as scalars
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    sdk.mark_app_as_ready()
    app.run(host="0.0.0.0", port=8000, debug=False)
