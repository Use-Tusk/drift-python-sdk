"""Utilities for serializing spans into tusk-drift-schemas protos."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from betterproto.lib.google.protobuf import Struct as ProtoStruct
from betterproto.lib.google.protobuf import Value as ProtoValue
from tusk.drift.core.v1 import (
    DecodedType as ProtoDecodedType,
)
from tusk.drift.core.v1 import (
    EncodingType as ProtoEncodingType,
)
from tusk.drift.core.v1 import (
    JsonSchema as ProtoJsonSchema,
)
from tusk.drift.core.v1 import (
    JsonSchemaType as ProtoJsonSchemaType,
)
from tusk.drift.core.v1 import (
    Span as ProtoSpan,
)
from tusk.drift.core.v1 import (
    SpanStatus as ProtoSpanStatus,
)

from .json_schema_helper import DecodedType, EncodingType, JsonSchema, JsonSchemaType
from .types import CleanSpanData, PackageType


def _value_to_proto(value: Any) -> ProtoValue:
    """Convert a single Python value to protobuf Value."""
    from betterproto.lib.google.protobuf import ListValue as ProtoListValue

    proto_value = ProtoValue()

    if value is None:
        # betterproto 2.0.0b7 uses integer 0 for null value (NullValue.NULL_VALUE doesn't exist)
        proto_value.null_value = 0  # type: ignore[assignment]
    elif isinstance(value, bool):
        proto_value.bool_value = value
    elif isinstance(value, (int, float)):
        proto_value.number_value = float(value)
    elif isinstance(value, str):
        proto_value.string_value = value
    elif isinstance(value, dict):
        proto_value.struct_value = _dict_to_struct(value)
    elif isinstance(value, list):
        # Create a new ListValue and populate it recursively
        list_val = ProtoListValue()
        for item in value:
            list_val.values.append(_value_to_proto(item))
        proto_value.list_value = list_val
    else:
        # Fallback: convert to string
        proto_value.string_value = str(value)

    return proto_value


def _dict_to_struct(data: Any) -> ProtoStruct:
    """Convert a Python dict/value to protobuf Struct.

    This recursively converts Python values to protobuf Value types.
    """
    if data is None or data == {}:
        return ProtoStruct()

    if not isinstance(data, dict):
        # If it's already a Struct, return it
        if isinstance(data, ProtoStruct):
            return data
        # Otherwise convert single value to struct with empty fields
        return ProtoStruct()

    struct = ProtoStruct()
    struct.fields = {}

    for key, value in data.items():
        struct.fields[key] = _value_to_proto(value)

    return struct


def _struct_from_precomputed_or_dict(proto_struct_bytes: bytes | None, data: Any) -> ProtoStruct:
    """Prefer precomputed protobuf bytes; fall back to Python dict conversion."""
    if proto_struct_bytes:
        try:
            struct = ProtoStruct()
            parse_fn = getattr(struct, "parse", None)
            if callable(parse_fn):
                parse_fn(proto_struct_bytes)
                return struct
        except Exception:
            # Fallback to Python conversion if parsing fails.
            pass
    return _dict_to_struct(data)


def clean_span_to_proto(span: CleanSpanData) -> ProtoSpan:
    """Convert a CleanSpanData instance to a tusk.drift.core.v1.Span message."""
    if span.proto_span_bytes:
        try:
            proto = ProtoSpan()
            parse_fn = getattr(proto, "parse", None)
            if callable(parse_fn):
                parse_fn(span.proto_span_bytes)
                return proto
        except Exception:
            pass

    return ProtoSpan(
        trace_id=span.trace_id,
        span_id=span.span_id,
        parent_span_id=span.parent_span_id,
        name=span.name,
        package_name=span.package_name,
        instrumentation_name=span.instrumentation_name,
        submodule_name=span.submodule_name,
        package_type=span.package_type.value if span.package_type else PackageType.UNSPECIFIED.value,  # type: ignore[arg-type]
        environment=span.environment,
        kind=span.kind.value if hasattr(span.kind, "value") else span.kind,
        input_value=_struct_from_precomputed_or_dict(span.input_value_proto_struct_bytes, span.input_value),
        output_value=_struct_from_precomputed_or_dict(span.output_value_proto_struct_bytes, span.output_value),
        input_schema=_json_schema_to_proto(span.input_schema),
        output_schema=_json_schema_to_proto(span.output_schema),
        input_schema_hash=span.input_schema_hash,
        output_schema_hash=span.output_schema_hash,
        input_value_hash=span.input_value_hash,
        output_value_hash=span.output_value_hash,
        status=ProtoSpanStatus(
            code=span.status.code.value if hasattr(span.status.code, "value") else span.status.code,
            message=span.status.message,
        ),
        is_pre_app_start=span.is_pre_app_start,
        is_root_span=span.is_root_span,
        timestamp=datetime.fromtimestamp(
            span.timestamp.seconds + span.timestamp.nanos / 1_000_000_000,
            tz=timezone.utc,
        ),
        duration=timedelta(
            seconds=span.duration.seconds,
            microseconds=span.duration.nanos // 1000,
        ),
        metadata=_dict_to_struct(_metadata_to_dict(span.metadata)),
    )


def _json_schema_to_proto(schema: JsonSchema | None) -> ProtoJsonSchema:
    if schema is None:
        return ProtoJsonSchema()

    return ProtoJsonSchema(
        type=_map_schema_type(schema.type),
        properties={k: _json_schema_to_proto(v) for k, v in schema.properties.items()},
        items=_json_schema_to_proto(schema.items) if schema.items else None,
        encoding=_map_encoding_type(schema.encoding) if schema.encoding else None,
        decoded_type=_map_decoded_type(schema.decoded_type) if schema.decoded_type else None,
        match_importance=schema.match_importance,
    )


def _map_schema_type(schema_type: JsonSchemaType) -> ProtoJsonSchemaType:
    return schema_type.value if hasattr(schema_type, "value") else schema_type


def _map_encoding_type(encoding: EncodingType) -> ProtoEncodingType:
    return encoding.value if hasattr(encoding, "value") else encoding


def _map_decoded_type(decoded: DecodedType) -> ProtoDecodedType:
    return decoded.value if hasattr(decoded, "value") else decoded


def _metadata_to_dict(metadata: Any) -> dict[str, Any]:
    if metadata is None:
        return {}

    if isinstance(metadata, Mapping):
        return {str(key): value for key, value in metadata.items() if value is not None}

    return {key: value for key, value in metadata.__dict__.items() if value is not None}
