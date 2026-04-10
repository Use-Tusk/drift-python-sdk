"""Unit tests for drift/instrumentation/utils/psycopg_utils.py"""

from __future__ import annotations

import base64
import datetime as dt
import uuid
from decimal import Decimal

from drift.instrumentation.utils.psycopg_utils import (
    POSTGRES_DATE_TYPE_CODE,
    deserialize_db_value,
    restore_row_date_types,
    restore_row_integer_types,
)


class TestDeserializeDbValue:
    """Tests for deserialize_db_value."""

    # --- bytes dict ---

    def test_bytes_dict_decodes_base64(self):
        data = b"\x00\x01\x02\x03"
        encoded = base64.b64encode(data).decode("ascii")
        result = deserialize_db_value({"__bytes__": encoded})
        assert result == data

    def test_bytes_dict_empty(self):
        encoded = base64.b64encode(b"").decode("ascii")
        result = deserialize_db_value({"__bytes__": encoded})
        assert result == b""

    def test_bytes_dict_not_triggered_when_extra_keys(self):
        # Only triggers for single-key dicts
        result = deserialize_db_value({"__bytes__": "aGVsbG8=", "extra": "x"})
        assert isinstance(result, dict)
        assert "__bytes__" in result

    # --- UUID dict ---

    def test_uuid_dict_returns_uuid(self):
        uid_str = "12345678-1234-5678-1234-567812345678"
        result = deserialize_db_value({"__uuid__": uid_str})
        assert isinstance(result, uuid.UUID)
        assert str(result) == uid_str

    def test_uuid_dict_not_triggered_when_extra_keys(self):
        uid_str = "12345678-1234-5678-1234-567812345678"
        result = deserialize_db_value({"__uuid__": uid_str, "extra": 1})
        assert isinstance(result, dict)

    # --- Decimal dict ---

    def test_decimal_dict_returns_decimal(self):
        result = deserialize_db_value({"__decimal__": "3.14159"})
        assert isinstance(result, Decimal)
        assert result == Decimal("3.14159")

    def test_decimal_preserves_precision(self):
        result = deserialize_db_value({"__decimal__": "0.1"})
        assert result == Decimal("0.1")

    def test_decimal_not_triggered_when_extra_keys(self):
        result = deserialize_db_value({"__decimal__": "1.0", "extra": 2})
        assert isinstance(result, dict)

    # --- timedelta dict ---

    def test_timedelta_dict_returns_timedelta(self):
        result = deserialize_db_value({"__timedelta__": 90.0})
        assert isinstance(result, dt.timedelta)
        assert result == dt.timedelta(seconds=90)

    def test_timedelta_zero(self):
        result = deserialize_db_value({"__timedelta__": 0})
        assert result == dt.timedelta(0)

    def test_timedelta_not_triggered_when_extra_keys(self):
        result = deserialize_db_value({"__timedelta__": 90.0, "extra": 1})
        assert isinstance(result, dict)

    # --- date dict ---

    def test_date_dict_returns_date(self):
        result = deserialize_db_value({"__date__": "2023-03-15"})
        assert isinstance(result, dt.date)
        assert result == dt.date(2023, 3, 15)

    def test_date_not_triggered_when_extra_keys(self):
        result = deserialize_db_value({"__date__": "2023-03-15", "extra": 1})
        assert isinstance(result, dict)

    # --- time dict ---

    def test_time_dict_returns_time(self):
        result = deserialize_db_value({"__time__": "14:30:00"})
        assert isinstance(result, dt.time)
        assert result == dt.time(14, 30, 0)

    def test_time_not_triggered_when_extra_keys(self):
        result = deserialize_db_value({"__time__": "14:30:00", "extra": 1})
        assert isinstance(result, dict)

    # --- range dict (no psycopg) ---

    def test_range_dict_without_psycopg_returns_range_data(self):
        range_val = {"__range__": {"lower": 1, "upper": 5, "bounds": "[)"}}
        result = deserialize_db_value(range_val)
        # Without psycopg, returns the inner range_data dict
        assert result == {"lower": 1, "upper": 5, "bounds": "[)"}

    def test_range_empty_without_psycopg(self):
        range_val = {"__range__": {"empty": True}}
        result = deserialize_db_value(range_val)
        assert result == {"empty": True}

    def test_range_not_triggered_when_extra_keys(self):
        result = deserialize_db_value({"__range__": {"lower": 1}, "extra": 1})
        assert isinstance(result, dict)

    # --- regular dict (recursive) ---

    def test_regular_dict_deserializes_recursively(self):
        result = deserialize_db_value({"a": {"__uuid__": "12345678-1234-5678-1234-567812345678"}, "b": 42})
        assert isinstance(result["a"], uuid.UUID)
        assert result["b"] == 42

    def test_empty_dict_passthrough(self):
        result = deserialize_db_value({})
        assert result == {}

    # --- strings ---

    def test_datetime_string_iso_t_format(self):
        result = deserialize_db_value("2023-01-15T10:30:00")
        assert isinstance(result, dt.datetime)
        assert result == dt.datetime(2023, 1, 15, 10, 30, 0)

    def test_datetime_string_space_format(self):
        result = deserialize_db_value("2023-01-15 10:30:00")
        assert isinstance(result, dt.datetime)
        assert result == dt.datetime(2023, 1, 15, 10, 30, 0)

    def test_datetime_string_with_z_suffix(self):
        result = deserialize_db_value("2023-01-15T10:30:00Z")
        assert isinstance(result, dt.datetime)
        assert result.tzinfo is not None

    def test_datetime_string_with_timezone_offset(self):
        result = deserialize_db_value("2023-01-15T10:30:00+05:00")
        assert isinstance(result, dt.datetime)

    def test_plain_date_string_not_parsed(self):
        # Date-only strings (no time component) should NOT be parsed
        result = deserialize_db_value("2023-01-15")
        assert result == "2023-01-15"

    def test_non_datetime_string_passthrough(self):
        result = deserialize_db_value("hello world")
        assert result == "hello world"

    def test_invalid_datetime_string_passthrough(self):
        # String that looks like datetime but is invalid
        result = deserialize_db_value("2023-13-45T25:99:99")
        assert result == "2023-13-45T25:99:99"

    def test_empty_string_passthrough(self):
        result = deserialize_db_value("")
        assert result == ""

    # --- list ---

    def test_list_deserializes_elements(self):
        result = deserialize_db_value([{"__uuid__": "12345678-1234-5678-1234-567812345678"}, 42, "hello"])
        assert isinstance(result[0], uuid.UUID)
        assert result[1] == 42
        assert result[2] == "hello"

    def test_empty_list(self):
        result = deserialize_db_value([])
        assert result == []

    def test_nested_list(self):
        result = deserialize_db_value([[{"__decimal__": "1.5"}]])
        assert result == [[Decimal("1.5")]]

    # --- primitives ---

    def test_int_passthrough(self):
        assert deserialize_db_value(42) == 42

    def test_float_passthrough(self):
        assert deserialize_db_value(3.14) == 3.14

    def test_none_passthrough(self):
        assert deserialize_db_value(None) is None

    def test_bool_passthrough(self):
        assert deserialize_db_value(True) is True


class TestRestoreRowIntegerTypes:
    """Tests for restore_row_integer_types."""

    # --- empty/None cases ---

    def test_empty_description_returns_row_unchanged(self):
        row = [1.0, 2.0]
        result = restore_row_integer_types(row, None)
        assert result == [1.0, 2.0]

    def test_empty_description_list_returns_row_unchanged(self):
        row = [1.0, 2.0]
        result = restore_row_integer_types(row, [])
        assert result == [1.0, 2.0]

    def test_empty_row_list_returns_unchanged(self):
        result = restore_row_integer_types([], [{"name": "id", "type_code": 23}])
        assert result == []

    def test_empty_row_dict_returns_unchanged(self):
        result = restore_row_integer_types({}, [{"name": "id", "type_code": 23}])
        assert result == {}

    # --- list row ---

    def test_list_row_converts_integer_float_to_int(self):
        row = [1.0, "hello", 3.0]
        description = [
            {"name": "id", "type_code": 23},  # INTEGER
            {"name": "name", "type_code": 25},  # TEXT
            {"name": "count", "type_code": 23},  # INTEGER
        ]
        result = restore_row_integer_types(row, description)
        assert result == [1, "hello", 3]
        assert isinstance(result[0], int)
        assert isinstance(result[2], int)

    def test_list_row_does_not_convert_non_whole_float(self):
        row = [1.5]
        description = [{"name": "val", "type_code": 23}]
        result = restore_row_integer_types(row, description)
        assert result == [1.5]
        assert isinstance(result[0], float)

    def test_list_row_does_not_convert_non_integer_type(self):
        row = [1.0]
        description = [{"name": "val", "type_code": 1700}]  # NUMERIC
        result = restore_row_integer_types(row, description)
        assert result == [1.0]
        assert isinstance(result[0], float)

    def test_list_row_bigint_type_code(self):
        row = [9999999999.0]
        description = [{"name": "val", "type_code": 20}]  # BIGINT
        result = restore_row_integer_types(row, description)
        assert result == [9999999999]
        assert isinstance(result[0], int)

    def test_list_row_smallint_type_code(self):
        row = [5.0]
        description = [{"name": "val", "type_code": 21}]  # SMALLINT
        result = restore_row_integer_types(row, description)
        assert result == [5]

    def test_list_row_oid_type_code(self):
        row = [12345.0]
        description = [{"name": "val", "type_code": 26}]  # OID
        result = restore_row_integer_types(row, description)
        assert result == [12345]

    def test_list_row_xid_type_code(self):
        row = [100.0]
        description = [{"name": "val", "type_code": 28}]  # XID
        result = restore_row_integer_types(row, description)
        assert result == [100]

    def test_list_row_preserves_non_float_integer_value(self):
        row = [42]  # already int
        description = [{"name": "val", "type_code": 23}]
        result = restore_row_integer_types(row, description)
        assert result == [42]

    def test_list_row_extra_columns_beyond_description(self):
        row = [1.0, 2.0, 3.0]
        description = [{"name": "a", "type_code": 23}]  # only one column described
        result = restore_row_integer_types(row, description)
        assert result[0] == 1
        # extra values beyond description appended as-is
        assert result[1] == 2.0
        assert result[2] == 3.0

    def test_list_row_description_with_non_dict_items(self):
        row = [1.0]
        description = [None]  # non-dict description item
        result = restore_row_integer_types(row, description)
        # type_code is None, not in POSTGRES_INTEGER_TYPE_CODES → no conversion
        assert result == [1.0]

    # --- dict row ---

    def test_dict_row_converts_integer_float_to_int(self):
        row = {"id": 5.0, "name": "test", "count": 3.0}
        description = [
            {"name": "id", "type_code": 23},
            {"name": "name", "type_code": 25},
            {"name": "count", "type_code": 23},
        ]
        result = restore_row_integer_types(row, description)
        assert result == {"id": 5, "name": "test", "count": 3}
        assert isinstance(result["id"], int)

    def test_dict_row_does_not_convert_non_whole_float(self):
        row = {"val": 1.5}
        description = [{"name": "val", "type_code": 23}]
        result = restore_row_integer_types(row, description)
        assert result["val"] == 1.5

    def test_dict_row_unknown_column_not_converted(self):
        row = {"unknown": 5.0}
        description = [{"name": "id", "type_code": 23}]
        result = restore_row_integer_types(row, description)
        # "unknown" not in type_code_by_name, so type_code is None
        assert result["unknown"] == 5.0

    def test_dict_row_description_with_non_dict_items(self):
        row = {"id": 5.0}
        description = [None]  # non-dict item
        result = restore_row_integer_types(row, description)
        # Non-dict items in description are skipped (no name extracted)
        assert result["id"] == 5.0


class TestRestoreRowDateTypes:
    """Tests for restore_row_date_types."""

    # --- empty/None cases ---

    def test_none_description_returns_row(self):
        row = ["2023-01-15"]
        result = restore_row_date_types(row, None)
        assert result == ["2023-01-15"]

    def test_empty_description_returns_row(self):
        row = ["2023-01-15"]
        result = restore_row_date_types(row, [])
        assert result == ["2023-01-15"]

    def test_empty_row_list(self):
        result = restore_row_date_types([], [{"name": "d", "type_code": POSTGRES_DATE_TYPE_CODE}])
        assert result == []

    def test_empty_row_dict(self):
        result = restore_row_date_types({}, [{"name": "d", "type_code": POSTGRES_DATE_TYPE_CODE}])
        assert result == {}

    # --- list row ---

    def test_list_row_converts_date_string(self):
        row = ["2023-03-15"]
        description = [{"name": "d", "type_code": POSTGRES_DATE_TYPE_CODE}]
        result = restore_row_date_types(row, description)
        assert result == [dt.date(2023, 3, 15)]

    def test_list_row_converts_time_without_tz(self):
        row = ["14:30:00"]
        description = [{"name": "t", "type_code": 1083}]  # TIME WITHOUT TIME ZONE
        result = restore_row_date_types(row, description)
        assert result == [dt.time(14, 30, 0)]

    def test_list_row_converts_time_with_tz(self):
        row = ["09:00:00"]
        description = [{"name": "t", "type_code": 1266}]  # TIME WITH TIME ZONE
        result = restore_row_date_types(row, description)
        assert result == [dt.time(9, 0, 0)]

    def test_list_row_non_string_not_converted(self):
        row = [dt.date(2023, 1, 1)]
        description = [{"name": "d", "type_code": POSTGRES_DATE_TYPE_CODE}]
        result = restore_row_date_types(row, description)
        assert result == [dt.date(2023, 1, 1)]

    def test_list_row_invalid_date_string_passthrough(self):
        row = ["not-a-date"]
        description = [{"name": "d", "type_code": POSTGRES_DATE_TYPE_CODE}]
        result = restore_row_date_types(row, description)
        assert result == ["not-a-date"]

    def test_list_row_invalid_time_string_passthrough(self):
        row = ["not-a-time"]
        description = [{"name": "t", "type_code": 1083}]
        result = restore_row_date_types(row, description)
        assert result == ["not-a-time"]

    def test_list_row_non_date_type_code_passthrough(self):
        row = ["2023-01-15"]
        description = [{"name": "s", "type_code": 25}]  # TEXT
        result = restore_row_date_types(row, description)
        assert result == ["2023-01-15"]

    def test_list_row_beyond_description_length(self):
        row = ["2023-01-15", "extra"]
        description = [{"name": "d", "type_code": POSTGRES_DATE_TYPE_CODE}]
        result = restore_row_date_types(row, description)
        # index 0 converted, index 1 beyond description → no type_code → passthrough
        assert result[0] == dt.date(2023, 1, 15)
        assert result[1] == "extra"

    def test_list_row_non_dict_description_item(self):
        row = ["2023-01-15"]
        description = [None]
        result = restore_row_date_types(row, description)
        assert result == ["2023-01-15"]

    # --- dict row ---

    def test_dict_row_converts_date_string(self):
        row = {"created": "2023-06-01", "name": "test"}
        description = [
            {"name": "created", "type_code": POSTGRES_DATE_TYPE_CODE},
            {"name": "name", "type_code": 25},
        ]
        result = restore_row_date_types(row, description)
        assert result["created"] == dt.date(2023, 6, 1)
        assert result["name"] == "test"

    def test_dict_row_converts_time_string(self):
        row = {"start_time": "08:00:00"}
        description = [{"name": "start_time", "type_code": 1083}]
        result = restore_row_date_types(row, description)
        assert result["start_time"] == dt.time(8, 0, 0)

    def test_dict_row_unknown_column_passthrough(self):
        row = {"unknown": "2023-01-15"}
        description = [{"name": "other", "type_code": POSTGRES_DATE_TYPE_CODE}]
        result = restore_row_date_types(row, description)
        # "unknown" not in type_code_by_name → type_code None → passthrough
        assert result["unknown"] == "2023-01-15"

    def test_dict_row_non_string_not_converted(self):
        row = {"d": 12345}
        description = [{"name": "d", "type_code": POSTGRES_DATE_TYPE_CODE}]
        result = restore_row_date_types(row, description)
        assert result["d"] == 12345

    def test_dict_row_non_dict_description_items_skipped(self):
        row = {"d": "2023-01-15"}
        description = [None]
        result = restore_row_date_types(row, description)
        assert result["d"] == "2023-01-15"

    # --- mixed columns ---

    def test_list_row_mixed_types(self):
        row = ["2023-01-15", "14:30:00", "plain text", 42]
        description = [
            {"name": "date_col", "type_code": POSTGRES_DATE_TYPE_CODE},
            {"name": "time_col", "type_code": 1083},
            {"name": "text_col", "type_code": 25},
            {"name": "int_col", "type_code": 23},
        ]
        result = restore_row_date_types(row, description)
        assert result[0] == dt.date(2023, 1, 15)
        assert result[1] == dt.time(14, 30, 0)
        assert result[2] == "plain text"
        assert result[3] == 42
