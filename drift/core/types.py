"""Core types and data structures for Drift Python SDK."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from .json_schema_helper import JsonSchema

TD_INSTRUMENTATION_LIBRARY_NAME = "tusk-drift-sdk"


class PackageType(Enum):
    """
    Package type classification enum (language agnostic).
    Maps to protobuf enum tusk.drift.core.v1.PackageType.
    """

    UNSPECIFIED = 0
    HTTP = 1
    GRAPHQL = 2
    GRPC = 3
    PG = 4
    MYSQL = 5
    MONGODB = 6
    REDIS = 7
    KAFKA = 8
    RABBITMQ = 9
    FIRESTORE = 10
    PRISMA = 11


class SpanKind(Enum):
    """
    OpenTelemetry-compatible span kinds.
    Maps to protobuf enum tusk.drift.core.v1.SpanKind.
    """

    UNSPECIFIED = 0
    INTERNAL = 1
    SERVER = 2
    CLIENT = 3
    PRODUCER = 4
    CONSUMER = 5


class StatusCode(Enum):
    """
    Span status code.
    Maps to protobuf enum tusk.drift.core.v1.StatusCode.
    """

    UNSPECIFIED = 0
    OK = 1
    ERROR = 2


@dataclass
class SpanStatus:
    """Span completion status."""

    code: StatusCode = StatusCode.UNSPECIFIED
    message: str = ""


@dataclass
class Duration:
    """Duration in seconds and nanoseconds."""

    seconds: int = 0
    nanos: int = 0


@dataclass
class Timestamp:
    """Timestamp in seconds and nanoseconds since epoch."""

    seconds: int = 0
    nanos: int = 0


@dataclass
class TransformAction:
    """Record of a transform action applied to a span."""

    type: Literal["redact", "mask", "replace", "drop"]
    field: str
    reason: str
    description: str | None = None


@dataclass
class TransformMetadata:
    """Metadata about transforms applied to a span."""

    transformed: bool = False
    actions: list[TransformAction] = field(default_factory=list)


@dataclass
class CleanSpanData:
    """
    Clean span data structure matching Node.js SDK.
    This is the internal representation used throughout the SDK.
    """

    # Identity
    trace_id: str
    span_id: str
    parent_span_id: str
    name: str

    # Classification
    package_name: str
    instrumentation_name: str
    submodule_name: str
    package_type: PackageType | None = None
    environment: str | None = None
    kind: SpanKind = SpanKind.INTERNAL

    # Data capture
    input_value: Any = None
    output_value: Any = None
    input_schema: JsonSchema = field(default_factory=JsonSchema)
    output_schema: JsonSchema = field(default_factory=JsonSchema)

    # Hashing (for deduplication/matching)
    input_schema_hash: str = ""
    output_schema_hash: str = ""
    input_value_hash: str = ""
    output_value_hash: str = ""
    input_value_proto_struct_bytes: bytes | None = None
    output_value_proto_struct_bytes: bytes | None = None
    proto_span_bytes: bytes | None = None

    # Status
    status: SpanStatus = field(default_factory=lambda: SpanStatus())

    # Flags
    is_pre_app_start: bool = False
    is_root_span: bool = False

    # Timing
    timestamp: Timestamp = field(default_factory=lambda: Timestamp())
    duration: Duration = field(default_factory=lambda: Duration())

    # Metadata
    metadata: dict[str, Any] | None = None
    transform_metadata: TransformMetadata | None = None

    # SDK-specific
    is_used: bool | None = None
    stack_trace: str | None = None

    def to_proto(self) -> Any:
        """Serialize this span to a tusk.drift.core.v1.Span message."""

        from .span_serialization import clean_span_to_proto

        return clean_span_to_proto(self)


@dataclass
class MockRequestData:
    """
    Data structure for requesting mocks from the CLI in replay mode.
    """

    trace_id: str
    span_id: str
    name: str
    package_name: str
    instrumentation_name: str
    submodule_name: str
    input_value: Any
    kind: SpanKind
    package_type: PackageType | None = None
    stack_trace: str | None = None


# Context variables for propagating state through async/sync execution
# Python's contextvars is the equivalent of OpenTelemetry's Context API
replay_trace_id_context: ContextVar[str | None] = ContextVar("replay_trace_id", default=None)
span_kind_context: ContextVar[SpanKind | None] = ContextVar("span_kind", default=None)
stop_recording_child_spans_context: ContextVar[bool] = ContextVar("stop_recording_child_spans", default=False)
calling_library_context: ContextVar[str | None] = ContextVar("calling_library", default=None)

# Trace context propagation (matches OpenTelemetry behavior)
# These allow child spans to inherit trace_id and set parent_span_id correctly
current_trace_id_context: ContextVar[str | None] = ContextVar("current_trace_id", default=None)
current_span_id_context: ContextVar[str | None] = ContextVar("current_span_id", default=None)


class TdSpanAttributes(str, Enum):
    """Span attribute keys used by the SDK."""

    NAME = "td.name"
    PACKAGE_TYPE = "td.packageType"
    PACKAGE_NAME = "td.packageName"
    INSTRUMENTATION_NAME = "td.instrumentationName"
    SUBMODULE_NAME = "td.submodule"
    IS_PRE_APP_START = "td.isPreAppStart"
    INPUT_VALUE = "td.inputValue"
    OUTPUT_VALUE = "td.outputValue"
    INPUT_SCHEMA_MERGES = "td.inputSchemaMerges"
    OUTPUT_SCHEMA_MERGES = "td.outputSchemaMerges"
    METADATA = "td.metadata"
    TRANSFORM_METADATA = "td.transformMetadata"


class TuskDriftMode(str, Enum):
    RECORD = "RECORD"
    REPLAY = "REPLAY"
    DISABLED = "DISABLED"
