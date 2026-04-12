"""Tests for drift_sdk.py - Main TuskDrift SDK class."""

from __future__ import annotations

import os

import pytest

from drift.core.adaptive_sampling import AdaptiveSamplingController, ResolvedSamplingConfig
from drift.core.config import RecordingConfig, SamplingConfig, TuskFileConfig
from drift.core.drift_sdk import TuskDrift
from drift.core.types import TuskDriftMode


class TestTuskDriftSingleton:
    """Tests for TuskDrift singleton behavior."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton state before each test."""
        TuskDrift._instance = None
        TuskDrift._initialized = False
        # Clear environment variables
        env_vars = [
            "TUSK_DRIFT_MODE",
            "TUSK_API_KEY",
            "TUSK_RECORDING_SAMPLING_RATE",
            "TUSK_SAMPLING_RATE",
            "ENV",
        ]
        original_env = {k: os.environ.get(k) for k in env_vars}
        for var in env_vars:
            if var in os.environ:
                del os.environ[var]
        yield
        TuskDrift._instance = None
        TuskDrift._initialized = False
        # Restore environment
        for var, value in original_env.items():
            if value is not None:
                os.environ[var] = value
            elif var in os.environ:
                del os.environ[var]

    def test_get_instance_returns_singleton(self, reset_singleton):
        """Should return the same instance on multiple calls."""
        instance1 = TuskDrift.get_instance()
        instance2 = TuskDrift.get_instance()

        assert instance1 is instance2

    def test_generates_sdk_instance_id(self, reset_singleton):
        """Should generate a unique SDK instance ID."""
        instance = TuskDrift.get_instance()

        assert instance._sdk_instance_id is not None
        assert instance._sdk_instance_id.startswith("sdk-")
        assert len(instance._sdk_instance_id) > 15  # sdk- + timestamp + random


class TestTuskDriftModeDetection:
    """Tests for mode detection from environment variables."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton state before each test."""
        TuskDrift._instance = None
        TuskDrift._initialized = False
        yield
        TuskDrift._instance = None
        TuskDrift._initialized = False

    def test_detects_record_mode(self, reset_singleton):
        """Should detect RECORD mode from env var."""
        os.environ["TUSK_DRIFT_MODE"] = "RECORD"

        instance = TuskDrift.get_instance()

        assert instance.mode == TuskDriftMode.RECORD

    def test_detects_replay_mode(self, reset_singleton):
        """Should detect REPLAY mode from env var."""
        os.environ["TUSK_DRIFT_MODE"] = "REPLAY"

        instance = TuskDrift.get_instance()

        assert instance.mode == TuskDriftMode.REPLAY

    def test_detects_disabled_mode(self, reset_singleton):
        """Should detect DISABLED mode from env var."""
        os.environ["TUSK_DRIFT_MODE"] = "DISABLED"

        instance = TuskDrift.get_instance()

        assert instance.mode == TuskDriftMode.DISABLED

    def test_detects_disable_mode_alias(self, reset_singleton):
        """Should detect DISABLE (alias) mode from env var."""
        os.environ["TUSK_DRIFT_MODE"] = "DISABLE"

        instance = TuskDrift.get_instance()

        assert instance.mode == TuskDriftMode.DISABLED

    def test_case_insensitive_mode_detection(self, reset_singleton):
        """Should detect mode case-insensitively."""
        os.environ["TUSK_DRIFT_MODE"] = "record"

        instance = TuskDrift.get_instance()

        assert instance.mode == TuskDriftMode.RECORD

    def test_defaults_to_disabled_mode(self, reset_singleton):
        """Should default to DISABLED mode when not set."""
        if "TUSK_DRIFT_MODE" in os.environ:
            del os.environ["TUSK_DRIFT_MODE"]

        instance = TuskDrift.get_instance()

        assert instance.mode == TuskDriftMode.DISABLED


class TestTuskDriftSamplingRate:
    """Tests for sampling rate determination."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton state before each test."""
        TuskDrift._instance = None
        TuskDrift._initialized = False
        # Clear sampling rate env vars
        for env_var in ("TUSK_RECORDING_SAMPLING_RATE", "TUSK_SAMPLING_RATE"):
            if env_var in os.environ:
                del os.environ[env_var]
        yield
        TuskDrift._instance = None
        TuskDrift._initialized = False

    def test_uses_init_param_sampling_rate(self, reset_singleton):
        """Should use sampling rate from init params."""
        os.environ["TUSK_DRIFT_MODE"] = "DISABLED"
        instance = TuskDrift.get_instance()

        result = instance._determine_sampling_rate(0.5)

        assert result == 0.5

    def test_uses_recording_env_var_sampling_rate(self, reset_singleton):
        """Should use the canonical recording env var if init param not provided."""
        os.environ["TUSK_DRIFT_MODE"] = "DISABLED"
        os.environ["TUSK_RECORDING_SAMPLING_RATE"] = "0.25"
        instance = TuskDrift.get_instance()

        result = instance._determine_sampling_rate(None)

        assert result == 0.25

    def test_uses_legacy_sampling_env_var_as_alias(self, reset_singleton):
        """Should fall back to the legacy env var for backward compatibility."""
        os.environ["TUSK_DRIFT_MODE"] = "DISABLED"
        os.environ["TUSK_SAMPLING_RATE"] = "0.2"
        instance = TuskDrift.get_instance()

        result = instance._determine_sampling_rate(None)

        assert result == 0.2

    def test_recording_env_var_takes_precedence_over_legacy_alias(self, reset_singleton):
        """Should prefer the canonical env var when both env vars are set."""
        os.environ["TUSK_DRIFT_MODE"] = "DISABLED"
        os.environ["TUSK_RECORDING_SAMPLING_RATE"] = "0.25"
        os.environ["TUSK_SAMPLING_RATE"] = "0.1"
        instance = TuskDrift.get_instance()

        result = instance._determine_sampling_rate(None)

        assert result == 0.25

    def test_init_param_takes_precedence_over_env_var(self, reset_singleton):
        """Should prefer init param over env var."""
        os.environ["TUSK_DRIFT_MODE"] = "DISABLED"
        os.environ["TUSK_RECORDING_SAMPLING_RATE"] = "0.25"
        instance = TuskDrift.get_instance()

        result = instance._determine_sampling_rate(0.75)

        assert result == 0.75

    def test_invalid_init_param_falls_back_to_env_var(self, reset_singleton):
        """Should use env var when init param is present but invalid."""
        os.environ["TUSK_DRIFT_MODE"] = "DISABLED"
        os.environ["TUSK_RECORDING_SAMPLING_RATE"] = "0.25"
        instance = TuskDrift.get_instance()

        result = instance._determine_sampling_rate(2.0)

        assert result == 0.25

    def test_invalid_init_param_falls_back_to_config_file(self, reset_singleton):
        """Should use config file when init param is present but invalid."""
        os.environ["TUSK_DRIFT_MODE"] = "DISABLED"
        instance = TuskDrift.get_instance()
        instance.file_config = TuskFileConfig(
            recording=RecordingConfig(
                sampling=SamplingConfig(base_rate=0.4),
            )
        )

        result = instance._determine_sampling_rate(2.0)

        assert result == 0.4

    def test_defaults_to_1_0(self, reset_singleton):
        """Should default to 1.0 (100%) sampling rate."""
        os.environ["TUSK_DRIFT_MODE"] = "DISABLED"
        instance = TuskDrift.get_instance()

        result = instance._determine_sampling_rate(None)

        assert result == 1.0

    def test_rejects_invalid_recording_env_var_sampling_rate(self, reset_singleton):
        """Should reject an invalid canonical env var and use default."""
        os.environ["TUSK_DRIFT_MODE"] = "DISABLED"
        os.environ["TUSK_RECORDING_SAMPLING_RATE"] = "invalid"
        instance = TuskDrift.get_instance()

        result = instance._determine_sampling_rate(None)

        assert result == 1.0

    def test_invalid_env_var_falls_back_to_config_file(self, reset_singleton):
        """Should use config file when env var is present but invalid."""
        os.environ["TUSK_DRIFT_MODE"] = "DISABLED"
        os.environ["TUSK_RECORDING_SAMPLING_RATE"] = "invalid"
        instance = TuskDrift.get_instance()
        instance.file_config = TuskFileConfig(
            recording=RecordingConfig(
                sampling=SamplingConfig(base_rate=0.4),
            )
        )

        result = instance._determine_sampling_rate(None)

        assert result == 0.4

    def test_invalid_recording_env_var_falls_back_to_legacy_alias(self, reset_singleton):
        """Should use the legacy alias when the canonical env var is invalid."""
        os.environ["TUSK_DRIFT_MODE"] = "DISABLED"
        os.environ["TUSK_RECORDING_SAMPLING_RATE"] = "invalid"
        os.environ["TUSK_SAMPLING_RATE"] = "0.4"
        instance = TuskDrift.get_instance()

        result = instance._determine_sampling_rate(None)

        assert result == 0.4


class TestTuskDriftInitialize:
    """Tests for TuskDrift.initialize method."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton state before each test."""
        TuskDrift._instance = None
        TuskDrift._initialized = False
        # Clear environment variables
        env_vars = [
            "TUSK_DRIFT_MODE",
            "TUSK_API_KEY",
            "TUSK_RECORDING_SAMPLING_RATE",
            "TUSK_SAMPLING_RATE",
            "ENV",
        ]
        for var in env_vars:
            if var in os.environ:
                del os.environ[var]
        yield
        TuskDrift._instance = None
        TuskDrift._initialized = False

    def test_returns_instance_in_disabled_mode(self, reset_singleton, mocker):
        """Should return instance early in DISABLED mode."""
        mocker.patch("drift.core.drift_sdk.install_hooks")
        mocker.patch("drift.core.drift_sdk.atexit")
        os.environ["TUSK_DRIFT_MODE"] = "DISABLED"

        instance = TuskDrift.initialize()

        assert instance is not None
        assert instance.mode == TuskDriftMode.DISABLED
        # Hooks should not be installed in DISABLED mode

    def test_initialize_sets_up_otel_provider(self, reset_singleton, mocker):
        """Should set up OpenTelemetry tracer provider."""
        mocker.patch("drift.core.drift_sdk.install_hooks")
        mocker.patch("drift.core.drift_sdk.atexit")
        mocker.patch("drift.core.drift_sdk.TracerProvider")
        mock_trace = mocker.patch("drift.core.drift_sdk.trace")
        os.environ["TUSK_DRIFT_MODE"] = "RECORD"

        instance = TuskDrift.initialize(env="test")

        assert instance._tracer_provider is not None
        mock_trace.set_tracer_provider.assert_called_once()

    def test_stores_init_params(self, reset_singleton, mocker):
        """Should store initialization parameters."""
        mocker.patch("drift.core.drift_sdk.install_hooks")
        mocker.patch("drift.core.drift_sdk.atexit")
        mocker.patch("drift.core.drift_sdk.TracerProvider")
        mocker.patch("drift.core.drift_sdk.trace")
        os.environ["TUSK_DRIFT_MODE"] = "RECORD"

        instance = TuskDrift.initialize(
            api_key="test-key",
            env="production",
            sampling_rate=0.5,
        )

        params = instance.get_init_params()
        assert params["api_key"] == "test-key"
        assert params["env"] == "production"
        assert params["sampling_rate"] == 0.5

    def test_idempotent_initialization(self, reset_singleton, mocker):
        """Should be idempotent - second call returns same instance."""
        mocker.patch("drift.core.drift_sdk.install_hooks")
        mocker.patch("drift.core.drift_sdk.atexit")
        mock_provider = mocker.patch("drift.core.drift_sdk.TracerProvider")
        mocker.patch("drift.core.drift_sdk.trace")
        os.environ["TUSK_DRIFT_MODE"] = "RECORD"

        instance1 = TuskDrift.initialize(env="test")
        instance2 = TuskDrift.initialize(env="test")

        assert instance1 is instance2
        # TracerProvider should only be created once
        assert mock_provider.call_count == 1

    def test_second_initialize_does_not_mutate_live_adaptive_sampling_state(self, reset_singleton, mocker):
        """Should keep sampling fields aligned with the live controller on repeated initialize calls."""
        mocker.patch("drift.core.drift_sdk.install_hooks")
        mocker.patch("drift.core.drift_sdk.atexit")
        mocker.patch("drift.core.drift_sdk.TracerProvider")
        mocker.patch("drift.core.drift_sdk.trace")
        mocker.patch.object(TuskDrift, "_start_adaptive_sampling_control_loop")
        os.environ["TUSK_DRIFT_MODE"] = "RECORD"

        instance = TuskDrift.get_instance()
        instance.file_config = TuskFileConfig(
            recording=RecordingConfig(
                sampling=SamplingConfig(mode="adaptive", base_rate=0.5, min_rate=0.1),
            )
        )

        initialized_instance = TuskDrift.initialize(env="test")
        initialized_instance._adaptive_sampling_controller = AdaptiveSamplingController(
            ResolvedSamplingConfig(mode="adaptive", base_rate=0.5, min_rate=0.1),
            random_fn=lambda: 0.0,
            now_fn=lambda: 0.0,
        )

        initialized_instance.file_config = TuskFileConfig(
            recording=RecordingConfig(
                sampling=SamplingConfig(mode="fixed", base_rate=0.2, min_rate=None),
            )
        )

        second_instance = TuskDrift.initialize(env="test", sampling_rate=0.9)

        assert second_instance is initialized_instance
        assert second_instance._sampling_rate == 0.5
        assert second_instance._sampling_mode == "adaptive"
        assert second_instance._min_sampling_rate == 0.1

        decision = second_instance.should_record_root_request(is_pre_app_start=False)
        assert decision.mode == "adaptive"
        assert decision.base_rate == 0.5
        assert decision.min_rate == 0.1


class TestTuskDriftMarkAppAsReady:
    """Tests for TuskDrift.mark_app_as_ready method."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton state before each test."""
        TuskDrift._instance = None
        TuskDrift._initialized = False
        yield
        TuskDrift._instance = None
        TuskDrift._initialized = False

    def test_marks_app_as_ready(self, reset_singleton, mocker):
        """Should mark app as ready."""
        mocker.patch("drift.core.drift_sdk.install_hooks")
        mocker.patch("drift.core.drift_sdk.atexit")
        mocker.patch("drift.core.drift_sdk.TracerProvider")
        mocker.patch("drift.core.drift_sdk.trace")
        os.environ["TUSK_DRIFT_MODE"] = "RECORD"

        instance = TuskDrift.initialize(env="test")

        assert instance.is_app_ready() is False

        instance.mark_app_as_ready()

        assert instance.is_app_ready() is True

    def test_logs_error_when_not_initialized(self, reset_singleton):
        """Should log error when called before initialize in non-disabled mode."""
        os.environ["TUSK_DRIFT_MODE"] = "RECORD"

        instance = TuskDrift.get_instance()

        # Should not raise, but should log error
        instance.mark_app_as_ready()

        # App should NOT be marked as ready
        assert instance.is_app_ready() is False


class TestTuskDriftGetters:
    """Tests for TuskDrift getter methods."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton state before each test."""
        TuskDrift._instance = None
        TuskDrift._initialized = False
        if "TUSK_DRIFT_MODE" in os.environ:
            del os.environ["TUSK_DRIFT_MODE"]
        yield
        TuskDrift._instance = None
        TuskDrift._initialized = False

    def test_get_sampling_rate(self, reset_singleton):
        """Should return current sampling rate."""
        instance = TuskDrift.get_instance()

        rate = instance.get_sampling_rate()

        assert rate == 1.0  # Default

    def test_get_mode(self, reset_singleton):
        """Should return current mode."""
        os.environ["TUSK_DRIFT_MODE"] = "RECORD"
        instance = TuskDrift.get_instance()

        mode = instance.get_mode()

        assert mode == TuskDriftMode.RECORD

    def test_is_initialized(self, reset_singleton):
        """Should return initialization status."""
        instance = TuskDrift.get_instance()

        assert instance.is_initialized() is False

    def test_get_config(self, reset_singleton):
        """Should return runtime config."""
        instance = TuskDrift.get_instance()

        config = instance.get_config()

        assert config is not None


class TestTuskDriftMockRequests:
    """Tests for mock request methods."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton state before each test."""
        TuskDrift._instance = None
        TuskDrift._initialized = False
        if "TUSK_DRIFT_MODE" in os.environ:
            del os.environ["TUSK_DRIFT_MODE"]
        yield
        TuskDrift._instance = None
        TuskDrift._initialized = False

    def test_request_mock_sync_fails_when_not_in_replay_mode(self, reset_singleton, mocker):
        """Should return error when not in REPLAY mode."""
        os.environ["TUSK_DRIFT_MODE"] = "RECORD"
        instance = TuskDrift.get_instance()

        mock_request = mocker.MagicMock()
        result = instance.request_mock_sync(mock_request)

        assert result.found is False
        assert result.error is not None and "Not in replay mode" in result.error

    def test_request_mock_sync_fails_without_communicator(self, reset_singleton, mocker):
        """Should return error when communicator not initialized."""
        os.environ["TUSK_DRIFT_MODE"] = "REPLAY"
        instance = TuskDrift.get_instance()
        instance.communicator = None

        mock_request = mocker.MagicMock()
        result = instance.request_mock_sync(mock_request)

        assert result.found is False
        assert result.error is not None and "Communicator not initialized" in result.error


class TestTuskDriftShutdown:
    """Tests for TuskDrift.shutdown method."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton state before each test."""
        TuskDrift._instance = None
        TuskDrift._initialized = False
        yield
        TuskDrift._instance = None
        TuskDrift._initialized = False

    def test_shutdown_cleans_up_resources(self, reset_singleton, mocker):
        """Should clean up all resources on shutdown."""
        mocker.patch("drift.core.drift_sdk.install_hooks")
        mocker.patch("drift.core.drift_sdk.atexit")
        mock_provider = mocker.patch("drift.core.drift_sdk.TracerProvider")
        mocker.patch("drift.core.drift_sdk.trace")
        mocker.patch("drift.core.drift_sdk.TraceBlockingManager")
        os.environ["TUSK_DRIFT_MODE"] = "RECORD"

        mock_td_processor = mocker.MagicMock()
        mock_tracer_provider = mocker.MagicMock()
        mock_provider.return_value = mock_tracer_provider

        instance = TuskDrift.initialize(env="test")
        instance._td_span_processor = mock_td_processor

        instance.shutdown()

        mock_td_processor.shutdown.assert_called_once()
        mock_tracer_provider.shutdown.assert_called_once()


class TestTuskDriftAdaptiveSampling:
    """Tests for adaptive sampling health monitoring."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton state before each test."""
        TuskDrift._instance = None
        TuskDrift._initialized = False
        yield
        TuskDrift._instance = None
        TuskDrift._initialized = False

    def test_safe_update_logs_and_swallows_health_update_exceptions(self, reset_singleton, mocker):
        """Should log and continue when health updates fail."""
        instance = TuskDrift.get_instance()
        mocker.patch.object(instance, "_update_adaptive_sampling_health", side_effect=RuntimeError("boom"))
        log_error = mocker.patch("drift.core.drift_sdk.logger.error")

        instance._safe_update_adaptive_sampling_health()

        log_error.assert_called_once()
        assert "Adaptive sampling health update failed" in log_error.call_args.args[0]

    def test_adaptive_sampling_loop_continues_after_update_exception(self, reset_singleton, mocker):
        """Should keep polling after a single health update failure."""
        instance = TuskDrift.get_instance()
        stop_event = mocker.MagicMock()
        stop_event.wait.side_effect = [False, False, True]
        instance._adaptive_sampling_stop_event = stop_event
        log_error = mocker.patch("drift.core.drift_sdk.logger.error")

        update_health = mocker.patch.object(
            instance,
            "_update_adaptive_sampling_health",
            side_effect=[RuntimeError("boom"), None],
        )

        instance._adaptive_sampling_loop()

        assert update_health.call_count == 2
        log_error.assert_called_once()
        assert "Adaptive sampling health update failed" in log_error.call_args.args[0]


class TestTuskDriftMemoryPressure:
    """Tests for memory pressure measurement helpers."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton state before each test."""
        TuskDrift._instance = None
        TuskDrift._initialized = False
        yield
        TuskDrift._instance = None
        TuskDrift._initialized = False

    def test_parse_proc_status_rss_bytes(self, reset_singleton):
        """Should parse current RSS from /proc/self/status."""
        raw_status = "Name:\tpython\nVmRSS:\t1234 kB\nThreads:\t8\n"

        assert TuskDrift._parse_proc_status_rss_bytes(raw_status) == 1234 * 1024

    def test_read_current_rss_bytes_falls_back_to_proc_statm(self, reset_singleton, mocker):
        """Should use /proc/self/statm when /proc/self/status is unavailable."""
        instance = TuskDrift.get_instance()

        mocker.patch(
            "drift.core.drift_sdk.Path.exists",
            autospec=True,
            side_effect=lambda path: str(path) == "/proc/self/statm",
        )
        mocker.patch(
            "drift.core.drift_sdk.Path.read_text",
            autospec=True,
            side_effect=lambda path: "100 25 0 0 0 0 0\n" if str(path) == "/proc/self/statm" else "",
        )
        mocker.patch("drift.core.drift_sdk.os.sysconf", return_value=4096)

        assert instance._read_current_rss_bytes() == 25 * 4096

    def test_get_memory_pressure_ratio_uses_current_rss_fallback(self, reset_singleton, mocker):
        """Should use current RSS fallback when cgroup current usage is unavailable."""
        instance = TuskDrift.get_instance()
        instance._effective_memory_limit_bytes = 1024
        mocker.patch.object(instance, "_read_numeric_control_file", return_value=None)
        mocker.patch.object(instance, "_read_current_rss_bytes", return_value=256)

        assert instance._get_memory_pressure_ratio() == 0.25

    def test_get_memory_pressure_ratio_returns_none_without_current_measurement(self, reset_singleton, mocker):
        """Should return None when no current memory measurement is available."""
        instance = TuskDrift.get_instance()
        instance._effective_memory_limit_bytes = 1024
        mocker.patch.object(instance, "_read_numeric_control_file", return_value=None)
        mocker.patch.object(instance, "_read_current_rss_bytes", return_value=None)

        assert instance._get_memory_pressure_ratio() is None


class TestTuskDriftGetTracer:
    """Tests for TuskDrift.get_tracer method."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton state before each test."""
        TuskDrift._instance = None
        TuskDrift._initialized = False
        yield
        TuskDrift._instance = None
        TuskDrift._initialized = False

    def test_get_tracer_returns_tracer(self, reset_singleton, mocker):
        """Should return a tracer from the provider."""
        mocker.patch("drift.core.drift_sdk.install_hooks")
        mocker.patch("drift.core.drift_sdk.atexit")
        mock_provider = mocker.patch("drift.core.drift_sdk.TracerProvider")
        mocker.patch("drift.core.drift_sdk.trace")
        os.environ["TUSK_DRIFT_MODE"] = "RECORD"

        mock_tracer = mocker.MagicMock()
        mock_tracer_provider = mocker.MagicMock()
        mock_tracer_provider.get_tracer.return_value = mock_tracer
        mock_provider.return_value = mock_tracer_provider

        instance = TuskDrift.initialize(env="test")

        result = instance.get_tracer()

        assert result is mock_tracer

    def test_get_tracer_falls_back_to_global(self, reset_singleton, mocker):
        """Should fall back to global tracer if provider not initialized."""
        mock_trace = mocker.patch("drift.core.drift_sdk.trace")
        mock_global_tracer = mocker.MagicMock()
        mock_trace.get_tracer.return_value = mock_global_tracer

        instance = TuskDrift.get_instance()
        instance._tracer_provider = None

        instance.get_tracer()

        mock_trace.get_tracer.assert_called()
