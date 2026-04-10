"""Unit tests for drift/instrumentation/utils/serialization.py"""

from __future__ import annotations

import base64
import datetime
import ipaddress
import uuid
from decimal import Decimal

from drift.instrumentation.utils.serialization import _serialize_bytes, serialize_value


class TestSerializeBytes:
    """Tests for _serialize_bytes."""

    def test_valid_utf8_bytes_decoded_as_string(self):
        result = _serialize_bytes(b"hello world")
        assert result == "hello world"

    def test_empty_bytes_decoded_as_empty_string(self):
        result = _serialize_bytes(b"")
        assert result == ""

    def test_utf8_bytes_with_unicode(self):
        result = _serialize_bytes("café".encode())
        assert result == "café"

    def test_binary_data_falls_back_to_base64(self):
        binary_data = bytes([0xFF, 0xFE, 0x00, 0x01])
        result = _serialize_bytes(binary_data)
        assert isinstance(result, dict)
        assert "__bytes__" in result
        assert result["__bytes__"] == base64.b64encode(binary_data).decode("ascii")

    def test_base64_encoded_value_is_ascii_string(self):
        binary_data = bytes(range(256))
        result = _serialize_bytes(binary_data)
        assert isinstance(result["__bytes__"], str)
        # Verify it's valid base64 and round-trips
        decoded = base64.b64decode(result["__bytes__"])
        assert decoded == binary_data


class TestSerializeValue:
    """Tests for serialize_value."""

    # --- Primitives that pass through ---

    def test_none_passthrough(self):
        assert serialize_value(None) is None

    def test_int_passthrough(self):
        assert serialize_value(42) == 42

    def test_float_passthrough(self):
        assert serialize_value(3.14) == 3.14

    def test_string_passthrough(self):
        assert serialize_value("hello") == "hello"

    def test_bool_passthrough(self):
        assert serialize_value(True) is True

    # --- datetime ---

    def test_datetime_to_isoformat(self):
        dt = datetime.datetime(2023, 1, 15, 10, 30, 0)
        result = serialize_value(dt)
        assert result == "2023-01-15T10:30:00"

    def test_datetime_with_timezone(self):
        dt = datetime.datetime(2023, 6, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
        result = serialize_value(dt)
        assert result == "2023-06-01T12:00:00+00:00"

    def test_datetime_with_microseconds(self):
        dt = datetime.datetime(2023, 1, 1, 0, 0, 0, 123456)
        result = serialize_value(dt)
        assert result == "2023-01-01T00:00:00.123456"

    # --- date (but NOT datetime, since datetime is a subclass of date) ---

    def test_date_tagged_format(self):
        d = datetime.date(2023, 3, 15)
        result = serialize_value(d)
        assert result == {"__date__": "2023-03-15"}

    def test_date_not_confused_with_datetime(self):
        # datetime is checked first, so datetime.datetime shouldn't produce __date__
        dt = datetime.datetime(2023, 3, 15, 10, 0)
        result = serialize_value(dt)
        assert isinstance(result, str)
        assert "__date__" not in str(result)

    # --- time ---

    def test_time_tagged_format(self):
        t = datetime.time(14, 30, 0)
        result = serialize_value(t)
        assert result == {"__time__": "14:30:00"}

    def test_time_with_microseconds(self):
        t = datetime.time(9, 0, 0, 500000)
        result = serialize_value(t)
        assert result == {"__time__": "09:00:00.500000"}

    # --- timedelta ---

    def test_timedelta_total_seconds(self):
        td = datetime.timedelta(seconds=90)
        result = serialize_value(td)
        assert result == {"__timedelta__": 90.0}

    def test_timedelta_days(self):
        td = datetime.timedelta(days=1, hours=2, minutes=3)
        result = serialize_value(td)
        assert result == {"__timedelta__": td.total_seconds()}

    def test_timedelta_zero(self):
        td = datetime.timedelta(0)
        result = serialize_value(td)
        assert result == {"__timedelta__": 0.0}

    # --- Decimal ---

    def test_decimal_tagged_format(self):
        d = Decimal("3.14159")
        result = serialize_value(d)
        assert result == {"__decimal__": "3.14159"}

    def test_decimal_integer(self):
        d = Decimal("100")
        result = serialize_value(d)
        assert result == {"__decimal__": "100"}

    def test_decimal_preserves_precision(self):
        d = Decimal("0.1")
        result = serialize_value(d)
        assert result == {"__decimal__": "0.1"}

    # --- UUID ---

    def test_uuid_tagged_format(self):
        uid = uuid.UUID("12345678-1234-5678-1234-567812345678")
        result = serialize_value(uid)
        assert result == {"__uuid__": "12345678-1234-5678-1234-567812345678"}

    def test_uuid_roundtrip(self):
        uid = uuid.uuid4()
        result = serialize_value(uid)
        assert result == {"__uuid__": str(uid)}

    # --- ipaddress types ---

    def test_ipv4_address(self):
        addr = ipaddress.IPv4Address("192.168.1.1")
        result = serialize_value(addr)
        assert result == "192.168.1.1"

    def test_ipv6_address(self):
        addr = ipaddress.IPv6Address("::1")
        result = serialize_value(addr)
        assert result == "::1"

    def test_ipv4_interface(self):
        iface = ipaddress.IPv4Interface("10.0.0.1/24")
        result = serialize_value(iface)
        assert result == "10.0.0.1/24"

    def test_ipv6_interface(self):
        iface = ipaddress.IPv6Interface("2001:db8::1/48")
        result = serialize_value(iface)
        assert result == "2001:db8::1/48"

    def test_ipv4_network(self):
        net = ipaddress.IPv4Network("192.168.0.0/16")
        result = serialize_value(net)
        assert result == "192.168.0.0/16"

    def test_ipv6_network(self):
        net = ipaddress.IPv6Network("2001:db8::/32")
        result = serialize_value(net)
        assert result == "2001:db8::/32"

    # --- objects with getquoted (psycopg2 adapters) ---

    def test_getquoted_with_adapted(self):
        """Object with getquoted and adapted should serialize via adapted."""

        class FakeAdapter:
            def getquoted(self):
                return b"'hello'"

            adapted = "hello world"

        obj = FakeAdapter()
        result = serialize_value(obj)
        assert result == "hello world"

    def test_getquoted_with_addr(self):
        """Object with getquoted and addr should serialize via addr."""

        class FakeAdapter:
            def getquoted(self):
                return b"'1.2.3.4'"

            addr = ipaddress.IPv4Address("1.2.3.4")

        obj = FakeAdapter()
        result = serialize_value(obj)
        assert result == "1.2.3.4"

    def test_getquoted_fallback_to_str(self):
        """Object with only getquoted falls back to str()."""

        class FakeAdapter:
            def getquoted(self):
                return b"'value'"

            def __str__(self):
                return "str_value"

        obj = FakeAdapter()
        result = serialize_value(obj)
        assert result == "str_value"

    def test_getquoted_with_adapted_nested(self):
        """getquoted with adapted that is a UUID serializes nested."""

        class FakeAdapter:
            def getquoted(self):
                return b"'uuid'"

            adapted = uuid.UUID("12345678-1234-5678-1234-567812345678")

        obj = FakeAdapter()
        result = serialize_value(obj)
        assert result == {"__uuid__": "12345678-1234-5678-1234-567812345678"}

    # --- memoryview ---

    def test_memoryview_valid_utf8(self):
        mv = memoryview(b"hello")
        result = serialize_value(mv)
        assert result == "hello"

    def test_memoryview_binary_data(self):
        binary_data = bytes([0xFF, 0xFE])
        mv = memoryview(binary_data)
        result = serialize_value(mv)
        assert isinstance(result, dict)
        assert "__bytes__" in result

    # --- bytes ---

    def test_bytes_valid_utf8(self):
        result = serialize_value(b"test data")
        assert result == "test data"

    def test_bytes_binary(self):
        binary_data = bytes([0x80, 0x81])
        result = serialize_value(binary_data)
        assert isinstance(result, dict)
        assert "__bytes__" in result

    # --- list and tuple ---

    def test_list_serializes_elements(self):
        result = serialize_value([datetime.date(2023, 1, 1), 42, "hello"])
        assert result == [{"__date__": "2023-01-01"}, 42, "hello"]

    def test_tuple_serializes_as_list(self):
        result = serialize_value((1, datetime.date(2023, 6, 1), "x"))
        assert result == [1, {"__date__": "2023-06-01"}, "x"]

    def test_empty_list(self):
        assert serialize_value([]) == []

    def test_empty_tuple(self):
        assert serialize_value(()) == []

    def test_nested_list(self):
        result = serialize_value([[1, 2], [3, 4]])
        assert result == [[1, 2], [3, 4]]

    # --- dict ---

    def test_dict_serializes_values(self):
        result = serialize_value({"key": datetime.date(2023, 1, 1), "other": 42})
        assert result == {"key": {"__date__": "2023-01-01"}, "other": 42}

    def test_empty_dict(self):
        assert serialize_value({}) == {}

    def test_nested_dict(self):
        result = serialize_value({"a": {"b": Decimal("1.5")}})
        assert result == {"a": {"b": {"__decimal__": "1.5"}}}

    def test_dict_with_mixed_values(self):
        result = serialize_value(
            {
                "uuid": uuid.UUID("12345678-1234-5678-1234-567812345678"),
                "count": 10,
                "name": "test",
            }
        )
        assert result == {
            "uuid": {"__uuid__": "12345678-1234-5678-1234-567812345678"},
            "count": 10,
            "name": "test",
        }

    # --- complex nested structures ---

    def test_list_of_dicts(self):
        result = serialize_value([{"a": Decimal("1.0")}, {"b": uuid.UUID("12345678-1234-5678-1234-567812345678")}])
        assert result == [
            {"a": {"__decimal__": "1.0"}},
            {"b": {"__uuid__": "12345678-1234-5678-1234-567812345678"}},
        ]

    def test_dict_with_list_value(self):
        result = serialize_value({"items": [1, Decimal("2.5"), "three"]})
        assert result == {"items": [1, {"__decimal__": "2.5"}, "three"]}
