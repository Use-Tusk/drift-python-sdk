"""Converter from OpenTelemetry spans to CleanSpanData.

This module provides functions to convert OpenTelemetry ReadableSpan objects
to CleanSpanData for protobuf export.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import StatusCode as OTelStatusCode

if TYPE_CHECKING:
    from ..json_schema_helper import JsonSchema

from ..types import (
    CleanSpanData,
    Duration,
    PackageType,
    SpanKind,
    SpanStatus,
    StatusCode,
    Timestamp,
    TransformAction,
    TransformMetadata,
)
from .td_attributes import TdSpanAttributes

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan

logger = logging.getLogger(__name__)


def _dict_to_transform_metadata(data: dict | None) -> TransformMetadata | None:
    """Convert a dictionary to TransformMetadata."""
    if data is None:
        return None

    actions = []
    if "actions" in data and isinstance(data["actions"], list):
        for action_dict in data["actions"]:
            if isinstance(action_dict, dict):
                actions.append(
                    TransformAction(
                        type=action_dict.get("type", "redact"),  # type: ignore[arg-type]
                        field=action_dict.get("field", ""),
                        reason=action_dict.get("reason", ""),
                        description=action_dict.get("description"),
                    )
                )

    return TransformMetadata(
        transformed=data.get("transformed", False),
        actions=actions,
    )


def format_trace_id(trace_id: int) -> str:
    """Format OpenTelemetry trace ID (int) to hex string."""
    return format(trace_id, "032x")


def format_span_id(span_id: int) -> str:
    """Format OpenTelemetry span ID (int) to hex string."""
    return format(span_id, "016x")


def ns_to_timestamp(time_ns: int) -> Timestamp:
    """Convert nanoseconds to Timestamp."""
    seconds = time_ns // 1_000_000_000
    nanos = time_ns % 1_000_000_000
    return Timestamp(seconds=seconds, nanos=nanos)


def ns_to_duration(duration_ns: int) -> Duration:
    """Convert nanoseconds to Duration."""
    seconds = duration_ns // 1_000_000_000
    nanos = duration_ns % 1_000_000_000
    return Duration(seconds=seconds, nanos=nanos)


def otel_status_to_drift_status(otel_status: Any) -> SpanStatus:
    """Convert OpenTelemetry status to Drift SpanStatus."""
    if otel_status.status_code == OTelStatusCode.OK:
        return SpanStatus(code=StatusCode.OK, message=otel_status.description or "")
    elif otel_status.status_code == OTelStatusCode.ERROR:
        return SpanStatus(code=StatusCode.ERROR, message=otel_status.description or "")
    else:
        return SpanStatus(code=StatusCode.UNSPECIFIED, message=otel_status.description or "")


def otel_span_kind_to_drift(otel_kind: Any) -> SpanKind:
    """Convert OpenTelemetry SpanKind to Drift SpanKind."""
    from opentelemetry.trace import SpanKind as OTelSpanKind

    # Handle case where kind is already an int (defensive)
    if isinstance(otel_kind, int):
        try:
            return SpanKind(otel_kind)
        except ValueError:
            logger.warning(f"Unknown span kind int value: {otel_kind}, defaulting to UNSPECIFIED")
            return SpanKind.UNSPECIFIED

    mapping = {
        OTelSpanKind.INTERNAL: SpanKind.INTERNAL,
        OTelSpanKind.SERVER: SpanKind.SERVER,
        OTelSpanKind.CLIENT: SpanKind.CLIENT,
        OTelSpanKind.PRODUCER: SpanKind.PRODUCER,
        OTelSpanKind.CONSUMER: SpanKind.CONSUMER,
    }
    return mapping.get(otel_kind, SpanKind.UNSPECIFIED)


def get_attribute_as_str(attributes: dict, key: str, default: str = "") -> str:
    """Get attribute as string, returning default if not found or not a string."""
    value = attributes.get(key)
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def get_attribute_as_dict(attributes: dict, key: str) -> dict | None:
    """Get attribute as dict by parsing JSON string."""
    value = attributes.get(key)
    if value is None:
        return None

    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse attribute {key} as JSON: {value[:100]}")
            return None

    if isinstance(value, dict):
        return value

    return None


def dict_to_json_schema(schema_dict: dict | None) -> JsonSchema:
    """Recursively convert a schema dict to JsonSchema object."""
    from ..json_schema_helper import DecodedType, EncodingType, JsonSchema, JsonSchemaType

    if schema_dict is None or not schema_dict:
        return JsonSchema()

    # Convert type
    schema_type = schema_dict.get("type", 0)
    if isinstance(schema_type, int):
        try:
            schema_type = JsonSchemaType(schema_type)
        except ValueError:
            schema_type = JsonSchemaType.UNSPECIFIED
    elif not isinstance(schema_type, JsonSchemaType):
        schema_type = JsonSchemaType.UNSPECIFIED

    # Recursively convert properties
    properties = {}
    if "properties" in schema_dict and isinstance(schema_dict["properties"], dict):
        properties = {
            k: dict_to_json_schema(v) if isinstance(v, dict) else v for k, v in schema_dict["properties"].items()
        }

    # Recursively convert items
    items = None
    if "items" in schema_dict:
        items_value = schema_dict["items"]
        if isinstance(items_value, dict):
            items = dict_to_json_schema(items_value)

    # Convert encoding
    encoding = None
    if "encoding" in schema_dict:
        enc_value = schema_dict["encoding"]
        if isinstance(enc_value, int):
            try:
                encoding = EncodingType(enc_value)
            except ValueError:
                pass
        elif isinstance(enc_value, EncodingType):
            encoding = enc_value

    # Convert decoded_type
    decoded_type = None
    if "decoded_type" in schema_dict:
        dec_value = schema_dict["decoded_type"]
        if isinstance(dec_value, int):
            try:
                decoded_type = DecodedType(dec_value)
            except ValueError:
                pass
        elif isinstance(dec_value, DecodedType):
            decoded_type = dec_value

    match_importance = schema_dict.get("match_importance")

    return JsonSchema(
        type=schema_type,
        properties=properties,
        items=items,
        encoding=encoding,
        decoded_type=decoded_type,
        match_importance=match_importance,
    )


def get_attribute_as_bool(attributes: dict, key: str, default: bool = False) -> bool:
    """Get attribute as boolean."""
    value = attributes.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)


def get_attribute_as_schema_merges(attributes: dict, key: str) -> dict | None:
    """Get attribute as SchemaMerges by parsing JSON string.

    Args:
        attributes: Span attributes dictionary
        key: Attribute key

    Returns:
        SchemaMerges dictionary or None if not found/invalid
    """
    from ..json_schema_helper import DecodedType, EncodingType, SchemaMerge

    value = attributes.get(key)
    if value is None:
        return None

    if isinstance(value, str):
        try:
            merges_dict = json.loads(value)
            if not isinstance(merges_dict, dict):
                return None

            # Convert dict to SchemaMerge objects
            result = {}
            for field_key, merge_data in merges_dict.items():
                if not isinstance(merge_data, dict):
                    continue

                encoding = None
                if "encoding" in merge_data:
                    try:
                        encoding = EncodingType(merge_data["encoding"])
                    except (ValueError, KeyError):
                        pass

                decoded_type = None
                if "decoded_type" in merge_data:
                    try:
                        decoded_type = DecodedType(merge_data["decoded_type"])
                    except (ValueError, KeyError):
                        pass

                match_importance = merge_data.get("match_importance")

                result[field_key] = SchemaMerge(
                    encoding=encoding, decoded_type=decoded_type, match_importance=match_importance
                )

            return result
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse attribute {key} as JSON: {value[:100]}")
            return None

    return None


def parse_package_type(package_type_str: str) -> PackageType:
    """Parse package type string to PackageType enum."""
    try:
        # Try to match by name (e.g., "HTTP" -> PackageType.HTTP)
        return PackageType[package_type_str.upper()]
    except (KeyError, AttributeError):
        logger.warning(f"Unknown package type: {package_type_str}, defaulting to UNSPECIFIED")
        return PackageType.UNSPECIFIED


def otel_span_to_clean_span_data(
    otel_span: ReadableSpan,
    environment: str | None = None,
) -> CleanSpanData:
    """Convert OpenTelemetry ReadableSpan to CleanSpanData.

    This function extracts all Drift-specific attributes from the OTel span
    and constructs a CleanSpanData object suitable for protobuf export.

    Args:
        otel_span: The OpenTelemetry ReadableSpan to convert
        environment: Optional environment name to include on the span

    Returns:
        CleanSpanData instance with all fields populated from OTel span
    """
    # Extract span context info
    span_context = otel_span.context
    trace_id = format_trace_id(span_context.trace_id)
    span_id = format_span_id(span_context.span_id)

    # Extract parent span ID
    parent_span_id = ""
    if otel_span.parent and otel_span.parent.span_id:
        parent_span_id = format_span_id(otel_span.parent.span_id)

    # Extract attributes
    attributes = dict(otel_span.attributes) if otel_span.attributes else {}

    # Extract core fields
    name = get_attribute_as_str(attributes, TdSpanAttributes.NAME, otel_span.name)
    package_name = get_attribute_as_str(attributes, TdSpanAttributes.PACKAGE_NAME)
    instrumentation_name = get_attribute_as_str(attributes, TdSpanAttributes.INSTRUMENTATION_NAME)
    submodule_name = get_attribute_as_str(attributes, TdSpanAttributes.SUBMODULE_NAME)

    # Parse package type
    package_type_str = get_attribute_as_str(attributes, TdSpanAttributes.PACKAGE_TYPE, "UNKNOWN")
    package_type = parse_package_type(package_type_str)

    # Extract data
    input_value = get_attribute_as_dict(attributes, TdSpanAttributes.INPUT_VALUE) or {}
    output_value = get_attribute_as_dict(attributes, TdSpanAttributes.OUTPUT_VALUE) or {}

    # Extract schema merges
    input_schema_merges = get_attribute_as_schema_merges(attributes, TdSpanAttributes.INPUT_SCHEMA_MERGES)
    output_schema_merges = get_attribute_as_schema_merges(attributes, TdSpanAttributes.OUTPUT_SCHEMA_MERGES)

    # Generate schemas and hashes at export time
    from ..json_schema_helper import JsonSchemaHelper

    input_schema_result = JsonSchemaHelper.generate_schema_and_hash(input_value, input_schema_merges)
    output_schema_result = JsonSchemaHelper.generate_schema_and_hash(output_value, output_schema_merges)

    # Extract computed values
    input_schema = input_schema_result.schema.to_primitive()
    output_schema = output_schema_result.schema.to_primitive()
    input_schema_hash = input_schema_result.decoded_schema_hash
    output_schema_hash = output_schema_result.decoded_schema_hash
    input_value_hash = input_schema_result.decoded_value_hash
    output_value_hash = output_schema_result.decoded_value_hash

    # Extract flags
    is_pre_app_start = get_attribute_as_bool(attributes, TdSpanAttributes.IS_PRE_APP_START)
    is_root_span = get_attribute_as_bool(attributes, TdSpanAttributes.IS_ROOT_SPAN)
    is_used = get_attribute_as_bool(attributes, TdSpanAttributes.IS_USED, default=True)

    # Extract metadata
    metadata = get_attribute_as_dict(attributes, TdSpanAttributes.METADATA)
    transform_metadata_dict = get_attribute_as_dict(attributes, TdSpanAttributes.TRANSFORM_METADATA)
    transform_metadata = _dict_to_transform_metadata(transform_metadata_dict)
    stack_trace = get_attribute_as_str(attributes, TdSpanAttributes.STACK_TRACE)

    # Convert timing
    start_time = otel_span.start_time or 0
    timestamp = ns_to_timestamp(start_time)
    end_time = otel_span.end_time or 0
    duration_ns = end_time - start_time if end_time else 0
    duration = ns_to_duration(duration_ns)

    # Convert status
    status = otel_status_to_drift_status(otel_span.status)

    # Convert kind
    kind = otel_span_kind_to_drift(otel_span.kind)

    # Convert schema dicts to JsonSchema objects recursively
    input_schema_obj = dict_to_json_schema(input_schema)
    output_schema_obj = dict_to_json_schema(output_schema)

    # Build CleanSpanData (note: no input_schema_merges or output_schema_merges fields)
    return CleanSpanData(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        name=name,
        package_name=package_name,
        instrumentation_name=instrumentation_name,
        submodule_name=submodule_name,
        package_type=package_type,
        environment=environment,
        kind=kind,
        input_value=input_value,
        output_value=output_value,
        input_schema=input_schema_obj,
        output_schema=output_schema_obj,
        input_schema_hash=input_schema_hash,
        output_schema_hash=output_schema_hash,
        input_value_hash=input_value_hash,
        output_value_hash=output_value_hash,
        status=status,
        is_pre_app_start=is_pre_app_start,
        is_root_span=is_root_span,
        is_used=is_used,
        timestamp=timestamp,
        duration=duration,
        metadata=metadata,
        transform_metadata=transform_metadata,
        stack_trace=stack_trace,
    )
