"""Unit tests for data normalization utilities.

Ported from src/core/utils/DataNormalization.test.ts
"""

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from drift.core.data_normalization import (
    create_mock_input_value,
    create_span_input_value,
    remove_none_values,
)


class TestRemoveNoneValues:
    """Tests for remove_none_values function (normalizeInputData equivalent)."""

    def test_should_remove_none_values_from_objects(self):
        """Remove None values but keep other falsy values."""
        input_data = {
            "a": "value",
            "b": None,
            "c": None,
            "d": 0,
            "e": False,
            "f": "",
        }

        result = remove_none_values(input_data)

        assert result == {
            "a": "value",
            "d": 0,
            "e": False,
            "f": "",
        }
        assert "b" not in result
        assert "c" not in result

    def test_should_handle_nested_objects_with_none_values(self):
        """Handle nested objects with None values."""
        input_data = {
            "user": {
                "name": "John",
                "age": None,
                "address": {
                    "street": "123 Main St",
                    "zip": None,
                    "country": None,
                },
            },
            "metadata": None,
        }

        result = remove_none_values(input_data)

        expected = {
            "user": {
                "name": "John",
                "address": {
                    "street": "123 Main St",
                },
            },
        }
        assert result == expected
        assert "age" not in result["user"]
        assert "zip" not in result["user"]["address"]
        assert "metadata" not in result

    def test_should_handle_arrays_with_none_values(self):
        """Arrays should preserve None (like JS null in arrays)."""
        input_data = {
            "items": ["a", None, "b", None, "c"],
            "numbers": [1, None, 2, 0],
        }

        result = remove_none_values(input_data)

        assert result == {
            "items": ["a", None, "b", None, "c"],
            "numbers": [1, None, 2, 0],
        }

    def test_should_handle_circular_references_safely(self):
        """Circular references should be replaced with '[Circular]'."""
        input_data = {
            "name": "test",
            "value": 123,
        }
        input_data["self"] = input_data

        result = remove_none_values(input_data)

        assert result == {
            "name": "test",
            "value": 123,
            "self": "[Circular]",
        }

    def test_should_handle_empty_objects(self):
        """Empty objects should remain empty."""
        input_data = {}
        result = remove_none_values(input_data)
        assert result == {}

    def test_should_handle_primitive_values_wrapped_in_objects(self):
        """Test various primitive values in objects."""
        input_data = {
            "string": "test",
            "number": 42,
            "boolean": True,
            "none": None,
            "undefined": None,
        }

        result = remove_none_values(input_data)

        assert result == {
            "string": "test",
            "number": 42,
            "boolean": True,
        }

    def test_should_preserve_date_objects_as_iso_strings(self):
        """Date objects should be converted to ISO strings."""
        date = datetime(2023, 1, 1, 12, 0, 0)
        input_data = {
            "timestamp": date,
            "other": None,
        }

        result = remove_none_values(input_data)

        assert result == {
            "timestamp": date.isoformat(),
        }

    def test_should_handle_complex_nested_structures(self):
        """Test complex nested structures with various types."""
        input_data = {
            "level1": {
                "level2": {
                    "level3": {
                        "value": "deep",
                        "none_val": None,
                    },
                    "array": [{"keep": "this", "remove": None}, None, "string"],
                },
            },
        }

        result = remove_none_values(input_data)

        expected = {
            "level1": {
                "level2": {
                    "level3": {
                        "value": "deep",
                    },
                    "array": [{"keep": "this"}, None, "string"],
                },
            },
        }
        assert result == expected


class TestCreateSpanInputValue:
    """Tests for create_span_input_value function."""

    def test_should_return_json_string_of_normalized_data(self):
        """Should return a JSON string of normalized data."""
        input_data = {
            "user": "john",
            "age": None,
            "active": True,
        }

        result = create_span_input_value(input_data)

        assert isinstance(result, str)
        assert json.loads(result) == {
            "user": "john",
            "active": True,
        }

    def test_should_handle_circular_references_in_span_values(self):
        """Circular references should be handled in JSON output."""
        input_data = {
            "name": "test",
        }
        input_data["circular"] = input_data

        result = create_span_input_value(input_data)

        assert isinstance(result, str)
        assert json.loads(result) == {
            "name": "test",
            "circular": "[Circular]",
        }

    def test_should_produce_consistent_output_for_identical_normalized_data(self):
        """Same normalized data should produce same JSON output."""
        input1 = {"a": 1, "b": None, "c": "test"}
        input2 = {"a": 1, "c": "test"}

        result1 = create_span_input_value(input1)
        result2 = create_span_input_value(input2)

        assert result1 == result2


class TestCreateMockInputValue:
    """Tests for create_mock_input_value function."""

    def test_should_return_normalized_object_data(self):
        """Should return normalized object data (not JSON string)."""
        input_data = {
            "user": "john",
            "age": None,
            "active": True,
        }

        result = create_mock_input_value(input_data)

        assert result == {
            "user": "john",
            "active": True,
        }
        assert "age" not in result

    def test_should_handle_circular_references_in_mock_values(self):
        """Circular references should be replaced with marker."""
        input_data = {
            "name": "test",
        }
        input_data["circular"] = input_data

        result = create_mock_input_value(input_data)

        assert result == {
            "name": "test",
            "circular": "[Circular]",
        }

    def test_should_produce_consistent_output_for_identical_normalized_data(self):
        """Same normalized data should produce same output."""
        input1 = {"a": 1, "b": None, "c": "test"}
        input2 = {"a": 1, "c": "test"}

        result1 = create_mock_input_value(input1)
        result2 = create_mock_input_value(input2)

        assert result1 == result2

    def test_should_preserve_type_information(self):
        """Type information should be preserved in output."""
        input_data = {
            "id": 1,
            "name": "test",
            "optional": None,
        }

        result = create_mock_input_value(input_data)

        assert result == {
            "id": 1,
            "name": "test",
        }


class TestConsistencyBetweenFunctions:
    """Tests ensuring consistency between span and mock value functions."""

    def test_should_ensure_functions_produce_equivalent_data_structures(self):
        """createSpanInputValue and createMockInputValue should produce equivalent data."""
        input_data = {
            "database": "users",
            "query": "SELECT * FROM users",
            "params": None,
            "options": {
                "timeout": 5000,
                "debug": None,
                "cache": True,
            },
        }

        span_value = create_span_input_value(input_data)
        mock_value = create_mock_input_value(input_data)

        assert json.loads(span_value) == mock_value

    def test_should_handle_edge_cases_consistently(self):
        """Edge cases should be handled consistently by both functions."""
        edge_cases = [
            {},
            {"only": None},
            {"mixed": "value", "empty": None},
            {"nested": {"deep": {"value": "test", "none_val": None}}},
        ]

        for test_case in edge_cases:
            span_value = create_span_input_value(test_case)
            mock_value = create_mock_input_value(test_case)

            assert json.loads(span_value) == mock_value, f"Mismatch for test case: {test_case}"


class TestEdgeCases:
    """Additional edge case tests."""

    def test_deeply_nested_circular_reference(self):
        """Test deeply nested circular references."""
        input_data = {"level1": {"level2": {"level3": {}}}}
        input_data["level1"]["level2"]["level3"]["back_to_root"] = input_data

        result = remove_none_values(input_data)

        assert result["level1"]["level2"]["level3"]["back_to_root"] == "[Circular]"

    def test_multiple_circular_references(self):
        """Test multiple circular references to same object."""
        shared = {"name": "shared"}
        input_data = {"ref1": shared, "ref2": shared, "nested": {"ref3": shared}}

        result = remove_none_values(input_data)

        assert result["ref1"]["name"] == "shared"
        assert result["ref2"]["name"] == "shared"
        assert result["nested"]["ref3"]["name"] == "shared"

    def test_list_containing_dicts_with_none(self):
        """Test list containing dicts with None values."""
        input_data = {
            "items": [
                {"id": 1, "name": "first", "extra": None},
                {"id": 2, "name": "second", "extra": None},
            ]
        }

        result = remove_none_values(input_data)

        expected = {
            "items": [
                {"id": 1, "name": "first"},
                {"id": 2, "name": "second"},
            ]
        }
        assert result == expected

    def test_empty_string_is_preserved(self):
        """Empty strings should be preserved."""
        input_data = {"empty": "", "none": None}
        result = remove_none_values(input_data)
        assert result == {"empty": ""}

    def test_zero_is_preserved(self):
        """Zero values should be preserved."""
        input_data = {"zero": 0, "none": None}
        result = remove_none_values(input_data)
        assert result == {"zero": 0}

    def test_false_is_preserved(self):
        """False values should be preserved."""
        input_data = {"false": False, "none": None}
        result = remove_none_values(input_data)
        assert result == {"false": False}
