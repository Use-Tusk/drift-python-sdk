"""Tests for SDK metrics."""

import threading
import time

import pytest

from drift.core.metrics import (
    ExportMetrics,
    MetricsCollector,
    QueueMetrics,
    SDKMetrics,
    get_metrics_collector,
    get_sdk_metrics,
)


class TestExportMetrics:
    """Tests for ExportMetrics dataclass."""

    def test_default_values(self):
        """Test default values."""
        metrics = ExportMetrics()
        assert metrics.spans_exported == 0
        assert metrics.spans_dropped == 0
        assert metrics.spans_failed == 0
        assert metrics.batches_exported == 0
        assert metrics.batches_failed == 0
        assert metrics.bytes_sent == 0
        assert metrics.bytes_compressed_saved == 0

    def test_average_latency_when_empty(self):
        """Test average latency is 0 when no exports."""
        metrics = ExportMetrics()
        assert metrics.average_export_latency_ms == 0.0

    def test_average_latency_calculation(self):
        """Test average latency calculation."""
        metrics = ExportMetrics(
            export_latency_sum_ms=300.0,
            export_count=3,
        )
        assert metrics.average_export_latency_ms == 100.0


class TestQueueMetrics:
    """Tests for QueueMetrics dataclass."""

    def test_default_values(self):
        """Test default values."""
        metrics = QueueMetrics()
        assert metrics.current_size == 0
        assert metrics.max_size == 0
        assert metrics.peak_size == 0


class TestMetricsCollector:
    """Tests for MetricsCollector."""

    @pytest.fixture
    def collector(self):
        """Create fresh collector for each test."""
        return MetricsCollector()

    def test_record_spans_exported(self, collector):
        """Test recording exported spans."""
        collector.record_spans_exported(10)
        collector.record_spans_exported(5)

        metrics = collector.get_metrics()
        assert metrics.export.spans_exported == 15
        assert metrics.export.batches_exported == 2

    def test_record_spans_dropped(self, collector):
        """Test recording dropped spans."""
        collector.record_spans_dropped()
        collector.record_spans_dropped(5)

        metrics = collector.get_metrics()
        assert metrics.export.spans_dropped == 6

    def test_record_spans_failed(self, collector):
        """Test recording failed spans."""
        collector.record_spans_failed(10)

        metrics = collector.get_metrics()
        assert metrics.export.spans_failed == 10
        assert metrics.export.batches_failed == 1

    def test_record_export_latency(self, collector):
        """Test recording export latency."""
        collector.record_export_latency(100.0)
        collector.record_export_latency(200.0)

        metrics = collector.get_metrics()
        assert metrics.export.export_latency_sum_ms == 300.0
        assert metrics.export.export_count == 2
        assert metrics.export.average_export_latency_ms == 150.0

    def test_record_bytes_sent(self, collector):
        """Test recording bytes sent."""
        collector.record_bytes_sent(1000, 200)
        collector.record_bytes_sent(500, 100)

        metrics = collector.get_metrics()
        assert metrics.export.bytes_sent == 1500
        assert metrics.export.bytes_compressed_saved == 300

    def test_update_queue_size(self, collector):
        """Test updating queue size."""
        collector.set_queue_max_size(1000)
        collector.update_queue_size(50)
        collector.update_queue_size(100)
        collector.update_queue_size(75)

        metrics = collector.get_metrics()
        assert metrics.queue.current_size == 75
        assert metrics.queue.peak_size == 100
        assert metrics.queue.max_size == 1000

    def test_instrumentation_tracking(self, collector):
        """Test instrumentation activation tracking."""
        collector.record_instrumentation_activated()
        collector.record_instrumentation_activated()
        collector.record_instrumentation_deactivated()

        metrics = collector.get_metrics()
        assert metrics.instrumentations_active == 1

    def test_instrumentation_deactivate_floor(self, collector):
        """Test that instrumentation count doesn't go below 0."""
        collector.record_instrumentation_deactivated()

        metrics = collector.get_metrics()
        assert metrics.instrumentations_active == 0

    def test_uptime_tracking(self, collector):
        """Test uptime tracking."""
        time.sleep(0.1)
        metrics = collector.get_metrics()
        assert metrics.uptime_seconds >= 0.1

    def test_reset(self, collector):
        """Test resetting metrics."""
        collector.record_spans_exported(100)
        collector.record_spans_dropped(10)
        collector.update_queue_size(50)

        collector.reset()

        metrics = collector.get_metrics()
        assert metrics.export.spans_exported == 0
        assert metrics.export.spans_dropped == 0
        assert metrics.queue.peak_size == 0

    def test_reset_clears_warning_flags(self, collector, mocker):
        """Test that reset clears warning flags so warnings can fire again."""
        collector.set_queue_max_size(100)

        mock_logger = mocker.patch("drift.core.metrics.logger")
        collector.update_queue_size(85)
        assert collector._warned_queue_capacity is True
        mock_logger.warning.assert_called_once()

        collector.reset()
        assert collector._warned_queue_capacity is False
        assert collector._warned_high_drop_rate is False
        assert collector._warned_high_failure_rate is False
        assert collector._warned_circuit_open is False

        mock_logger.reset_mock()
        collector.set_queue_max_size(100)
        collector.update_queue_size(85)
        mock_logger.warning.assert_called_once()

    def test_thread_safety(self, collector):
        """Test that metrics collection is thread-safe."""
        iterations = 1000
        threads = []

        def record_metrics():
            for _ in range(iterations):
                collector.record_spans_exported(1)
                collector.record_spans_dropped()
                collector.update_queue_size(10)

        for _ in range(5):
            t = threading.Thread(target=record_metrics)
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        metrics = collector.get_metrics()
        assert metrics.export.spans_exported == 5 * iterations
        assert metrics.export.spans_dropped == 5 * iterations


class TestGlobalMetricsCollector:
    """Tests for global metrics functions."""

    def test_get_metrics_collector_returns_same_instance(self):
        """Test that get_metrics_collector returns the same instance."""
        collector1 = get_metrics_collector()
        collector2 = get_metrics_collector()
        assert collector1 is collector2

    def test_get_sdk_metrics_returns_snapshot(self):
        """Test that get_sdk_metrics returns a metrics snapshot."""
        metrics = get_sdk_metrics()
        assert isinstance(metrics, SDKMetrics)


class TestEventDrivenWarnings:
    """Tests for event-driven warning logs."""

    @pytest.fixture
    def collector(self):
        """Create fresh collector for each test."""
        return MetricsCollector()

    def test_high_drop_rate_warning(self, collector, mocker):
        """Test that high drop rate triggers a warning."""
        mock_logger = mocker.patch("drift.core.metrics.logger")
        for _ in range(90):
            collector.record_spans_exported(1)
        for _ in range(10):
            collector.record_spans_dropped(1)

        mock_logger.warning.assert_called()
        call_args = mock_logger.warning.call_args[0][0]
        assert "drop rate" in call_args.lower()

    def test_drop_rate_warning_not_spam(self, collector, mocker):
        """Test that drop rate warning is not repeated."""
        mock_logger = mocker.patch("drift.core.metrics.logger")
        for _ in range(90):
            collector.record_spans_exported(1)
        for _ in range(10):
            collector.record_spans_dropped(1)

        for _ in range(10):
            collector.record_spans_dropped(1)

        warning_calls = [c for c in mock_logger.warning.call_args_list if "drop rate" in str(c).lower()]
        assert len(warning_calls) == 1

    def test_high_failure_rate_warning(self, collector, mocker):
        """Test that high failure rate triggers a warning."""
        mock_logger = mocker.patch("drift.core.metrics.logger")
        for _ in range(85):
            collector.record_spans_exported(1)
        collector.record_spans_failed(15)

        mock_logger.warning.assert_called()
        call_args = mock_logger.warning.call_args[0][0]
        assert "failure rate" in call_args.lower()

    def test_queue_capacity_warning(self, collector, mocker):
        """Test that high queue capacity triggers a warning."""
        collector.set_queue_max_size(100)

        mock_logger = mocker.patch("drift.core.metrics.logger")
        collector.update_queue_size(85)

        mock_logger.warning.assert_called()
        call_args = mock_logger.warning.call_args[0][0]
        assert "queue" in call_args.lower()
        assert "capacity" in call_args.lower()

    def test_queue_capacity_warning_clears(self, collector, mocker):
        """Test that queue capacity warning clears when capacity decreases."""
        collector.set_queue_max_size(100)

        mocker.patch("drift.core.metrics.logger")
        collector.update_queue_size(85)
        assert collector._warned_queue_capacity is True

        collector.update_queue_size(50)
        assert collector._warned_queue_capacity is False

    def test_circuit_breaker_warning(self, collector, mocker):
        """Test circuit breaker open warning."""
        mock_logger = mocker.patch("drift.core.metrics.logger")
        collector.warn_circuit_breaker_open()

        mock_logger.warning.assert_called()
        call_args = mock_logger.warning.call_args[0][0]
        assert "circuit breaker" in call_args.lower()
        assert "open" in call_args.lower()

    def test_circuit_breaker_closed_notification(self, collector, mocker):
        """Test circuit breaker closed notification."""
        mock_logger = mocker.patch("drift.core.metrics.logger")
        collector.warn_circuit_breaker_open()
        collector.notify_circuit_breaker_closed()

        info_calls = mock_logger.info.call_args_list
        assert any("closed" in str(c).lower() for c in info_calls)

    def test_no_warning_below_threshold(self, collector, mocker):
        """Test that no warnings are logged below thresholds."""
        collector.set_queue_max_size(100)

        mock_logger = mocker.patch("drift.core.metrics.logger")
        for _ in range(95):
            collector.record_spans_exported(1)
        for _ in range(5):
            collector.record_spans_dropped(1)

        collector.update_queue_size(50)

        mock_logger.warning.assert_not_called()

    def test_no_warning_without_enough_samples(self, collector, mocker):
        """Test that no drop rate warning without enough samples."""
        mock_logger = mocker.patch("drift.core.metrics.logger")
        for _ in range(45):
            collector.record_spans_exported(1)
        for _ in range(5):
            collector.record_spans_dropped(1)

        mock_logger.warning.assert_not_called()
