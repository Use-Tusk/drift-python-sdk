"""FastAPI + PostgreSQL (psycopg3) test application."""

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Optional

import psycopg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from drift import TuskDrift

# Initialize SDK
sdk = TuskDrift.initialize(
    api_key="tusk-test-key",
    log_level="debug",
)


def get_conn_string():
    """Build connection string from environment variables."""
    return (
        f"host={os.getenv('POSTGRES_HOST', 'postgres')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"dbname={os.getenv('POSTGRES_DB', 'testdb')} "
        f"user={os.getenv('POSTGRES_USER', 'testuser')} "
        f"password={os.getenv('POSTGRES_PASSWORD', 'testpass')}"
    )


# Connection pool for async operations
_async_pool = None


@asynccontextmanager
async def get_async_connection():
    """Get an async connection from pool."""
    global _async_pool
    if _async_pool is None:
        _async_pool = psycopg.AsyncConnectionPool(get_conn_string(), min_size=1, max_size=5)
        await _async_pool.open()
    async with _async_pool.connection() as conn:
        yield conn


app = FastAPI(title="FastAPI + PostgreSQL Stack Test")


# Health check endpoint
@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy"}


@app.get("/db/query")
async def db_query():
    """Test simple SELECT query using async psycopg3."""
    try:
        async with get_async_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT id, name, email, created_at FROM users ORDER BY id LIMIT 10")
                rows = await cur.fetchall()
                columns = [desc[0] for desc in cur.description]
                results = [dict(zip(columns, row)) for row in rows]

        # Convert datetime objects to strings for JSON serialization
        for row in results:
            if row.get("created_at"):
                row["created_at"] = str(row["created_at"])

        return {"count": len(results), "data": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class CreateUserRequest(BaseModel):
    name: str = "Test User"
    email: Optional[str] = None


@app.post("/db/insert", status_code=201)
async def db_insert(user: CreateUserRequest):
    """Test INSERT operation using async psycopg3."""
    try:
        email = user.email or f"test{os.urandom(4).hex()}@example.com"

        async with get_async_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO users (name, email) VALUES (%s, %s) RETURNING id, name, email, created_at",
                    (user.name, email),
                )
                row = await cur.fetchone()
                columns = [desc[0] for desc in cur.description]
                result = dict(zip(columns, row))
                await conn.commit()

        # Convert datetime to string
        if result.get("created_at"):
            result["created_at"] = str(result["created_at"])

        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class UpdateUserRequest(BaseModel):
    name: str


@app.put("/db/update/{user_id}")
async def db_update(user_id: int, user: UpdateUserRequest):
    """Test UPDATE operation using async psycopg3."""
    try:
        async with get_async_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE users SET name = %s WHERE id = %s RETURNING id, name, email",
                    (user.name, user_id),
                )
                row = await cur.fetchone()

                if row:
                    columns = [desc[0] for desc in cur.description]
                    result = dict(zip(columns, row))
                    await conn.commit()
                    return result
                else:
                    raise HTTPException(status_code=404, detail="User not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/db/delete/{user_id}")
async def db_delete(user_id: int):
    """Test DELETE operation using async psycopg3."""
    try:
        async with get_async_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM users WHERE id = %s RETURNING id", (user_id,))
                row = await cur.fetchone()
                await conn.commit()

                if row:
                    return {"id": row[0], "deleted": True}
                else:
                    raise HTTPException(status_code=404, detail="User not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/db/async-context")
async def db_async_context():
    """Test async context propagation with concurrent database queries.

    This tests that OpenTelemetry context is properly propagated across
    async boundaries when making concurrent database calls.
    """
    try:

        async def query_count():
            async with get_async_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT COUNT(*) FROM users")
                    row = await cur.fetchone()
                    return row[0]

        async def query_max_id():
            async with get_async_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT MAX(id) FROM users")
                    row = await cur.fetchone()
                    return row[0]

        async def query_min_id():
            async with get_async_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT MIN(id) FROM users")
                    row = await cur.fetchone()
                    return row[0]

        # Run queries concurrently
        results = await asyncio.gather(
            query_count(),
            query_max_id(),
            query_min_id(),
        )

        return {
            "count": results[0],
            "max_id": results[1],
            "min_id": results[2],
            "concurrent_queries": 3,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/db/transaction")
async def db_transaction():
    """Test async transaction with rollback."""
    try:
        async with get_async_connection() as conn:
            # Use explicit transaction
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        "INSERT INTO users (name, email) VALUES (%s, %s) RETURNING id",
                        ("Transaction User", "transaction@example.com"),
                    )
                    insert_id = (await cur.fetchone())[0]

                    # Query inside transaction
                    await cur.execute("SELECT COUNT(*) FROM users")
                    count_inside = (await cur.fetchone())[0]

            # After transaction commits, query again
            async with conn.cursor() as cur:
                await cur.execute("SELECT COUNT(*) FROM users")
                count_after = (await cur.fetchone())[0]

        return {
            "insert_id": insert_id,
            "count_inside_tx": count_inside,
            "count_after_commit": count_after,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/db/sync-fallback")
async def db_sync_fallback():
    """Test synchronous psycopg3 fallback in async context.

    Some applications may use sync psycopg3 within async handlers.
    This tests that the instrumentation handles this case.
    """
    try:
        # Use synchronous connection within async handler
        with psycopg.connect(get_conn_string()) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM users")
                count = cur.fetchone()[0]

        return {
            "status": "success",
            "count": count,
            "mode": "sync_in_async",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/db/pipeline")
async def db_pipeline():
    """Test async pipeline mode.

    Pipeline mode batches multiple queries for better performance.
    This tests that the instrumentation handles pipeline mode correctly.
    """
    try:
        async with get_async_connection() as conn:
            async with conn.pipeline():
                async with conn.cursor() as cur1:
                    await cur1.execute("SELECT id, name FROM users ORDER BY id LIMIT 3")
                async with conn.cursor() as cur2:
                    await cur2.execute("SELECT COUNT(*) FROM users")

                rows = await cur1.fetchall()
                count = (await cur2.fetchone())[0]

        return {
            "rows": [{"id": r[0], "name": r[1]} for r in rows],
            "count": count,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    sdk.mark_app_as_ready()
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
