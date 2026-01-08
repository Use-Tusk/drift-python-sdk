"""Shared utilities for psycopg, psycopg2"""

from __future__ import annotations

import datetime as dt
from typing import Any


def deserialize_db_value(val: Any) -> Any:
    """Convert ISO datetime strings back to datetime objects for consistent serialization.

    During recording, datetime objects from the database are serialized to ISO format strings.
    During replay, we need to convert them back to datetime objects so that Flask/Django
    serializes them the same way (e.g., RFC 2822 vs ISO 8601 format).

    Only parses strings that contain a time component (T or space separator with :) to avoid
    incorrectly converting date-only strings or text that happens to look like dates.

    Args:
        val: A value from the mocked database rows. Can be a string, list, dict, or any other type.

    Returns:
        The value with ISO datetime strings converted back to datetime objects.
    """
    if isinstance(val, str):
        # Only parse strings that look like full datetime (must have time component)
        # This avoids converting date-only strings like "2024-01-15" or text columns
        # that happen to match date patterns
        if ("T" in val or (" " in val and ":" in val)) and "-" in val:
            try:
                # Handle Z suffix for UTC
                parsed = dt.datetime.fromisoformat(val.replace("Z", "+00:00"))
                return parsed
            except ValueError:
                pass
    elif isinstance(val, list):
        return [deserialize_db_value(v) for v in val]
    elif isinstance(val, dict):
        return {k: deserialize_db_value(v) for k, v in val.items()}
    return val
