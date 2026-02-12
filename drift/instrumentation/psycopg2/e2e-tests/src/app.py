"""Flask app with Psycopg2 (legacy) operations for e2e testing."""

import os

import psycopg2
import psycopg2.extras
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
        conn = psycopg2.connect(get_conn_string())
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM users ORDER BY id LIMIT 10")
        results = cur.fetchall()
        cur.close()
        conn.close()

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

        conn = psycopg2.connect(get_conn_string())
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "INSERT INTO users (name, email) VALUES (%s, %s) RETURNING id, name, email, created_at", (name, email)
        )
        user = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()

        return jsonify(dict(user)), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/db/update/<int:user_id>", methods=["PUT"])
def db_update(user_id):
    """Test UPDATE operation."""
    try:
        data = request.get_json()
        name = data.get("name")

        conn = psycopg2.connect(get_conn_string())
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("UPDATE users SET name = %s WHERE id = %s RETURNING id, name, email", (name, user_id))
        row = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()

        if row:
            return jsonify(dict(row))
        else:
            return jsonify({"error": "User not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/db/delete/<int:user_id>", methods=["DELETE"])
def db_delete(user_id):
    """Test DELETE operation."""
    try:
        conn = psycopg2.connect(get_conn_string())
        cur = conn.cursor()
        cur.execute("DELETE FROM users WHERE id = %s RETURNING id", (user_id,))
        row = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()

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

        conn = psycopg2.connect(get_conn_string())
        cur = conn.cursor()
        cur.executemany("INSERT INTO users (name, email) VALUES (%s, %s)", [(u["name"], u["email"]) for u in users])
        conn.commit()
        cur.close()
        conn.close()

        return jsonify({"inserted": len(users)}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/db/fetchmany-arraysize")
def db_fetchmany_arraysize():
    """Test that fetchmany() respects runtime cursor.arraysize updates."""
    try:
        conn = psycopg2.connect(get_conn_string())
        cur = conn.cursor()
        cur.execute("SELECT id, name FROM users ORDER BY id")
        cur.arraysize = 2
        batch = cur.fetchmany()
        cur.close()
        conn.close()

        return jsonify({"arraysize": 2, "batch_len": len(batch), "ids": [row[0] for row in batch]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/db/error-then-query")
def db_error_then_query():
    """Test error handling + follow-up query in same session/transaction."""
    error_message = ""
    follow_up_count = 0
    conn = None
    cur = None
    try:
        conn = psycopg2.connect(get_conn_string())
        cur = conn.cursor()
        try:
            cur.execute("SELECT * FROM users_missing_table")
            cur.fetchall()
        except Exception as exc:
            error_message = str(exc)
            conn.rollback()

        cur.execute("SELECT id FROM users ORDER BY id LIMIT 1")
        rows = cur.fetchall()
        follow_up_count = len(rows)
        cur.close()
        conn.close()

        return jsonify(
            {
                "had_error": bool(error_message),
                "error_contains_missing_table": "users_missing_table" in error_message,
                "follow_up_count": follow_up_count,
            }
        )
    except Exception as e:
        if cur:
            cur.close()
        if conn:
            conn.close()
        return jsonify({"error": str(e)}), 500


@app.route("/db/transaction", methods=["POST"])
def db_transaction():
    """Test transaction with rollback."""
    try:
        conn = psycopg2.connect(get_conn_string())
        cur = conn.cursor()
        # Start transaction
        cur.execute("INSERT INTO users (name, email) VALUES (%s, %s) RETURNING id", ("Temp User", "temp@example.com"))
        temp_id = cur.fetchone()[0]

        # Intentionally rollback
        conn.rollback()
        cur.close()
        conn.close()

        return jsonify({"temp_id": temp_id, "rolled_back": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/db/register-jsonb")
def db_register_jsonb():
    """Test register_default_jsonb on InstrumentedConnection.

    This reproduces the Django admin panel bug where Django calls
    psycopg2.extras.register_default_jsonb(connection) after connect(),
    but the connection is wrapped in InstrumentedConnection which fails
    the C extension type check.
    """
    try:
        conn = psycopg2.connect(get_conn_string())

        # This simulates what Django's PostgreSQL backend does:
        # After getting a connection, it registers JSON/JSONB types
        # This will fail if conn is InstrumentedConnection because
        # register_type is a C extension that does strict type checking
        psycopg2.extras.register_default_jsonb(conn, globally=False)

        # If we get here, registration succeeded
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        conn.close()

        return jsonify({"status": "success", "message": "register_default_jsonb worked on InstrumentedConnection"})
    except TypeError as e:
        # The expected error when InstrumentedConnection fails type check
        return jsonify({"error": str(e), "error_type": "TypeError"}), 500
    except Exception as e:
        return jsonify({"error": str(e), "error_type": type(e).__name__}), 500


if __name__ == "__main__":
    sdk.mark_app_as_ready()
    app.run(host="0.0.0.0", port=8000, debug=False)
