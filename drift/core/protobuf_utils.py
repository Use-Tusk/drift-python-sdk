"""Protobuf conversion utilities.

This module provides utilities for converting protobuf Value and Struct objects
to Python native types. Handles both Google protobuf and betterproto variants.
"""

from __future__ import annotations

from typing import Any


def value_to_python(value: Any) -> Any:
    """Convert protobuf Value or Python native type to Python native type.

    Handles:
    1. Python native types (from betterproto's Struct which stores values directly)
    2. Google protobuf Value objects (from google.protobuf.struct_pb2)
    3. betterproto Value objects (from betterproto.lib.google.protobuf)

    Args:
        value: A protobuf Value, native Python type, or nested structure

    Returns:
        Python native type (None, bool, int, float, str, list, or dict)
    """
    # Handle Python native types (from betterproto or already converted)
    if value is None:
        return None
    if isinstance(value, (bool, int, float, str)):
        # Note: bool must be checked before int since bool is a subclass of int
        return value
    if isinstance(value, list):
        return [value_to_python(v) for v in value]
    if isinstance(value, dict):
        return {k: value_to_python(v) for k, v in value.items()}

    # Handle Google protobuf Value objects using WhichOneof
    if hasattr(value, "WhichOneof"):
        kind = value.WhichOneof("kind")
        if kind == "null_value":
            return None
        elif kind == "number_value":
            return value.number_value
        elif kind == "string_value":
            return value.string_value
        elif kind == "bool_value":
            return value.bool_value
        elif kind == "struct_value":
            return struct_to_dict(value.struct_value)
        elif kind == "list_value":
            return [value_to_python(v) for v in value.list_value.values]

    # Handle betterproto Value objects using is_set method
    if hasattr(value, "is_set"):
        try:
            if value.is_set("null_value"):
                return None
            elif value.is_set("number_value"):
                return value.number_value
            elif value.is_set("string_value"):
                return value.string_value
            elif value.is_set("bool_value"):
                return value.bool_value
            elif value.is_set("struct_value"):
                sv = value.struct_value
                if isinstance(sv, dict):
                    return {k: value_to_python(v) for k, v in sv.get("fields", sv).items()}
                return struct_to_dict(sv)
            elif value.is_set("list_value"):
                lv = value.list_value
                # Handle dict-style list_value (betterproto can store dicts)
                if isinstance(lv, dict):
                    return [value_to_python(v) for v in lv.get("values", [])]
                return [value_to_python(v) for v in lv.values]
        except (AttributeError, TypeError):
            pass  # Not a betterproto Value, fall through

    # Fallback: return the value as-is
    return value


def struct_to_dict(struct: Any) -> dict[str, Any]:
    """Convert protobuf Struct to Python dict.

    Args:
        struct: A protobuf Struct object (Google or betterproto)

    Returns:
        Python dict with converted values
    """
    if not struct:
        return {}

    # Handle dict-like struct (betterproto sometimes returns dicts directly)
    if isinstance(struct, dict):
        return {k: value_to_python(v) for k, v in struct.items()}

    # Handle struct with fields attribute
    if hasattr(struct, "fields"):
        return {k: value_to_python(v) for k, v in struct.fields.items()}

    return {}
