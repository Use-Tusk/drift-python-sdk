"""Batch span processor for efficient span export."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .types import CleanSpanData
    from .tracing.adapters.base import SpanExportAdapter

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
        adapters: list["SpanExportAdapter"],
        config: BatchSpanProcessorConfig | None = None,
    ) -> None:
        """
        Initialize the batch processor.

        Args:
            adapters: List of adapters to export spans to
            config: Optional configuration (uses defaults if not provided)
        """
        self._adapters = adapters
        self._config = config or BatchSpanProcessorConfig()
        self._queue: deque[CleanSpanData] = deque(maxlen=self._config.max_queue_size)
        self._lock = threading.Lock()
        self._shutdown_event = threading.Event()
        self._export_thread: threading.Thread | None = None
        self._started = False
        self._dropped_spans = 0

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

        if self._export_thread is not None:
            self._export_thread.join(timeout=timeout or self._config.export_timeout_seconds)

        # Final export of remaining spans
        self._force_flush()

        self._started = False
        logger.debug(f"BatchSpanProcessor stopped. Dropped {self._dropped_spans} spans total.")

    def add_span(self, span: "CleanSpanData") -> bool:
        """
        Add a span to the queue for export.

        Args:
            span: The span to add

        Returns:
            True if span was added, False if queue is full and span was dropped
        """
        with self._lock:
            if len(self._queue) >= self._config.max_queue_size:
                self._dropped_spans += 1
                logger.warning(
                    f"Span queue full ({self._config.max_queue_size}), dropping span. "
                    f"Total dropped: {self._dropped_spans}"
                )
                return False

            self._queue.append(span)

            # Trigger immediate export if batch size reached
            if len(self._queue) >= self._config.max_export_batch_size:
                # Signal export thread to wake up (if using condition variable)
                pass

            return True

    def _export_loop(self) -> None:
        """Background thread that periodically exports spans."""
        while not self._shutdown_event.is_set():
            # Wait for scheduled delay or shutdown
            self._shutdown_event.wait(timeout=self._config.scheduled_delay_seconds)

            if self._shutdown_event.is_set():
                break

            self._export_batch()

    def _export_batch(self) -> None:
        """Export a batch of spans from the queue."""
        # Get batch of spans
        batch: list[CleanSpanData] = []
        with self._lock:
            while self._queue and len(batch) < self._config.max_export_batch_size:
                batch.append(self._queue.popleft())

        if not batch:
            return

        # Export to all adapters
        for adapter in self._adapters:
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

                logger.debug(f"Exported {len(batch)} spans via {adapter.name}")

            except Exception as e:
                logger.error(f"Failed to export batch via {adapter.name}: {e}")

    def _force_flush(self) -> None:
        """Force export all remaining spans in the queue."""
        while True:
            with self._lock:
                if not self._queue:
                    break

            self._export_batch()

    @property
    def queue_size(self) -> int:
        """Get the current queue size."""
        with self._lock:
            return len(self._queue)

    @property
    def dropped_span_count(self) -> int:
        """Get the number of dropped spans."""
        return self._dropped_spans
