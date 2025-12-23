"""Instrumentation for psycopg (psycopg3) PostgreSQL client library."""

from .instrumentation import PsycopgInstrumentation

__all__ = ["PsycopgInstrumentation"]
