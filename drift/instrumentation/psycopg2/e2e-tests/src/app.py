"""Flask app with Psycopg2 (legacy) operations for e2e testing."""

import os
import psycopg2
import psycopg2.extras
from drift import TuskDrift
from flask import Flask, jsonify, request

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
            "INSERT INTO users (name, email) VALUES (%s, %s) RETURNING id, name, email, created_at",
            (name, email)
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
        cur.execute(
            "UPDATE users SET name = %s WHERE id = %s RETURNING id, name, email",
            (name, user_id)
        )
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
        cur.executemany(
            "INSERT INTO users (name, email) VALUES (%s, %s)",
            [(u["name"], u["email"]) for u in users]
        )
        conn.commit()
        cur.close()
        conn.close()

        return jsonify({"inserted": len(users)}), 201
    except Exception as e:
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


if __name__ == "__main__":
    sdk.mark_app_as_ready()
    app.run(host="0.0.0.0", port=8000, debug=False)
