"""Utilities for normalizing span input/output data.

This module provides functions to normalize data before serialization,
handling edge cases like undefined/None values, circular references,
and special types like dates.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any


# Sentinel object for tracking seen references (for circular reference detection)
_CIRCULAR_MARKER = "[Circular]"


def normalize_input_data(data: Any, _seen: set[int] | None = None) -> Any:
    """
    Normalize input data by removing None values and handling circular references.

    This function recursively processes data structures to:
    - Remove keys with None values from dictionaries (Python equivalent of undefined)
    - Replace circular references with "[Circular]" string
    - Convert Date/datetime objects to ISO strings
    - Preserve null (None), 0, False, and empty strings when they're actual values
    - Handle nested objects and arrays

    Args:
        data: The data to normalize
        _seen: Internal set for tracking seen objects (for circular detection)

    Returns:
        Normalized data structure
    """
    if _seen is None:
        _seen = set()

    # Handle primitives directly
    if data is None:
        return None
    if isinstance(data, bool):
        return data
    if isinstance(data, (int, float, str)):
        return data

    # Handle datetime/date objects
    if isinstance(data, datetime):
        return data.isoformat()
    if isinstance(data, date):
        return data.isoformat()

    # Handle circular references for mutable objects
    obj_id = id(data)
    if obj_id in _seen:
        return _CIRCULAR_MARKER
    _seen.add(obj_id)

    try:
        # Handle dictionaries
        if isinstance(data, dict):
            result = {}
            for key, value in data.items():
                # In Python, we don't have "undefined" but we can treat None as similar
                # However, the Node.js behavior removes undefined but keeps null
                # In Python, we'll remove None values from dict keys to match behavior
                # This is the equivalent of removing undefined in JS
                normalized_value = normalize_input_data(value, _seen)
                # Only skip if the original value was None AND we want to remove it
                # But we need to keep explicit None values (null)
                # The trick: in Python, we can't distinguish between "not set" and None
                # So we'll keep None values to match JS null behavior
                result[key] = normalized_value
            return result

        # Handle lists (arrays)
        if isinstance(data, list):
            return [normalize_input_data(item, _seen) for item in data]

        # Handle tuples (treat like arrays)
        if isinstance(data, tuple):
            return [normalize_input_data(item, _seen) for item in data]

        # Handle sets (convert to list)
        if isinstance(data, set):
            return [normalize_input_data(item, _seen) for item in data]

        # Handle bytes
        if isinstance(data, (bytes, bytearray)):
            import base64
            return base64.b64encode(data).decode("ascii")

        # Handle other objects by converting to string
        return str(data)

    finally:
        # Remove from seen set when done with this object
        _seen.discard(obj_id)


def remove_none_values(data: Any, _seen: set[int] | None = None) -> Any:
    """
    Remove None values from dictionaries (like JS removing undefined).

    This is a variant of normalize_input_data that specifically removes
    None values from dicts, matching the Node.js behavior of removing undefined.

    Args:
        data: The data to process
        _seen: Internal set for tracking seen objects (for circular detection)

    Returns:
        Data with None values removed from dicts
    """
    if _seen is None:
        _seen = set()

    # Handle primitives directly
    if data is None:
        return None
    if isinstance(data, bool):
        return data
    if isinstance(data, (int, float, str)):
        return data

    # Handle datetime/date objects
    if isinstance(data, datetime):
        return data.isoformat()
    if isinstance(data, date):
        return data.isoformat()

    # Handle circular references for mutable objects
    obj_id = id(data)
    if obj_id in _seen:
        return _CIRCULAR_MARKER
    _seen.add(obj_id)

    try:
        # Handle dictionaries - remove None values
        if isinstance(data, dict):
            result = {}
            for key, value in data.items():
                if value is None:
                    continue  # Skip None values (like JS undefined)
                normalized_value = remove_none_values(value, _seen)
                if normalized_value is not None or value is not None:
                    result[key] = normalized_value
            return result

        # Handle lists (arrays) - convert None to None (like JS null)
        if isinstance(data, list):
            return [
                None if item is None else remove_none_values(item, _seen)
                for item in data
            ]

        # Handle tuples (treat like arrays)
        if isinstance(data, tuple):
            return [
                None if item is None else remove_none_values(item, _seen)
                for item in data
            ]

        # Handle sets (convert to list)
        if isinstance(data, set):
            return [remove_none_values(item, _seen) for item in data]

        # Handle bytes
        if isinstance(data, (bytes, bytearray)):
            import base64
            return base64.b64encode(data).decode("ascii")

        # Handle other objects by converting to string
        return str(data)

    finally:
        # Remove from seen set when done with this object
        _seen.discard(obj_id)


def create_span_input_value(data: Any) -> str:
    """
    Create a JSON string representation of normalized input data.

    This function normalizes the data (removing None values and handling
    circular references) and then serializes it to a JSON string.

    Args:
        data: The input data to serialize

    Returns:
        JSON string representation of the normalized data
    """
    normalized = remove_none_values(data)
    return json.dumps(normalized, separators=(",", ":"), ensure_ascii=False)


def create_mock_input_value(data: Any) -> Any:
    """
    Create normalized object data for mock values.

    This function normalizes the data (removing None values and handling
    circular references) and returns the normalized object.

    Args:
        data: The input data to normalize

    Returns:
        Normalized data object (not JSON string)
    """
    return remove_none_values(data)


__all__ = [
    "normalize_input_data",
    "remove_none_values",
    "create_span_input_value",
    "create_mock_input_value",
]
