"""Type definitions for CLI communication protocol.

This module defines the message types and data structures used for
communication between the Drift SDK and the Tusk CLI.

The protocol uses:
- 4-byte big-endian length prefix for message framing
- Protocol Buffers for message serialization
- Bidirectional communication over Unix sockets or TCP
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class SDKMessageType(IntEnum):
    """Message types sent from SDK to CLI."""

    UNSPECIFIED = 0
    SDK_CONNECT = 1          # Initial handshake
    MOCK_REQUEST = 2         # Request mocked response for outbound span
    ENV_VAR_REQUEST = 3      # Request environment variables
    INBOUND_SPAN = 4         # Send inbound span for replay
    ALERT = 5                # Fire-and-forget alerts


class CLIMessageType(IntEnum):
    """Message types sent from CLI to SDK."""

    UNSPECIFIED = 0
    CONNECT_RESPONSE = 1     # Response to SDK_CONNECT
    MOCK_RESPONSE = 2        # Response with mocked data
    ENV_VAR_RESPONSE = 3     # Environment variable values
    INBOUND_SPAN_RESPONSE = 4  # Acknowledgment of inbound span


# Alias for convenience
MessageType = SDKMessageType | CLIMessageType


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


@dataclass
class GetMockRequest:
    """Request for mocked response data.

    Sent when an instrumented outbound call is made in REPLAY mode.
    The CLI matches this against recorded spans and returns the
    appropriate mocked response.
    """

    request_id: str
    """Unique identifier for this request."""

    test_id: str
    """Identifier of the test being run."""

    outbound_span: dict[str, Any]
    """The outbound span data to match against recorded spans."""

    stack_trace: list[str] | None = None
    """Optional stack trace for debugging."""

    operation: str | None = None
    """Operation type (e.g., "http", "database")."""

    tags: dict[str, str] = field(default_factory=dict)
    """Additional tags for span matching."""

    requested_at: int | None = None
    """Timestamp when request was made (milliseconds since epoch)."""


@dataclass
class GetMockResponse:
    """Response containing mocked data.

    Returned by CLI when a matching recorded span is found.
    """

    request_id: str
    """Matches the request_id from GetMockRequest."""

    found: bool
    """Whether a matching span was found."""

    response_data: dict[str, Any] | None = None
    """The mocked response data to return."""

    matched_span_id: str | None = None
    """ID of the span that was matched."""

    matched_at: int | None = None
    """Timestamp when match was found (milliseconds since epoch)."""

    error: str | None = None
    """Error message if matching failed."""

    metadata: dict[str, Any] | None = None
    """Additional metadata about the match."""


@dataclass
class EnvVarRequest:
    """Request for environment variables.

    Used to retrieve environment variable values that were recorded
    during the original test execution.
    """

    request_id: str
    """Unique identifier for this request."""

    trace_id: str
    """Trace ID to get environment variables for."""

    var_names: list[str]
    """Names of environment variables to retrieve."""


@dataclass
class EnvVarResponse:
    """Response containing environment variable values."""

    request_id: str
    """Matches the request_id from EnvVarRequest."""

    values: dict[str, str]
    """Map of variable name to value."""

    error: str | None = None
    """Error message if retrieval failed."""


@dataclass
class SendInboundSpanRequest:
    """Request to send an inbound span for replay validation."""

    request_id: str
    """Unique identifier for this request."""

    test_id: str
    """Identifier of the test being run."""

    inbound_span: dict[str, Any]
    """The inbound span data."""


@dataclass
class SendInboundSpanResponse:
    """Acknowledgment of inbound span receipt."""

    request_id: str
    """Matches the request_id from SendInboundSpanRequest."""

    success: bool
    """Whether the span was accepted."""

    error: str | None = None
    """Error message if validation failed."""


@dataclass
class AlertRequest:
    """Fire-and-forget alert message.

    Used to notify CLI of non-critical issues like version mismatches
    or unpatched dependencies.
    """

    alert_type: str
    """Type of alert (e.g., "version_mismatch", "unpatched_dependency")."""

    message: str
    """Human-readable alert message."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Additional alert metadata."""
