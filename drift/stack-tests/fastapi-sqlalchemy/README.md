# FastAPI + SQLAlchemy Stack Test

## Status: ⚠️ WORK IN PROGRESS

This stack test is **not fully working** yet. Recording works, but replay fails.

## The Problem

SQLAlchemy's psycopg dialect accesses `cursor.pgresult` (a low-level libpq result object) during dialect initialization to get the PostgreSQL server version. In REPLAY mode, since no real query was executed, `pgresult` returns `None`, and SQLAlchemy passes this to `re.match()` causing:

```text
TypeError: expected string or bytes-like object
```

This happens during SQLAlchemy's initialization when it runs `SELECT pg_catalog.version()`.

## What Works

- **RECORD mode**: All database queries are properly captured and traced
- **Health endpoint in REPLAY**: Non-database endpoints work fine

## What Doesn't Work

- **REPLAY mode for DB endpoints**: SQLAlchemy's dialect initialization fails because it can't access the mocked version query result

## Root Cause

psycopg3 uses **C-level descriptors** for properties like `pgresult`. Attempts to override this property at the Python level (even a simple pass-through `return super().pgresult`) break RECORD mode entirely. Python-level property overrides don't work well with psycopg's C internals.

## Attempted Solutions

1. **Adding a `pgresult` property** that returns `None` in mock mode → Breaks RECORD mode entirely
2. **Using sync fetch methods** → Breaks raw psycopg async (`await cursor.fetchone()`)
3. **Adding `__iter__`/`__next__` methods** → SQLAlchemy uses `pgresult`, not fetch methods
4. **MockAsyncCursor** → Would require major refactoring

## Potential Future Solutions

1. **Add SQLAlchemy instrumentation** (most promising): Instead of fighting psycopg's C-level descriptors, create a dedicated SQLAlchemy instrumentation that patches at a higher level:
   - SQLAlchemy's code is pure Python, not C extensions, making it more patchable
   - Intercept `dialect.initialize()` in REPLAY mode to skip or mock the version query
   - Intercept at `Session.execute()` level where SQLAlchemy wraps results in its own `Result` object
   - Similar to how OpenTelemetry has both driver-level (psycopg) AND ORM-level (SQLAlchemy) instrumentations

2. **Use MockAsyncCursor in REPLAY mode**: Return a fully mocked cursor instead of an instrumented real cursor when SQLAlchemy is detected

3. **Upstream psycopg changes**: May require changes to how psycopg exposes `pgresult`

## Strategic Considerations: SQLAlchemy Instrumentation

A dedicated SQLAlchemy instrumentation would be a **high-leverage investment** because it would provide multi-database support from a single implementation.

### What You'd Get "For Free"

SQLAlchemy supports multiple database backends. An instrumentation at the SQLAlchemy layer would automatically cover:
- **PostgreSQL** (via psycopg, asyncpg, psycopg2)
- **MySQL** (via pymysql, mysqlclient, aiomysql)
- **SQLite** (via sqlite3, aiosqlite)
- **Oracle** (via cx_Oracle)
- **SQL Server** (via pyodbc, pymssql)

All captured at `Session.execute()` / `Engine.execute()` level with a single instrumentation.

### Caveats

1. **Only covers SQLAlchemy/SQLModel users**: Applications using raw database drivers directly (e.g., `pymysql.connect()`, `sqlite3.connect()`) would not be instrumented

2. **REPLAY mode complexity**: Even with SQLAlchemy instrumentation, REPLAY still needs to handle connections. Options:
   - Mock at SQLAlchemy's `Result` level (cleanest approach)
   - Or still need mock connections per database (defeats the purpose)

3. **Query matching across dialects**: Different databases have different SQL dialects. A query recorded with PostgreSQL syntax won't match MySQL syntax for the same logical operation.

### Recommended Architecture

| Layer | Coverage | Use Case |
|-------|----------|----------|
| SQLAlchemy instrumentation | All SQLAlchemy-supported DBs | ORM users (majority of modern Python apps) |
| Driver instrumentations (psycopg, etc.) | Specific database | Raw driver users, edge cases |

### Risks of SQLAlchemy Instrumentation

1. **API surface complexity**: SQLAlchemy has a large API surface (Core, ORM, async, sync). Would need to handle all code paths.

2. **Version compatibility**: SQLAlchemy 1.x vs 2.x have significant differences. May need version-specific handling.

3. **Greenlet complexity**: SQLAlchemy uses greenlets for async execution. Dynamic patching of async methods may interact poorly with greenlet context switching (similar to the current issue).

4. **ORM-specific behaviors**: Lazy loading, relationship fetching, identity maps all have complex query patterns that may be tricky to mock correctly.

### Mitigations

- Target SQLAlchemy 2.x only (modern apps)
- Focus on `Session.execute()` level, not internal query generation
- Leverage SQLAlchemy's event system (`before_execute`, `after_execute`) instead of monkey-patching
- Extensive test coverage across multiple database backends

## Running This Test

```bash
cd drift/stack-tests/fastapi-sqlalchemy
./run.sh
```

Expected outcome: RECORD phase succeeds, REPLAY phase fails for database endpoints.
