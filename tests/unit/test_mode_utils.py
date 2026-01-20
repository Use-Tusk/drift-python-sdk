"""Tests for mode_utils.py - Mode handler utilities for RECORD and REPLAY modes."""

from __future__ import annotations

from opentelemetry.trace import SpanKind as OTelSpanKind

from drift.core.mode_utils import (
    handle_record_mode,
    handle_replay_mode,
    is_background_request,
    should_record_inbound_http_request,
)


class TestHandleRecordMode:
    """Tests for handle_record_mode function."""

    def test_calls_record_handler_when_app_not_ready(self, mocker):
        """Should call record_mode_handler with is_pre_app_start=True when app not ready."""
        original_call = mocker.MagicMock(return_value="original")
        record_handler = mocker.MagicMock(return_value="recorded")

        mock_drift = mocker.patch("drift.core.drift_sdk.TuskDrift")
        mock_sdk = mocker.MagicMock()
        mock_sdk.is_app_ready.return_value = False
        mock_drift.get_instance.return_value = mock_sdk

        mock_span_utils = mocker.patch("drift.core.tracing.span_utils.SpanUtils")
        mock_span_utils.get_current_span_info.return_value = None

        result = handle_record_mode(
            original_function_call=original_call,
            record_mode_handler=record_handler,
            span_kind=OTelSpanKind.CLIENT,
        )

        assert result == "recorded"
        record_handler.assert_called_once_with(True)
        original_call.assert_not_called()

    def test_calls_record_handler_when_app_ready_with_span_context(self, mocker):
        """Should call record_mode_handler with is_pre_app_start=False when app ready and has context."""
        original_call = mocker.MagicMock(return_value="original")
        record_handler = mocker.MagicMock(return_value="recorded")

        mock_drift = mocker.patch("drift.core.drift_sdk.TuskDrift")
        mock_sdk = mocker.MagicMock()
        mock_sdk.is_app_ready.return_value = True
        mock_drift.get_instance.return_value = mock_sdk

        mock_span_utils = mocker.patch("drift.core.tracing.span_utils.SpanUtils")
        mock_span_info = mocker.MagicMock()
        mock_span_info.is_pre_app_start = False
        mock_span_utils.get_current_span_info.return_value = mock_span_info

        result = handle_record_mode(
            original_function_call=original_call,
            record_mode_handler=record_handler,
            span_kind=OTelSpanKind.CLIENT,
        )

        assert result == "recorded"
        record_handler.assert_called_once_with(False)

    def test_calls_original_when_no_span_context_and_not_server(self, mocker):
        """Should call original function when no span context and not SERVER span."""
        original_call = mocker.MagicMock(return_value="original")
        record_handler = mocker.MagicMock(return_value="recorded")

        mock_drift = mocker.patch("drift.core.drift_sdk.TuskDrift")
        mock_sdk = mocker.MagicMock()
        mock_sdk.is_app_ready.return_value = True
        mock_drift.get_instance.return_value = mock_sdk

        mock_span_utils = mocker.patch("drift.core.tracing.span_utils.SpanUtils")
        mock_span_utils.get_current_span_info.return_value = None

        result = handle_record_mode(
            original_function_call=original_call,
            record_mode_handler=record_handler,
            span_kind=OTelSpanKind.CLIENT,
        )

        assert result == "original"
        original_call.assert_called_once()
        record_handler.assert_not_called()

    def test_calls_record_handler_when_no_span_context_but_server_span(self, mocker):
        """Should call record_mode_handler when no span context but is SERVER span."""
        original_call = mocker.MagicMock(return_value="original")
        record_handler = mocker.MagicMock(return_value="recorded")

        mock_drift = mocker.patch("drift.core.drift_sdk.TuskDrift")
        mock_sdk = mocker.MagicMock()
        mock_sdk.is_app_ready.return_value = True
        mock_drift.get_instance.return_value = mock_sdk

        mock_span_utils = mocker.patch("drift.core.tracing.span_utils.SpanUtils")
        mock_span_utils.get_current_span_info.return_value = None

        result = handle_record_mode(
            original_function_call=original_call,
            record_mode_handler=record_handler,
            span_kind=OTelSpanKind.SERVER,
        )

        assert result == "recorded"
        record_handler.assert_called_once_with(False)

    def test_calls_original_when_within_pre_app_start_span(self, mocker):
        """Should call original function when within a pre-app-start span."""
        original_call = mocker.MagicMock(return_value="original")
        record_handler = mocker.MagicMock(return_value="recorded")

        mock_drift = mocker.patch("drift.core.drift_sdk.TuskDrift")
        mock_sdk = mocker.MagicMock()
        mock_sdk.is_app_ready.return_value = True
        mock_drift.get_instance.return_value = mock_sdk

        mock_span_utils = mocker.patch("drift.core.tracing.span_utils.SpanUtils")
        mock_span_info = mocker.MagicMock()
        mock_span_info.is_pre_app_start = True  # Pre-app-start span
        mock_span_utils.get_current_span_info.return_value = mock_span_info

        result = handle_record_mode(
            original_function_call=original_call,
            record_mode_handler=record_handler,
            span_kind=OTelSpanKind.CLIENT,
        )

        assert result == "original"
        original_call.assert_called_once()

    def test_handles_exception_gracefully(self, mocker):
        """Should call original function when exception occurs."""
        original_call = mocker.MagicMock(return_value="original")
        record_handler = mocker.MagicMock(return_value="recorded")

        mock_drift = mocker.patch("drift.core.drift_sdk.TuskDrift")
        mock_drift.get_instance.side_effect = Exception("SDK error")

        result = handle_record_mode(
            original_function_call=original_call,
            record_mode_handler=record_handler,
            span_kind=OTelSpanKind.CLIENT,
        )

        assert result == "original"
        original_call.assert_called_once()


class TestHandleReplayMode:
    """Tests for handle_replay_mode function."""

    def test_calls_noop_for_background_request(self, mocker):
        """Should call no_op_request_handler for background requests."""
        replay_handler = mocker.MagicMock(return_value="replayed")
        noop_handler = mocker.MagicMock(return_value="noop")

        mock_drift = mocker.patch("drift.core.drift_sdk.TuskDrift")
        mock_sdk = mocker.MagicMock()
        mock_sdk.is_app_ready.return_value = True
        mock_drift.get_instance.return_value = mock_sdk

        mock_span_utils = mocker.patch("drift.core.tracing.span_utils.SpanUtils")
        mock_span_utils.get_current_span_info.return_value = None

        result = handle_replay_mode(
            replay_mode_handler=replay_handler,
            no_op_request_handler=noop_handler,
            is_server_request=False,
        )

        assert result == "noop"
        noop_handler.assert_called_once()
        replay_handler.assert_not_called()

    def test_calls_replay_handler_when_not_background_request(self, mocker):
        """Should call replay_mode_handler when not a background request."""
        replay_handler = mocker.MagicMock(return_value="replayed")
        noop_handler = mocker.MagicMock(return_value="noop")

        mock_drift = mocker.patch("drift.core.drift_sdk.TuskDrift")
        mock_sdk = mocker.MagicMock()
        mock_sdk.is_app_ready.return_value = True
        mock_drift.get_instance.return_value = mock_sdk

        mock_span_utils = mocker.patch("drift.core.tracing.span_utils.SpanUtils")
        mock_span_info = mocker.MagicMock()
        mock_span_utils.get_current_span_info.return_value = mock_span_info

        result = handle_replay_mode(
            replay_mode_handler=replay_handler,
            no_op_request_handler=noop_handler,
            is_server_request=False,
        )

        assert result == "replayed"
        replay_handler.assert_called_once()
        noop_handler.assert_not_called()

    def test_calls_replay_handler_for_server_request(self, mocker):
        """Should call replay_mode_handler for server requests even without context."""
        replay_handler = mocker.MagicMock(return_value="replayed")
        noop_handler = mocker.MagicMock(return_value="noop")

        mock_drift = mocker.patch("drift.core.drift_sdk.TuskDrift")
        mock_sdk = mocker.MagicMock()
        mock_sdk.is_app_ready.return_value = True
        mock_drift.get_instance.return_value = mock_sdk

        mock_span_utils = mocker.patch("drift.core.tracing.span_utils.SpanUtils")
        mock_span_utils.get_current_span_info.return_value = None

        result = handle_replay_mode(
            replay_mode_handler=replay_handler,
            no_op_request_handler=noop_handler,
            is_server_request=True,
        )

        assert result == "replayed"
        replay_handler.assert_called_once()

    def test_calls_replay_handler_when_app_not_ready(self, mocker):
        """Should call replay_mode_handler when app not ready."""
        replay_handler = mocker.MagicMock(return_value="replayed")
        noop_handler = mocker.MagicMock(return_value="noop")

        mock_drift = mocker.patch("drift.core.drift_sdk.TuskDrift")
        mock_sdk = mocker.MagicMock()
        mock_sdk.is_app_ready.return_value = False
        mock_drift.get_instance.return_value = mock_sdk

        mock_span_utils = mocker.patch("drift.core.tracing.span_utils.SpanUtils")
        mock_span_utils.get_current_span_info.return_value = None

        result = handle_replay_mode(
            replay_mode_handler=replay_handler,
            no_op_request_handler=noop_handler,
            is_server_request=False,
        )

        assert result == "replayed"
        replay_handler.assert_called_once()


class TestIsBackgroundRequest:
    """Tests for is_background_request function."""

    def test_returns_true_for_background_request(self, mocker):
        """Should return True for background requests."""
        mock_drift = mocker.patch("drift.core.drift_sdk.TuskDrift")
        mock_sdk = mocker.MagicMock()
        mock_sdk.is_app_ready.return_value = True
        mock_drift.get_instance.return_value = mock_sdk

        mock_span_utils = mocker.patch("drift.core.tracing.span_utils.SpanUtils")
        mock_span_utils.get_current_span_info.return_value = None

        result = is_background_request(is_server_request=False)

        assert result is True

    def test_returns_false_when_app_not_ready(self, mocker):
        """Should return False when app is not ready."""
        mock_drift = mocker.patch("drift.core.drift_sdk.TuskDrift")
        mock_sdk = mocker.MagicMock()
        mock_sdk.is_app_ready.return_value = False
        mock_drift.get_instance.return_value = mock_sdk

        mock_span_utils = mocker.patch("drift.core.tracing.span_utils.SpanUtils")
        mock_span_utils.get_current_span_info.return_value = None

        result = is_background_request(is_server_request=False)

        assert result is False

    def test_returns_false_when_has_span_context(self, mocker):
        """Should return False when there is span context."""
        mock_drift = mocker.patch("drift.core.drift_sdk.TuskDrift")
        mock_sdk = mocker.MagicMock()
        mock_sdk.is_app_ready.return_value = True
        mock_drift.get_instance.return_value = mock_sdk

        mock_span_utils = mocker.patch("drift.core.tracing.span_utils.SpanUtils")
        mock_span_utils.get_current_span_info.return_value = mocker.MagicMock()

        result = is_background_request(is_server_request=False)

        assert result is False

    def test_returns_false_for_server_request(self, mocker):
        """Should return False for server requests."""
        mock_drift = mocker.patch("drift.core.drift_sdk.TuskDrift")
        mock_sdk = mocker.MagicMock()
        mock_sdk.is_app_ready.return_value = True
        mock_drift.get_instance.return_value = mock_sdk

        mock_span_utils = mocker.patch("drift.core.tracing.span_utils.SpanUtils")
        mock_span_utils.get_current_span_info.return_value = None

        result = is_background_request(is_server_request=True)

        assert result is False


class TestShouldRecordInboundHttpRequest:
    """Tests for should_record_inbound_http_request function."""

    def test_returns_true_when_no_drop_and_sampled(self, mocker):
        """Should return (True, None) when not dropped and sampled."""
        mock_drift = mocker.patch("drift.core.drift_sdk.TuskDrift")
        mock_sdk = mocker.MagicMock()
        mock_sdk.get_sampling_rate.return_value = 1.0
        mock_drift.get_instance.return_value = mock_sdk

        mocker.patch("drift.core.sampling.should_sample", return_value=True)

        result, reason = should_record_inbound_http_request(
            method="GET",
            target="/api/users",
            headers={},
            transform_engine=None,
            is_pre_app_start=False,
        )

        assert result is True
        assert reason is None

    def test_returns_true_when_pre_app_start(self):
        """Should always return True during pre-app-start (no sampling)."""
        # Even if sampling would say no, pre-app-start should always record
        result, reason = should_record_inbound_http_request(
            method="GET",
            target="/api/users",
            headers={},
            transform_engine=None,
            is_pre_app_start=True,
        )

        assert result is True
        assert reason is None

    def test_returns_false_when_dropped_by_transform(self, mocker):
        """Should return (False, 'dropped') when transform engine drops request."""
        mock_transform = mocker.MagicMock()
        mock_transform.should_drop_inbound_request.return_value = True

        result, reason = should_record_inbound_http_request(
            method="GET",
            target="/health",
            headers={},
            transform_engine=mock_transform,
            is_pre_app_start=False,
        )

        assert result is False
        assert reason == "dropped"

    def test_returns_false_when_not_sampled(self, mocker):
        """Should return (False, 'not_sampled') when sampling decides to skip."""
        mock_drift = mocker.patch("drift.core.drift_sdk.TuskDrift")
        mock_sdk = mocker.MagicMock()
        mock_sdk.get_sampling_rate.return_value = 0.0
        mock_drift.get_instance.return_value = mock_sdk

        mocker.patch("drift.core.sampling.should_sample", return_value=False)

        result, reason = should_record_inbound_http_request(
            method="GET",
            target="/api/users",
            headers={},
            transform_engine=None,
            is_pre_app_start=False,
        )

        assert result is False
        assert reason == "not_sampled"

    def test_drop_check_happens_before_sampling(self, mocker):
        """Should check drop rules before sampling."""
        mock_transform = mocker.MagicMock()
        mock_transform.should_drop_inbound_request.return_value = True

        mock_sample = mocker.patch("drift.core.sampling.should_sample")

        result, reason = should_record_inbound_http_request(
            method="GET",
            target="/health",
            headers={},
            transform_engine=mock_transform,
            is_pre_app_start=False,
        )

        # should_sample should never be called if dropped
        mock_sample.assert_not_called()
        assert result is False
        assert reason == "dropped"
