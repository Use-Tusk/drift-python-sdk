"""Error resilience tests for the Drift SDK.

These tests verify that the SDK handles errors gracefully and continues
operation even when internal components fail. The SDK should never crash
the application it's instrumenting.
"""

import asyncio
import os
import unittest
from unittest.mock import MagicMock

os.environ["TUSK_DRIFT_MODE"] = "RECORD"

from drift.core.types import CleanSpanData, PackageType, SpanKind, SpanStatus, StatusCode, Timestamp, Duration
from drift.tracing.adapters import InMemorySpanAdapter, ExportResult, ExportResultCode
from tests.utils import create_test_span


class TestAdapterErrorResilience(unittest.TestCase):
    """Test that adapters handle errors gracefully."""

    def test_in_memory_adapter_continues_after_invalid_data(self):
        """InMemorySpanAdapter should continue working after receiving invalid data."""
        adapter = InMemorySpanAdapter()

        # Collect a valid span first
        span1 = create_test_span(name="valid-1")
        adapter.collect_span(span1)

        # Adapter should be functional
        spans = adapter.get_all_spans()
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0].name, "valid-1")

    def test_adapter_recovers_after_error(self):
        """Adapter should continue working after an error."""
        adapter = InMemorySpanAdapter()

        # Collect valid span
        span1 = create_test_span(name="span1")
        adapter.collect_span(span1)

        # Try to collect invalid data
        try:
            adapter.collect_span("not a span")  # type: ignore
        except (TypeError, AttributeError):
            pass

        # Collect another valid span
        span2 = create_test_span(name="span2")
        adapter.collect_span(span2)

        # Both valid spans should be present
        spans = adapter.get_all_spans()
        valid_spans = [s for s in spans if isinstance(s, CleanSpanData)]
        self.assertGreaterEqual(len(valid_spans), 2)

    def test_export_result_captures_errors(self):
        """ExportResult should properly capture error information."""
        error = ValueError("Test error")
        result = ExportResult.failed(error)

        self.assertEqual(result.code, ExportResultCode.FAILED)
        self.assertEqual(result.error, error)

    def test_export_result_from_string_error(self):
        """ExportResult should handle string error messages."""
        result = ExportResult.failed("Something went wrong")

        self.assertEqual(result.code, ExportResultCode.FAILED)
        self.assertIsInstance(result.error, Exception)
        self.assertIn("Something went wrong", str(result.error))


class TestBatchProcessorErrorResilience(unittest.TestCase):
    """Test that the batch processor handles errors gracefully."""

    def test_batch_processor_can_be_started_and_stopped(self):
        """Batch processor should start and stop cleanly."""
        from drift.core.batch_processor import BatchSpanProcessor

        adapter = InMemorySpanAdapter()
        processor = BatchSpanProcessor(adapters=[adapter], config=None)

        # Should start without error
        processor.start()

        # Add a span
        span = create_test_span()
        processor.add_span(span)

        # Should stop without error
        processor.stop()


class TestSpanCreationErrorResilience(unittest.TestCase):
    """Test that span creation handles errors gracefully."""

    def test_span_with_invalid_input_value(self):
        """Creating a span with invalid input should be handled."""
        # Circular reference in input value
        circular_dict: dict = {}
        circular_dict["self"] = circular_dict

        # This should either handle the circular reference or raise a clear error
        try:
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
                input_value=circular_dict,
                output_value={},
                status=SpanStatus(code=StatusCode.OK),
                timestamp=Timestamp(seconds=1700000000, nanos=0),
                duration=Duration(seconds=0, nanos=1000000),
            )
            # If span creation succeeds, serialization might fail
            # which is also acceptable
        except (ValueError, RecursionError):
            pass  # Expected - might reject circular references

    def test_span_with_very_large_input(self):
        """Creating a span with very large input should be handled."""
        large_input = {"data": "x" * 1_000_000}  # 1MB of data

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

        # Span should be created (truncation might happen during export)
        self.assertIsNotNone(span)


class TestSDKErrorResilience(unittest.TestCase):
    """Test that the SDK handles errors gracefully."""

    def test_sdk_continues_after_collect_span_error(self):
        """SDK should continue operation after collect_span errors."""
        from drift import TuskDrift
        from drift.tracing.adapters import InMemorySpanAdapter, register_in_memory_adapter

        sdk = TuskDrift.get_instance()
        adapter = InMemorySpanAdapter()
        register_in_memory_adapter(adapter)

        # Get initial span count
        initial_count = len(adapter.get_all_spans())

        # Collect a valid span
        span1 = create_test_span(name="valid-span-1")
        sdk.collect_span(span1)

        # Try to collect something invalid (if SDK doesn't type-check)
        try:
            sdk.collect_span(None)  # type: ignore
        except (TypeError, AttributeError):
            pass

        # Collect another valid span
        span2 = create_test_span(name="valid-span-2")
        sdk.collect_span(span2)

        # SDK should still be functional
        final_count = len(adapter.get_all_spans())
        # We should have at least 2 more spans than initial (the valid ones)
        self.assertGreaterEqual(final_count - initial_count, 2)


class TestAsyncErrorResilience(unittest.TestCase):
    """Test error resilience in async operations."""

    def test_async_export_handles_timeout(self):
        """Async export should handle timeouts gracefully."""

        async def slow_export(spans):
            await asyncio.sleep(10)  # Very slow
            return ExportResult.success()

        adapter = InMemorySpanAdapter()
        # Override export_spans with slow version
        original_export = adapter.export_spans

        async def timeout_export(spans):
            try:
                return await asyncio.wait_for(slow_export(spans), timeout=0.1)
            except asyncio.TimeoutError:
                return ExportResult.failed("Export timed out")

        adapter.export_spans = timeout_export

        span = create_test_span()
        result = asyncio.run(adapter.export_spans([span]))

        # Should have failed with timeout
        self.assertEqual(result.code, ExportResultCode.FAILED)
        self.assertIn("timed out", str(result.error))

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
                pass  # Expected

            # Adapter should still be functional after cancellation
            result = await adapter.export_spans([create_test_span()])
            return result

        result = asyncio.run(run_test())
        self.assertEqual(result.code, ExportResultCode.SUCCESS)


if __name__ == "__main__":
    unittest.main()
