"""FastAPI + SQLAlchemy test application.

This stack test validates that our psycopg instrumentation correctly captures
database queries made through SQLAlchemy ORM. SQLAlchemy uses psycopg as the
underlying driver, so our driver-level instrumentation should capture all queries.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import Column, DateTime, Integer, String, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from drift import TuskDrift

# Initialize SDK
sdk = TuskDrift.initialize(
    api_key="tusk-test-key",
    log_level="debug",
)


def get_database_url():
    """Build async database URL from environment variables."""
    host = os.getenv("POSTGRES_HOST", "postgres")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "testdb")
    user = os.getenv("POSTGRES_USER", "testuser")
    password = os.getenv("POSTGRES_PASSWORD", "testpass")
    # Use psycopg (async) driver
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{db}"


# SQLAlchemy setup
engine = create_async_engine(get_database_url(), echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# Base class for ORM models
class Base(DeclarativeBase):
    pass


# User ORM model
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    email = Column(String(255), unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


# Dependency to get database session
async def get_db() -> AsyncSession:
    async with async_session() as session:
        yield session


# Pydantic models
class UserCreate(BaseModel):
    name: str = "Test User"
    email: Optional[str] = None


class UserUpdate(BaseModel):
    name: str


class UserResponse(BaseModel):
    id: int
    name: str
    email: str
    created_at: Optional[str] = None

    class Config:
        from_attributes = True


app = FastAPI(title="FastAPI + SQLAlchemy Stack Test")


# Health check endpoint
@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy"}


@app.get("/db/query")
async def db_query(db: AsyncSession = Depends(get_db)):
    """Test simple SELECT query using SQLAlchemy ORM."""
    try:
        # ORM query using select()
        result = await db.execute(select(User).order_by(User.id).limit(10))
        users = result.scalars().all()

        return {
            "count": len(users),
            "data": [
                {
                    "id": user.id,
                    "name": user.name,
                    "email": user.email,
                    "created_at": str(user.created_at) if user.created_at else None,
                }
                for user in users
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/db/insert", status_code=201)
async def db_insert(user: UserCreate, db: AsyncSession = Depends(get_db)):
    """Test INSERT operation using SQLAlchemy ORM."""
    try:
        email = user.email or f"test{os.urandom(4).hex()}@example.com"

        # Create new user via ORM
        new_user = User(name=user.name, email=email)
        db.add(new_user)
        await db.commit()
        await db.refresh(new_user)

        return {
            "id": new_user.id,
            "name": new_user.name,
            "email": new_user.email,
            "created_at": str(new_user.created_at) if new_user.created_at else None,
        }
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/db/update/{user_id}")
async def db_update(user_id: int, user: UserUpdate, db: AsyncSession = Depends(get_db)):
    """Test UPDATE operation using SQLAlchemy ORM."""
    try:
        # Fetch user
        result = await db.execute(select(User).where(User.id == user_id))
        db_user = result.scalar_one_or_none()

        if not db_user:
            raise HTTPException(status_code=404, detail="User not found")

        # Update user
        db_user.name = user.name
        await db.commit()
        await db.refresh(db_user)

        return {
            "id": db_user.id,
            "name": db_user.name,
            "email": db_user.email,
        }
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/db/delete/{user_id}")
async def db_delete(user_id: int, db: AsyncSession = Depends(get_db)):
    """Test DELETE operation using SQLAlchemy ORM."""
    try:
        # Fetch user
        result = await db.execute(select(User).where(User.id == user_id))
        db_user = result.scalar_one_or_none()

        if not db_user:
            raise HTTPException(status_code=404, detail="User not found")

        # Delete user
        await db.delete(db_user)
        await db.commit()

        return {"id": user_id, "deleted": True}
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/db/raw-sql")
async def db_raw_sql(db: AsyncSession = Depends(get_db)):
    """Test raw SQL execution through SQLAlchemy.

    This tests that even raw SQL queries through SQLAlchemy are captured
    by our psycopg instrumentation.
    """
    try:
        result = await db.execute(text("SELECT id, name, email FROM users ORDER BY id LIMIT 5"))
        rows = result.fetchall()

        return {
            "count": len(rows),
            "data": [{"id": row[0], "name": row[1], "email": row[2]} for row in rows],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/db/aggregation")
async def db_aggregation(db: AsyncSession = Depends(get_db)):
    """Test aggregation queries using SQLAlchemy."""
    try:
        # Count query
        count_result = await db.execute(select(func.count(User.id)))
        count = count_result.scalar()

        # Max/Min queries
        max_result = await db.execute(select(func.max(User.id)))
        max_id = max_result.scalar()

        min_result = await db.execute(select(func.min(User.id)))
        min_id = min_result.scalar()

        return {
            "count": count,
            "max_id": max_id,
            "min_id": min_id,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/db/transaction")
async def db_transaction(db: AsyncSession = Depends(get_db)):
    """Test explicit transaction handling with SQLAlchemy."""
    try:
        # Start explicit transaction
        async with db.begin():
            # Insert user
            new_user = User(name="Transaction User", email=f"tx{os.urandom(4).hex()}@example.com")
            db.add(new_user)
            await db.flush()  # Get ID without committing

            insert_id = new_user.id

            # Query inside transaction
            result = await db.execute(select(func.count(User.id)))
            count_inside = result.scalar()

        # After transaction commits, query again
        result = await db.execute(select(func.count(User.id)))
        count_after = result.scalar()

        return {
            "insert_id": insert_id,
            "count_inside_tx": count_inside,
            "count_after_commit": count_after,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/db/async-context")
async def db_async_context():
    """Test async context propagation with concurrent database queries.

    This tests that OpenTelemetry context is properly propagated across
    async boundaries when making concurrent database calls through SQLAlchemy.
    """
    try:

        async def query_count():
            async with async_session() as session:
                result = await session.execute(select(func.count(User.id)))
                return result.scalar()

        async def query_max_id():
            async with async_session() as session:
                result = await session.execute(select(func.max(User.id)))
                return result.scalar()

        async def query_min_id():
            async with async_session() as session:
                result = await session.execute(select(func.min(User.id)))
                return result.scalar()

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


@app.get("/db/relationships")
async def db_relationships(db: AsyncSession = Depends(get_db)):
    """Test multiple related queries.

    This tests the common pattern of fetching related data in sequence.
    """
    try:
        # Get first user
        result = await db.execute(select(User).order_by(User.id).limit(1))
        first_user = result.scalar_one_or_none()

        if not first_user:
            return {"message": "No users found"}

        # Get last user
        result = await db.execute(select(User).order_by(User.id.desc()).limit(1))
        last_user = result.scalar_one_or_none()

        return {
            "first_user": {
                "id": first_user.id,
                "name": first_user.name,
                "email": first_user.email,
            },
            "last_user": {
                "id": last_user.id,
                "name": last_user.name,
                "email": last_user.email,
            }
            if last_user
            else None,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    sdk.mark_app_as_ready()
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
