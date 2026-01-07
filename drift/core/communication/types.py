"""Protocol message types for SDK-CLI communication.

This module provides SDK-friendly wrappers around the protobuf message types
from tusk-drift-schemas, plus conversion utilities.

The protocol uses:
- 4-byte big-endian length prefix for message framing
- Protocol Buffers for message serialization
- Bidirectional communication over Unix sockets or TCP
"""

from __future__ import annotations

__all__ = [
    # Re-exported protobuf types
    "CliMessage",
    "InstrumentationVersionMismatchAlert",
    "MessageType",
    "SdkMessage",
    "SendAlertRequest",
    "SendInboundSpanForReplayRequest",
    "UnpatchedDependencyAlert",
    # Aliases
    "SDKMessageType",
    "CLIMessageType",
    # Dataclasses
    "ConnectRequest",
    "ConnectResponse",
    "GetMockRequest",
    "GetMockResponse",
    "MockRequestInput",
    "MockResponseOutput",
    # Utility functions
    "dict_to_span",
    "span_to_proto",
    "extract_response_data",
]

from dataclasses import dataclass, field
from typing import Any

from tusk.drift.core.v1 import (
    CliMessage,
    InstrumentationVersionMismatchAlert,
    MessageType,
    SdkMessage,
    SendAlertRequest,
    SendInboundSpanForReplayRequest,
    UnpatchedDependencyAlert,
)
from tusk.drift.core.v1 import (
    ConnectRequest as ProtoConnectRequest,
)
from tusk.drift.core.v1 import (
    ConnectResponse as ProtoConnectResponse,
)
from tusk.drift.core.v1 import (
    GetMockRequest as ProtoGetMockRequest,
)
from tusk.drift.core.v1 import (
    GetMockResponse as ProtoGetMockResponse,
)
from tusk.drift.core.v1 import (
    Span as ProtoSpan,
)
from tusk.drift.core.v1 import (
    SpanKind as ProtoSpanKind,
)
from tusk.drift.core.v1 import (
    SpanStatus as ProtoSpanStatus,
)
from tusk.drift.core.v1 import (
    StatusCode as ProtoStatusCode,
)

SDKMessageType = MessageType
CLIMessageType = MessageType


def _python_to_value(value: Any) -> Any:
    """Convert Python value to protobuf Value."""
    from betterproto.lib.google.protobuf import ListValue, Value

    if value is None:
        from betterproto.lib.google.protobuf import NullValue

        return Value(null_value=NullValue.NULL_VALUE)  # type: ignore[arg-type]
    elif isinstance(value, bool):
        return Value(bool_value=value)
    elif isinstance(value, (int, float)):
        return Value(number_value=float(value))
    elif isinstance(value, str):
        return Value(string_value=value)
    elif isinstance(value, dict):
        from betterproto.lib.google.protobuf import Struct

        struct = Struct()
        struct.fields = {k: _python_to_value(v) for k, v in value.items()}
        return Value(struct_value=struct)
    elif isinstance(value, (list, tuple)):
        list_val = ListValue(values=[_python_to_value(item) for item in value])
        return Value(list_value=list_val)
    else:
        return Value(string_value=str(value))


def _dict_to_struct(data: dict[str, Any]) -> Any:
    """Convert Python dict to protobuf Struct."""
    from betterproto.lib.google.protobuf import Struct

    struct = Struct()
    if data:
        struct.fields = {k: _python_to_value(v) for k, v in data.items()}
    return struct


@dataclass
class ConnectRequest:
    """Initial connection request from SDK to CLI.

    Sent when the SDK starts in REPLAY mode to establish
    communication with the CLI.
    """

    service_id: str
    """Unique identifier for the service being tested."""

    sdk_version: str
    """Version of the SDK (e.g., "0.1.0")."""

    min_cli_version: str
    """Minimum required CLI version for compatibility."""

    sdk_language: str = "python"
    """Programming language of the SDK."""

    metadata: dict[str, str] = field(default_factory=dict)
    """Additional metadata."""

    def to_proto(self) -> ProtoConnectRequest:
        """Convert to protobuf message."""
        from betterproto.lib.google.protobuf import Struct

        # Convert metadata dict to protobuf Struct
        metadata_struct = Struct()
        if self.metadata:
            metadata_struct.fields = {k: _python_to_value(v) for k, v in self.metadata.items()}

        return ProtoConnectRequest(
            service_id=self.service_id,
            sdk_version=self.sdk_version,
            min_cli_version=self.min_cli_version,
            metadata=metadata_struct,
        )


@dataclass
class ConnectResponse:
    """Response from CLI to SDK connect request."""

    success: bool
    """Whether the connection was accepted."""

    error: str | None = None
    """Error message if connection was rejected."""

    cli_version: str | None = None
    """Version of the CLI."""

    session_id: str | None = None
    """Unique session identifier for this connection."""

    @classmethod
    def from_proto(cls, proto: ProtoConnectResponse) -> ConnectResponse:
        """Create from protobuf message."""
        # Note: ProtoConnectResponse only has success and error fields
        # cli_version and session_id are SDK-only extensions not in the protobuf schema
        return cls(
            success=proto.success,
            cli_version=None,
            session_id=None,
            error=proto.error or None,
        )


@dataclass
class GetMockRequest:
    """Request for mocked response data.

    Sent when an instrumented outbound call is made in REPLAY mode.
    """

    request_id: str
    """Unique identifier for this request."""

    test_id: str
    """Identifier of the test being run."""

    outbound_span: dict[str, Any]
    """The outbound span data to match against recorded spans."""

    operation: str | None = None
    """Operation type (e.g., "http", "database")."""

    tags: dict[str, str] = field(default_factory=dict)
    """Additional tags for span matching."""

    stack_trace: str | None = None
    """Stack trace for debugging."""

    def to_proto(self) -> ProtoGetMockRequest:
        """Convert to protobuf message."""
        span = dict_to_span(self.outbound_span) if self.outbound_span else ProtoSpan()
        return ProtoGetMockRequest(
            request_id=self.request_id,
            test_id=self.test_id,
            outbound_span=span,
            tags=self.tags,
            stack_trace=self.stack_trace or "",
        )


@dataclass
class GetMockResponse:
    """Response containing mocked data."""

    request_id: str
    """Matches the request_id from GetMockRequest."""

    found: bool
    """Whether a matching span was found."""

    response_data: dict[str, Any] | None = None
    """The mocked response data to return."""

    matched_span_id: str | None = None
    """ID of the span that was matched."""

    error: str | None = None
    """Error message if matching failed."""

    @classmethod
    def from_proto(cls, proto: ProtoGetMockResponse, request_id: str = "") -> GetMockResponse:
        """Create from protobuf message."""
        response_data = None
        if proto.response_data:
            response_data = extract_response_data(proto.response_data)

        return cls(
            request_id=request_id,
            found=proto.found,
            response_data=response_data,
            matched_span_id=proto.matched_span_id or None,
            error=proto.error or None,
        )


@dataclass
class MockRequestInput:
    """Input for mock request (matches Node.js interface)."""

    test_id: str
    outbound_span: Any  # CleanSpanData


@dataclass
class MockResponseOutput:
    """Output from mock request (matches Node.js interface)."""

    found: bool
    response: dict[str, Any] | None = None
    error: str | None = None


def dict_to_span(data: dict[str, Any]) -> ProtoSpan:
    """Convert a dictionary to ProtoSpan.

    Handles both snake_case and camelCase field names.
    """
    # Map span kind to enum value
    kind = data.get("kind", 0)
    if isinstance(kind, str):
        kind_map = {
            "INTERNAL": ProtoSpanKind.INTERNAL,
            "SERVER": ProtoSpanKind.SERVER,
            "CLIENT": ProtoSpanKind.CLIENT,
            "PRODUCER": ProtoSpanKind.PRODUCER,
            "CONSUMER": ProtoSpanKind.CONSUMER,
        }
        kind = kind_map.get(kind.upper(), ProtoSpanKind.UNSPECIFIED)
    elif hasattr(kind, "value"):
        kind = kind.value

    # Get status
    status_data = data.get("status", {})
    if isinstance(status_data, dict):
        status = ProtoSpanStatus(
            code=ProtoStatusCode(status_data.get("code", 0)),
            message=status_data.get("message", ""),
        )
    else:
        # UNSET is not a valid StatusCode in the proto - use UNSPECIFIED (0)
        status = ProtoSpanStatus(code=ProtoStatusCode.UNSPECIFIED)

    return ProtoSpan(
        trace_id=data.get("trace_id", data.get("traceId", "")),
        span_id=data.get("span_id", data.get("spanId", "")),
        parent_span_id=data.get("parent_span_id", data.get("parentSpanId", "")),
        name=data.get("name", ""),
        package_name=data.get("package_name", data.get("packageName", "")),
        instrumentation_name=data.get("instrumentation_name", data.get("instrumentationName", "")),
        submodule_name=data.get("submodule_name", data.get("submoduleName", "")),
        input_value=_dict_to_struct(data.get("input_value", data.get("inputValue", {}))),
        output_value=_dict_to_struct(data.get("output_value", data.get("outputValue", {}))),
        input_schema_hash=data.get("input_schema_hash", data.get("inputSchemaHash", "")),
        output_schema_hash=data.get("output_schema_hash", data.get("outputSchemaHash", "")),
        input_value_hash=data.get("input_value_hash", data.get("inputValueHash", "")),
        output_value_hash=data.get("output_value_hash", data.get("outputValueHash", "")),
        kind=kind,
        status=status,
        is_root_span=data.get("is_root_span", data.get("isRootSpan", False)),
    )


def span_to_proto(span: Any) -> ProtoSpan:
    """Convert a CleanSpanData to ProtoSpan.

    Args:
        span: CleanSpanData object from drift.core.types

    Returns:
        ProtoSpan protobuf message
    """
    # Handle dictionary input
    if isinstance(span, dict):
        return dict_to_span(span)

    # Handle CleanSpanData object
    kind = span.kind
    if hasattr(kind, "value"):
        kind = kind.value

    status = ProtoSpanStatus(
        code=ProtoStatusCode(span.status.code.value if hasattr(span.status.code, "value") else span.status.code),
        message=span.status.message or "",
    )

    return ProtoSpan(
        trace_id=span.trace_id,
        span_id=span.span_id,
        parent_span_id=span.parent_span_id or "",
        name=span.name or "",
        package_name=span.package_name or "",
        instrumentation_name=span.instrumentation_name or "",
        submodule_name=span.submodule_name or "",
        input_value=_dict_to_struct(span.input_value or {}),
        output_value=_dict_to_struct(span.output_value or {}),
        input_schema_hash=span.input_schema_hash or "",
        output_schema_hash=span.output_schema_hash or "",
        input_value_hash=span.input_value_hash or "",
        output_value_hash=span.output_value_hash or "",
        kind=kind,
        status=status,
        is_root_span=span.is_root_span if span.is_root_span is not None else False,
    )


def extract_response_data(struct: Any) -> dict[str, Any]:
    """Extract response data from protobuf Struct.

    The CLI returns response data wrapped in a Struct with a "response" field.
    """
    try:
        # Handle betterproto dict-like struct
        if hasattr(struct, "items"):
            data = dict(struct)
            if "response" in data:
                return data["response"]
            return data

        # Handle struct with fields attribute
        if hasattr(struct, "fields"):
            fields = struct.fields
            if "response" in fields:
                return _value_to_python(fields["response"])
            return {k: _value_to_python(v) for k, v in fields.items()}

        # Direct dict access
        if isinstance(struct, dict):
            if "response" in struct:
                return struct["response"]
            return struct

        return {}
    except Exception:
        return {}


def _value_to_python(value: Any) -> Any:
    """Convert a protobuf Value to Python native type."""
    if hasattr(value, "null_value"):
        return None
    if hasattr(value, "number_value"):
        return value.number_value
    if hasattr(value, "string_value"):
        return value.string_value
    if hasattr(value, "bool_value"):
        return value.bool_value
    if hasattr(value, "struct_value"):
        return {k: _value_to_python(v) for k, v in value.struct_value.fields.items()}
    if hasattr(value, "list_value"):
        return [_value_to_python(v) for v in value.list_value.values]
    return value
