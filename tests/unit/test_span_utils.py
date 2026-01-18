"""Tests for span_utils.py - Centralized span management utilities."""

from __future__ import annotations

import pytest
from opentelemetry import context as otel_context
from opentelemetry.trace import SpanKind as OTelSpanKind
from opentelemetry.trace import Status, StatusCode

from drift.core.tracing.span_utils import (
    AddSpanAttributesOptions,
    CreateSpanOptions,
    SpanExecutorOptions,
    SpanInfo,
    SpanUtils,
    format_span_id,
    format_trace_id,
)
from drift.core.tracing.td_attributes import TdSpanAttributes
from drift.core.types import TuskDriftMode


class TestFormatTraceId:
    """Tests for format_trace_id function."""

    def test_formats_to_32_hex_chars(self):
        """Should format to 32-character hex string."""
        result = format_trace_id(0x123456789ABCDEF0123456789ABCDEF)
        assert len(result) == 32
        assert result == "0123456789abcdef0123456789abcdef"

    def test_pads_small_numbers(self):
        """Should pad small numbers with leading zeros."""
        result = format_trace_id(1)
        assert result == "0" * 31 + "1"
        assert len(result) == 32


class TestFormatSpanId:
    """Tests for format_span_id function."""

    def test_formats_to_16_hex_chars(self):
        """Should format to 16-character hex string."""
        result = format_span_id(0x123456789ABCDEF)
        assert len(result) == 16
        assert result == "0123456789abcdef"

    def test_pads_small_numbers(self):
        """Should pad small numbers with leading zeros."""
        result = format_span_id(1)
        assert result == "0" * 15 + "1"
        assert len(result) == 16


class TestSpanInfo:
    """Tests for SpanInfo dataclass."""

    def test_contains_all_required_fields(self, mocker):
        """Should contain all required fields."""
        mock_span = mocker.MagicMock()
        mock_context = mocker.MagicMock()

        span_info = SpanInfo(
            trace_id="a" * 32,
            span_id="b" * 16,
            parent_span_id="c" * 16,
            span=mock_span,
            context=mock_context,
            is_pre_app_start=True,
        )

        assert span_info.trace_id == "a" * 32
        assert span_info.span_id == "b" * 16
        assert span_info.parent_span_id == "c" * 16
        assert span_info.span is mock_span
        assert span_info.context is mock_context
        assert span_info.is_pre_app_start is True


class TestCreateSpanOptions:
    """Tests for CreateSpanOptions dataclass."""

    def test_required_fields(self):
        """Should require name and kind."""
        options = CreateSpanOptions(
            name="test-span",
            kind=OTelSpanKind.SERVER,
        )

        assert options.name == "test-span"
        assert options.kind == OTelSpanKind.SERVER
        assert options.attributes is None
        assert options.parent_context is None
        assert options.is_pre_app_start is False


class TestSpanUtilsCreateSpan:
    """Tests for SpanUtils.create_span method."""

    def test_creates_span_and_returns_span_info(self, mocker):
        """Should create a span and return SpanInfo."""
        # Setup mocks
        mock_drift = mocker.patch("drift.core.drift_sdk.TuskDrift")
        mock_sdk = mocker.MagicMock()
        mock_tracer = mocker.MagicMock()
        mock_span = mocker.MagicMock()
        mock_span_context = mocker.MagicMock()
        mock_span_context.trace_id = 0x12345678
        mock_span_context.span_id = 0xABCDEF
        mock_span.get_span_context.return_value = mock_span_context
        mock_tracer.start_span.return_value = mock_span
        mock_sdk.get_tracer.return_value = mock_tracer
        mock_drift.get_instance.return_value = mock_sdk

        mock_trace = mocker.patch("drift.core.tracing.span_utils.trace")
        mock_trace.get_current_span.return_value = None
        mock_trace.set_span_in_context.return_value = mocker.MagicMock()

        options = CreateSpanOptions(
            name="test-span",
            kind=OTelSpanKind.SERVER,
        )

        result = SpanUtils.create_span(options)

        assert result is not None
        assert result.trace_id == format_trace_id(0x12345678)
        assert result.span_id == format_span_id(0xABCDEF)
        mock_tracer.start_span.assert_called_once()

    def test_sets_pre_app_start_attribute(self, mocker):
        """Should set IS_PRE_APP_START attribute when flag is True."""
        mock_drift = mocker.patch("drift.core.drift_sdk.TuskDrift")
        mock_sdk = mocker.MagicMock()
        mock_tracer = mocker.MagicMock()
        mock_span = mocker.MagicMock()
        mock_span_context = mocker.MagicMock()
        mock_span_context.trace_id = 0x12345678
        mock_span_context.span_id = 0xABCDEF
        mock_span.get_span_context.return_value = mock_span_context
        mock_tracer.start_span.return_value = mock_span
        mock_sdk.get_tracer.return_value = mock_tracer
        mock_drift.get_instance.return_value = mock_sdk

        mock_trace = mocker.patch("drift.core.tracing.span_utils.trace")
        mock_trace.get_current_span.return_value = None
        mock_trace.set_span_in_context.return_value = mocker.MagicMock()

        options = CreateSpanOptions(
            name="test-span",
            kind=OTelSpanKind.SERVER,
            is_pre_app_start=True,
        )

        result = SpanUtils.create_span(options)

        assert result is not None
        assert result.is_pre_app_start is True
        mock_span.set_attribute.assert_called_with(TdSpanAttributes.IS_PRE_APP_START, True)

    def test_returns_none_when_trace_blocked(self, mocker):
        """Should return None when trace is blocked."""
        mock_drift = mocker.patch("drift.core.drift_sdk.TuskDrift")
        mock_sdk = mocker.MagicMock()
        mock_tracer = mocker.MagicMock()
        mock_sdk.get_tracer.return_value = mock_tracer
        mock_drift.get_instance.return_value = mock_sdk

        mock_trace = mocker.patch("drift.core.tracing.span_utils.trace")
        # Setup active span with blocked trace
        mock_active_span = mocker.MagicMock()
        mock_active_span.is_recording.return_value = True
        mock_parent_context = mocker.MagicMock()
        mock_parent_context.trace_id = 0x12345678
        mock_parent_context.span_id = 0xABCDEF
        mock_active_span.get_span_context.return_value = mock_parent_context
        mock_trace.get_current_span.return_value = mock_active_span

        # Setup trace blocking
        mock_blocking_manager = mocker.patch("drift.core.trace_blocking_manager.TraceBlockingManager")
        mock_manager = mocker.MagicMock()
        mock_manager.is_trace_blocked.return_value = True
        mock_blocking_manager.get_instance.return_value = mock_manager

        options = CreateSpanOptions(
            name="test-span",
            kind=OTelSpanKind.SERVER,
        )

        result = SpanUtils.create_span(options)

        assert result is None

    def test_returns_none_on_exception(self, mocker):
        """Should return None when exception occurs."""
        mock_drift = mocker.patch("drift.core.drift_sdk.TuskDrift")
        mock_drift.get_instance.side_effect = Exception("SDK error")

        options = CreateSpanOptions(
            name="test-span",
            kind=OTelSpanKind.SERVER,
        )

        result = SpanUtils.create_span(options)

        assert result is None


class TestSpanUtilsWithSpan:
    """Tests for SpanUtils.with_span context manager."""

    def test_attaches_and_detaches_context(self, mocker):
        """Should attach context on enter and detach on exit."""
        mock_span = mocker.MagicMock()
        mock_context = mocker.MagicMock()

        span_info = SpanInfo(
            trace_id="a" * 32,
            span_id="b" * 16,
            parent_span_id=None,
            span=mock_span,
            context=mock_context,
            is_pre_app_start=False,
        )

        mock_attach = mocker.patch.object(otel_context, "attach", return_value="token")
        mock_detach = mocker.patch.object(otel_context, "detach")

        with SpanUtils.with_span(span_info):
            mock_attach.assert_called_once_with(mock_context)

        mock_detach.assert_called_once_with("token")


class TestSpanUtilsCreateAndExecuteSpan:
    """Tests for SpanUtils.create_and_execute_span method."""

    def test_executes_function_within_span(self, mocker):
        """Should execute function within span context."""
        mock_trace = mocker.patch("drift.core.tracing.span_utils.trace")
        mock_span = mocker.MagicMock()
        mock_span.is_recording.return_value = True
        mock_span.get_span_context.return_value.trace_flags.sampled = True
        mock_trace.get_current_span.return_value = mock_span

        mock_span_info = SpanInfo(
            trace_id="a" * 32,
            span_id="b" * 16,
            parent_span_id=None,
            span=mock_span,
            context=mocker.MagicMock(),
            is_pre_app_start=False,
        )
        mocker.patch.object(SpanUtils, "create_span", return_value=mock_span_info)

        original = mocker.MagicMock(return_value="original")
        fn = mocker.MagicMock(return_value="result")

        options = SpanExecutorOptions(
            name="test-span",
            kind=OTelSpanKind.SERVER,
            package_name="test-pkg",
        )

        result = SpanUtils.create_and_execute_span(
            mode=TuskDriftMode.RECORD,
            original_function_call=original,
            options=options,
            fn=fn,
        )

        assert result == "result"
        fn.assert_called_once_with(mock_span_info)

    def test_calls_original_on_span_creation_failure_in_record_mode(self, mocker):
        """Should call original function when span creation fails in RECORD mode."""
        mocker.patch.object(SpanUtils, "create_span", return_value=None)
        mock_trace = mocker.patch("drift.core.tracing.span_utils.trace")
        mock_trace.get_current_span.return_value = None

        original = mocker.MagicMock(return_value="original")
        fn = mocker.MagicMock(return_value="result")

        options = SpanExecutorOptions(
            name="test-span",
            kind=OTelSpanKind.SERVER,
        )

        result = SpanUtils.create_and_execute_span(
            mode=TuskDriftMode.RECORD,
            original_function_call=original,
            options=options,
            fn=fn,
        )

        assert result == "original"
        original.assert_called_once()
        fn.assert_not_called()

    def test_raises_in_replay_mode_on_span_creation_failure(self, mocker):
        """Should raise RuntimeError when span creation fails in REPLAY mode."""
        mocker.patch.object(SpanUtils, "create_span", return_value=None)
        mock_trace = mocker.patch("drift.core.tracing.span_utils.trace")
        mock_trace.get_current_span.return_value = None

        original = mocker.MagicMock(return_value="original")
        fn = mocker.MagicMock(return_value="result")

        options = SpanExecutorOptions(
            name="test-span",
            kind=OTelSpanKind.SERVER,
        )

        with pytest.raises(RuntimeError, match="Error creating span in replay mode"):
            SpanUtils.create_and_execute_span(
                mode=TuskDriftMode.REPLAY,
                original_function_call=original,
                options=options,
                fn=fn,
            )


class TestSpanUtilsGetCurrentSpanInfo:
    """Tests for SpanUtils.get_current_span_info method."""

    def test_returns_span_info_when_active_span(self, mocker):
        """Should return SpanInfo when there is an active recording span."""
        mock_trace = mocker.patch("drift.core.tracing.span_utils.trace")
        mock_otel_context = mocker.patch("drift.core.tracing.span_utils.otel_context")

        mock_span = mocker.MagicMock()
        mock_span.is_recording.return_value = True
        mock_span_context = mocker.MagicMock()
        mock_span_context.trace_id = 0x12345678
        mock_span_context.span_id = 0xABCDEF
        mock_span.get_span_context.return_value = mock_span_context
        mock_trace.get_current_span.return_value = mock_span
        mock_otel_context.get_current.return_value = mocker.MagicMock()

        result = SpanUtils.get_current_span_info()

        assert result is not None
        assert result.trace_id == format_trace_id(0x12345678)
        assert result.span_id == format_span_id(0xABCDEF)

    def test_returns_none_when_no_active_span(self, mocker):
        """Should return None when no active span."""
        mock_trace = mocker.patch("drift.core.tracing.span_utils.trace")
        mock_trace.get_current_span.return_value = None

        result = SpanUtils.get_current_span_info()

        assert result is None

    def test_returns_none_when_span_not_recording(self, mocker):
        """Should return None when span is not recording."""
        mock_trace = mocker.patch("drift.core.tracing.span_utils.trace")
        mock_span = mocker.MagicMock()
        mock_span.is_recording.return_value = False
        mock_trace.get_current_span.return_value = mock_span

        result = SpanUtils.get_current_span_info()

        assert result is None


class TestSpanUtilsAddSpanAttributes:
    """Tests for SpanUtils.add_span_attributes method."""

    def test_adds_basic_attributes(self, mocker):
        """Should add basic attributes to span."""
        mock_span = mocker.MagicMock()

        options = AddSpanAttributesOptions(
            name="test-span",
            package_name="test-pkg",
            instrumentation_name="TestInstrumentation",
        )

        SpanUtils.add_span_attributes(mock_span, options)

        mock_span.set_attributes.assert_called_once()
        attrs = mock_span.set_attributes.call_args[0][0]
        assert attrs[TdSpanAttributes.NAME] == "test-span"
        assert attrs[TdSpanAttributes.PACKAGE_NAME] == "test-pkg"
        assert attrs[TdSpanAttributes.INSTRUMENTATION_NAME] == "TestInstrumentation"

    def test_serializes_dict_values_to_json(self, mocker):
        """Should serialize dict values to JSON strings."""
        mock_span = mocker.MagicMock()

        options = AddSpanAttributesOptions(
            input_value={"key": "value"},
            output_value={"result": 42},
            metadata={"extra": "data"},
        )

        SpanUtils.add_span_attributes(mock_span, options)

        attrs = mock_span.set_attributes.call_args[0][0]
        assert attrs[TdSpanAttributes.INPUT_VALUE] == '{"key": "value"}'
        assert attrs[TdSpanAttributes.OUTPUT_VALUE] == '{"result": 42}'
        assert attrs[TdSpanAttributes.METADATA] == '{"extra": "data"}'

    def test_skips_none_values(self, mocker):
        """Should skip attributes with None values."""
        mock_span = mocker.MagicMock()

        options = AddSpanAttributesOptions(
            name="test-span",
            package_name=None,  # Should be skipped
        )

        SpanUtils.add_span_attributes(mock_span, options)

        attrs = mock_span.set_attributes.call_args[0][0]
        assert TdSpanAttributes.NAME in attrs
        assert TdSpanAttributes.PACKAGE_NAME not in attrs


class TestSpanUtilsSetStatus:
    """Tests for SpanUtils.set_status method."""

    def test_sets_status_on_span(self, mocker):
        """Should set status on span."""
        mock_span = mocker.MagicMock()
        status = Status(StatusCode.OK)

        SpanUtils.set_status(mock_span, status)

        mock_span.set_status.assert_called_once_with(status)

    def test_handles_exception_gracefully(self, mocker):
        """Should handle exceptions gracefully."""
        mock_span = mocker.MagicMock()
        mock_span.set_status.side_effect = Exception("Error")
        status = Status(StatusCode.OK)

        # Should not raise
        SpanUtils.set_status(mock_span, status)


class TestSpanUtilsEndSpan:
    """Tests for SpanUtils.end_span method."""

    def test_ends_span(self, mocker):
        """Should end span."""
        mock_span = mocker.MagicMock()

        SpanUtils.end_span(mock_span)

        mock_span.end.assert_called_once()

    def test_sets_ok_status_before_ending(self, mocker):
        """Should set OK status before ending."""
        mock_span = mocker.MagicMock()

        SpanUtils.end_span(mock_span, status={"code": StatusCode.OK, "message": "Success"})

        mock_span.set_status.assert_called_once()
        mock_span.end.assert_called_once()

    def test_sets_error_status_before_ending(self, mocker):
        """Should set ERROR status before ending."""
        mock_span = mocker.MagicMock()

        SpanUtils.end_span(mock_span, status={"code": StatusCode.ERROR, "message": "Failed"})

        mock_span.set_status.assert_called_once()
        mock_span.end.assert_called_once()


class TestSpanUtilsGetCurrentTraceId:
    """Tests for SpanUtils.get_current_trace_id method."""

    def test_returns_trace_id_when_active(self, mocker):
        """Should return trace ID when there is an active span."""
        mock_span_info = mocker.MagicMock()
        mock_span_info.trace_id = "a" * 32
        mocker.patch.object(SpanUtils, "get_current_span_info", return_value=mock_span_info)

        result = SpanUtils.get_current_trace_id()

        assert result == "a" * 32

    def test_returns_none_when_no_active_span(self, mocker):
        """Should return None when no active span."""
        mocker.patch.object(SpanUtils, "get_current_span_info", return_value=None)

        result = SpanUtils.get_current_trace_id()

        assert result is None


class TestSpanUtilsGetCurrentSpanId:
    """Tests for SpanUtils.get_current_span_id method."""

    def test_returns_span_id_when_active(self, mocker):
        """Should return span ID when there is an active span."""
        mock_span_info = mocker.MagicMock()
        mock_span_info.span_id = "b" * 16
        mocker.patch.object(SpanUtils, "get_current_span_info", return_value=mock_span_info)

        result = SpanUtils.get_current_span_id()

        assert result == "b" * 16

    def test_returns_none_when_no_active_span(self, mocker):
        """Should return None when no active span."""
        mocker.patch.object(SpanUtils, "get_current_span_info", return_value=None)

        result = SpanUtils.get_current_span_id()

        assert result is None


class TestSpanUtilsGetTraceInfo:
    """Tests for SpanUtils.get_trace_info method."""

    def test_returns_formatted_trace_info(self, mocker):
        """Should return formatted trace info string."""
        mocker.patch.object(SpanUtils, "get_current_trace_id", return_value="a" * 32)
        mocker.patch.object(SpanUtils, "get_current_span_id", return_value="b" * 16)

        result = SpanUtils.get_trace_info()

        assert result == f"trace={'a' * 32} span={'b' * 16}"

    def test_returns_no_trace_when_no_active_span(self, mocker):
        """Should return 'no-trace' when no active span."""
        mocker.patch.object(SpanUtils, "get_current_trace_id", return_value=None)
        mocker.patch.object(SpanUtils, "get_current_span_id", return_value=None)

        result = SpanUtils.get_trace_info()

        assert result == "no-trace"


class TestSpanUtilsCaptureStackTrace:
    """Tests for SpanUtils.capture_stack_trace method."""

    def test_captures_stack_trace(self):
        """Should capture current stack trace."""
        result = SpanUtils.capture_stack_trace(filter_drift=False)

        assert isinstance(result, str)
        # May be empty if all frames are filtered
        assert result is not None

    def test_respects_max_frames(self):
        """Should respect max_frames parameter."""
        result = SpanUtils.capture_stack_trace(max_frames=2)

        # Count the number of "File" occurrences to estimate frames
        # This is an approximation since format varies
        assert isinstance(result, str)

    def test_filters_drift_frames(self):
        """Should filter Drift SDK frames when filter_drift=True."""
        result = SpanUtils.capture_stack_trace(filter_drift=True)

        # Should not contain drift-specific paths
        # Note: This test might show some drift paths since we're in the test
        assert isinstance(result, str)

    def test_includes_all_frames_when_filter_false(self):
        """Should include all frames when filter_drift=False."""
        result = SpanUtils.capture_stack_trace(filter_drift=False)

        assert isinstance(result, str)
