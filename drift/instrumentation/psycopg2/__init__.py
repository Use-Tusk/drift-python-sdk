"""Instrumentation for psycopg2 PostgreSQL client library."""

from .instrumentation import Psycopg2Instrumentation

__all__ = ["Psycopg2Instrumentation"]
