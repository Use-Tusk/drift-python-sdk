"""Flask app with SQLAlchemy operations for e2e testing."""

from __future__ import annotations

import os
from datetime import datetime

from flask import Flask, jsonify, request
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    delete,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.orm import sessionmaker

from drift import TuskDrift

# Initialize Drift SDK
sdk = TuskDrift.initialize(
    api_key="tusk-test-key",
    log_level="debug",
)

app = Flask(__name__)


def _database_url() -> str:
    user = os.getenv("POSTGRES_USER", "testuser")
    password = os.getenv("POSTGRES_PASSWORD", "testpass")
    host = os.getenv("POSTGRES_HOST", "postgres")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "testdb")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{db}"


engine = create_engine(_database_url(), future=True)
SessionLocal = sessionmaker(bind=engine, future=True)
metadata = MetaData()
users = Table(
    "users",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(255), nullable=False),
    Column("email", String(255), unique=True, nullable=False),
    Column("created_at", DateTime, nullable=False),
)


def init_db() -> None:
    with engine.begin() as conn:
        metadata.drop_all(conn, checkfirst=True)
        metadata.create_all(conn, checkfirst=True)
        conn.execute(
            insert(users),
            [
                {"name": "John Doe", "email": "john@example.com", "created_at": datetime.utcnow()},
                {"name": "Jane Smith", "email": "jane@example.com", "created_at": datetime.utcnow()},
            ],
        )


init_db()


@app.route("/health")
def health():
    return jsonify({"status": "healthy"})


@app.route("/db/query")
def db_query():
    with SessionLocal() as session:
        rows = session.execute(select(users).order_by(users.c.id).limit(20)).mappings().all()
        return jsonify({"count": len(rows), "data": [dict(r) for r in rows]})


@app.route("/db/insert", methods=["POST"])
def db_insert():
    payload = request.get_json() or {}
    name = payload.get("name", "Test User")
    email = payload.get("email", f"user-{os.urandom(3).hex()}@example.com")
    with SessionLocal() as session:
        row = session.execute(
            insert(users)
            .values(name=name, email=email, created_at=datetime.utcnow())
            .returning(users.c.id, users.c.name, users.c.email)
        ).one()
        session.commit()
        return jsonify({"id": row.id, "name": row.name, "email": row.email}), 201


@app.route("/db/update/<int:user_id>", methods=["PUT"])
def db_update(user_id: int):
    payload = request.get_json() or {}
    new_name = payload.get("name", "Updated User")
    with SessionLocal() as session:
        row = session.execute(
            update(users).where(users.c.id == user_id).values(name=new_name).returning(users.c.id, users.c.name)
        ).first()
        session.commit()
        if not row:
            return jsonify({"error": "User not found"}), 404
        return jsonify({"id": row.id, "name": row.name})


@app.route("/db/delete/<int:user_id>", methods=["DELETE"])
def db_delete(user_id: int):
    with SessionLocal() as session:
        row = session.execute(delete(users).where(users.c.id == user_id).returning(users.c.id)).first()
        session.commit()
        if not row:
            return jsonify({"error": "User not found"}), 404
        return jsonify({"id": row.id, "deleted": True})


@app.route("/db/batch-insert", methods=["POST"])
def db_batch_insert():
    payload = request.get_json() or {}
    batch = payload.get("users", [])
    values = [
        {
            "name": item["name"],
            "email": item["email"],
            "created_at": datetime.utcnow(),
        }
        for item in batch
    ]
    with SessionLocal() as session:
        if values:
            session.execute(insert(users), values)
            session.commit()
        return jsonify({"inserted": len(values)})


@app.route("/db/fetchmany-arraysize")
def db_fetchmany_arraysize():
    with engine.connect() as conn:
        result = conn.execute(text("SELECT id, name FROM users ORDER BY id"))
        cursor = result.cursor
        cursor.arraysize = 2
        batch = cursor.fetchmany()
        return jsonify(
            {
                "arraysize": cursor.arraysize,
                "batch_len": len(batch),
                "ids": [row[0] for row in batch],
            }
        )


@app.route("/db/error-then-query")
def db_error_then_query():
    error_message = ""
    with SessionLocal() as session:
        try:
            session.execute(text("SELECT * FROM users_missing_table")).all()
        except Exception as exc:
            error_message = str(exc)
            session.rollback()

        rows = session.execute(select(users.c.id).order_by(users.c.id).limit(1)).all()
        return jsonify(
            {
                "had_error": bool(error_message),
                "error_contains_missing_table": "users_missing_table" in error_message,
                "follow_up_count": len(rows),
            }
        )


if __name__ == "__main__":
    sdk.mark_app_as_ready()
    app.run(host="0.0.0.0", port=8000, debug=False)
