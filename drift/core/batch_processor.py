"""Batch span processor for efficient span export."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .metrics import get_metrics_collector

if TYPE_CHECKING:
    from .tracing.span_exporter import TdSpanExporter
    from .types import CleanSpanData

logger = logging.getLogger(__name__)


@dataclass
class BatchSpanProcessorConfig:
    """Configuration for the batch span processor."""

    # Maximum queue size before spans are dropped
    max_queue_size: int = 2048
    # Maximum batch size per export
    max_export_batch_size: int = 512
    # Interval between scheduled exports (in seconds)
    scheduled_delay_seconds: float = 2.0
    # Maximum time to wait for export (in seconds)
    export_timeout_seconds: float = 30.0


class BatchSpanProcessor:
    """
    Batches spans and exports them periodically or when batch size is reached.

    Matches the behavior of OpenTelemetry's BatchSpanProcessor:
    - Queues spans in memory
    - Exports in batches at regular intervals or when max batch size is reached
    - Drops spans if queue is full
    - Handles graceful shutdown with final export
    """

    def __init__(
        self,
        exporter: TdSpanExporter,
        config: BatchSpanProcessorConfig | None = None,
    ) -> None:
        """
        Initialize the batch processor.

        Args:
            exporter: Span exporter to delegate exports to
            config: Optional configuration (uses defaults if not provided)
        """
        self._exporter = exporter
        self._config = config or BatchSpanProcessorConfig()
        self._queue: deque[CleanSpanData] = deque(maxlen=self._config.max_queue_size)
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._shutdown_event = threading.Event()
        self._export_thread: threading.Thread | None = None
        self._started = False
        self._dropped_spans = 0

        # Set up metrics
        self._metrics = get_metrics_collector()
        self._metrics.set_queue_max_size(self._config.max_queue_size)

    def start(self) -> None:
        """Start the background export thread."""
        if self._started:
            return

        self._started = True
        self._shutdown_event.clear()
        self._export_thread = threading.Thread(
            target=self._export_loop,
            daemon=True,
            name="drift-batch-exporter",
        )
        self._export_thread.start()
        logger.debug("BatchSpanProcessor started")

    def stop(self, timeout: float | None = None) -> None:
        """
        Stop the processor and export remaining spans.

        Args:
            timeout: Maximum time to wait for final export
        """
        if not self._started:
            return

        self._shutdown_event.set()
        # Wake up the export thread so it can see the shutdown event
        with self._condition:
            self._condition.notify_all()

        if self._export_thread is not None:
            self._export_thread.join(timeout=timeout or self._config.export_timeout_seconds)

        # Final export of remaining spans
        self._force_flush()

        self._started = False
        logger.debug(f"BatchSpanProcessor stopped. Dropped {self._dropped_spans} spans total.")

    def add_span(self, span: CleanSpanData) -> bool:
        """
        Add a span to the queue for export.

        Args:
            span: The span to add

        Returns:
            True if span was added, False if queue is full or trace is blocked
        """
        from .trace_blocking_manager import TraceBlockingManager, should_block_span

        # Check blocking conditions outside lock (read-only checks)
        is_blocked = should_block_span(span)
        is_trace_blocked = TraceBlockingManager.get_instance().is_trace_blocked(span.trace_id)

        with self._condition:
            # Handle blocked spans (increment counter under lock)
            if is_blocked:
                self._dropped_spans += 1
                self._metrics.record_spans_dropped()
                return False

            if is_trace_blocked:
                logger.debug(f"Skipping span '{span.name}' - trace {span.trace_id} is blocked")
                self._dropped_spans += 1
                self._metrics.record_spans_dropped()
                return False

            if len(self._queue) >= self._config.max_queue_size:
                self._dropped_spans += 1
                self._metrics.record_spans_dropped()
                logger.warning(
                    f"Span queue full ({self._config.max_queue_size}), dropping span. "
                    f"Total dropped: {self._dropped_spans}"
                )
                return False

            self._queue.append(span)
            self._metrics.update_queue_size(len(self._queue))

            # Trigger immediate export if batch size reached
            if len(self._queue) >= self._config.max_export_batch_size:
                self._condition.notify()

            return True

    def _export_loop(self) -> None:
        """Background thread that periodically exports spans."""
        while not self._shutdown_event.is_set():
            # Wait for either: batch size reached, scheduled delay, or shutdown
            with self._condition:
                # Wait until batch is ready or timeout
                self._condition.wait(timeout=self._config.scheduled_delay_seconds)

            if self._shutdown_event.is_set():
                break

            self._export_batch()

    def _export_batch(self) -> None:
        """Export a batch of spans from the queue."""
        # Get batch of spans
        batch: list[CleanSpanData] = []
        with self._condition:
            while self._queue and len(batch) < self._config.max_export_batch_size:
                batch.append(self._queue.popleft())
            self._metrics.update_queue_size(len(self._queue))

        if not batch:
            return

        # Get current adapters from exporter (ensures adapter changes take effect)
        adapters = self._exporter.get_adapters()

        # Export to all adapters
        for adapter in adapters:
            start_time = time.monotonic()
            try:
                # Handle async adapters (create new event loop for this thread)
                if asyncio.iscoroutinefunction(adapter.export_spans):
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        loop.run_until_complete(adapter.export_spans(batch))
                    finally:
                        loop.close()
                else:
                    adapter.export_spans(batch)  # type: ignore

                latency_ms = (time.monotonic() - start_time) * 1000
                self._metrics.record_spans_exported(len(batch))
                self._metrics.record_export_latency(latency_ms)

            except Exception as e:
                logger.error(f"Failed to export batch via {adapter.name}: {e}")
                self._metrics.record_spans_failed(len(batch))

    def _force_flush(self) -> None:
        """Force export all remaining spans in the queue."""
        while True:
            with self._condition:
                if not self._queue:
                    break

            self._export_batch()

    @property
    def queue_size(self) -> int:
        """Get the current queue size."""
        with self._condition:
            return len(self._queue)

    @property
    def dropped_span_count(self) -> int:
        """Get the number of dropped spans."""
        return self._dropped_spans
