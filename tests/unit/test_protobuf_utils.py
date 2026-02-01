"""Unit tests for protobuf conversion utilities."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from drift.core.protobuf_utils import struct_to_dict, value_to_python


class TestValueToPythonNativeTypes:
    """Tests for value_to_python with Python native types."""

    def test_returns_none_for_none(self):
        """None should return None."""
        assert value_to_python(None) is None

    def test_returns_bool_unchanged(self):
        """Boolean values should be returned unchanged."""
        assert value_to_python(True) is True
        assert value_to_python(False) is False

    def test_returns_int_unchanged(self):
        """Integer values should be returned unchanged."""
        assert value_to_python(42) == 42
        assert value_to_python(0) == 0
        assert value_to_python(-1) == -1

    def test_returns_float_unchanged(self):
        """Float values should be returned unchanged."""
        assert value_to_python(3.14) == 3.14
        assert value_to_python(0.0) == 0.0

    def test_returns_str_unchanged(self):
        """String values should be returned unchanged."""
        assert value_to_python("hello") == "hello"
        assert value_to_python("") == ""

    def test_processes_list_recursively(self):
        """List values should be processed recursively."""
        assert value_to_python([1, 2, 3]) == [1, 2, 3]
        assert value_to_python(["a", "b"]) == ["a", "b"]
        assert value_to_python([]) == []

    def test_processes_dict_recursively(self):
        """Dict values should be processed recursively."""
        assert value_to_python({"a": 1, "b": 2}) == {"a": 1, "b": 2}
        assert value_to_python({}) == {}

    def test_processes_nested_structures(self):
        """Nested structures should be processed recursively."""
        nested = {
            "users": [
                {"name": "Alice", "age": 30},
                {"name": "Bob", "age": 25},
            ],
            "count": 2,
        }
        assert value_to_python(nested) == nested

    def test_bool_not_confused_with_int(self):
        """Bool should be returned as bool, not converted to int."""
        # This is important because bool is a subclass of int in Python
        result = value_to_python(True)
        assert result is True
        assert type(result) is bool


class TestValueToPythonGoogleProtobuf:
    """Tests for value_to_python with Google protobuf Value objects."""

    def test_handles_null_value(self):
        """Google protobuf null_value should return None."""
        mock_value = MagicMock()
        mock_value.WhichOneof.return_value = "null_value"
        assert value_to_python(mock_value) is None

    def test_handles_number_value(self):
        """Google protobuf number_value should return the number."""
        mock_value = MagicMock()
        mock_value.WhichOneof.return_value = "number_value"
        mock_value.number_value = 42.0
        assert value_to_python(mock_value) == 42.0

    def test_handles_string_value(self):
        """Google protobuf string_value should return the string."""
        mock_value = MagicMock()
        mock_value.WhichOneof.return_value = "string_value"
        mock_value.string_value = "hello"
        assert value_to_python(mock_value) == "hello"

    def test_handles_bool_value(self):
        """Google protobuf bool_value should return the bool."""
        mock_value = MagicMock()
        mock_value.WhichOneof.return_value = "bool_value"
        mock_value.bool_value = True
        assert value_to_python(mock_value) is True

    def test_handles_list_value(self):
        """Google protobuf list_value should return a list."""
        # Create mock list items
        item1 = MagicMock()
        item1.WhichOneof.return_value = "number_value"
        item1.number_value = 1.0

        item2 = MagicMock()
        item2.WhichOneof.return_value = "number_value"
        item2.number_value = 2.0

        mock_value = MagicMock()
        mock_value.WhichOneof.return_value = "list_value"
        mock_value.list_value.values = [item1, item2]

        assert value_to_python(mock_value) == [1.0, 2.0]


class TestValueToPythonBetterproto:
    """Tests for value_to_python with betterproto Value objects."""

    def _make_betterproto_value(self, field_name, field_value):
        """Helper to create a mock betterproto Value."""
        mock_value = MagicMock()
        # No WhichOneof (not Google protobuf)
        del mock_value.WhichOneof

        def is_set(name):
            return name == field_name

        mock_value.is_set = is_set
        setattr(mock_value, field_name, field_value)
        return mock_value

    def test_handles_null_value(self):
        """Betterproto null_value should return None."""
        mock_value = self._make_betterproto_value("null_value", 0)
        assert value_to_python(mock_value) is None

    def test_handles_number_value(self):
        """Betterproto number_value should return the number."""
        mock_value = self._make_betterproto_value("number_value", 42.0)
        assert value_to_python(mock_value) == 42.0

    def test_handles_string_value(self):
        """Betterproto string_value should return the string."""
        mock_value = self._make_betterproto_value("string_value", "hello")
        assert value_to_python(mock_value) == "hello"

    def test_handles_bool_value(self):
        """Betterproto bool_value should return the bool."""
        mock_value = self._make_betterproto_value("bool_value", True)
        assert value_to_python(mock_value) is True

    def test_handles_dict_style_struct_value(self):
        """Betterproto struct_value as dict should be handled."""
        mock_value = MagicMock()
        del mock_value.WhichOneof

        def is_set(name):
            return name == "struct_value"

        mock_value.is_set = is_set
        mock_value.struct_value = {"key": "value"}

        result = value_to_python(mock_value)
        assert result == {"key": "value"}

    def test_handles_dict_style_list_value(self):
        """Betterproto list_value as dict should be handled."""
        mock_value = MagicMock()
        del mock_value.WhichOneof

        def is_set(name):
            return name == "list_value"

        mock_value.is_set = is_set
        mock_value.list_value = {"values": [1, 2, 3]}

        result = value_to_python(mock_value)
        assert result == [1, 2, 3]


class TestValueToPythonFallback:
    """Tests for value_to_python fallback behavior."""

    def test_returns_unknown_type_as_is(self):
        """Unknown types should be returned as-is (not None)."""

        class CustomType:
            pass

        obj = CustomType()
        result = value_to_python(obj)
        assert result is obj

    def test_handles_is_set_exception_gracefully(self):
        """If is_set raises an exception, should fall through gracefully."""
        mock_value = MagicMock()
        del mock_value.WhichOneof

        def is_set(name):
            raise TypeError("Unexpected error")

        mock_value.is_set = is_set

        # Should not raise, should return value as-is
        result = value_to_python(mock_value)
        assert result is mock_value


class TestStructToDict:
    """Tests for struct_to_dict function."""

    def test_returns_empty_dict_for_none(self):
        """None should return empty dict."""
        assert struct_to_dict(None) == {}

    def test_returns_empty_dict_for_empty_struct(self):
        """Empty struct should return empty dict."""
        mock_struct = MagicMock()
        mock_struct.fields = {}
        assert struct_to_dict(mock_struct) == {}

    def test_handles_dict_input(self):
        """Dict input should be processed and returned."""
        result = struct_to_dict({"a": 1, "b": "hello"})
        assert result == {"a": 1, "b": "hello"}

    def test_handles_struct_with_fields(self):
        """Struct with fields attribute should be converted."""
        mock_struct = MagicMock()
        mock_struct.fields = {"name": "Alice", "age": 30}
        # Remove items() to ensure we go through the fields path
        del mock_struct.items

        result = struct_to_dict(mock_struct)
        assert result == {"name": "Alice", "age": 30}

    def test_processes_nested_values(self):
        """Nested values in struct should be processed recursively."""
        mock_struct = MagicMock()
        mock_struct.fields = {
            "user": {"name": "Bob", "scores": [1, 2, 3]},
        }
        del mock_struct.items

        result = struct_to_dict(mock_struct)
        assert result == {"user": {"name": "Bob", "scores": [1, 2, 3]}}
