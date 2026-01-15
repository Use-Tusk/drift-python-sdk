"""Shared utilities for psycopg, psycopg2"""

from __future__ import annotations

import base64
import datetime as dt
import uuid
from typing import Any


def deserialize_db_value(val: Any) -> Any:
    """Convert serialized values back to their original Python types.

    During recording, database values are serialized for JSON storage:
    - datetime objects -> ISO format strings
    - bytes/memoryview -> {"__bytes__": "<base64_encoded_data>"}
    - uuid.UUID -> {"__uuid__": "<uuid_string>"}

    During replay, we need to convert them back to their original types so that
    application code (Flask/Django) handles them the same way.

    Args:
        val: A value from the mocked database rows. Can be a string, list, dict, or any other type.

    Returns:
        The value with serialized types converted back to their original Python types.
    """
    if isinstance(val, dict):
        # Check for bytes tagged structure
        if "__bytes__" in val and len(val) == 1:
            # Decode base64 back to bytes
            return base64.b64decode(val["__bytes__"])
        # Check for UUID tagged structure
        if "__uuid__" in val and len(val) == 1:
            return uuid.UUID(val["__uuid__"])
        # Recursively deserialize dict values
        return {k: deserialize_db_value(v) for k, v in val.items()}
    elif isinstance(val, str):
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
    return val
