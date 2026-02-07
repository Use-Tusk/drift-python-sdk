"""Tests for td_span_processor.py - Custom OpenTelemetry SpanProcessor for Drift SDK."""

from __future__ import annotations

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanKind as OTelSpanKind

from drift.core.tracing.td_span_processor import TdSpanProcessor
from drift.core.types import TD_INSTRUMENTATION_LIBRARY_NAME, SpanKind, StatusCode, TuskDriftMode


class TestTdSpanProcessorInitialization:
    """Tests for TdSpanProcessor initialization."""

    def test_initializes_with_required_params(self, mocker):
        """Should initialize with required parameters."""
        mock_exporter = mocker.MagicMock()

        processor = TdSpanProcessor(
            exporter=mock_exporter,
            mode=TuskDriftMode.RECORD,
        )

        assert processor._exporter is mock_exporter
        assert processor._mode == TuskDriftMode.RECORD
        assert processor._sampling_rate == 1.0
        assert processor._app_ready is False
        assert processor._started is False

    def test_initializes_with_optional_params(self, mocker):
        """Should initialize with optional parameters."""
        mock_exporter = mocker.MagicMock()

        processor = TdSpanProcessor(
            exporter=mock_exporter,
            mode=TuskDriftMode.REPLAY,
            sampling_rate=0.5,
            app_ready=True,
            environment="production",
        )

        assert processor._mode == TuskDriftMode.REPLAY
        assert processor._sampling_rate == 0.5
        assert processor._app_ready is True
        assert processor._environment == "production"


class TestTdSpanProcessorStart:
    """Tests for TdSpanProcessor.start method."""

    def test_starts_batch_processor(self, mocker):
        """Should create and start batch processor."""
        mock_exporter = mocker.MagicMock()
        processor = TdSpanProcessor(
            exporter=mock_exporter,
            mode=TuskDriftMode.RECORD,
        )

        mock_batch = mocker.patch("drift.core.batch_processor.BatchSpanProcessor")
        mock_batch_instance = mocker.MagicMock()
        mock_batch.return_value = mock_batch_instance

        processor.start()

        assert processor._started is True
        assert processor._batch_processor is not None
        mock_batch_instance.start.assert_called_once()

    def test_start_is_idempotent(self, mocker):
        """Should not restart if already started."""
        mock_exporter = mocker.MagicMock()
        processor = TdSpanProcessor(
            exporter=mock_exporter,
            mode=TuskDriftMode.RECORD,
        )

        mock_batch = mocker.patch("drift.core.batch_processor.BatchSpanProcessor")
        mock_batch_instance = mocker.MagicMock()
        mock_batch.return_value = mock_batch_instance

        processor.start()
        processor.start()  # Second call should be no-op

        # BatchSpanProcessor should only be created once
        mock_batch.assert_called_once()


class TestTdSpanProcessorOnStart:
    """Tests for TdSpanProcessor.on_start method."""

    def test_on_start_is_noop(self, mocker):
        """Should be a no-op (all processing happens on_end)."""
        mock_exporter = mocker.MagicMock()
        processor = TdSpanProcessor(
            exporter=mock_exporter,
            mode=TuskDriftMode.RECORD,
        )

        mock_span = mocker.MagicMock()
        mock_context = mocker.MagicMock()

        # Should not raise
        processor.on_start(mock_span, mock_context)


class TestTdSpanProcessorOnEnd:
    """Tests for TdSpanProcessor.on_end method."""

    def _create_mock_span(
        self,
        mocker,
        instrumentation_scope_name: str = TD_INSTRUMENTATION_LIBRARY_NAME,
        name: str = "test-span",
    ):
        """Create a mock ReadableSpan for testing."""
        mock_span = mocker.MagicMock(spec=ReadableSpan)
        mock_span.name = name
        mock_span.kind = OTelSpanKind.SERVER
        mock_span.start_time = 1700000000_000_000_000
        mock_span.end_time = 1700000001_000_000_000

        # Setup instrumentation scope
        mock_scope = mocker.MagicMock()
        mock_scope.name = instrumentation_scope_name
        mock_span.instrumentation_scope = mock_scope

        # Setup span context
        mock_context = mocker.MagicMock()
        mock_context.trace_id = 0x12345678
        mock_context.span_id = 0xABCDEF
        mock_span.context = mock_context

        mock_span.parent = None
        mock_span.attributes = {}

        mock_status = mocker.MagicMock()
        mock_status.status_code = 0  # UNSET
        mock_status.description = ""
        mock_span.status = mock_status

        return mock_span

    def test_skips_when_not_started(self, mocker):
        """Should skip processing when processor not started."""
        mock_exporter = mocker.MagicMock()
        processor = TdSpanProcessor(
            exporter=mock_exporter,
            mode=TuskDriftMode.RECORD,
        )

        mock_span = self._create_mock_span(mocker)

        # Should not raise
        processor.on_end(mock_span)

    def test_skips_when_mode_disabled(self, mocker):
        """Should skip processing when mode is DISABLED."""
        mock_exporter = mocker.MagicMock()
        processor = TdSpanProcessor(
            exporter=mock_exporter,
            mode=TuskDriftMode.DISABLED,
        )
        processor._started = True
        processor._batch_processor = mocker.MagicMock()

        mock_span = self._create_mock_span(mocker)

        processor.on_end(mock_span)

        # Batch processor should not receive any spans
        processor._batch_processor.add_span.assert_not_called()

    def test_skips_non_drift_spans(self, mocker):
        """Should skip spans not from Drift SDK."""
        mock_exporter = mocker.MagicMock()
        processor = TdSpanProcessor(
            exporter=mock_exporter,
            mode=TuskDriftMode.RECORD,
        )
        processor._started = True
        processor._batch_processor = mocker.MagicMock()

        # Create span with different instrumentation scope
        mock_span = self._create_mock_span(mocker, instrumentation_scope_name="other-library")

        processor.on_end(mock_span)

        processor._batch_processor.add_span.assert_not_called()

    def test_skips_span_with_no_instrumentation_scope(self, mocker):
        """Should skip spans with no instrumentation scope."""
        mock_exporter = mocker.MagicMock()
        processor = TdSpanProcessor(
            exporter=mock_exporter,
            mode=TuskDriftMode.RECORD,
        )
        processor._started = True
        processor._batch_processor = mocker.MagicMock()

        mock_span = self._create_mock_span(mocker)
        mock_span.instrumentation_scope = None

        processor.on_end(mock_span)

        processor._batch_processor.add_span.assert_not_called()

    def test_processes_drift_span_in_record_mode(self, mocker):
        """Should process Drift SDK spans in RECORD mode."""
        mock_exporter = mocker.MagicMock()
        processor = TdSpanProcessor(
            exporter=mock_exporter,
            mode=TuskDriftMode.RECORD,
        )
        processor._started = True
        processor._batch_processor = mocker.MagicMock()

        mock_blocking = mocker.patch("drift.core.tracing.td_span_processor.TraceBlockingManager")
        mock_blocking_instance = mocker.MagicMock()
        mock_blocking_instance.is_trace_blocked.return_value = False
        mock_blocking.get_instance.return_value = mock_blocking_instance

        mock_converter = mocker.patch("drift.core.tracing.td_span_processor.otel_span_to_clean_span_data")
        mock_clean_span = mocker.MagicMock()
        mock_clean_span.trace_id = "a" * 32
        mock_clean_span.name = "test-span"
        mock_clean_span.kind = SpanKind.CLIENT  # Not SERVER
        mock_clean_span.status.code = StatusCode.OK
        mock_clean_span.to_proto.return_value = mocker.MagicMock()
        mock_converter.return_value = mock_clean_span

        mock_span = self._create_mock_span(mocker)

        mocker.patch("drift.core.tracing.td_span_processor.should_block_span", return_value=False)
        processor.on_end(mock_span)

        processor._batch_processor.add_span.assert_called_once_with(mock_clean_span)

    def test_skips_span_already_exported_by_instrumentation(self, mocker):
        """Should skip spans with EXPORTED_BY_INSTRUMENTATION attribute set.

        Framework middlewares (e.g., Django _capture_span) export a full
        CleanSpanData with HTTP body data via sdk.collect_span() and then
        set this attribute. Processing the span again in on_end() would
        produce a duplicate empty root span.
        """
        from drift.core.tracing.td_attributes import TdSpanAttributes

        mock_exporter = mocker.MagicMock()
        processor = TdSpanProcessor(
            exporter=mock_exporter,
            mode=TuskDriftMode.RECORD,
        )
        processor._started = True
        processor._batch_processor = mocker.MagicMock()

        mock_span = self._create_mock_span(mocker)
        mock_span.attributes = {
            TdSpanAttributes.EXPORTED_BY_INSTRUMENTATION: True,
        }

        mock_converter = mocker.patch("drift.core.tracing.td_span_processor.otel_span_to_clean_span_data")

        processor.on_end(mock_span)

        # Should not convert or add to batch — span was already exported
        mock_converter.assert_not_called()
        processor._batch_processor.add_span.assert_not_called()

    def test_processes_span_without_exported_flag(self, mocker):
        """Should process spans that don't have EXPORTED_BY_INSTRUMENTATION set.

        Child spans (redis, psycopg2, etc.) and framework spans that use
        the OTel-only export path should still be processed normally.
        """
        mock_exporter = mocker.MagicMock()
        processor = TdSpanProcessor(
            exporter=mock_exporter,
            mode=TuskDriftMode.RECORD,
        )
        processor._started = True
        processor._batch_processor = mocker.MagicMock()

        mock_blocking = mocker.patch("drift.core.tracing.td_span_processor.TraceBlockingManager")
        mock_blocking_instance = mocker.MagicMock()
        mock_blocking_instance.is_trace_blocked.return_value = False
        mock_blocking.get_instance.return_value = mock_blocking_instance

        mock_converter = mocker.patch("drift.core.tracing.td_span_processor.otel_span_to_clean_span_data")
        mock_clean_span = mocker.MagicMock()
        mock_clean_span.trace_id = "a" * 32
        mock_clean_span.name = "redis.MGET"
        mock_clean_span.kind = SpanKind.CLIENT
        mock_clean_span.status.code = StatusCode.OK
        mock_clean_span.to_proto.return_value = mocker.MagicMock()
        mock_converter.return_value = mock_clean_span

        mock_span = self._create_mock_span(mocker, name="redis.MGET")
        # No EXPORTED_BY_INSTRUMENTATION attribute — should process normally
        mock_span.attributes = {}

        mocker.patch("drift.core.tracing.td_span_processor.should_block_span", return_value=False)
        processor.on_end(mock_span)

        mock_converter.assert_called_once()
        processor._batch_processor.add_span.assert_called_once_with(mock_clean_span)

    def test_skips_blocked_trace(self, mocker):
        """Should skip processing when trace is blocked."""
        mock_exporter = mocker.MagicMock()
        processor = TdSpanProcessor(
            exporter=mock_exporter,
            mode=TuskDriftMode.RECORD,
        )
        processor._started = True
        processor._batch_processor = mocker.MagicMock()

        mock_blocking = mocker.patch("drift.core.tracing.td_span_processor.TraceBlockingManager")
        mock_blocking_instance = mocker.MagicMock()
        mock_blocking_instance.is_trace_blocked.return_value = True
        mock_blocking.get_instance.return_value = mock_blocking_instance

        mock_converter = mocker.patch("drift.core.tracing.td_span_processor.otel_span_to_clean_span_data")
        mock_clean_span = mocker.MagicMock()
        mock_clean_span.trace_id = "blocked_trace"
        mock_converter.return_value = mock_clean_span

        mock_span = self._create_mock_span(mocker)

        processor.on_end(mock_span)

        processor._batch_processor.add_span.assert_not_called()

    def test_skips_span_that_should_be_blocked(self, mocker):
        """Should skip span when should_block_span returns True."""
        mock_exporter = mocker.MagicMock()
        processor = TdSpanProcessor(
            exporter=mock_exporter,
            mode=TuskDriftMode.RECORD,
        )
        processor._started = True
        processor._batch_processor = mocker.MagicMock()

        mock_blocking = mocker.patch("drift.core.tracing.td_span_processor.TraceBlockingManager")
        mock_blocking_instance = mocker.MagicMock()
        mock_blocking_instance.is_trace_blocked.return_value = False
        mock_blocking.get_instance.return_value = mock_blocking_instance

        mock_converter = mocker.patch("drift.core.tracing.td_span_processor.otel_span_to_clean_span_data")
        mock_clean_span = mocker.MagicMock()
        mock_clean_span.trace_id = "a" * 32
        mock_converter.return_value = mock_clean_span

        mock_span = self._create_mock_span(mocker)

        mocker.patch("drift.core.tracing.td_span_processor.should_block_span", return_value=True)
        processor.on_end(mock_span)

        processor._batch_processor.add_span.assert_not_called()

    def test_skips_span_with_proto_serialization_error(self, mocker):
        """Should skip span when protobuf serialization fails."""
        mock_exporter = mocker.MagicMock()
        processor = TdSpanProcessor(
            exporter=mock_exporter,
            mode=TuskDriftMode.RECORD,
        )
        processor._started = True
        processor._batch_processor = mocker.MagicMock()

        mock_blocking = mocker.patch("drift.core.tracing.td_span_processor.TraceBlockingManager")
        mock_blocking_instance = mocker.MagicMock()
        mock_blocking_instance.is_trace_blocked.return_value = False
        mock_blocking.get_instance.return_value = mock_blocking_instance

        mock_converter = mocker.patch("drift.core.tracing.td_span_processor.otel_span_to_clean_span_data")
        mock_clean_span = mocker.MagicMock()
        mock_clean_span.trace_id = "a" * 32
        mock_clean_span.kind = SpanKind.CLIENT
        mock_clean_span.to_proto.side_effect = Exception("Serialization error")
        mock_converter.return_value = mock_clean_span

        mock_span = self._create_mock_span(mocker)

        mocker.patch("drift.core.tracing.td_span_processor.should_block_span", return_value=False)
        processor.on_end(mock_span)

        processor._batch_processor.add_span.assert_not_called()


class TestTdSpanProcessorShutdown:
    """Tests for TdSpanProcessor.shutdown method."""

    def test_shutdown_stops_batch_processor(self, mocker):
        """Should stop batch processor on shutdown."""
        mock_exporter = mocker.MagicMock()
        processor = TdSpanProcessor(
            exporter=mock_exporter,
            mode=TuskDriftMode.RECORD,
        )
        processor._started = True
        processor._batch_processor = mocker.MagicMock()

        processor.shutdown()

        processor._batch_processor.stop.assert_called_once_with(timeout=30.0)
        assert processor._started is False

    def test_shutdown_is_safe_when_not_started(self, mocker):
        """Should be safe to call shutdown when not started."""
        mock_exporter = mocker.MagicMock()
        processor = TdSpanProcessor(
            exporter=mock_exporter,
            mode=TuskDriftMode.RECORD,
        )

        # Should not raise
        processor.shutdown()


class TestTdSpanProcessorForceFlush:
    """Tests for TdSpanProcessor.force_flush method."""

    def test_force_flush_delegates_to_batch_processor(self, mocker):
        """Should delegate to batch processor's force flush."""
        mock_exporter = mocker.MagicMock()
        processor = TdSpanProcessor(
            exporter=mock_exporter,
            mode=TuskDriftMode.RECORD,
        )
        processor._started = True
        processor._batch_processor = mocker.MagicMock()

        result = processor.force_flush()

        processor._batch_processor._force_flush.assert_called_once()
        assert result is True

    def test_force_flush_returns_true_when_not_started(self, mocker):
        """Should return True when not started."""
        mock_exporter = mocker.MagicMock()
        processor = TdSpanProcessor(
            exporter=mock_exporter,
            mode=TuskDriftMode.RECORD,
        )

        result = processor.force_flush()

        assert result is True

    def test_force_flush_handles_exception(self, mocker):
        """Should return False on exception."""
        mock_exporter = mocker.MagicMock()
        processor = TdSpanProcessor(
            exporter=mock_exporter,
            mode=TuskDriftMode.RECORD,
        )
        processor._started = True
        processor._batch_processor = mocker.MagicMock()
        processor._batch_processor._force_flush.side_effect = Exception("Error")

        result = processor.force_flush()

        assert result is False


class TestTdSpanProcessorUpdateMethods:
    """Tests for TdSpanProcessor update methods."""

    def test_update_app_ready(self, mocker):
        """Should update app_ready flag."""
        mock_exporter = mocker.MagicMock()
        processor = TdSpanProcessor(
            exporter=mock_exporter,
            mode=TuskDriftMode.RECORD,
        )

        assert processor._app_ready is False

        processor.update_app_ready(True)

        assert processor._app_ready is True

    def test_update_sampling_rate(self, mocker):
        """Should update sampling rate."""
        mock_exporter = mocker.MagicMock()
        processor = TdSpanProcessor(
            exporter=mock_exporter,
            mode=TuskDriftMode.RECORD,
        )

        assert processor._sampling_rate == 1.0

        processor.update_sampling_rate(0.5)

        assert processor._sampling_rate == 0.5
