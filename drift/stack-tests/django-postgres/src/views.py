"""Django views for PostgreSQL test."""

import json
import os

import psycopg2
import psycopg2.extras
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST


@require_GET
def health(request):
    """Health check endpoint."""
    return JsonResponse({"status": "healthy"})


@require_GET
def db_query(request):
    """Test simple SELECT query using Django's connection."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id, name, email, created_at FROM users ORDER BY id LIMIT 10")
            columns = [col[0] for col in cursor.description]
            results = [dict(zip(columns, row)) for row in cursor.fetchall()]

        # Convert datetime objects to strings for JSON serialization
        for row in results:
            if row.get("created_at"):
                row["created_at"] = str(row["created_at"])

        return JsonResponse({"count": len(results), "data": results})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
@require_POST
def db_insert(request):
    """Test INSERT operation using Django's connection."""
    try:
        data = json.loads(request.body)
        name = data.get("name", "Test User")
        email = data.get("email", f"test{os.urandom(4).hex()}@example.com")

        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO users (name, email) VALUES (%s, %s) RETURNING id, name, email, created_at",
                [name, email],
            )
            row = cursor.fetchone()
            columns = [col[0] for col in cursor.description]
            user = dict(zip(columns, row))

        # Convert datetime to string
        if user.get("created_at"):
            user["created_at"] = str(user["created_at"])

        return JsonResponse(user, status=201)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
@require_http_methods(["PUT"])
def db_update(request, user_id):
    """Test UPDATE operation using Django's connection."""
    try:
        data = json.loads(request.body)
        name = data.get("name")

        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE users SET name = %s WHERE id = %s RETURNING id, name, email",
                [name, user_id],
            )
            row = cursor.fetchone()

            if row:
                columns = [col[0] for col in cursor.description]
                user = dict(zip(columns, row))
                return JsonResponse(user)
            else:
                return JsonResponse({"error": "User not found"}, status=404)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
@require_http_methods(["DELETE"])
def db_delete(request, user_id):
    """Test DELETE operation using Django's connection."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM users WHERE id = %s RETURNING id", [user_id])
            row = cursor.fetchone()

            if row:
                return JsonResponse({"id": row[0], "deleted": True})
            else:
                return JsonResponse({"error": "User not found"}, status=404)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@require_GET
def db_register_jsonb(request):
    """Test register_default_jsonb on InstrumentedConnection.

    This is the key test that validates the fix for the bug where
    Django's PostgreSQL backend calls psycopg2.extras.register_default_jsonb(connection)
    after connect(), which fails if the connection is wrapped in InstrumentedConnection
    because the C extension register_type() does strict type checking.

    This test uses a direct psycopg2 connection (not Django's pooled connection)
    to simulate what Django does internally during connection initialization.
    """
    try:
        # Build connection string from environment
        conn_string = (
            f"host={os.getenv('POSTGRES_HOST', 'postgres')} "
            f"port={os.getenv('POSTGRES_PORT', '5432')} "
            f"dbname={os.getenv('POSTGRES_DB', 'testdb')} "
            f"user={os.getenv('POSTGRES_USER', 'testuser')} "
            f"password={os.getenv('POSTGRES_PASSWORD', 'testpass')}"
        )

        # Get a fresh connection (will be wrapped in InstrumentedConnection by SDK)
        conn = psycopg2.connect(conn_string)

        # This simulates what Django's PostgreSQL backend does:
        # After getting a connection, it registers JSON/JSONB types
        # This will fail if conn is InstrumentedConnection and the SDK
        # doesn't properly unwrap it before calling the C extension
        psycopg2.extras.register_default_jsonb(conn, globally=False)

        # If we get here, registration succeeded - verify with a query
        cur = conn.cursor()
        cur.execute("SELECT 1 as test")
        cur.close()
        conn.close()

        return JsonResponse({"status": "success", "message": "register_default_jsonb worked on InstrumentedConnection"})
    except TypeError as e:
        # This is the error that occurs when InstrumentedConnection fails type check
        return JsonResponse(
            {
                "error": str(e),
                "error_type": "TypeError",
                "message": "register_default_jsonb failed - InstrumentedConnection not properly handled",
            },
            status=500,
        )
    except Exception as e:
        return JsonResponse({"error": str(e), "error_type": type(e).__name__}, status=500)


@csrf_exempt
@require_POST
def db_transaction(request):
    """Test transaction with rollback using Django's connection."""
    try:
        with connection.cursor() as cursor:
            # Start transaction
            cursor.execute(
                "INSERT INTO users (name, email) VALUES (%s, %s) RETURNING id",
                ["Temp User", "temp@example.com"],
            )
            temp_id = cursor.fetchone()[0]

        # Intentionally rollback by not committing
        # Django auto-commits, so we need to use atomic() for explicit transactions
        from django.db import transaction

        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO users (name, email) VALUES (%s, %s) RETURNING id",
                        ["Rollback User", "rollback@example.com"],
                    )
                    _rollback_id = cursor.fetchone()[0]  # noqa: F841
                    # Force rollback by raising exception
                    raise Exception("Intentional rollback")
        except Exception:
            pass  # Expected

        return JsonResponse({"temp_id": temp_id, "message": "Transaction test completed"})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@require_GET
def db_raw_connection(request):
    """Test using Django's raw database connection.

    This tests that Django's connection wrapper works correctly with the SDK's
    psycopg2 instrumentation.
    """
    try:
        # Get the raw psycopg2 connection from Django
        raw_conn = connection.connection

        # Use a cursor from the raw connection
        cur = raw_conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users")
        count = cur.fetchone()[0]
        cur.close()

        return JsonResponse(
            {
                "status": "success",
                "count": count,
                "connection_type": type(raw_conn).__name__,
            }
        )
    except Exception as e:
        return JsonResponse({"error": str(e), "error_type": type(e).__name__}, status=500)
