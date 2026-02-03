"""Tests for psycopg_utils module."""

from drift.instrumentation.utils.psycopg_utils import (
    POSTGRES_INTEGER_TYPE_CODES,
    deserialize_db_value,
    restore_row_integer_types,
)


class TestRestoreRowIntegerTypes:
    """Tests for restore_row_integer_types function."""

    def test_restores_integer_from_float_for_integer_column(self):
        """Float values in INTEGER columns should be converted to int."""
        row = [0.0, "test", 42.0]
        description = [
            {"name": "order", "type_code": 23},  # INTEGER
            {"name": "name", "type_code": 25},  # TEXT
            {"name": "count", "type_code": 23},  # INTEGER
        ]
        result = restore_row_integer_types(row, description)
        assert result == [0, "test", 42]
        assert isinstance(result[0], int)
        assert isinstance(result[2], int)

    def test_preserves_float_for_float_column(self):
        """Float values in FLOAT columns should remain float."""
        row = [3.14, 2.0]
        description = [
            {"name": "pi", "type_code": 701},  # DOUBLE PRECISION
            {"name": "value", "type_code": 700},  # REAL
        ]
        result = restore_row_integer_types(row, description)
        assert result == [3.14, 2.0]
        assert isinstance(result[0], float)
        assert isinstance(result[1], float)

    def test_handles_mixed_columns(self):
        """Mix of INTEGER and FLOAT columns should be handled correctly."""
        row = [1.0, 2.5, 3.0, "hello", None]
        description = [
            {"name": "int_col", "type_code": 23},  # INTEGER - should convert
            {"name": "float_col", "type_code": 701},  # FLOAT8 - should NOT convert
            {"name": "bigint_col", "type_code": 20},  # BIGINT - should convert
            {"name": "text_col", "type_code": 25},  # TEXT
            {"name": "null_col", "type_code": 23},  # INTEGER but NULL value
        ]
        result = restore_row_integer_types(row, description)
        assert result[0] == 1 and isinstance(result[0], int)
        assert result[1] == 2.5 and isinstance(result[1], float)
        assert result[2] == 3 and isinstance(result[2], int)
        assert result[3] == "hello"
        assert result[4] is None

    def test_handles_empty_row(self):
        """Empty row should return empty list."""
        result = restore_row_integer_types([], [])
        assert result == []

    def test_handles_none_description(self):
        """None description should return row unchanged."""
        row = [1.0, 2.0, 3.0]
        result = restore_row_integer_types(row, None)
        assert result == [1.0, 2.0, 3.0]

    def test_handles_empty_description(self):
        """Empty description should return row unchanged."""
        row = [1.0, 2.0, 3.0]
        result = restore_row_integer_types(row, [])
        assert result == row

    def test_handles_row_longer_than_description(self):
        """Row with more columns than description should handle extra columns."""
        row = [1.0, 2.0, 3.0]
        description = [
            {"name": "col1", "type_code": 23},  # INTEGER
        ]
        result = restore_row_integer_types(row, description)
        assert result[0] == 1 and isinstance(result[0], int)
        assert result[1] == 2.0  # Extra columns unchanged
        assert result[2] == 3.0

    def test_preserves_non_whole_floats_in_integer_column(self):
        """Non-whole float values in INTEGER columns should remain float (edge case)."""
        # This shouldn't happen in practice, but the function should handle it safely
        row = [1.5, 2.0]
        description = [
            {"name": "col1", "type_code": 23},  # INTEGER
            {"name": "col2", "type_code": 23},  # INTEGER
        ]
        result = restore_row_integer_types(row, description)
        assert result[0] == 1.5 and isinstance(result[0], float)  # Not converted (not whole number)
        assert result[1] == 2 and isinstance(result[1], int)  # Converted

    def test_handles_smallint_and_bigint(self):
        """SMALLINT and BIGINT columns should also be converted."""
        row = [100.0, 9999999999.0]
        description = [
            {"name": "small", "type_code": 21},  # SMALLINT
            {"name": "big", "type_code": 20},  # BIGINT
        ]
        result = restore_row_integer_types(row, description)
        assert result == [100, 9999999999]
        assert all(isinstance(v, int) for v in result)

    def test_handles_oid_and_xid(self):
        """OID and XID columns should be converted to int."""
        row = [12345.0, 67890.0]
        description = [
            {"name": "oid_col", "type_code": 26},  # OID
            {"name": "xid_col", "type_code": 28},  # XID
        ]
        result = restore_row_integer_types(row, description)
        assert result == [12345, 67890]
        assert all(isinstance(v, int) for v in result)

    def test_handles_missing_type_code(self):
        """Description without type_code should not cause errors."""
        row = [1.0, 2.0]
        description = [
            {"name": "col1"},  # No type_code
            {"name": "col2", "type_code": 23},  # Has type_code
        ]
        result = restore_row_integer_types(row, description)
        assert result[0] == 1.0  # Not converted (no type_code)
        assert result[1] == 2 and isinstance(result[1], int)


class TestIntegerTypeCodes:
    """Tests for POSTGRES_INTEGER_TYPE_CODES constant."""

    def test_contains_expected_types(self):
        """POSTGRES_INTEGER_TYPE_CODES should contain all PostgreSQL integer types."""
        assert 21 in POSTGRES_INTEGER_TYPE_CODES  # SMALLINT
        assert 23 in POSTGRES_INTEGER_TYPE_CODES  # INTEGER
        assert 20 in POSTGRES_INTEGER_TYPE_CODES  # BIGINT
        assert 26 in POSTGRES_INTEGER_TYPE_CODES  # OID
        assert 28 in POSTGRES_INTEGER_TYPE_CODES  # XID

    def test_does_not_contain_float_types(self):
        """POSTGRES_INTEGER_TYPE_CODES should not contain float types."""
        assert 700 not in POSTGRES_INTEGER_TYPE_CODES  # REAL
        assert 701 not in POSTGRES_INTEGER_TYPE_CODES  # DOUBLE PRECISION
        assert 1700 not in POSTGRES_INTEGER_TYPE_CODES  # NUMERIC


class TestDeserializeDbValueIntegration:
    """Integration tests for deserialize_db_value with restore_row_integer_types."""

    def test_deserialize_then_restore_integers(self):
        """Full pipeline: deserialize special types, then restore integers."""
        import uuid as uuid_lib
        from datetime import datetime

        # Simulated row from mock data after CLI deserialization
        row = [
            0.0,  # INTEGER column (as float from JSON)
            {"__uuid__": "12345678-1234-5678-1234-567812345678"},
            "2026-01-29T12:00:00+00:00",
            42.0,  # Another INTEGER
        ]
        description = [
            {"name": "order", "type_code": 23},  # INTEGER
            {"name": "id", "type_code": 2950},  # UUID
            {"name": "created", "type_code": 1184},  # TIMESTAMPTZ
            {"name": "count", "type_code": 23},  # INTEGER
        ]

        # First deserialize special types
        deserialized = [deserialize_db_value(v) for v in row]
        # Then restore integers
        result = restore_row_integer_types(deserialized, description)

        assert result[0] == 0 and isinstance(result[0], int)
        assert result[1] == uuid_lib.UUID("12345678-1234-5678-1234-567812345678")
        assert isinstance(result[2], datetime)
        assert result[3] == 42 and isinstance(result[3], int)
