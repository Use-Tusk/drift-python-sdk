"""Trace blocking manager to prevent recording oversized traces.

This singleton manages a set of blocked trace IDs to prevent memory exhaustion
from traces that produce spans exceeding size limits.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Set

logger = logging.getLogger(__name__)

# Size limits (matching Node SDK)
MAX_SPAN_SIZE_MB = 1
MAX_SPAN_SIZE_BYTES = MAX_SPAN_SIZE_MB * 1024 * 1024
METADATA_BUFFER_KB = 50
METADATA_BUFFER_BYTES = METADATA_BUFFER_KB * 1024


class TraceBlockingManager:
    """
    Singleton manager for blocking traces that exceed size limits.

    When a span exceeds the maximum allowed size (1MB), its entire trace is
    blocked to prevent future spans from being recorded. This prevents memory
    exhaustion from pathologically large traces.

    Features:
    - O(1) trace blocking check via Set
    - Automatic cleanup of old trace IDs (10 min TTL)
    - Thread-safe operations
    """

    _instance: TraceBlockingManager | None = None
    _lock = threading.Lock()

    # Time to live for blocked traces (10 minutes)
    DEFAULT_TTL_MS = 10 * 60 * 1000
    # Cleanup interval (2 minutes)
    CLEANUP_INTERVAL_MS = 2 * 60 * 1000

    def __init__(self) -> None:
        self._blocked_trace_ids: Set[str] = set()
        self._trace_timestamps: dict[str, float] = {}
        self._cleanup_thread: threading.Thread | None = None
        self._stop_cleanup = threading.Event()

    @classmethod
    def get_instance(cls) -> TraceBlockingManager:
        """Get the singleton instance."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = TraceBlockingManager()
                    cls._instance._start_cleanup_thread()
        return cls._instance

    def block_trace(self, trace_id: str) -> None:
        """Block a trace ID from being recorded.

        Args:
            trace_id: The trace ID to block
        """
        with self._lock:
            if trace_id not in self._blocked_trace_ids:
                self._blocked_trace_ids.add(trace_id)
                self._trace_timestamps[trace_id] = time.time() * 1000  # milliseconds
                logger.debug(f"Blocked trace: {trace_id}")

    def is_trace_blocked(self, trace_id: str) -> bool:
        """Check if a trace ID is blocked.

        Args:
            trace_id: The trace ID to check

        Returns:
            True if the trace is blocked, False otherwise
        """
        with self._lock:
            return trace_id in self._blocked_trace_ids

    def unblock_trace(self, trace_id: str) -> None:
        """Unblock a trace ID.

        Args:
            trace_id: The trace ID to unblock
        """
        with self._lock:
            self._blocked_trace_ids.discard(trace_id)
            self._trace_timestamps.pop(trace_id, None)
            logger.debug(f"Unblocked trace: {trace_id}")

    def get_blocked_count(self) -> int:
        """Get the number of currently blocked traces."""
        with self._lock:
            return len(self._blocked_trace_ids)

    def clear_all(self) -> None:
        """Clear all blocked traces."""
        with self._lock:
            self._blocked_trace_ids.clear()
            self._trace_timestamps.clear()
            logger.debug("Cleared all blocked traces")

    def _start_cleanup_thread(self) -> None:
        """Start the background cleanup thread."""
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop,
            daemon=True,
            name="TraceBlockingManager-Cleanup",
        )
        self._cleanup_thread.start()

    def _cleanup_loop(self) -> None:
        """Background loop to clean up old trace IDs."""
        interval_seconds = self.CLEANUP_INTERVAL_MS / 1000

        while not self._stop_cleanup.wait(timeout=interval_seconds):
            self._cleanup_old_traces()

    def _cleanup_old_traces(self) -> None:
        """Remove trace IDs older than TTL."""
        current_time = time.time() * 1000  # milliseconds
        ttl = self.DEFAULT_TTL_MS

        with self._lock:
            expired_traces = [
                trace_id
                for trace_id, timestamp in self._trace_timestamps.items()
                if current_time - timestamp > ttl
            ]

            for trace_id in expired_traces:
                self._blocked_trace_ids.discard(trace_id)
                self._trace_timestamps.pop(trace_id, None)

            if expired_traces:
                logger.debug(f"Cleaned up {len(expired_traces)} expired traces")

    def shutdown(self) -> None:
        """Shutdown the cleanup thread."""
        if self._cleanup_thread and self._cleanup_thread.is_alive():
            self._stop_cleanup.set()
            self._cleanup_thread.join(timeout=5)
            logger.debug("Trace blocking manager shutdown complete")


def estimate_span_size(span: Any) -> int:
    """Estimate the size of a span in bytes.

    Args:
        span: CleanSpanData object

    Returns:
        Estimated size in bytes
    """
    import json

    # Estimate input size
    input_size = 0
    if hasattr(span, "input_value") and span.input_value:
        try:
            input_size = len(json.dumps(span.input_value).encode("utf-8"))
        except Exception:
            input_size = 0

    # Estimate output size
    output_size = 0
    if hasattr(span, "output_value") and span.output_value:
        try:
            output_size = len(json.dumps(span.output_value).encode("utf-8"))
        except Exception:
            output_size = 0

    # Add metadata buffer
    total_size = input_size + output_size + METADATA_BUFFER_BYTES

    return total_size


def should_block_span(span: Any) -> bool:
    """Check if a span should be blocked due to size.

    If the span exceeds the maximum size, blocks the entire trace
    and returns True.

    Args:
        span: CleanSpanData object

    Returns:
        True if the span should be blocked, False otherwise
    """
    size = estimate_span_size(span)

    if size > MAX_SPAN_SIZE_BYTES:
        trace_id = getattr(span, "trace_id", "unknown")
        size_mb = size / (1024 * 1024)

        logger.warning(
            f"Blocking trace {trace_id} - span '{getattr(span, 'name', 'unknown')}' "
            f"has estimated size of {size_mb:.2f} MB, exceeding limit of {MAX_SPAN_SIZE_MB} MB"
        )

        TraceBlockingManager.get_instance().block_trace(trace_id)
        return True

    return False
