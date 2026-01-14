"""Serialization utilities for instrumentation modules."""

from __future__ import annotations

import datetime
from typing import Any


def serialize_value(val: Any) -> Any:
    """Convert non-JSON-serializable values to JSON-compatible types.

    Handles datetime objects, bytes, and nested structures (lists, tuples, dicts).

    Args:
        val: The value to serialize.

    Returns:
        A JSON-serializable version of the value.
    """
    if isinstance(val, (datetime.datetime, datetime.date, datetime.time)):
        return val.isoformat()
    elif isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    elif isinstance(val, memoryview):
        return bytes(val).decode("utf-8", errors="replace")
    elif isinstance(val, (list, tuple)):
        return [serialize_value(v) for v in val]
    elif isinstance(val, dict):
        return {k: serialize_value(v) for k, v in val.items()}
    return val
