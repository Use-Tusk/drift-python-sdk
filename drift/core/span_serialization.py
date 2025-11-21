"""Utilities for serializing spans into tusk-drift-schemas protos."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from tusk.drift.core.v1 import (
    DecodedType as ProtoDecodedType,
    EncodingType as ProtoEncodingType,
    JsonSchema as ProtoJsonSchema,
    JsonSchemaType as ProtoJsonSchemaType,
    PackageType as ProtoPackageType,
    Span as ProtoSpan,
    SpanKind as ProtoSpanKind,
    SpanStatus as ProtoSpanStatus,
    StatusCode as ProtoStatusCode,
)

from .json_schema_helper import DecodedType, EncodingType, JsonSchema, JsonSchemaType
from .types import CleanSpanData


def clean_span_to_proto(span: CleanSpanData) -> ProtoSpan:
    """Convert a CleanSpanData instance to a tusk.drift.core.v1.Span message."""

    return ProtoSpan(
        trace_id=span.trace_id,
        span_id=span.span_id,
        parent_span_id=span.parent_span_id,
        name=span.name,
        package_name=span.package_name,
        instrumentation_name=span.instrumentation_name,
        submodule_name=span.submodule_name,
        package_type=ProtoPackageType(span.package_type.value) if span.package_type else ProtoPackageType.UNSPECIFIED,
        kind=ProtoSpanKind(span.kind.value),
        input_value=span.input_value or {},
        output_value=span.output_value or {},
        input_schema=_json_schema_to_proto(span.input_schema),
        output_schema=_json_schema_to_proto(span.output_schema),
        input_schema_hash=span.input_schema_hash,
        output_schema_hash=span.output_schema_hash,
        input_value_hash=span.input_value_hash,
        output_value_hash=span.output_value_hash,
        status=ProtoSpanStatus(
            code=ProtoStatusCode(span.status.code.value),
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
        metadata=_metadata_to_dict(span.metadata),
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
    return ProtoJsonSchemaType(schema_type.value)


def _map_encoding_type(encoding: EncodingType) -> ProtoEncodingType:
    return ProtoEncodingType(encoding.value)


def _map_decoded_type(decoded: DecodedType) -> ProtoDecodedType:
    return ProtoDecodedType(decoded.value)


def _metadata_to_dict(metadata: Any) -> dict[str, Any]:
    if metadata is None:
        return {}

    return {
        key: value for key, value in metadata.__dict__.items() if value is not None
    }
