"""Centralized mock request utilities matching Node SDK's mockResponseUtils.ts.

This module provides utilities for finding mock responses in REPLAY mode,
centralizing schema generation logic that was previously duplicated across
instrumentations.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .communication.types import MockResponseOutput
    from .drift_sdk import TuskDrift
    from .json_schema_helper import SchemaMerges
    from .types import CleanSpanData

from .json_schema_helper import JsonSchemaHelper
from .types import (
    Duration,
    PackageType,
    SpanKind,
    SpanStatus,
    StatusCode,
    Timestamp,
    replay_trace_id_context,
)

logger = logging.getLogger(__name__)


def convert_mock_request_to_clean_span(
    *,
    trace_id: str,
    span_id: str,
    name: str,
    package_name: str,
    package_type: PackageType,
    instrumentation_name: str,
    submodule_name: str,
    input_value: dict[str, Any],
    kind: SpanKind,
    input_schema_merges: SchemaMerges | None = None,
    stack_trace: str | None = None,
    is_pre_app_start: bool = False,
) -> CleanSpanData:
    """Convert mock request data to CleanSpanData with schema generation.

    This centralizes the schema generation logic that was previously duplicated
    across instrumentations. Matches Node SDK's convertMockRequestDataToCleanSpanData.

    Args:
        trace_id: Trace ID for the span
        span_id: Span ID
        name: Span name (e.g., "GET /api/users")
        package_name: Package name (e.g., "http", "https", "redis")
        package_type: Package type (HTTP, DATABASE, etc.)
        instrumentation_name: Instrumentation name
        submodule_name: Submodule name (e.g., "GET", "SET")
        input_value: Input value dictionary
        kind: Span kind (typically CLIENT for outbound requests)
        input_schema_merges: Schema merge hints for input
        stack_trace: Optional stack trace
        is_pre_app_start: Whether this span occurred before app start

    Returns:
        CleanSpanData with generated schemas and hashes
    """
    from .types import CleanSpanData

    # Generate schema and hashes from input value and merges
    input_result = JsonSchemaHelper.generate_schema_and_hash(input_value, input_schema_merges)

    # Get current timestamp
    timestamp_ms = time.time() * 1000
    timestamp_seconds = int(timestamp_ms // 1000)
    timestamp_nanos = int((timestamp_ms % 1000) * 1_000_000)

    return CleanSpanData(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id="",
        name=name,
        package_name=package_name,
        package_type=package_type,
        instrumentation_name=instrumentation_name,
        submodule_name=submodule_name,
        input_value=input_value,
        output_value=None,
        input_schema=input_result.schema,
        input_schema_hash=input_result.decoded_schema_hash,
        input_value_hash=input_result.decoded_value_hash,
        output_schema=None,  # type: ignore[arg-type] - Must be None to avoid betterproto serialization issues
        output_schema_hash="",
        output_value_hash="",
        kind=kind,
        status=SpanStatus(code=StatusCode.OK, message="OK"),
        timestamp=Timestamp(seconds=timestamp_seconds, nanos=timestamp_nanos),
        duration=Duration(seconds=0, nanos=0),
        is_root_span=False,
        is_pre_app_start=is_pre_app_start,
        stack_trace=stack_trace or "",
    )


def find_mock_response_sync(
    *,
    sdk: TuskDrift,
    trace_id: str,
    span_id: str,
    name: str,
    package_name: str,
    package_type: PackageType,
    instrumentation_name: str,
    submodule_name: str,
    input_value: dict[str, Any],
    kind: SpanKind = SpanKind.CLIENT,
    input_schema_merges: SchemaMerges | None = None,
    stack_trace: str | None = None,
    is_pre_app_start: bool = False,
) -> MockResponseOutput | None:
    """Find mock response for outbound request in REPLAY mode (synchronous).

    Centralizes the common logic of:
    1. Getting the replay trace ID from context
    2. Generating schemas from input value and merges
    3. Creating CleanSpanData for mock request
    4. Making the mock request to CLI
    5. Handling the response and error cases

    Matches Node SDK's findMockResponseSync.

    Args:
        sdk: TuskDrift instance
        trace_id: Trace ID for the outbound span
        span_id: Span ID for the outbound span
        name: Span name (e.g., "GET /api/users")
        package_name: Package name (e.g., "http", "https")
        package_type: Package type (typically HTTP)
        instrumentation_name: Instrumentation name
        submodule_name: Submodule name (e.g., "GET", "POST")
        input_value: Input value dictionary
        kind: Span kind (default CLIENT)
        input_schema_merges: Schema merge hints for input
        stack_trace: Optional stack trace
        is_pre_app_start: Whether this span occurred before app start

    Returns:
        MockResponseOutput if found, None otherwise
    """
    try:
        # Get replay trace ID from context
        replay_trace_id = replay_trace_id_context.get()

        # Convert to CleanSpanData with schema generation
        outbound_span = convert_mock_request_to_clean_span(
            trace_id=trace_id,
            span_id=span_id,
            name=name,
            package_name=package_name,
            package_type=package_type,
            instrumentation_name=instrumentation_name,
            submodule_name=submodule_name,
            input_value=input_value,
            kind=kind,
            input_schema_merges=input_schema_merges,
            stack_trace=stack_trace,
            is_pre_app_start=is_pre_app_start,
        )

        logger.debug(f"Finding mock for {trace_id} with replay trace ID: {replay_trace_id}")

        # Request mock from CLI
        from .communication.types import MockRequestInput

        mock_request = MockRequestInput(
            test_id=replay_trace_id or "",
            outbound_span=outbound_span,
        )

        mock_response = sdk.request_mock_sync(mock_request)

        if not mock_response or not mock_response.found:
            logger.debug(f"No matching mock found for {trace_id} with input value: {input_value}")
            return None

        logger.debug(f"Found mock response for {trace_id}")

        # Update time travel to match mock's recorded timestamp
        _update_time_travel(mock_response, replay_trace_id)

        return mock_response

    except Exception as e:
        logger.error(f"Error finding mock response for {trace_id}: {e}")
        return None


async def find_mock_response_async(
    *,
    sdk: TuskDrift,
    trace_id: str,
    span_id: str,
    name: str,
    package_name: str,
    package_type: PackageType,
    instrumentation_name: str,
    submodule_name: str,
    input_value: dict[str, Any],
    kind: SpanKind = SpanKind.CLIENT,
    input_schema_merges: SchemaMerges | None = None,
    stack_trace: str | None = None,
    is_pre_app_start: bool = False,
) -> MockResponseOutput | None:
    """Find mock response for outbound request in REPLAY mode (asynchronous).

    Async version of find_mock_response_sync. See that function for details.

    Matches Node SDK's findMockResponseAsync.
    """
    try:
        # Get replay trace ID from context
        replay_trace_id = replay_trace_id_context.get()

        # Convert to CleanSpanData with schema generation
        outbound_span = convert_mock_request_to_clean_span(
            trace_id=trace_id,
            span_id=span_id,
            name=name,
            package_name=package_name,
            package_type=package_type,
            instrumentation_name=instrumentation_name,
            submodule_name=submodule_name,
            input_value=input_value,
            kind=kind,
            input_schema_merges=input_schema_merges,
            stack_trace=stack_trace,
            is_pre_app_start=is_pre_app_start,
        )

        logger.debug(f"Finding mock for {trace_id} with replay trace ID: {replay_trace_id}")

        # Request mock from CLI
        from .communication.types import MockRequestInput

        mock_request = MockRequestInput(
            test_id=replay_trace_id or "",
            outbound_span=outbound_span,
        )

        mock_response = await sdk.request_mock_async(mock_request)

        if not mock_response or not mock_response.found:
            logger.debug(f"No matching mock found for {trace_id} with input value: {input_value}")
            return None

        logger.debug(f"Found mock response for {trace_id}")

        # Update time travel to match mock's recorded timestamp
        _update_time_travel(mock_response, replay_trace_id)

        return mock_response

    except Exception as e:
        logger.error(f"Error finding mock response for {trace_id}: {e}")
        return None


def _update_time_travel(
    mock_response: MockResponseOutput,
    replay_trace_id: str | None,
) -> None:
    """Update time travel to match the mock response's recorded timestamp.

    Sets the clock to the timestamp from each mock response, keeping time
    in sync with when the original requests were made during recording.

    Args:
        mock_response: The mock response containing the timestamp
        replay_trace_id: The replay trace ID for this session
    """
    if not replay_trace_id:
        return

    try:
        from drift.instrumentation.datetime import start_time_travel

        response_data = mock_response.response

        # Timestamp is extracted from MockInteraction and added to response by communicator
        timestamp = response_data.get("timestamp") if isinstance(response_data, dict) else None

        if timestamp:
            logger.debug(f"Setting time travel to timestamp: {timestamp}")
            start_time_travel(timestamp, trace_id=replay_trace_id)
        else:
            logger.debug("No timestamp in mock response, skipping time travel")
    except Exception as e:
        logger.debug(f"Failed to start time travel: {e}")
