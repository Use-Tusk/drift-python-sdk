"""Unit tests for JsonSchemaHelper.

Ported from src/core/tracing/JsonSchemaHelper.test.ts
"""

import base64
import json
import sys
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


class TestGetDetailedType:
    """Tests for JsonSchemaHelper._determine_type (getDetailedType equivalent)."""

    def test_should_correctly_identify_primitive_types(self):
        """Test correct identification of primitive types."""
        assert JsonSchemaHelper._determine_type(None) == JsonSchemaType.NULL
        assert JsonSchemaHelper._determine_type("hello") == JsonSchemaType.STRING
        assert JsonSchemaHelper._determine_type(42) == JsonSchemaType.NUMBER
        assert JsonSchemaHelper._determine_type(3.14) == JsonSchemaType.NUMBER
        assert JsonSchemaHelper._determine_type(True) == JsonSchemaType.BOOLEAN
        assert JsonSchemaHelper._determine_type(False) == JsonSchemaType.BOOLEAN

    def test_should_correctly_identify_object_types(self):
        """Test correct identification of object types."""
        assert JsonSchemaHelper._determine_type({}) == JsonSchemaType.OBJECT
        assert JsonSchemaHelper._determine_type([]) == JsonSchemaType.ORDERED_LIST
        assert JsonSchemaHelper._determine_type(set()) == JsonSchemaType.UNORDERED_LIST

    def test_should_identify_callable_as_function(self):
        """Test identification of callable as function."""
        assert JsonSchemaHelper._determine_type(lambda: None) == JsonSchemaType.FUNCTION

    def test_should_handle_tuples_as_ordered_lists(self):
        """Test handling of tuples as ordered lists."""
        assert JsonSchemaHelper._determine_type((1, 2, 3)) == JsonSchemaType.ORDERED_LIST


class TestGenerateSchema:
    """Tests for JsonSchemaHelper.generate_schema."""

    def test_should_generate_schema_for_primitive_types(self):
        """Test schema generation for primitive types."""
        null_schema = JsonSchemaHelper.generate_schema(None)
        assert null_schema.type == JsonSchemaType.NULL
        assert null_schema.properties == {}

        string_schema = JsonSchemaHelper.generate_schema("test")
        assert string_schema.type == JsonSchemaType.STRING
        assert string_schema.properties == {}

        number_schema = JsonSchemaHelper.generate_schema(42)
        assert number_schema.type == JsonSchemaType.NUMBER
        assert number_schema.properties == {}

        bool_schema = JsonSchemaHelper.generate_schema(True)
        assert bool_schema.type == JsonSchemaType.BOOLEAN
        assert bool_schema.properties == {}

    def test_should_generate_schema_for_empty_arrays(self):
        """Test schema generation for empty arrays."""
        schema = JsonSchemaHelper.generate_schema([])
        assert schema.type == JsonSchemaType.ORDERED_LIST
        assert schema.properties == {}
        assert schema.items is None

    def test_should_generate_schema_for_number_arrays(self):
        """Test schema generation for number arrays."""
        schema = JsonSchemaHelper.generate_schema([1, 2, 3])
        assert schema.type == JsonSchemaType.ORDERED_LIST
        assert schema.items is not None
        assert schema.items.type == JsonSchemaType.NUMBER

    def test_should_generate_schema_for_string_arrays(self):
        """Test schema generation for string arrays."""
        schema = JsonSchemaHelper.generate_schema(["a", "b"])
        assert schema.type == JsonSchemaType.ORDERED_LIST
        assert schema.items is not None
        assert schema.items.type == JsonSchemaType.STRING

    def test_should_generate_schema_for_object_arrays(self):
        """Test schema generation for object arrays."""
        schema = JsonSchemaHelper.generate_schema([{"id": 1}])
        assert schema.type == JsonSchemaType.ORDERED_LIST
        assert schema.items is not None
        assert schema.items.type == JsonSchemaType.OBJECT
        assert "id" in schema.items.properties
        assert schema.items.properties["id"].type == JsonSchemaType.NUMBER

    def test_should_generate_schema_for_simple_objects(self):
        """Test schema generation for simple objects."""
        simple_obj = {"name": "John", "age": 30}
        schema = JsonSchemaHelper.generate_schema(simple_obj)

        assert schema.type == JsonSchemaType.OBJECT
        assert "name" in schema.properties
        assert "age" in schema.properties
        assert schema.properties["name"].type == JsonSchemaType.STRING
        assert schema.properties["age"].type == JsonSchemaType.NUMBER

    def test_should_generate_schema_for_nested_objects(self):
        """Test schema generation for nested objects."""
        nested_obj = {
            "user": {
                "profile": {
                    "name": "Jane",
                },
            },
        }
        schema = JsonSchemaHelper.generate_schema(nested_obj)

        assert schema.type == JsonSchemaType.OBJECT
        assert "user" in schema.properties

        user_schema = schema.properties["user"]
        assert user_schema.type == JsonSchemaType.OBJECT
        assert "profile" in user_schema.properties

        profile_schema = user_schema.properties["profile"]
        assert profile_schema.type == JsonSchemaType.OBJECT
        assert "name" in profile_schema.properties
        assert profile_schema.properties["name"].type == JsonSchemaType.STRING

    def test_should_generate_schema_for_empty_set(self):
        """Test schema generation for empty set."""
        schema = JsonSchemaHelper.generate_schema(set())
        assert schema.type == JsonSchemaType.UNORDERED_LIST
        assert schema.properties == {}

    def test_should_generate_schema_for_number_set(self):
        """Test schema generation for number set."""
        schema = JsonSchemaHelper.generate_schema({1, 2, 3})
        assert schema.type == JsonSchemaType.UNORDERED_LIST
        assert schema.items is not None
        assert schema.items.type == JsonSchemaType.NUMBER

    def test_should_generate_schema_for_string_set(self):
        """Test schema generation for string set."""
        schema = JsonSchemaHelper.generate_schema({"a", "b"})
        assert schema.type == JsonSchemaType.UNORDERED_LIST
        assert schema.items is not None
        assert schema.items.type == JsonSchemaType.STRING

    def test_should_apply_schema_merges(self):
        """Test applying schema merges."""
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

        assert schema.type == JsonSchemaType.OBJECT
        assert "body" in schema.properties
        assert "header" in schema.properties

        body_schema = schema.properties["body"]
        assert body_schema.type == JsonSchemaType.STRING
        assert body_schema.encoding == EncodingType.BASE64
        assert body_schema.decoded_type == DecodedType.JSON

        header_schema = schema.properties["header"]
        assert header_schema.type == JsonSchemaType.STRING
        assert header_schema.encoding is None
        assert header_schema.decoded_type is None

    def test_should_not_apply_schema_merges_to_nested_properties_with_same_name(self):
        """Test that schema merges don't apply to nested properties with same name."""
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

        body_schema = schema.properties["body"]
        assert body_schema.type == JsonSchemaType.OBJECT
        assert body_schema.encoding == EncodingType.BASE64
        assert body_schema.decoded_type == DecodedType.JSON

        nested_body_schema = body_schema.properties["body"]
        assert nested_body_schema.type == JsonSchemaType.STRING
        assert nested_body_schema.encoding is None
        assert nested_body_schema.decoded_type is None


class TestSortObjectKeysRecursively:
    """Tests for JsonSchemaHelper._sort_object_keys."""

    def test_should_sort_object_keys_recursively(self):
        """Test recursive sorting of object keys."""
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
        assert result == expected

    def test_should_handle_primitive_values(self):
        """Test handling of primitive values."""
        assert JsonSchemaHelper._sort_object_keys(None) is None
        assert JsonSchemaHelper._sort_object_keys("string") == "string"
        assert JsonSchemaHelper._sort_object_keys(42) == 42
        assert JsonSchemaHelper._sort_object_keys(True) is True

    def test_should_handle_arrays_with_objects(self):
        """Test handling of arrays with objects."""
        input_data = [
            {"c": 1, "a": 2},
            {"z": 3, "b": 4},
        ]

        expected = [
            {"a": 2, "c": 1},
            {"b": 4, "z": 3},
        ]

        result = JsonSchemaHelper._sort_object_keys(input_data)
        assert result == expected


class TestGenerateDeterministicHash:
    """Tests for JsonSchemaHelper.generate_deterministic_hash."""

    def test_should_generate_consistent_hashes_for_same_data(self):
        """Test consistent hash generation for same data."""
        data1 = {"b": 2, "a": 1}
        data2 = {"a": 1, "b": 2}

        hash1 = JsonSchemaHelper.generate_deterministic_hash(data1)
        hash2 = JsonSchemaHelper.generate_deterministic_hash(data2)

        assert hash1 == hash2
        assert isinstance(hash1, str)
        assert len(hash1) == 64  # SHA256 hex length

    def test_should_generate_different_hashes_for_different_data(self):
        """Test different hash generation for different data."""
        data1 = {"a": 1, "b": 2}
        data2 = {"a": 1, "b": 3}

        hash1 = JsonSchemaHelper.generate_deterministic_hash(data1)
        hash2 = JsonSchemaHelper.generate_deterministic_hash(data2)

        assert hash1 != hash2

    def test_should_handle_complex_nested_structures(self):
        """Test handling of complex nested structures."""
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
        assert isinstance(hash_result, str)
        assert len(hash_result) == 64

        hash2 = JsonSchemaHelper.generate_deterministic_hash(data)
        assert hash_result == hash2


class TestGenerateSchemaAndHash:
    """Tests for JsonSchemaHelper.generate_schema_and_hash."""

    def test_should_generate_schema_and_hashes_for_simple_data(self):
        """Test schema and hash generation for simple data."""
        data = {"name": "John", "age": 30}
        result = JsonSchemaHelper.generate_schema_and_hash(data)

        assert result.schema.type == JsonSchemaType.OBJECT
        assert "name" in result.schema.properties
        assert "age" in result.schema.properties
        assert result.schema.properties["name"].type == JsonSchemaType.STRING
        assert result.schema.properties["age"].type == JsonSchemaType.NUMBER

        assert isinstance(result.decoded_value_hash, str)
        assert isinstance(result.decoded_schema_hash, str)
        assert len(result.decoded_value_hash) == 64
        assert len(result.decoded_schema_hash) == 64

    def test_should_handle_schema_merges_with_base64_encoding(self):
        """Test schema merges with base64 encoding."""
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

        body_schema = result.schema.properties["body"]
        assert body_schema.type == JsonSchemaType.OBJECT
        assert "message" in body_schema.properties
        assert body_schema.properties["message"].type == JsonSchemaType.STRING
        assert body_schema.encoding == EncodingType.BASE64
        assert body_schema.decoded_type == DecodedType.JSON

        assert isinstance(result.decoded_value_hash, str)
        assert isinstance(result.decoded_schema_hash, str)

    def test_should_handle_empty_objects_and_arrays(self):
        """Test handling of empty objects and arrays."""
        data = {
            "emptyObj": {},
            "emptyArr": [],
            "items": [1, 2, 3],
        }

        result = JsonSchemaHelper.generate_schema_and_hash(data)

        assert result.schema.type == JsonSchemaType.OBJECT

        empty_obj_schema = result.schema.properties["emptyObj"]
        assert empty_obj_schema.type == JsonSchemaType.OBJECT
        assert empty_obj_schema.properties == {}

        empty_arr_schema = result.schema.properties["emptyArr"]
        assert empty_arr_schema.type == JsonSchemaType.ORDERED_LIST

        items_schema = result.schema.properties["items"]
        assert items_schema.type == JsonSchemaType.ORDERED_LIST
        assert items_schema.items is not None
        assert items_schema.items.type == JsonSchemaType.NUMBER

    def test_should_handle_decoding_errors_gracefully(self):
        """Test graceful handling of decoding errors."""
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

        result = JsonSchemaHelper.generate_schema_and_hash(data, schema_merges)

        body_schema = result.schema.properties["body"]
        assert body_schema.type == JsonSchemaType.STRING
        assert body_schema.encoding == EncodingType.BASE64
        assert body_schema.decoded_type == DecodedType.JSON


class TestEncodingAndDecodedTypeEnums:
    """Tests for EncodingType and DecodedType enums."""

    def test_should_have_correct_encoding_type_values(self):
        """Test correct EncodingType values."""
        assert EncodingType.UNSPECIFIED.value == 0
        assert EncodingType.BASE64.value == 1

    def test_should_have_correct_decoded_type_values(self):
        """Test correct DecodedType values."""
        assert DecodedType.UNSPECIFIED.value == 0
        assert DecodedType.JSON.value == 1
        assert DecodedType.HTML.value == 2
        assert DecodedType.CSS.value == 3
        assert DecodedType.JAVASCRIPT.value == 4
        assert DecodedType.XML.value == 5
        assert DecodedType.YAML.value == 6
        assert DecodedType.MARKDOWN.value == 7
        assert DecodedType.CSV.value == 8
        assert DecodedType.SQL.value == 9
        assert DecodedType.GRAPHQL.value == 10
        assert DecodedType.PLAIN_TEXT.value == 11
        assert DecodedType.FORM_DATA.value == 12
        assert DecodedType.MULTIPART_FORM.value == 13
        assert DecodedType.PDF.value == 14
        assert DecodedType.AUDIO.value == 15
        assert DecodedType.VIDEO.value == 16
        assert DecodedType.GZIP.value == 17
        assert DecodedType.BINARY.value == 18
        assert DecodedType.JPEG.value == 19
        assert DecodedType.PNG.value == 20
        assert DecodedType.GIF.value == 21
        assert DecodedType.WEBP.value == 22
        assert DecodedType.SVG.value == 23
        assert DecodedType.ZIP.value == 24


class TestJsonSchemaToPrimitive:
    """Tests for JsonSchema.to_primitive conversion."""

    def test_should_convert_simple_schema_to_primitive(self):
        """Test simple schema conversion to primitive."""
        schema = JsonSchema(type=JsonSchemaType.STRING)
        primitive = schema.to_primitive()

        assert primitive["type"] == JsonSchemaType.STRING.value
        assert primitive["properties"] == {}
        assert "items" not in primitive
        assert "encoding" not in primitive
        assert "decoded_type" not in primitive

    def test_should_convert_complex_schema_to_primitive(self):
        """Test complex schema conversion to primitive."""
        schema = JsonSchema(
            type=JsonSchemaType.OBJECT,
            properties={
                "name": JsonSchema(type=JsonSchemaType.STRING),
                "age": JsonSchema(type=JsonSchemaType.NUMBER),
            },
        )
        primitive = schema.to_primitive()

        assert primitive["type"] == JsonSchemaType.OBJECT.value
        assert "name" in primitive["properties"]
        assert "age" in primitive["properties"]
        assert primitive["properties"]["name"]["type"] == JsonSchemaType.STRING.value
        assert primitive["properties"]["age"]["type"] == JsonSchemaType.NUMBER.value

    def test_should_include_encoding_and_decoded_type_when_set(self):
        """Test inclusion of encoding and decoded_type when set."""
        schema = JsonSchema(
            type=JsonSchemaType.STRING,
            encoding=EncodingType.BASE64,
            decoded_type=DecodedType.JSON,
        )
        primitive = schema.to_primitive()

        assert primitive["encoding"] == EncodingType.BASE64.value
        assert primitive["decoded_type"] == DecodedType.JSON.value

    def test_should_include_items_for_arrays(self):
        """Test inclusion of items for arrays."""
        schema = JsonSchema(
            type=JsonSchemaType.ORDERED_LIST,
            items=JsonSchema(type=JsonSchemaType.NUMBER),
        )
        primitive = schema.to_primitive()

        assert primitive["type"] == JsonSchemaType.ORDERED_LIST.value
        assert "items" in primitive
        assert primitive["items"]["type"] == JsonSchemaType.NUMBER.value
