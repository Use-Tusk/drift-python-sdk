"""SDK self-metrics for observability.

Provides metrics about the SDK's internal state and performance.
Uses event-driven logging at WARN level for anomalies.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .resilience import CircuitBreaker

logger = logging.getLogger(__name__)

DROP_RATE_WARN_THRESHOLD = 0.05
QUEUE_CAPACITY_WARN_THRESHOLD = 0.80
FAILURE_RATE_WARN_THRESHOLD = 0.10
MIN_SAMPLES_FOR_RATE_WARNING = 100


@dataclass
class ExportMetrics:
    """Metrics for span export operations."""

    spans_exported: int = 0
    spans_dropped: int = 0
    spans_failed: int = 0
    batches_exported: int = 0
    batches_failed: int = 0
    bytes_sent: int = 0
    bytes_compressed_saved: int = 0
    export_latency_sum_ms: float = 0.0
    export_count: int = 0

    @property
    def average_export_latency_ms(self) -> float:
        """Average export latency in milliseconds."""
        if self.export_count == 0:
            return 0.0
        return self.export_latency_sum_ms / self.export_count


@dataclass
class QueueMetrics:
    """Metrics for the span queue."""

    current_size: int = 0
    max_size: int = 0
    peak_size: int = 0  # Highest size observed


@dataclass
class CircuitBreakerMetrics:
    """Metrics for circuit breaker state."""

    state: str = "closed"
    total_requests: int = 0
    rejected_requests: int = 0
    state_transitions: int = 0


@dataclass
class SDKMetrics:
    """Aggregated SDK metrics."""

    export: ExportMetrics = field(default_factory=ExportMetrics)
    queue: QueueMetrics = field(default_factory=QueueMetrics)
    circuit_breaker: CircuitBreakerMetrics = field(default_factory=CircuitBreakerMetrics)
    uptime_seconds: float = 0.0
    instrumentations_active: int = 0


class MetricsCollector:
    """Collects and aggregates SDK metrics.

    Thread-safe metrics collection for SDK observability.
    Uses event-driven WARN logging when anomalies are detected.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._start_time = time.monotonic()

        # Export metrics
        self._spans_exported = 0
        self._spans_dropped = 0
        self._spans_failed = 0
        self._batches_exported = 0
        self._batches_failed = 0
        self._bytes_sent = 0
        self._bytes_compressed_saved = 0
        self._export_latency_sum_ms = 0.0
        self._export_count = 0

        # Queue metrics
        self._queue_size = 0
        self._queue_max_size = 0
        self._queue_peak_size = 0

        # Circuit breaker reference (set externally)
        self._circuit_breaker: CircuitBreaker | None = None

        # Instrumentation count
        self._instrumentations_active = 0

        # Warning state tracking (to avoid log spam)
        self._warned_high_drop_rate = False
        self._warned_high_failure_rate = False
        self._warned_queue_capacity = False
        self._warned_circuit_open = False

    def set_circuit_breaker(self, cb: CircuitBreaker) -> None:
        """Set the circuit breaker reference for metrics."""
        self._circuit_breaker = cb

    def _check_and_warn_drop_rate(self) -> None:
        """Check drop rate and warn if threshold exceeded."""
        total = self._spans_exported + self._spans_dropped
        if total < MIN_SAMPLES_FOR_RATE_WARNING:
            return

        drop_rate = self._spans_dropped / total
        if drop_rate > DROP_RATE_WARN_THRESHOLD and not self._warned_high_drop_rate:
            logger.warning(f"Span export high drop rate: {drop_rate:.1%} ({self._spans_dropped}/{total} spans dropped)")
            self._warned_high_drop_rate = True
        elif drop_rate <= DROP_RATE_WARN_THRESHOLD and self._warned_high_drop_rate:
            # Reset warning flag when rate recovers
            self._warned_high_drop_rate = False

    def _check_and_warn_failure_rate(self) -> None:
        """Check failure rate and warn if threshold exceeded."""
        total = self._spans_exported + self._spans_failed
        if total < MIN_SAMPLES_FOR_RATE_WARNING:
            return

        failure_rate = self._spans_failed / total
        if failure_rate > FAILURE_RATE_WARN_THRESHOLD and not self._warned_high_failure_rate:
            logger.warning(
                f"Span export high failure rate: {failure_rate:.1%} ({self._spans_failed}/{total} spans failed)"
            )
            self._warned_high_failure_rate = True
        elif failure_rate <= FAILURE_RATE_WARN_THRESHOLD and self._warned_high_failure_rate:
            self._warned_high_failure_rate = False

    def _check_and_warn_queue_capacity(self) -> None:
        """Check queue capacity and warn if threshold exceeded."""
        if self._queue_max_size == 0:
            return

        capacity = self._queue_size / self._queue_max_size
        if capacity > QUEUE_CAPACITY_WARN_THRESHOLD and not self._warned_queue_capacity:
            logger.warning(
                f"Span export queue nearing capacity: {capacity:.0%} full ({self._queue_size}/{self._queue_max_size})"
            )
            self._warned_queue_capacity = True
        elif capacity <= QUEUE_CAPACITY_WARN_THRESHOLD and self._warned_queue_capacity:
            self._warned_queue_capacity = False

    def warn_circuit_breaker_open(self) -> None:
        """Log warning when circuit breaker opens (called externally)."""
        if not self._warned_circuit_open:
            logger.warning("Span export circuit breaker open: requests temporarily disabled")
            self._warned_circuit_open = True

    def notify_circuit_breaker_closed(self) -> None:
        """Reset circuit breaker warning state when it closes."""
        if self._warned_circuit_open:
            logger.info("Span export circuit breaker closed: requests resumed")
            self._warned_circuit_open = False

    def set_queue_max_size(self, max_size: int) -> None:
        """Set the maximum queue size."""
        self._queue_max_size = max_size

    def record_spans_exported(self, count: int) -> None:
        """Record successfully exported spans."""
        with self._lock:
            self._spans_exported += count
            self._batches_exported += 1
            # Check if drop/failure rates have recovered
            self._check_and_warn_drop_rate()
            self._check_and_warn_failure_rate()

    def record_spans_dropped(self, count: int = 1) -> None:
        """Record dropped spans (queue full or blocked)."""
        with self._lock:
            self._spans_dropped += count
            self._check_and_warn_drop_rate()

    def record_spans_failed(self, count: int) -> None:
        """Record failed span exports."""
        with self._lock:
            self._spans_failed += count
            self._batches_failed += 1
            self._check_and_warn_failure_rate()

    def record_export_latency(self, latency_ms: float) -> None:
        """Record export operation latency."""
        with self._lock:
            self._export_latency_sum_ms += latency_ms
            self._export_count += 1

    def record_bytes_sent(self, bytes_sent: int, bytes_saved: int = 0) -> None:
        """Record bytes sent and compression savings."""
        with self._lock:
            self._bytes_sent += bytes_sent
            self._bytes_compressed_saved += bytes_saved

    def update_queue_size(self, size: int) -> None:
        """Update current queue size."""
        with self._lock:
            self._queue_size = size
            if size > self._queue_peak_size:
                self._queue_peak_size = size
            self._check_and_warn_queue_capacity()

    def record_instrumentation_activated(self) -> None:
        """Record an instrumentation being activated."""
        with self._lock:
            self._instrumentations_active += 1

    def record_instrumentation_deactivated(self) -> None:
        """Record an instrumentation being deactivated."""
        with self._lock:
            self._instrumentations_active = max(0, self._instrumentations_active - 1)

    def get_metrics(self) -> SDKMetrics:
        """Get current SDK metrics snapshot."""
        with self._lock:
            export_metrics = ExportMetrics(
                spans_exported=self._spans_exported,
                spans_dropped=self._spans_dropped,
                spans_failed=self._spans_failed,
                batches_exported=self._batches_exported,
                batches_failed=self._batches_failed,
                bytes_sent=self._bytes_sent,
                bytes_compressed_saved=self._bytes_compressed_saved,
                export_latency_sum_ms=self._export_latency_sum_ms,
                export_count=self._export_count,
            )

            queue_metrics = QueueMetrics(
                current_size=self._queue_size,
                max_size=self._queue_max_size,
                peak_size=self._queue_peak_size,
            )

            cb_metrics = CircuitBreakerMetrics()
            if self._circuit_breaker:
                cb_metrics = CircuitBreakerMetrics(
                    state=self._circuit_breaker.state.value,
                    total_requests=self._circuit_breaker.stats.total_requests,
                    rejected_requests=self._circuit_breaker.stats.rejected_requests,
                    state_transitions=self._circuit_breaker.stats.state_transitions,
                )

            return SDKMetrics(
                export=export_metrics,
                queue=queue_metrics,
                circuit_breaker=cb_metrics,
                uptime_seconds=time.monotonic() - self._start_time,
                instrumentations_active=self._instrumentations_active,
            )

    def reset(self) -> None:
        """Reset all metrics to initial state."""
        with self._lock:
            self._spans_exported = 0
            self._spans_dropped = 0
            self._spans_failed = 0
            self._batches_exported = 0
            self._batches_failed = 0
            self._bytes_sent = 0
            self._bytes_compressed_saved = 0
            self._export_latency_sum_ms = 0.0
            self._export_count = 0
            self._queue_peak_size = 0
            self._start_time = time.monotonic()


# Global metrics collector instance
_metrics_collector: MetricsCollector | None = None
_metrics_lock = threading.Lock()


def get_metrics_collector() -> MetricsCollector:
    """Get or create the global metrics collector."""
    global _metrics_collector
    with _metrics_lock:
        if _metrics_collector is None:
            _metrics_collector = MetricsCollector()
        return _metrics_collector


def get_sdk_metrics() -> SDKMetrics:
    """Get current SDK metrics snapshot.

    Convenience function to get metrics without accessing the collector directly.
    """
    return get_metrics_collector().get_metrics()
