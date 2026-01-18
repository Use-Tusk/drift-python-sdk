"""Trace blocking manager to prevent recording oversized traces.

This singleton manages a set of blocked trace IDs to prevent memory exhaustion
from traces that produce spans exceeding size limits.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .types import CleanSpanData

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
        self._blocked_trace_ids: set[str] = set()
        self._trace_timestamps: dict[str, float] = {}
        self._block_reasons: dict[str, str] = {}
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
        assert cls._instance is not None
        return cls._instance

    def block_trace(self, trace_id: str, reason: str = "size_limit") -> None:
        """Block a trace ID from being recorded.

        Args:
            trace_id: The trace ID to block
            reason: The reason for blocking (e.g., "size_limit", "binary_content:PNG")
        """
        with self._lock:
            if trace_id not in self._blocked_trace_ids:
                self._blocked_trace_ids.add(trace_id)
                self._trace_timestamps[trace_id] = time.time() * 1000  # milliseconds
                self._block_reasons[trace_id] = reason
                logger.debug(f"Blocked trace {trace_id}: {reason}")

    def is_trace_blocked(self, trace_id: str) -> bool:
        """Check if a trace ID is blocked.

        Args:
            trace_id: The trace ID to check

        Returns:
            True if the trace is blocked, False otherwise
        """
        with self._lock:
            return trace_id in self._blocked_trace_ids

    def get_block_reason(self, trace_id: str) -> str | None:
        """Get the reason a trace was blocked.

        Args:
            trace_id: The trace ID to check

        Returns:
            The block reason string, or None if not blocked
        """
        with self._lock:
            return self._block_reasons.get(trace_id)

    def unblock_trace(self, trace_id: str) -> None:
        """Unblock a trace ID.

        Args:
            trace_id: The trace ID to unblock
        """
        with self._lock:
            self._blocked_trace_ids.discard(trace_id)
            self._trace_timestamps.pop(trace_id, None)
            self._block_reasons.pop(trace_id, None)
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
            self._block_reasons.clear()
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
                trace_id for trace_id, timestamp in self._trace_timestamps.items() if current_time - timestamp > ttl
            ]

            for trace_id in expired_traces:
                self._blocked_trace_ids.discard(trace_id)
                self._trace_timestamps.pop(trace_id, None)
                self._block_reasons.pop(trace_id, None)

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


def should_block_span(span: CleanSpanData) -> bool:
    """Check if a span should be blocked due to size or server error status.

    Blocks the trace if:
    1. The span is a SERVER span with ERROR status (e.g., HTTP >= 400)
    2. The span exceeds the maximum size limit (1MB)

    This matches Node SDK behavior in TdSpanExporter.ts.

    Args:
        span: CleanSpanData object

    Returns:
        True if the span should be blocked, False otherwise
    """
    from .types import SpanKind, StatusCode

    trace_id = span.trace_id
    span_name = span.name
    blocking_manager = TraceBlockingManager.get_instance()

    # Check 1: Block SERVER spans with ERROR status (e.g., HTTP >= 400)
    if span.kind == SpanKind.SERVER and span.status.code == StatusCode.ERROR:
        logger.debug(f"Blocking trace {trace_id} - server span '{span_name}' has error status")
        blocking_manager.block_trace(trace_id, reason="server_error")
        return True

    # Check 2: Block spans exceeding size limit
    size = estimate_span_size(span)
    if size > MAX_SPAN_SIZE_BYTES:
        size_mb = size / (1024 * 1024)
        logger.debug(
            f"Blocking trace {trace_id} - span '{span_name}' "
            f"has estimated size of {size_mb:.2f} MB, exceeding limit of {MAX_SPAN_SIZE_MB} MB"
        )
        blocking_manager.block_trace(trace_id, reason=f"size_limit:{size_mb:.2f}MB")
        return True

    return False
