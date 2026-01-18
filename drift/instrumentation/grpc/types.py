"""Type definitions for gRPC instrumentation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union


@dataclass
class BufferMetadata:
    """Metadata for handling binary buffers in gRPC payloads."""

    buffer_map: dict[str, dict[str, str]] = field(default_factory=dict)
    """Map of field paths to buffer info (value + encoding)."""

    jsonable_string_map: dict[str, str] = field(default_factory=dict)
    """Map of field paths to JSON-able strings."""


@dataclass
class GrpcClientInputValue:
    """Input value structure for gRPC client requests."""

    method: str
    """gRPC method name."""

    service: str
    """gRPC service name (package.ServiceName)."""

    body: Any
    """Request body (protobuf message as dict)."""

    metadata: dict[str, list[str | dict[str, str]]]
    """gRPC metadata (headers)."""

    input_meta: BufferMetadata | None = None
    """Buffer metadata for binary fields."""


@dataclass
class GrpcStatus:
    """gRPC response status."""

    code: int
    """gRPC status code."""

    details: str
    """Status details/message."""

    metadata: dict[str, list[str | dict[str, str]]] = field(default_factory=dict)
    """Trailing metadata."""


@dataclass
class GrpcOutputValue:
    """Output value structure for successful gRPC responses."""

    body: Any
    """Response body (protobuf message as dict)."""

    metadata: dict[str, list[str | dict[str, str]]]
    """Initial response metadata."""

    status: GrpcStatus
    """gRPC status."""

    buffer_map: dict[str, dict[str, str]] = field(default_factory=dict)
    """Buffer metadata for binary fields in response."""

    jsonable_string_map: dict[str, str] = field(default_factory=dict)
    """Map of field paths to JSON-able strings."""


@dataclass
class GrpcErrorOutput:
    """Output value structure for gRPC errors."""

    error: dict[str, str]
    """Error info (message, name, stack)."""

    status: GrpcStatus
    """gRPC status."""

    metadata: dict[str, list[str | dict[str, str]]] = field(default_factory=dict)
    """Response metadata."""


# Type alias for readable metadata values
ReadableMetadataValue = Union[str, dict[str, str]]
ReadableMetadata = dict[str, list[ReadableMetadataValue]]
