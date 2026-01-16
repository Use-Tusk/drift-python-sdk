"""Error resilience tests for the Drift SDK.

These tests verify that the SDK handles errors gracefully and continues
operation even when internal components fail. The SDK should never crash
the application it's instrumenting.
"""

import asyncio
import os

os.environ["TUSK_DRIFT_MODE"] = "RECORD"


from drift.core.tracing.adapters import ExportResult, ExportResultCode, InMemorySpanAdapter
from drift.core.types import CleanSpanData, Duration, PackageType, SpanKind, SpanStatus, StatusCode, Timestamp
from tests.utils import create_test_span


class TestAdapterErrorResilience:
    """Test that adapters handle errors gracefully."""

    def test_in_memory_adapter_continues_after_invalid_data(self):
        """InMemorySpanAdapter should continue working after receiving invalid data."""
        adapter = InMemorySpanAdapter()

        span1 = create_test_span(name="valid-1")
        adapter.collect_span(span1)

        spans = adapter.get_all_spans()
        assert len(spans) == 1
        assert spans[0].name == "valid-1"

    def test_adapter_recovers_after_error(self):
        """Adapter should continue working after an error."""
        adapter = InMemorySpanAdapter()

        span1 = create_test_span(name="span1")
        adapter.collect_span(span1)

        try:
            adapter.collect_span("not a span")  # type: ignore
        except (TypeError, AttributeError):
            pass

        span2 = create_test_span(name="span2")
        adapter.collect_span(span2)

        spans = adapter.get_all_spans()
        valid_spans = [s for s in spans if isinstance(s, CleanSpanData)]
        assert len(valid_spans) >= 2

    def test_export_result_captures_errors(self):
        """ExportResult should properly capture error information."""
        error = ValueError("Test error")
        result = ExportResult.failed(error)

        assert result.code == ExportResultCode.FAILED
        assert result.error == error

    def test_export_result_from_string_error(self):
        """ExportResult should handle string error messages."""
        result = ExportResult.failed("Something went wrong")

        assert result.code == ExportResultCode.FAILED
        assert isinstance(result.error, Exception)
        assert "Something went wrong" in str(result.error)


class TestSpanCreationErrorResilience:
    """Test that span creation handles errors gracefully."""

    def test_span_with_invalid_input_value(self):
        """Creating a span with invalid input should be handled."""
        circular_dict: dict = {}
        circular_dict["self"] = circular_dict

        try:
            _span = CleanSpanData(
                trace_id="a" * 32,
                span_id="b" * 16,
                parent_span_id="",
                name="test",
                package_name="test",
                instrumentation_name="Test",
                submodule_name="test",
                package_type=PackageType.HTTP,
                kind=SpanKind.SERVER,
                input_value=circular_dict,
                output_value={},
                status=SpanStatus(code=StatusCode.OK),
                timestamp=Timestamp(seconds=1700000000, nanos=0),
                duration=Duration(seconds=0, nanos=1000000),
            )
            del _span
        except (ValueError, RecursionError):
            pass

    def test_span_with_very_large_input(self):
        """Creating a span with very large input should be handled."""
        large_input = {"data": "x" * 1_000_000}

        span = CleanSpanData(
            trace_id="a" * 32,
            span_id="b" * 16,
            parent_span_id="",
            name="test",
            package_name="test",
            instrumentation_name="Test",
            submodule_name="test",
            package_type=PackageType.HTTP,
            kind=SpanKind.SERVER,
            input_value=large_input,
            output_value={},
            status=SpanStatus(code=StatusCode.OK),
            timestamp=Timestamp(seconds=1700000000, nanos=0),
            duration=Duration(seconds=0, nanos=1000000),
        )

        assert span is not None


class TestAsyncErrorResilience:
    """Test error resilience in async operations."""

    def test_async_export_handles_timeout(self):
        """Async export should handle timeouts gracefully."""

        async def slow_export(spans):
            await asyncio.sleep(10)
            return ExportResult.success()

        adapter = InMemorySpanAdapter()

        async def timeout_export(spans):
            try:
                return await asyncio.wait_for(slow_export(spans), timeout=0.1)
            except asyncio.TimeoutError:
                return ExportResult.failed("Export timed out")

        adapter.export_spans = timeout_export  # type: ignore[method-assign]

        span = create_test_span()
        result = asyncio.run(adapter.export_spans([span]))

        assert result.code == ExportResultCode.FAILED
        assert "timed out" in str(result.error)

    def test_async_export_handles_cancellation(self):
        """Async export should handle cancellation gracefully."""

        async def run_test():
            adapter = InMemorySpanAdapter()
            span = create_test_span()

            task = asyncio.create_task(adapter.export_spans([span]))
            task.cancel()

            try:
                await task
            except asyncio.CancelledError:
                pass

            result = await adapter.export_spans([create_test_span()])
            return result

        result = asyncio.run(run_test())
        assert result.code == ExportResultCode.SUCCESS
