"""Tests for drift_sdk.py - Main TuskDrift SDK class."""

from __future__ import annotations

import os

import pytest

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
        env_vars = ["TUSK_DRIFT_MODE", "TUSK_API_KEY", "TUSK_SAMPLING_RATE", "ENV"]
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
        # Clear sampling rate env var
        if "TUSK_SAMPLING_RATE" in os.environ:
            del os.environ["TUSK_SAMPLING_RATE"]
        yield
        TuskDrift._instance = None
        TuskDrift._initialized = False

    def test_uses_init_param_sampling_rate(self, reset_singleton):
        """Should use sampling rate from init params."""
        os.environ["TUSK_DRIFT_MODE"] = "DISABLED"
        instance = TuskDrift.get_instance()

        result = instance._determine_sampling_rate(0.5)

        assert result == 0.5

    def test_uses_env_var_sampling_rate(self, reset_singleton):
        """Should use sampling rate from env var if init param not provided."""
        os.environ["TUSK_DRIFT_MODE"] = "DISABLED"
        os.environ["TUSK_SAMPLING_RATE"] = "0.25"
        instance = TuskDrift.get_instance()

        result = instance._determine_sampling_rate(None)

        assert result == 0.25

    def test_init_param_takes_precedence_over_env_var(self, reset_singleton):
        """Should prefer init param over env var."""
        os.environ["TUSK_DRIFT_MODE"] = "DISABLED"
        os.environ["TUSK_SAMPLING_RATE"] = "0.25"
        instance = TuskDrift.get_instance()

        result = instance._determine_sampling_rate(0.75)

        assert result == 0.75

    def test_defaults_to_1_0(self, reset_singleton):
        """Should default to 1.0 (100%) sampling rate."""
        os.environ["TUSK_DRIFT_MODE"] = "DISABLED"
        instance = TuskDrift.get_instance()

        result = instance._determine_sampling_rate(None)

        assert result == 1.0

    def test_rejects_invalid_env_var_sampling_rate(self, reset_singleton):
        """Should reject invalid env var and use default."""
        os.environ["TUSK_DRIFT_MODE"] = "DISABLED"
        os.environ["TUSK_SAMPLING_RATE"] = "invalid"
        instance = TuskDrift.get_instance()

        result = instance._determine_sampling_rate(None)

        assert result == 1.0


class TestTuskDriftInitialize:
    """Tests for TuskDrift.initialize method."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton state before each test."""
        TuskDrift._instance = None
        TuskDrift._initialized = False
        # Clear environment variables
        env_vars = ["TUSK_DRIFT_MODE", "TUSK_API_KEY", "TUSK_SAMPLING_RATE", "ENV"]
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
