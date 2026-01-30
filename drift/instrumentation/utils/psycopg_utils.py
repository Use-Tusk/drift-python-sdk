"""Shared utilities for psycopg, psycopg2"""

from __future__ import annotations

import base64
import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

# PostgreSQL integer type OIDs
# These are the type codes for integer columns in PostgreSQL
# See: https://github.com/postgres/postgres/blob/master/src/include/catalog/pg_type.dat
POSTGRES_INTEGER_TYPE_CODES = {
    20,  # BIGINT (int8)
    21,  # SMALLINT (int2)
    23,  # INTEGER (int4)
    26,  # OID
    28,  # XID
}

# Try to import psycopg Range type for deserialization support
try:
    from psycopg.types.range import Range as PsycopgRange  # type: ignore[import-untyped]

    HAS_PSYCOPG_RANGE = True
except ImportError:
    HAS_PSYCOPG_RANGE = False
    PsycopgRange = None


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
        # Check for Decimal tagged structure
        if "__decimal__" in val and len(val) == 1:
            return Decimal(val["__decimal__"])
        # Check for timedelta tagged structure
        if "__timedelta__" in val and len(val) == 1:
            return dt.timedelta(seconds=val["__timedelta__"])
        # Check for Range tagged structure (psycopg Range types)
        if "__range__" in val and len(val) == 1:
            range_data = val["__range__"]
            if HAS_PSYCOPG_RANGE and PsycopgRange is not None:
                if range_data.get("empty"):
                    return PsycopgRange(empty=True)
                # Recursively deserialize the lower and upper bounds
                # (they may contain datetime or other serialized types)
                lower = deserialize_db_value(range_data.get("lower"))
                upper = deserialize_db_value(range_data.get("upper"))
                bounds = range_data.get("bounds", "[)")
                # Convert floats back to ints if they represent whole numbers
                # This is needed because JSON doesn't distinguish int/float
                if isinstance(lower, float) and lower.is_integer():
                    lower = int(lower)
                if isinstance(upper, float) and upper.is_integer():
                    upper = int(upper)
                return PsycopgRange(lower, upper, bounds)
            else:
                # If psycopg is not available, return the dict as-is
                return range_data
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


def restore_row_integer_types(
    row: list[Any] | dict[str, Any], description: list[dict[str, Any]] | None
) -> list[Any] | dict[str, Any]:
    """Restore integer types for database row values using column metadata.

    During the record/replay cycle, integer values are lost due to JSON serialization:
    - Recording: PostgreSQL INTEGER column → psycopg2 returns int(0) -> JSON stores 0
    - Replay: CLI parses JSON -> Go float64(0) -> protobuf double -> Python float(0.0)

    This function uses the column type_code from the cursor description to identify
    which columns should contain integers and converts whole-number floats back to int.

    Args:
        row: A row of values from the mocked database query. Can be a list (standard cursor)
             or dict (dict cursor like RealDictCursor).
        description: Column metadata from the cursor description, containing 'type_code' for each column.
                    Format: [{"name": "col1", "type_code": 23}, ...]

    Returns:
        The row with integer types restored for INTEGER columns.
    """
    if not description or not row:
        return row

    # Handle dict rows (from dict cursors like RealDictCursor)
    if isinstance(row, dict):
        # Build a mapping of column names to type codes
        type_code_by_name = {}
        for col in description:
            if isinstance(col, dict):
                col_name = col.get("name")
                if col_name:
                    type_code_by_name[col_name] = col.get("type_code")

        result = {}
        for key, value in row.items():
            type_code = type_code_by_name.get(key)
            if type_code in POSTGRES_INTEGER_TYPE_CODES and isinstance(value, float) and value.is_integer():
                result[key] = int(value)
            else:
                result[key] = value
        return result

    # Handle list/tuple rows (standard cursors)
    result = []
    for i, value in enumerate(row):
        if i < len(description):
            type_code = description[i].get("type_code") if isinstance(description[i], dict) else None
            if type_code in POSTGRES_INTEGER_TYPE_CODES and isinstance(value, float) and value.is_integer():
                result.append(int(value))
            else:
                result.append(value)
        else:
            result.append(value)
    return result
