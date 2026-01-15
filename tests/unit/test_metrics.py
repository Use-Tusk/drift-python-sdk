"""Tests for SDK metrics."""

import threading
import time
import unittest
from unittest.mock import patch

from drift.core.metrics import (
    ExportMetrics,
    MetricsCollector,
    QueueMetrics,
    SDKMetrics,
    get_metrics_collector,
    get_sdk_metrics,
)


class TestExportMetrics(unittest.TestCase):
    """Tests for ExportMetrics dataclass."""

    def test_default_values(self):
        """Test default values."""
        metrics = ExportMetrics()
        self.assertEqual(metrics.spans_exported, 0)
        self.assertEqual(metrics.spans_dropped, 0)
        self.assertEqual(metrics.spans_failed, 0)
        self.assertEqual(metrics.batches_exported, 0)
        self.assertEqual(metrics.batches_failed, 0)
        self.assertEqual(metrics.bytes_sent, 0)
        self.assertEqual(metrics.bytes_compressed_saved, 0)

    def test_average_latency_when_empty(self):
        """Test average latency is 0 when no exports."""
        metrics = ExportMetrics()
        self.assertEqual(metrics.average_export_latency_ms, 0.0)

    def test_average_latency_calculation(self):
        """Test average latency calculation."""
        metrics = ExportMetrics(
            export_latency_sum_ms=300.0,
            export_count=3,
        )
        self.assertEqual(metrics.average_export_latency_ms, 100.0)


class TestQueueMetrics(unittest.TestCase):
    """Tests for QueueMetrics dataclass."""

    def test_default_values(self):
        """Test default values."""
        metrics = QueueMetrics()
        self.assertEqual(metrics.current_size, 0)
        self.assertEqual(metrics.max_size, 0)
        self.assertEqual(metrics.peak_size, 0)


class TestMetricsCollector(unittest.TestCase):
    """Tests for MetricsCollector."""

    def setUp(self):
        """Create fresh collector for each test."""
        self.collector = MetricsCollector()

    def test_record_spans_exported(self):
        """Test recording exported spans."""
        self.collector.record_spans_exported(10)
        self.collector.record_spans_exported(5)

        metrics = self.collector.get_metrics()
        self.assertEqual(metrics.export.spans_exported, 15)
        self.assertEqual(metrics.export.batches_exported, 2)

    def test_record_spans_dropped(self):
        """Test recording dropped spans."""
        self.collector.record_spans_dropped()
        self.collector.record_spans_dropped(5)

        metrics = self.collector.get_metrics()
        self.assertEqual(metrics.export.spans_dropped, 6)

    def test_record_spans_failed(self):
        """Test recording failed spans."""
        self.collector.record_spans_failed(10)

        metrics = self.collector.get_metrics()
        self.assertEqual(metrics.export.spans_failed, 10)
        self.assertEqual(metrics.export.batches_failed, 1)

    def test_record_export_latency(self):
        """Test recording export latency."""
        self.collector.record_export_latency(100.0)
        self.collector.record_export_latency(200.0)

        metrics = self.collector.get_metrics()
        self.assertEqual(metrics.export.export_latency_sum_ms, 300.0)
        self.assertEqual(metrics.export.export_count, 2)
        self.assertEqual(metrics.export.average_export_latency_ms, 150.0)

    def test_record_bytes_sent(self):
        """Test recording bytes sent."""
        self.collector.record_bytes_sent(1000, 200)
        self.collector.record_bytes_sent(500, 100)

        metrics = self.collector.get_metrics()
        self.assertEqual(metrics.export.bytes_sent, 1500)
        self.assertEqual(metrics.export.bytes_compressed_saved, 300)

    def test_update_queue_size(self):
        """Test updating queue size."""
        self.collector.set_queue_max_size(1000)
        self.collector.update_queue_size(50)
        self.collector.update_queue_size(100)
        self.collector.update_queue_size(75)

        metrics = self.collector.get_metrics()
        self.assertEqual(metrics.queue.current_size, 75)
        self.assertEqual(metrics.queue.peak_size, 100)
        self.assertEqual(metrics.queue.max_size, 1000)

    def test_instrumentation_tracking(self):
        """Test instrumentation activation tracking."""
        self.collector.record_instrumentation_activated()
        self.collector.record_instrumentation_activated()
        self.collector.record_instrumentation_deactivated()

        metrics = self.collector.get_metrics()
        self.assertEqual(metrics.instrumentations_active, 1)

    def test_instrumentation_deactivate_floor(self):
        """Test that instrumentation count doesn't go below 0."""
        self.collector.record_instrumentation_deactivated()

        metrics = self.collector.get_metrics()
        self.assertEqual(metrics.instrumentations_active, 0)

    def test_uptime_tracking(self):
        """Test uptime tracking."""
        time.sleep(0.1)
        metrics = self.collector.get_metrics()
        self.assertGreaterEqual(metrics.uptime_seconds, 0.1)

    def test_reset(self):
        """Test resetting metrics."""
        self.collector.record_spans_exported(100)
        self.collector.record_spans_dropped(10)
        self.collector.update_queue_size(50)

        self.collector.reset()

        metrics = self.collector.get_metrics()
        self.assertEqual(metrics.export.spans_exported, 0)
        self.assertEqual(metrics.export.spans_dropped, 0)
        self.assertEqual(metrics.queue.peak_size, 0)
        # Current queue size isn't reset (it's external state)

    def test_reset_clears_warning_flags(self):
        """Test that reset clears warning flags so warnings can fire again."""
        self.collector.set_queue_max_size(100)

        with patch("drift.core.metrics.logger") as mock_logger:
            # Trigger warning (85% capacity)
            self.collector.update_queue_size(85)
            self.assertTrue(self.collector._warned_queue_capacity)
            mock_logger.warning.assert_called_once()

            # Reset should clear the flag
            self.collector.reset()
            self.assertFalse(self.collector._warned_queue_capacity)
            self.assertFalse(self.collector._warned_high_drop_rate)
            self.assertFalse(self.collector._warned_high_failure_rate)
            self.assertFalse(self.collector._warned_circuit_open)

            # Warning should fire again after reset
            mock_logger.reset_mock()
            self.collector.set_queue_max_size(100)
            self.collector.update_queue_size(85)
            mock_logger.warning.assert_called_once()

    def test_thread_safety(self):
        """Test that metrics collection is thread-safe."""
        iterations = 1000
        threads = []

        def record_metrics():
            for _ in range(iterations):
                self.collector.record_spans_exported(1)
                self.collector.record_spans_dropped()
                self.collector.update_queue_size(10)

        for _ in range(5):
            t = threading.Thread(target=record_metrics)
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        metrics = self.collector.get_metrics()
        self.assertEqual(metrics.export.spans_exported, 5 * iterations)
        self.assertEqual(metrics.export.spans_dropped, 5 * iterations)


class TestGlobalMetricsCollector(unittest.TestCase):
    """Tests for global metrics functions."""

    def test_get_metrics_collector_returns_same_instance(self):
        """Test that get_metrics_collector returns the same instance."""
        collector1 = get_metrics_collector()
        collector2 = get_metrics_collector()
        self.assertIs(collector1, collector2)

    def test_get_sdk_metrics_returns_snapshot(self):
        """Test that get_sdk_metrics returns a metrics snapshot."""
        metrics = get_sdk_metrics()
        self.assertIsInstance(metrics, SDKMetrics)


class TestEventDrivenWarnings(unittest.TestCase):
    """Tests for event-driven warning logs."""

    def setUp(self):
        """Create fresh collector for each test."""
        self.collector = MetricsCollector()

    def test_high_drop_rate_warning(self):
        """Test that high drop rate triggers a warning."""
        # Need at least 100 samples for warning to trigger
        with patch("drift.core.metrics.logger") as mock_logger:
            # 90 exported, 10 dropped = 10% drop rate (above 5% threshold)
            for _ in range(90):
                self.collector.record_spans_exported(1)
            for _ in range(10):
                self.collector.record_spans_dropped(1)

            # Should have warned about high drop rate
            mock_logger.warning.assert_called()
            call_args = mock_logger.warning.call_args[0][0]
            self.assertIn("drop rate", call_args.lower())

    def test_drop_rate_warning_not_spam(self):
        """Test that drop rate warning is not repeated."""
        with patch("drift.core.metrics.logger") as mock_logger:
            # First batch: 90 exported, 10 dropped = 10% drop rate
            for _ in range(90):
                self.collector.record_spans_exported(1)
            for _ in range(10):
                self.collector.record_spans_dropped(1)

            # More drops should not spam warnings
            for _ in range(10):
                self.collector.record_spans_dropped(1)

            # Should only have warned once
            warning_calls = [c for c in mock_logger.warning.call_args_list if "drop rate" in str(c).lower()]
            self.assertEqual(len(warning_calls), 1)

    def test_high_failure_rate_warning(self):
        """Test that high failure rate triggers a warning."""
        with patch("drift.core.metrics.logger") as mock_logger:
            # 85 exported, 15 failed = ~15% failure rate (above 10% threshold)
            for _ in range(85):
                self.collector.record_spans_exported(1)
            self.collector.record_spans_failed(15)

            # Should have warned about high failure rate
            mock_logger.warning.assert_called()
            call_args = mock_logger.warning.call_args[0][0]
            self.assertIn("failure rate", call_args.lower())

    def test_queue_capacity_warning(self):
        """Test that high queue capacity triggers a warning."""
        self.collector.set_queue_max_size(100)

        with patch("drift.core.metrics.logger") as mock_logger:
            # 85% capacity (above 80% threshold)
            self.collector.update_queue_size(85)

            mock_logger.warning.assert_called()
            call_args = mock_logger.warning.call_args[0][0]
            self.assertIn("queue", call_args.lower())
            self.assertIn("capacity", call_args.lower())

    def test_queue_capacity_warning_clears(self):
        """Test that queue capacity warning clears when capacity decreases."""
        self.collector.set_queue_max_size(100)

        with patch("drift.core.metrics.logger"):
            # Trigger warning at 85%
            self.collector.update_queue_size(85)
            self.assertTrue(self.collector._warned_queue_capacity)

            # Drop below threshold
            self.collector.update_queue_size(50)
            self.assertFalse(self.collector._warned_queue_capacity)

    def test_circuit_breaker_warning(self):
        """Test circuit breaker open warning."""
        with patch("drift.core.metrics.logger") as mock_logger:
            self.collector.warn_circuit_breaker_open()

            mock_logger.warning.assert_called()
            call_args = mock_logger.warning.call_args[0][0]
            self.assertIn("circuit breaker", call_args.lower())
            self.assertIn("open", call_args.lower())

    def test_circuit_breaker_closed_notification(self):
        """Test circuit breaker closed notification."""
        with patch("drift.core.metrics.logger") as mock_logger:
            # First open
            self.collector.warn_circuit_breaker_open()
            # Then close
            self.collector.notify_circuit_breaker_closed()

            # Should have logged info about closing
            info_calls = mock_logger.info.call_args_list
            self.assertTrue(any("closed" in str(c).lower() for c in info_calls))

    def test_no_warning_below_threshold(self):
        """Test that no warnings are logged below thresholds."""
        self.collector.set_queue_max_size(100)

        with patch("drift.core.metrics.logger") as mock_logger:
            # 95 exported, 5 dropped = 5% drop rate (at threshold, not above)
            for _ in range(95):
                self.collector.record_spans_exported(1)
            for _ in range(5):
                self.collector.record_spans_dropped(1)

            # Queue at 50% (below 80% threshold)
            self.collector.update_queue_size(50)

            # No warnings should be logged
            mock_logger.warning.assert_not_called()

    def test_no_warning_without_enough_samples(self):
        """Test that no drop rate warning without enough samples."""
        with patch("drift.core.metrics.logger") as mock_logger:
            # Only 50 samples (below 100 minimum)
            for _ in range(45):
                self.collector.record_spans_exported(1)
            for _ in range(5):
                self.collector.record_spans_dropped(1)

            # No warning due to insufficient samples
            mock_logger.warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
