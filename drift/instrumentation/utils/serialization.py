"""Serialization utilities for instrumentation modules."""

from __future__ import annotations

import base64
import datetime
import ipaddress
import uuid
from decimal import Decimal
from typing import Any

# Try to import psycopg Range type for serialization support
try:
    from psycopg.types.range import Range as PsycopgRange  # type: ignore[import-untyped]

    HAS_PSYCOPG_RANGE = True
except ImportError:
    HAS_PSYCOPG_RANGE = False
    PsycopgRange = None


def _serialize_bytes(val: bytes) -> Any:
    """Serialize bytes to a JSON-compatible format.

    Attempts UTF-8 decode first for text data (like COPY output).
    Falls back to base64 encoding with tagged structure for binary data
    that contains invalid UTF-8 sequences (like bytea columns).

    Args:
        val: The bytes value to serialize.

    Returns:
        Either a string (if valid UTF-8) or a dict {"__bytes__": "base64_data"}.
    """
    try:
        # Try UTF-8 decode first - works for text data like COPY output
        return val.decode("utf-8")
    except UnicodeDecodeError:
        # Fall back to base64 for binary data with invalid UTF-8 sequences
        return {"__bytes__": base64.b64encode(val).decode("ascii")}


def serialize_value(val: Any) -> Any:
    """Convert non-JSON-serializable values to JSON-compatible types.

    Handles datetime objects, bytes, Decimal, and nested structures (lists, tuples, dicts).

    Args:
        val: The value to serialize.

    Returns:
        A JSON-serializable version of the value.
    """
    if isinstance(val, (datetime.datetime, datetime.date, datetime.time)):
        return val.isoformat()
    elif isinstance(val, datetime.timedelta):
        # Serialize timedelta as total seconds for consistent hashing
        return {"__timedelta__": val.total_seconds()}
    elif isinstance(val, Decimal):
        # Serialize Decimal as string to preserve precision and ensure consistent hashing
        return {"__decimal__": str(val)}
    elif isinstance(val, uuid.UUID):
        return {"__uuid__": str(val)}
    elif HAS_PSYCOPG_RANGE and PsycopgRange is not None and isinstance(val, PsycopgRange):
        # Serialize psycopg Range objects to a deterministic dict format
        # This handles INT4RANGE, TSRANGE, and other PostgreSQL range types
        if val.isempty:
            return {"__range__": {"empty": True}}
        return {
            "__range__": {
                "lower": serialize_value(val.lower),
                "upper": serialize_value(val.upper),
                "bounds": val.bounds,
            }
        }
    elif isinstance(
        val,
        (
            ipaddress.IPv4Address,
            ipaddress.IPv6Address,
            ipaddress.IPv4Interface,
            ipaddress.IPv6Interface,
            ipaddress.IPv4Network,
            ipaddress.IPv6Network,
        ),
    ):
        # Serialize ipaddress types to string for inet/cidr PostgreSQL columns
        # These are returned by psycopg when querying inet and cidr columns
        return str(val)
    elif isinstance(val, memoryview):
        # Convert memoryview to bytes first, then serialize
        return _serialize_bytes(bytes(val))
    elif isinstance(val, bytes):
        return _serialize_bytes(val)
    elif isinstance(val, (list, tuple)):
        return [serialize_value(v) for v in val]
    elif isinstance(val, dict):
        return {k: serialize_value(v) for k, v in val.items()}
    return val
