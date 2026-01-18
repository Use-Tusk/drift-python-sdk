"""Utility functions for gRPC instrumentation."""

from __future__ import annotations

import base64
import copy
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Sentinel value for replaced buffers
BUFFER_PLACEHOLDER = "__tusk_drift_buffer_replaced__"


def is_utf8(data: bytes) -> bool:
    """Check if bytes contain valid UTF-8 text."""
    try:
        decoded = data.decode("utf-8")
        # Verify round-trip works
        return data == decoded.encode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return False


def serialize_grpc_metadata(metadata: Any) -> dict[str, list[str | dict[str, str]]]:
    """
    Convert gRPC Metadata object to a plain Python dict.

    Args:
        metadata: grpc.Metadata or similar object

    Returns:
        Dict mapping keys to lists of values (strings or encoded buffers)
    """
    if metadata is None:
        return {}

    readable_metadata: dict[str, list[str | dict[str, str]]] = {}

    # Handle different metadata formats
    # grpc.Metadata can be iterated as (key, value) tuples
    try:
        items = list(metadata) if hasattr(metadata, "__iter__") else []
    except TypeError:
        return {}

    for key, value in items:
        if key not in readable_metadata:
            readable_metadata[key] = []

        if isinstance(value, str):
            readable_metadata[key].append(value)
        elif isinstance(value, bytes):
            # Handle binary values
            if is_utf8(value):
                readable_metadata[key].append({"value": value.decode("utf-8"), "encoding": "utf8"})
            else:
                readable_metadata[key].append({"value": base64.b64encode(value).decode("ascii"), "encoding": "base64"})
        else:
            # Convert other types to string
            readable_metadata[key].append(str(value))

    return readable_metadata


def deserialize_grpc_metadata(
    readable_metadata: dict[str, list[str | dict[str, str]]],
) -> list[tuple[str, str | bytes]]:
    """
    Convert a plain Python dict back to gRPC metadata tuples.

    Args:
        readable_metadata: Dict from serialize_grpc_metadata

    Returns:
        List of (key, value) tuples suitable for grpc.Metadata
    """
    result: list[tuple[str, str | bytes]] = []

    for key, values in readable_metadata.items():
        for value in values:
            if isinstance(value, str):
                result.append((key, value))
            elif isinstance(value, dict) and "value" in value and "encoding" in value:
                # Handle encoded buffer
                if value["encoding"] == "utf8":
                    result.append((key, value["value"].encode("utf-8")))
                else:
                    result.append((key, base64.b64decode(value["value"])))

    return result


def parse_grpc_path(path: str) -> tuple[str, str]:
    """
    Extract service and method name from gRPC path.

    Path format: /package.ServiceName/MethodName

    Args:
        path: gRPC method path

    Returns:
        Tuple of (method, service)
    """
    if not path:
        return ("", "")

    # Remove leading slash and split
    parts = path.lstrip("/").split("/")
    service = parts[0] if len(parts) > 0 else ""
    method = parts[1] if len(parts) > 1 else ""

    return (method, service)


def serialize_grpc_payload(payload: Any) -> tuple[Any, dict[str, dict[str, str]], dict[str, str]]:
    """
    Convert request/response body to a serializable format, handling bytes.

    Protobuf messages often contain bytes fields which need special handling
    for JSON serialization. This function replaces bytes with placeholders
    and stores the actual data in a separate map.

    Args:
        payload: Protobuf message (as dict or object with __dict__)

    Returns:
        Tuple of (readable_body, buffer_map, jsonable_string_map)
    """
    buffer_map: dict[str, dict[str, str]] = {}
    jsonable_string_map: dict[str, str] = {}

    # Convert protobuf message to dict if needed
    if hasattr(payload, "DESCRIPTOR"):
        # It's a protobuf message - convert to dict
        try:
            from google.protobuf.json_format import MessageToDict

            readable_body = MessageToDict(payload, preserving_proto_field_name=True)
        except ImportError:
            # Fallback: try to access fields directly
            readable_body = _proto_to_dict(payload)
    elif isinstance(payload, dict):
        readable_body = copy.deepcopy(payload)
    else:
        # Try to convert to dict
        readable_body = copy.deepcopy(payload) if payload is not None else None

    # Process the body recursively to handle bytes
    if readable_body is not None:
        _process_payload_for_serialization(readable_body, buffer_map, jsonable_string_map, [])

    return (readable_body, buffer_map, jsonable_string_map)


def _proto_to_dict(message: Any) -> dict[str, Any]:
    """Convert a protobuf message to dict without protobuf library."""
    result: dict[str, Any] = {}

    if hasattr(message, "DESCRIPTOR"):
        for field in message.DESCRIPTOR.fields:
            value = getattr(message, field.name)
            if field.message_type is not None:
                # Nested message
                if field.label == field.LABEL_REPEATED:
                    result[field.name] = [_proto_to_dict(v) for v in value]
                else:
                    result[field.name] = _proto_to_dict(value)
            else:
                result[field.name] = value
    elif hasattr(message, "__dict__"):
        result = copy.deepcopy(message.__dict__)

    return result


def _process_payload_for_serialization(
    payload: Any,
    buffer_map: dict[str, dict[str, str]],
    jsonable_string_map: dict[str, str],
    path: list[str],
) -> None:
    """
    Recursively process a payload to convert bytes to placeholders.

    Args:
        payload: Object to process (dict or list)
        buffer_map: Map to store buffer info
        jsonable_string_map: Map for JSON-able strings
        path: Current path in the object tree
    """
    if payload is None or not isinstance(payload, (dict, list)):
        return

    if isinstance(payload, list):
        for i, item in enumerate(payload):
            current_path = [*path, str(i)]
            if isinstance(item, bytes):
                path_str = ".".join(current_path)
                if is_utf8(item):
                    buffer_map[path_str] = {"value": item.decode("utf-8"), "encoding": "utf8"}
                else:
                    buffer_map[path_str] = {"value": base64.b64encode(item).decode("ascii"), "encoding": "base64"}
                payload[i] = BUFFER_PLACEHOLDER
            elif isinstance(item, (dict, list)):
                _process_payload_for_serialization(item, buffer_map, jsonable_string_map, current_path)
        return

    # Handle dict
    for key in list(payload.keys()):
        current_path = [*path, key]
        path_str = ".".join(current_path)
        value = payload[key]

        if isinstance(value, bytes):
            if is_utf8(value):
                buffer_map[path_str] = {"value": value.decode("utf-8"), "encoding": "utf8"}
            else:
                buffer_map[path_str] = {"value": base64.b64encode(value).decode("ascii"), "encoding": "base64"}
            payload[key] = BUFFER_PLACEHOLDER
        elif isinstance(value, (dict, list)):
            _process_payload_for_serialization(value, buffer_map, jsonable_string_map, current_path)


def deserialize_grpc_payload(
    readable_payload: Any,
    buffer_map: dict[str, dict[str, str]],
    jsonable_string_map: dict[str, str],
) -> Any:
    """
    Convert a serialized payload back to its original format with bytes restored.

    Args:
        readable_payload: Payload from serialize_grpc_payload
        buffer_map: Buffer map from serialize_grpc_payload
        jsonable_string_map: String map from serialize_grpc_payload

    Returns:
        Payload with bytes fields restored
    """
    if readable_payload is None:
        return None

    result = copy.deepcopy(readable_payload)
    _restore_payload_from_serialization(result, buffer_map, jsonable_string_map, [])
    return result


def _restore_payload_from_serialization(
    payload: Any,
    buffer_map: dict[str, dict[str, str]],
    jsonable_string_map: dict[str, str],
    path: list[str],
) -> None:
    """
    Recursively restore bytes in a payload.

    Args:
        payload: Object to process
        buffer_map: Buffer map with stored values
        jsonable_string_map: String map
        path: Current path in the object tree
    """
    if payload is None or not isinstance(payload, (dict, list)):
        return

    if isinstance(payload, list):
        for i, item in enumerate(payload):
            current_path = [*path, str(i)]
            path_str = ".".join(current_path)
            if item == BUFFER_PLACEHOLDER and path_str in buffer_map:
                buffer_info = buffer_map[path_str]
                if buffer_info["encoding"] == "utf8":
                    payload[i] = buffer_info["value"].encode("utf-8")
                else:
                    payload[i] = base64.b64decode(buffer_info["value"])
            elif isinstance(item, (dict, list)):
                _restore_payload_from_serialization(item, buffer_map, jsonable_string_map, current_path)
        return

    # Handle dict
    for key in list(payload.keys()):
        current_path = [*path, key]
        path_str = ".".join(current_path)
        value = payload[key]

        if value == BUFFER_PLACEHOLDER and path_str in buffer_map:
            buffer_info = buffer_map[path_str]
            if buffer_info["encoding"] == "utf8":
                payload[key] = buffer_info["value"].encode("utf-8")
            else:
                payload[key] = base64.b64decode(buffer_info["value"])
        elif isinstance(value, (dict, list)):
            _restore_payload_from_serialization(value, buffer_map, jsonable_string_map, current_path)
