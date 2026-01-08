"""Unit tests for JsonSchemaHelper.

Ported from src/core/tracing/JsonSchemaHelper.test.ts
"""

import base64
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from drift.core.json_schema_helper import (
    DecodedType,
    EncodingType,
    JsonSchema,
    JsonSchemaHelper,
    JsonSchemaType,
    SchemaMerge,
)


class TestGetDetailedType(unittest.TestCase):
    """Tests for JsonSchemaHelper._determine_type (getDetailedType equivalent)."""

    def test_should_correctly_identify_primitive_types(self):
        self.assertEqual(JsonSchemaHelper._determine_type(None), JsonSchemaType.NULL)
        self.assertEqual(JsonSchemaHelper._determine_type("hello"), JsonSchemaType.STRING)
        self.assertEqual(JsonSchemaHelper._determine_type(42), JsonSchemaType.NUMBER)
        self.assertEqual(JsonSchemaHelper._determine_type(3.14), JsonSchemaType.NUMBER)
        self.assertEqual(JsonSchemaHelper._determine_type(True), JsonSchemaType.BOOLEAN)
        self.assertEqual(JsonSchemaHelper._determine_type(False), JsonSchemaType.BOOLEAN)

    def test_should_correctly_identify_object_types(self):
        self.assertEqual(JsonSchemaHelper._determine_type({}), JsonSchemaType.OBJECT)
        self.assertEqual(JsonSchemaHelper._determine_type([]), JsonSchemaType.ORDERED_LIST)
        self.assertEqual(JsonSchemaHelper._determine_type(set()), JsonSchemaType.UNORDERED_LIST)

    def test_should_identify_callable_as_function(self):
        self.assertEqual(JsonSchemaHelper._determine_type(lambda: None), JsonSchemaType.FUNCTION)

    def test_should_handle_tuples_as_ordered_lists(self):
        self.assertEqual(JsonSchemaHelper._determine_type((1, 2, 3)), JsonSchemaType.ORDERED_LIST)


class TestGenerateSchema(unittest.TestCase):
    """Tests for JsonSchemaHelper.generate_schema."""

    def test_should_generate_schema_for_primitive_types(self):
        null_schema = JsonSchemaHelper.generate_schema(None)
        self.assertEqual(null_schema.type, JsonSchemaType.NULL)
        self.assertEqual(null_schema.properties, {})

        string_schema = JsonSchemaHelper.generate_schema("test")
        self.assertEqual(string_schema.type, JsonSchemaType.STRING)
        self.assertEqual(string_schema.properties, {})

        number_schema = JsonSchemaHelper.generate_schema(42)
        self.assertEqual(number_schema.type, JsonSchemaType.NUMBER)
        self.assertEqual(number_schema.properties, {})

        bool_schema = JsonSchemaHelper.generate_schema(True)
        self.assertEqual(bool_schema.type, JsonSchemaType.BOOLEAN)
        self.assertEqual(bool_schema.properties, {})

    def test_should_generate_schema_for_empty_arrays(self):
        schema = JsonSchemaHelper.generate_schema([])
        self.assertEqual(schema.type, JsonSchemaType.ORDERED_LIST)
        self.assertEqual(schema.properties, {})
        self.assertIsNone(schema.items)

    def test_should_generate_schema_for_number_arrays(self):
        schema = JsonSchemaHelper.generate_schema([1, 2, 3])
        self.assertEqual(schema.type, JsonSchemaType.ORDERED_LIST)
        self.assertIsNotNone(schema.items)
        assert schema.items is not None
        self.assertEqual(schema.items.type, JsonSchemaType.NUMBER)

    def test_should_generate_schema_for_string_arrays(self):
        schema = JsonSchemaHelper.generate_schema(["a", "b"])
        self.assertEqual(schema.type, JsonSchemaType.ORDERED_LIST)
        self.assertIsNotNone(schema.items)
        assert schema.items is not None
        self.assertEqual(schema.items.type, JsonSchemaType.STRING)

    def test_should_generate_schema_for_object_arrays(self):
        schema = JsonSchemaHelper.generate_schema([{"id": 1}])
        self.assertEqual(schema.type, JsonSchemaType.ORDERED_LIST)
        self.assertIsNotNone(schema.items)
        assert schema.items is not None
        self.assertEqual(schema.items.type, JsonSchemaType.OBJECT)
        self.assertIn("id", schema.items.properties)
        self.assertEqual(schema.items.properties["id"].type, JsonSchemaType.NUMBER)

    def test_should_generate_schema_for_simple_objects(self):
        simple_obj = {"name": "John", "age": 30}
        schema = JsonSchemaHelper.generate_schema(simple_obj)

        self.assertEqual(schema.type, JsonSchemaType.OBJECT)
        self.assertIn("name", schema.properties)
        self.assertIn("age", schema.properties)
        self.assertEqual(schema.properties["name"].type, JsonSchemaType.STRING)
        self.assertEqual(schema.properties["age"].type, JsonSchemaType.NUMBER)

    def test_should_generate_schema_for_nested_objects(self):
        nested_obj = {
            "user": {
                "profile": {
                    "name": "Jane",
                },
            },
        }
        schema = JsonSchemaHelper.generate_schema(nested_obj)

        self.assertEqual(schema.type, JsonSchemaType.OBJECT)
        self.assertIn("user", schema.properties)

        user_schema = schema.properties["user"]
        self.assertEqual(user_schema.type, JsonSchemaType.OBJECT)
        self.assertIn("profile", user_schema.properties)

        profile_schema = user_schema.properties["profile"]
        self.assertEqual(profile_schema.type, JsonSchemaType.OBJECT)
        self.assertIn("name", profile_schema.properties)
        self.assertEqual(profile_schema.properties["name"].type, JsonSchemaType.STRING)

    def test_should_generate_schema_for_empty_set(self):
        schema = JsonSchemaHelper.generate_schema(set())
        self.assertEqual(schema.type, JsonSchemaType.UNORDERED_LIST)
        self.assertEqual(schema.properties, {})

    def test_should_generate_schema_for_number_set(self):
        schema = JsonSchemaHelper.generate_schema({1, 2, 3})
        self.assertEqual(schema.type, JsonSchemaType.UNORDERED_LIST)
        self.assertIsNotNone(schema.items)
        assert schema.items is not None
        self.assertEqual(schema.items.type, JsonSchemaType.NUMBER)

    def test_should_generate_schema_for_string_set(self):
        schema = JsonSchemaHelper.generate_schema({"a", "b"})
        self.assertEqual(schema.type, JsonSchemaType.UNORDERED_LIST)
        self.assertIsNotNone(schema.items)
        assert schema.items is not None
        self.assertEqual(schema.items.type, JsonSchemaType.STRING)

    def test_should_apply_schema_merges(self):
        data = {
            "body": "eyJuYW1lIjoiSm9obiJ9",  # base64 encoded JSON
            "header": "regular string",
        }
        merges = {
            "body": SchemaMerge(
                encoding=EncodingType.BASE64,
                decoded_type=DecodedType.JSON,
            ),
        }

        schema = JsonSchemaHelper.generate_schema(data, merges)

        self.assertEqual(schema.type, JsonSchemaType.OBJECT)
        self.assertIn("body", schema.properties)
        self.assertIn("header", schema.properties)

        body_schema = schema.properties["body"]
        self.assertEqual(body_schema.type, JsonSchemaType.STRING)
        self.assertEqual(body_schema.encoding, EncodingType.BASE64)
        self.assertEqual(body_schema.decoded_type, DecodedType.JSON)

        header_schema = schema.properties["header"]
        self.assertEqual(header_schema.type, JsonSchemaType.STRING)
        self.assertIsNone(header_schema.encoding)
        self.assertIsNone(header_schema.decoded_type)

    def test_should_not_apply_schema_merges_to_nested_properties_with_same_name(self):
        data = {
            "body": {
                "title": "Example Post",
                "body": "Hello everyone",
            },
        }
        merges = {
            "body": SchemaMerge(
                encoding=EncodingType.BASE64,
                decoded_type=DecodedType.JSON,
            ),
        }

        schema = JsonSchemaHelper.generate_schema(data, merges)

        # Top-level body should have the merge applied
        body_schema = schema.properties["body"]
        self.assertEqual(body_schema.type, JsonSchemaType.OBJECT)
        self.assertEqual(body_schema.encoding, EncodingType.BASE64)
        self.assertEqual(body_schema.decoded_type, DecodedType.JSON)

        # Nested body should NOT have merge applied
        nested_body_schema = body_schema.properties["body"]
        self.assertEqual(nested_body_schema.type, JsonSchemaType.STRING)
        self.assertIsNone(nested_body_schema.encoding)
        self.assertIsNone(nested_body_schema.decoded_type)


class TestSortObjectKeysRecursively(unittest.TestCase):
    """Tests for JsonSchemaHelper._sort_object_keys."""

    def test_should_sort_object_keys_recursively(self):
        input_data = {
            "z": 1,
            "a": {
                "y": 2,
                "b": 3,
            },
            "m": [{"z": 4, "a": 5}, "string"],
        }

        expected = {
            "a": {
                "b": 3,
                "y": 2,
            },
            "m": [{"a": 5, "z": 4}, "string"],
            "z": 1,
        }

        result = JsonSchemaHelper._sort_object_keys(input_data)
        self.assertEqual(result, expected)

    def test_should_handle_primitive_values(self):
        self.assertIsNone(JsonSchemaHelper._sort_object_keys(None))
        self.assertEqual(JsonSchemaHelper._sort_object_keys("string"), "string")
        self.assertEqual(JsonSchemaHelper._sort_object_keys(42), 42)
        self.assertEqual(JsonSchemaHelper._sort_object_keys(True), True)

    def test_should_handle_arrays_with_objects(self):
        input_data = [
            {"c": 1, "a": 2},
            {"z": 3, "b": 4},
        ]

        expected = [
            {"a": 2, "c": 1},
            {"b": 4, "z": 3},
        ]

        result = JsonSchemaHelper._sort_object_keys(input_data)
        self.assertEqual(result, expected)


class TestGenerateDeterministicHash(unittest.TestCase):
    """Tests for JsonSchemaHelper.generate_deterministic_hash."""

    def test_should_generate_consistent_hashes_for_same_data(self):
        data1 = {"b": 2, "a": 1}
        data2 = {"a": 1, "b": 2}

        hash1 = JsonSchemaHelper.generate_deterministic_hash(data1)
        hash2 = JsonSchemaHelper.generate_deterministic_hash(data2)

        self.assertEqual(hash1, hash2)
        self.assertIsInstance(hash1, str)
        self.assertEqual(len(hash1), 64)  # SHA256 hex length

    def test_should_generate_different_hashes_for_different_data(self):
        data1 = {"a": 1, "b": 2}
        data2 = {"a": 1, "b": 3}

        hash1 = JsonSchemaHelper.generate_deterministic_hash(data1)
        hash2 = JsonSchemaHelper.generate_deterministic_hash(data2)

        self.assertNotEqual(hash1, hash2)

    def test_should_handle_complex_nested_structures(self):
        data = {
            "users": [
                {"name": "John", "age": 30},
                {"name": "Jane", "age": 25},
            ],
            "metadata": {
                "total": 2,
                "updated": "2023-01-01",
            },
        }

        hash_result = JsonSchemaHelper.generate_deterministic_hash(data)
        self.assertIsInstance(hash_result, str)
        self.assertEqual(len(hash_result), 64)

        # Same data should produce same hash
        hash2 = JsonSchemaHelper.generate_deterministic_hash(data)
        self.assertEqual(hash_result, hash2)


class TestGenerateSchemaAndHash(unittest.TestCase):
    """Tests for JsonSchemaHelper.generate_schema_and_hash."""

    def test_should_generate_schema_and_hashes_for_simple_data(self):
        data = {"name": "John", "age": 30}
        result = JsonSchemaHelper.generate_schema_and_hash(data)

        self.assertEqual(result.schema.type, JsonSchemaType.OBJECT)
        self.assertIn("name", result.schema.properties)
        self.assertIn("age", result.schema.properties)
        self.assertEqual(result.schema.properties["name"].type, JsonSchemaType.STRING)
        self.assertEqual(result.schema.properties["age"].type, JsonSchemaType.NUMBER)

        self.assertIsInstance(result.decoded_value_hash, str)
        self.assertIsInstance(result.decoded_schema_hash, str)
        self.assertEqual(len(result.decoded_value_hash), 64)
        self.assertEqual(len(result.decoded_schema_hash), 64)

    def test_should_handle_schema_merges_with_base64_encoding(self):
        json_data = {"message": "Hello World"}
        base64_data = base64.b64encode(json.dumps(json_data).encode()).decode()

        data = {
            "body": base64_data,
            "contentType": "application/json",
        }

        schema_merges = {
            "body": SchemaMerge(
                encoding=EncodingType.BASE64,
                decoded_type=DecodedType.JSON,
            ),
        }

        result = JsonSchemaHelper.generate_schema_and_hash(data, schema_merges)

        # The decoded body should now be an object schema
        body_schema = result.schema.properties["body"]
        self.assertEqual(body_schema.type, JsonSchemaType.OBJECT)
        self.assertIn("message", body_schema.properties)
        self.assertEqual(body_schema.properties["message"].type, JsonSchemaType.STRING)
        self.assertEqual(body_schema.encoding, EncodingType.BASE64)
        self.assertEqual(body_schema.decoded_type, DecodedType.JSON)

        self.assertIsInstance(result.decoded_value_hash, str)
        self.assertIsInstance(result.decoded_schema_hash, str)

    def test_should_handle_empty_objects_and_arrays(self):
        data = {
            "emptyObj": {},
            "emptyArr": [],
            "items": [1, 2, 3],
        }

        result = JsonSchemaHelper.generate_schema_and_hash(data)

        self.assertEqual(result.schema.type, JsonSchemaType.OBJECT)

        empty_obj_schema = result.schema.properties["emptyObj"]
        self.assertEqual(empty_obj_schema.type, JsonSchemaType.OBJECT)
        self.assertEqual(empty_obj_schema.properties, {})

        empty_arr_schema = result.schema.properties["emptyArr"]
        self.assertEqual(empty_arr_schema.type, JsonSchemaType.ORDERED_LIST)

        items_schema = result.schema.properties["items"]
        self.assertEqual(items_schema.type, JsonSchemaType.ORDERED_LIST)
        self.assertIsNotNone(items_schema.items)
        assert items_schema.items is not None
        self.assertEqual(items_schema.items.type, JsonSchemaType.NUMBER)

    def test_should_handle_decoding_errors_gracefully(self):
        data = {
            "body": "invalid-base64!!!",
            "other": "valid",
        }

        schema_merges = {
            "body": SchemaMerge(
                encoding=EncodingType.BASE64,
                decoded_type=DecodedType.JSON,
            ),
        }

        # Should not raise, should handle gracefully
        result = JsonSchemaHelper.generate_schema_and_hash(data, schema_merges)

        # Body should remain a string since decode failed
        body_schema = result.schema.properties["body"]
        self.assertEqual(body_schema.type, JsonSchemaType.STRING)
        self.assertEqual(body_schema.encoding, EncodingType.BASE64)
        self.assertEqual(body_schema.decoded_type, DecodedType.JSON)


class TestEncodingAndDecodedTypeEnums(unittest.TestCase):
    """Tests for EncodingType and DecodedType enums."""

    def test_should_have_correct_encoding_type_values(self):
        self.assertEqual(EncodingType.UNSPECIFIED.value, 0)
        self.assertEqual(EncodingType.BASE64.value, 1)

    def test_should_have_correct_decoded_type_values(self):
        self.assertEqual(DecodedType.UNSPECIFIED.value, 0)
        self.assertEqual(DecodedType.JSON.value, 1)
        self.assertEqual(DecodedType.HTML.value, 2)
        self.assertEqual(DecodedType.CSS.value, 3)
        self.assertEqual(DecodedType.JAVASCRIPT.value, 4)
        self.assertEqual(DecodedType.XML.value, 5)
        self.assertEqual(DecodedType.YAML.value, 6)
        self.assertEqual(DecodedType.MARKDOWN.value, 7)
        self.assertEqual(DecodedType.CSV.value, 8)
        self.assertEqual(DecodedType.SQL.value, 9)
        self.assertEqual(DecodedType.GRAPHQL.value, 10)
        self.assertEqual(DecodedType.PLAIN_TEXT.value, 11)
        self.assertEqual(DecodedType.FORM_DATA.value, 12)
        self.assertEqual(DecodedType.MULTIPART_FORM.value, 13)
        self.assertEqual(DecodedType.PDF.value, 14)
        self.assertEqual(DecodedType.AUDIO.value, 15)
        self.assertEqual(DecodedType.VIDEO.value, 16)
        self.assertEqual(DecodedType.GZIP.value, 17)
        self.assertEqual(DecodedType.BINARY.value, 18)
        self.assertEqual(DecodedType.JPEG.value, 19)
        self.assertEqual(DecodedType.PNG.value, 20)
        self.assertEqual(DecodedType.GIF.value, 21)
        self.assertEqual(DecodedType.WEBP.value, 22)
        self.assertEqual(DecodedType.SVG.value, 23)
        self.assertEqual(DecodedType.ZIP.value, 24)


class TestJsonSchemaToPrimitive(unittest.TestCase):
    """Tests for JsonSchema.to_primitive conversion."""

    def test_should_convert_simple_schema_to_primitive(self):
        schema = JsonSchema(type=JsonSchemaType.STRING)
        primitive = schema.to_primitive()

        self.assertEqual(primitive["type"], JsonSchemaType.STRING.value)
        self.assertEqual(primitive["properties"], {})
        self.assertNotIn("items", primitive)
        self.assertNotIn("encoding", primitive)
        self.assertNotIn("decoded_type", primitive)

    def test_should_convert_complex_schema_to_primitive(self):
        schema = JsonSchema(
            type=JsonSchemaType.OBJECT,
            properties={
                "name": JsonSchema(type=JsonSchemaType.STRING),
                "age": JsonSchema(type=JsonSchemaType.NUMBER),
            },
        )
        primitive = schema.to_primitive()

        self.assertEqual(primitive["type"], JsonSchemaType.OBJECT.value)
        self.assertIn("name", primitive["properties"])
        self.assertIn("age", primitive["properties"])
        self.assertEqual(primitive["properties"]["name"]["type"], JsonSchemaType.STRING.value)
        self.assertEqual(primitive["properties"]["age"]["type"], JsonSchemaType.NUMBER.value)

    def test_should_include_encoding_and_decoded_type_when_set(self):
        schema = JsonSchema(
            type=JsonSchemaType.STRING,
            encoding=EncodingType.BASE64,
            decoded_type=DecodedType.JSON,
        )
        primitive = schema.to_primitive()

        self.assertEqual(primitive["encoding"], EncodingType.BASE64.value)
        self.assertEqual(primitive["decoded_type"], DecodedType.JSON.value)

    def test_should_include_items_for_arrays(self):
        schema = JsonSchema(
            type=JsonSchemaType.ORDERED_LIST,
            items=JsonSchema(type=JsonSchemaType.NUMBER),
        )
        primitive = schema.to_primitive()

        self.assertEqual(primitive["type"], JsonSchemaType.ORDERED_LIST.value)
        self.assertIn("items", primitive)
        self.assertEqual(primitive["items"]["type"], JsonSchemaType.NUMBER.value)


if __name__ == "__main__":
    unittest.main()
