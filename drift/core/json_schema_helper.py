"""Helpers for generating tusk-drift JsonSchema objects and hashes."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping, MutableMapping
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any

from .rust_core_binding import deterministic_hash_jsonable, normalize_and_hash_jsonable, normalize_json_jsonable


class JsonSchemaType(Enum):
    UNSPECIFIED = 0
    NUMBER = 1
    STRING = 2
    BOOLEAN = 3
    NULL = 4
    UNDEFINED = 5
    OBJECT = 6
    ORDERED_LIST = 7
    UNORDERED_LIST = 8
    FUNCTION = 9


class EncodingType(Enum):
    UNSPECIFIED = 0
    BASE64 = 1


class DecodedType(Enum):
    UNSPECIFIED = 0
    JSON = 1
    HTML = 2
    CSS = 3
    JAVASCRIPT = 4
    XML = 5
    YAML = 6
    MARKDOWN = 7
    CSV = 8
    SQL = 9
    GRAPHQL = 10
    PLAIN_TEXT = 11
    FORM_DATA = 12
    MULTIPART_FORM = 13
    PDF = 14
    AUDIO = 15
    VIDEO = 16
    GZIP = 17
    BINARY = 18
    JPEG = 19
    PNG = 20
    GIF = 21
    WEBP = 22
    SVG = 23
    ZIP = 24


@dataclass
class SchemaMerge:
    encoding: EncodingType | None = None
    decoded_type: DecodedType | None = None
    match_importance: float | None = None


SchemaMerges = dict[str, SchemaMerge]


@dataclass
class SchemaComputationResult:
    schema: JsonSchema
    decoded_value_hash: str
    decoded_schema_hash: str


@dataclass
class JsonSchema:
    type: JsonSchemaType = JsonSchemaType.UNSPECIFIED
    properties: dict[str, JsonSchema] = field(default_factory=dict)
    items: JsonSchema | None = None
    encoding: EncodingType | None = None
    decoded_type: DecodedType | None = None
    match_importance: float | None = None

    def to_primitive(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "type": self.type.value if isinstance(self.type, Enum) else self.type,
            "properties": {k: v.to_primitive() for k, v in self.properties.items()},
        }
        if self.items is not None:
            data["items"] = self.items.to_primitive()
        if self.encoding is not None:
            data["encoding"] = self.encoding.value if isinstance(self.encoding, Enum) else self.encoding
        if self.decoded_type is not None:
            data["decoded_type"] = self.decoded_type.value if isinstance(self.decoded_type, Enum) else self.decoded_type
        if self.match_importance is not None:
            data["match_importance"] = float(self.match_importance)
        return data


class JsonSchemaHelper:
    @staticmethod
    def generate_schema(data: Any, schema_merges: SchemaMerges | None = None) -> JsonSchema:
        schema_merges = schema_merges or {}
        schema_type = JsonSchemaHelper._determine_type(data)

        if schema_type == JsonSchemaType.OBJECT and isinstance(data, Mapping):
            schema = JsonSchema(type=JsonSchemaType.OBJECT)
            for key, value in data.items():
                child_schema = JsonSchemaHelper.generate_schema(value)
                merge = schema_merges.get(str(key))
                if merge:
                    JsonSchemaHelper._apply_merge(child_schema, merge)
                schema.properties[str(key)] = child_schema
            return schema

        if schema_type == JsonSchemaType.ORDERED_LIST and isinstance(data, (list, tuple)):
            schema = JsonSchema(type=JsonSchemaType.ORDERED_LIST)
            if data:
                schema.items = JsonSchemaHelper.generate_schema(data[0])
            return schema

        if schema_type == JsonSchemaType.UNORDERED_LIST and isinstance(data, set):
            schema = JsonSchema(type=JsonSchemaType.UNORDERED_LIST)
            first = next(iter(data), None)
            if first is not None:
                schema.items = JsonSchemaHelper.generate_schema(first)
            return schema

        return JsonSchema(type=schema_type)

    @staticmethod
    def generate_schema_and_hash(data: Any, schema_merges: SchemaMerges | None = None) -> SchemaComputationResult:
        # Convert non-JSON primitives once. Rust/Python paths consume this shared shape.
        sanitized = JsonSchemaHelper._to_jsonable(data)

        normalized: Any
        decoded: Any
        decoded_value_hash: str

        # If there are no merges, use one coarse Rust call for normalize+hash.
        if not schema_merges:
            rust_normalized = normalize_and_hash_jsonable(sanitized)
            if rust_normalized is not None:
                normalized, decoded_value_hash = rust_normalized
            else:
                normalized = json.loads(json.dumps(sanitized))
                decoded_value_hash = JsonSchemaHelper.generate_deterministic_hash(normalized)
            decoded = normalized
        else:
            # Merges require decode before value-hash. Let Rust handle normalize only.
            rust_normalized_only = normalize_json_jsonable(sanitized)
            if rust_normalized_only is not None:
                normalized = rust_normalized_only
            else:
                normalized = json.loads(json.dumps(sanitized))

            decoded = JsonSchemaHelper._decode_with_merges(normalized, schema_merges)
            rust_decoded_hash = deterministic_hash_jsonable(decoded)
            if rust_decoded_hash is not None:
                decoded_value_hash = rust_decoded_hash
            else:
                sorted_decoded = JsonSchemaHelper._sort_object_keys(decoded)
                payload = json.dumps(sorted_decoded, ensure_ascii=False, separators=(",", ":"))
                decoded_value_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()

        schema = JsonSchemaHelper.generate_schema(decoded, schema_merges)

        schema_primitive = schema.to_primitive()
        rust_schema_hash = deterministic_hash_jsonable(schema_primitive)
        if rust_schema_hash is not None:
            decoded_schema_hash = rust_schema_hash
        else:
            sorted_schema = JsonSchemaHelper._sort_object_keys(schema_primitive)
            payload = json.dumps(sorted_schema, ensure_ascii=False, separators=(",", ":"))
            decoded_schema_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return SchemaComputationResult(
            schema=schema,
            decoded_value_hash=decoded_value_hash,
            decoded_schema_hash=decoded_schema_hash,
        )

    @staticmethod
    def generate_deterministic_hash(data: Any) -> str:
        rust_hash = deterministic_hash_jsonable(data)
        if rust_hash is not None:
            return rust_hash
        sorted_data = JsonSchemaHelper._sort_object_keys(data)
        payload = json.dumps(sorted_data, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _determine_type(value: Any) -> JsonSchemaType:
        if value is None:
            return JsonSchemaType.NULL
        if isinstance(value, bool):
            return JsonSchemaType.BOOLEAN
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return JsonSchemaType.NUMBER
        if isinstance(value, str):
            return JsonSchemaType.STRING
        if isinstance(value, (list, tuple)):
            return JsonSchemaType.ORDERED_LIST
        if isinstance(value, set):
            return JsonSchemaType.UNORDERED_LIST
        if callable(value):
            return JsonSchemaType.FUNCTION
        if isinstance(value, Mapping):
            return JsonSchemaType.OBJECT
        return JsonSchemaType.OBJECT

    @staticmethod
    def _apply_merge(schema: JsonSchema, merge: SchemaMerge) -> JsonSchema:
        if merge.encoding is not None:
            schema.encoding = merge.encoding
        if merge.decoded_type is not None:
            schema.decoded_type = merge.decoded_type
        if merge.match_importance is not None:
            schema.match_importance = merge.match_importance
        return schema

    @staticmethod
    def _normalize_data(data: Any) -> Any:
        sanitized = JsonSchemaHelper._to_jsonable(data)
        return json.loads(json.dumps(sanitized))

    @staticmethod
    def _to_jsonable(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(k): JsonSchemaHelper._to_jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [JsonSchemaHelper._to_jsonable(v) for v in value]
        if isinstance(value, set):
            return [JsonSchemaHelper._to_jsonable(v) for v in value]
        if isinstance(value, (bytes, bytearray)):
            return base64.b64encode(value).decode("ascii")
        if is_dataclass(value):
            return JsonSchemaHelper._to_jsonable(asdict(value))
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    @staticmethod
    def _decode_with_merges(data: Any, schema_merges: SchemaMerges | None) -> Any:
        if not schema_merges or not isinstance(data, MutableMapping):
            return data
        decoded = dict(data)
        for key, merge in schema_merges.items():
            if key not in decoded:
                continue
            value = decoded[key]
            try:
                if merge.encoding == EncodingType.BASE64 and isinstance(value, str):
                    value_bytes = base64.b64decode(value.encode("utf-8"))
                    value = value_bytes.decode("utf-8", errors="ignore")
                if merge.decoded_type == DecodedType.JSON and isinstance(value, str):
                    value = json.loads(value)
            except Exception:
                pass
            decoded[key] = value
        return decoded

    @staticmethod
    def _sort_object_keys(value: Any) -> Any:
        if isinstance(value, list):
            return [JsonSchemaHelper._sort_object_keys(v) for v in value]
        if isinstance(value, Mapping):
            return {k: JsonSchemaHelper._sort_object_keys(value[k]) for k in sorted(value)}
        return value
