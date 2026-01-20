"""Tests for BatchSpanProcessor event loop management."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from drift.core.batch_processor import BatchSpanProcessor, BatchSpanProcessorConfig
from tests.utils.test_helpers import create_test_span


class MockExporter:
    """Mock exporter that returns configurable adapters.

    Implements the minimal interface needed by BatchSpanProcessor.
    """

    def __init__(self, adapters: list[Any]) -> None:
        self._adapters = adapters

    def get_adapters(self) -> list[Any]:
        return self._adapters


def _create_processor(exporter: MockExporter, config: BatchSpanProcessorConfig) -> BatchSpanProcessor:
    """Create BatchSpanProcessor with mock exporter (type-safe wrapper)."""
    # MockExporter implements get_adapters() which is all BatchSpanProcessor needs
    return BatchSpanProcessor(exporter, config)  # type: ignore[arg-type]


class TestBatchSpanProcessorEventLoop:
    """Test event loop reuse in BatchSpanProcessor."""

    def test_reuses_event_loop_across_exports(self):
        """Verify the same event loop is reused for multiple exports."""
        loops_used = []

        class TrackingAdapter:
            name = "tracking"

            async def export_spans(self, spans):
                loops_used.append(asyncio.get_event_loop())

        adapter = TrackingAdapter()
        exporter = MockExporter([adapter])

        config = BatchSpanProcessorConfig(
            scheduled_delay_seconds=0.1,
            max_export_batch_size=1,
        )
        processor = _create_processor(exporter, config)
        processor.start()

        # Add spans to trigger multiple exports
        processor.add_span(create_test_span(name="span-1"))
        processor.add_span(create_test_span(name="span-2"))

        time.sleep(0.3)  # Allow exports to happen

        processor.stop()

        # All exports should use the same event loop
        assert len(loops_used) >= 2, f"Expected at least 2 exports, got {len(loops_used)}"
        assert all(loop is loops_used[0] for loop in loops_used), "Different event loops were used"

    def test_force_flush_works_after_thread_shutdown(self):
        """Verify force_flush creates temporary loop when thread loop is closed."""
        exported_spans = []

        class CollectingAdapter:
            name = "collecting"

            async def export_spans(self, spans):
                exported_spans.extend(spans)

        adapter = CollectingAdapter()
        exporter = MockExporter([adapter])

        # Long delay so spans won't export before stop()
        config = BatchSpanProcessorConfig(
            scheduled_delay_seconds=10.0,
            max_export_batch_size=100,
        )
        processor = _create_processor(exporter, config)
        processor.start()

        # Add spans
        for i in range(5):
            processor.add_span(create_test_span(name=f"span-{i}"))

        # Stop immediately - force_flush should handle export
        processor.stop()

        assert len(exported_spans) == 5, f"Expected 5 spans, got {len(exported_spans)}"

    def test_event_loop_closed_after_stop(self):
        """Verify the thread's event loop is properly cleaned up after stop."""

        class NoOpAdapter:
            name = "noop"

            async def export_spans(self, spans):
                pass

        adapter = NoOpAdapter()
        exporter = MockExporter([adapter])

        config = BatchSpanProcessorConfig(scheduled_delay_seconds=0.1)
        processor = _create_processor(exporter, config)

        processor.start()
        time.sleep(0.05)  # Let thread start

        # Thread loop should exist while running
        assert processor._thread_loop is not None

        processor.stop()

        # Thread loop should be cleaned up after stop
        assert processor._thread_loop is None

    def test_sync_adapter_does_not_use_event_loop(self):
        """Verify sync adapters work without event loop involvement."""
        exported_spans = []

        class SyncAdapter:
            name = "sync"

            def export_spans(self, spans):
                # Sync method - not a coroutine
                exported_spans.extend(spans)

        adapter = SyncAdapter()
        exporter = MockExporter([adapter])

        config = BatchSpanProcessorConfig(
            scheduled_delay_seconds=0.1,
            max_export_batch_size=5,
        )
        processor = _create_processor(exporter, config)
        processor.start()

        for i in range(3):
            processor.add_span(create_test_span(name=f"span-{i}"))

        time.sleep(0.2)
        processor.stop()

        assert len(exported_spans) == 3

    def test_force_flush_from_different_thread_uses_temporary_loop(self):
        """Verify force_flush doesn't reuse export thread's loop from a different thread.

        This tests the scenario where stop() times out and force_flush runs while
        the export thread is still alive. Using the export thread's loop from
        another thread would cause RuntimeError.
        """
        loops_used = []
        export_thread_loop = None

        class TrackingAdapter:
            name = "tracking"

            async def export_spans(self, spans):
                loops_used.append(asyncio.get_event_loop())

        adapter = TrackingAdapter()

        # Use a slow adapter that blocks the export thread
        class SlowAdapter:
            name = "slow"

            async def export_spans(self, spans):
                nonlocal export_thread_loop
                export_thread_loop = asyncio.get_event_loop()
                # Block for a while to simulate slow export
                await asyncio.sleep(0.5)

        slow_adapter = SlowAdapter()
        exporter_with_slow = MockExporter([slow_adapter, adapter])

        config = BatchSpanProcessorConfig(
            scheduled_delay_seconds=0.05,
            max_export_batch_size=1,
        )
        processor = _create_processor(exporter_with_slow, config)
        processor.start()

        # Add a span to trigger export
        processor.add_span(create_test_span(name="span-1"))
        time.sleep(0.1)  # Let export start

        # Stop with very short timeout - export thread will still be running
        processor.stop(timeout=0.01)

        # Add more spans and force flush from main thread
        processor._queue.append(create_test_span(name="span-2"))
        processor._force_flush()

        # Verify: loops used during force_flush should NOT be the export thread's loop
        # (because force_flush runs on the main thread)
        main_thread_loops = [loop for loop in loops_used if loop is not export_thread_loop]
        assert len(main_thread_loops) > 0, "force_flush should have used a different loop"
