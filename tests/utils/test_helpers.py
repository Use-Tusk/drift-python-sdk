"""Common test utilities for Drift Python SDK tests."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from drift.core.types import CleanSpanData


def wait_for_spans(timeout_seconds: float = 0.5) -> None:
    """
    Wait for spans to be processed and exported.

    Args:
        timeout_seconds: Time to wait in seconds (default: 0.5)
    """
    time.sleep(timeout_seconds)


def create_test_span(
    trace_id: str | None = None,
    span_id: str | None = None,
    parent_span_id: str = "",
    name: str = "test-span",
    package_name: str = "test",
    instrumentation_name: str = "TestInstrumentation",
    submodule_name: str = "test",
    input_value: dict[str, Any] | None = None,
    output_value: dict[str, Any] | None = None,
) -> CleanSpanData:
    """
    Create a minimal test span for unit tests.

    Args:
        trace_id: 32-character hex string (default: 'a' * 32)
        span_id: 16-character hex string (default: 'b' * 16)
        parent_span_id: Parent span ID (default: empty string)
        name: Span name
        package_name: Package name
        instrumentation_name: Instrumentation name
        submodule_name: Submodule name
        input_value: Input data dict
        output_value: Output data dict

    Returns:
        CleanSpanData instance for testing
    """
    from drift.core.types import (
        CleanSpanData,
        Duration,
        PackageType,
        SpanKind,
        SpanStatus,
        StatusCode,
        Timestamp,
    )

    return CleanSpanData(
        trace_id=trace_id or "a" * 32,
        span_id=span_id or "b" * 16,
        parent_span_id=parent_span_id,
        name=name,
        package_name=package_name,
        instrumentation_name=instrumentation_name,
        submodule_name=submodule_name,
        package_type=PackageType.HTTP,
        kind=SpanKind.SERVER,
        input_value=input_value or {"method": "GET"},
        output_value=output_value or {"status": 200},
        status=SpanStatus(code=StatusCode.OK),
        timestamp=Timestamp(seconds=1700000000, nanos=0),
        duration=Duration(seconds=0, nanos=1000000),
    )
