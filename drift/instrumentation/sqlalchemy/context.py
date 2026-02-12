"""Shared context state between SQLAlchemy and DB driver instrumentations."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

# Indicates that the current DBAPI call is happening within a SQLAlchemy execution.
sqlalchemy_execution_active_context: ContextVar[bool] = ContextVar("sqlalchemy_execution_active", default=False)

# Holds replay mock payload resolved by SQLAlchemy instrumentation for the current call.
sqlalchemy_replay_mock_context: ContextVar[dict[str, Any] | None] = ContextVar("sqlalchemy_replay_mock", default=None)
