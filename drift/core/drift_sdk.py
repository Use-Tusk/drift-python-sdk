from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import platform
import random
import stat
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import SpanKind as OTelSpanKind

from ..instrumentation.registry import install_hooks
from ..version import SDK_VERSION
from .adaptive_sampling import (
    AdaptiveSamplingController,
    AdaptiveSamplingHealthSnapshot,
    ResolvedSamplingConfig,
    RootSamplingDecision,
    SamplingMode,
)
from .communication.communicator import CommunicatorConfig, ProtobufCommunicator
from .communication.types import MockRequestInput, MockResponseOutput
from .config import TuskConfig, TuskFileConfig, load_tusk_config
from .logger import LogLevel, configure_logger, get_log_level
from .sampling import should_sample, validate_sampling_rate
from .trace_blocking_manager import TraceBlockingManager, should_block_span
from .tracing import TdSpanAttributes, TdSpanExporter, TdSpanExporterConfig
from .tracing.td_span_processor import TdSpanProcessor
from .types import TD_INSTRUMENTATION_LIBRARY_NAME, CleanSpanData, SpanKind, TuskDriftMode

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer

logger = logging.getLogger(__name__)


class TuskDrift:
    """Main SDK singleton managing the Drift SDK lifecycle."""

    _instance: TuskDrift | None = None
    _initialized = False

    def __init__(self) -> None:
        self.mode: TuskDriftMode = self._detect_mode()
        self.config = TuskConfig()
        self.file_config: TuskFileConfig | None = None
        self.app_ready = False
        self._sdk_instance_id = self._generate_sdk_instance_id()
        self._sampling_rate: float = 1.0
        self._sampling_mode: str = "fixed"
        self._min_sampling_rate: float = 0.0
        self._adaptive_sampling_controller: AdaptiveSamplingController | None = None
        self._adaptive_sampling_thread: threading.Thread | None = None
        self._adaptive_sampling_stop_event = threading.Event()
        self._effective_memory_limit_bytes: int | None = None
        self._transform_configs: dict[str, Any] | None = None
        self._init_params: dict[str, Any] = {}

        self.span_exporter: TdSpanExporter | None = None

        # OpenTelemetry components
        self._tracer_provider: TracerProvider | None = None
        self._td_span_processor: TdSpanProcessor | None = None

        self.communicator: ProtobufCommunicator | None = None
        self._cli_connection_task: asyncio.Task | None = None
        self._is_connected_with_cli = False

        self.file_config = load_tusk_config()

        # Disable Sentry in replay mode
        if self.mode == TuskDriftMode.REPLAY and "SENTRY_DSN" in os.environ:
            del os.environ["SENTRY_DSN"]

    @classmethod
    def get_instance(cls) -> TuskDrift:
        """Get the singleton TuskDrift instance."""
        if cls._instance is None:
            cls._instance = TuskDrift()
        return cls._instance

    def _generate_sdk_instance_id(self) -> str:
        """Generate a unique SDK instance ID matching Node SDK pattern."""
        timestamp_ms = int(time.time() * 1000)
        random_suffix = "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=9))
        return f"sdk-{timestamp_ms}-{random_suffix}"

    @staticmethod
    def _log_rust_core_startup_status() -> None:
        from .rust_core_binding import get_rust_core_startup_status

        status = get_rust_core_startup_status()
        env_display = status["raw_env"] if status["raw_env"] is not None else "<unset>"

        if status["reason"] == "invalid_env_value_defaulted":
            logger.warning(
                "Invalid TUSK_USE_RUST_CORE value '%s'; defaulting to enabled rust core path.",
                env_display,
            )

        if not status["enabled"]:
            logger.info("Rust core path disabled at startup (env=%s, reason=%s).", env_display, status["reason"])
            return

        if status["binding_loaded"]:
            logger.info("Rust core path enabled at startup (env=%s, reason=%s).", env_display, status["reason"])
            return

        logger.info(
            "Rust core path unavailable at startup; using Python path instead (env=%s, reason=%s, error=%s).",
            env_display,
            status["reason"],
            status["binding_error"],
        )

    def _log_startup_summary(self, env: str, use_remote_export: bool) -> None:
        service_name = (
            self.file_config.service.name
            if self.file_config and self.file_config.service and self.file_config.service.name
            else "unknown"
        )
        service_id = (
            self.file_config.service.id
            if self.file_config and self.file_config.service and self.file_config.service.id
            else "<unset>"
        )

        logger.info(
            "SDK initialized successfully (version=%s, mode=%s, env=%s, service=%s, serviceId=%s, exportSpans=%s, samplingMode=%s, samplingBaseRate=%s, samplingMinRate=%s, logLevel=%s, runtime=python %s, platform=%s/%s).",
            SDK_VERSION,
            self.mode,
            env,
            service_name,
            service_id,
            use_remote_export,
            self._sampling_mode,
            self._sampling_rate,
            self._min_sampling_rate,
            get_log_level(),
            platform.python_version(),
            platform.system().lower(),
            platform.machine().lower(),
        )

    @classmethod
    def initialize(
        cls,
        api_key: str | None = None,
        env: str | None = None,
        sampling_rate: float | None = None,
        transforms: dict[str, Any] | None = None,
        log_level: LogLevel = "info",
    ) -> TuskDrift:
        """Initialize the TuskDrift SDK.

        Args:
            api_key: Tusk API key
            env: Environment name
            sampling_rate: Sampling rate (0.0-1.0)
            transforms: Transform configurations
            log_level: Logging level

        Returns:
            Initialized SDK instance
        """
        instance = cls.get_instance()

        configure_logger(log_level=log_level, prefix="TuskDrift")

        instance._init_params = {
            "api_key": api_key,
            "env": env,
            "sampling_rate": sampling_rate,
            "transforms": transforms,
            "log_level": log_level,
        }

        sampling_config = instance._determine_sampling_config(sampling_rate)
        instance._sampling_rate = sampling_config.base_rate
        instance._sampling_mode = sampling_config.mode
        instance._min_sampling_rate = sampling_config.min_rate

        effective_api_key = api_key or os.environ.get("TUSK_API_KEY")

        if not env:
            env_from_var = os.environ.get("ENV") or "development"
            logger.warning(
                f"Environment not provided in initialization parameters. Using '{env_from_var}' as the environment."
            )
            env = env_from_var

        if cls._initialized:
            logger.debug("Already initialized, skipping...")
            return instance

        # Start coverage collection after the _initialized guard so repeated
        # initialize() calls don't stop/restart coverage and lose accumulated data.
        from .coverage_server import start_coverage_collection

        start_coverage_collection()

        file_config = instance.file_config

        if (
            instance.mode == TuskDriftMode.RECORD
            and file_config
            and file_config.recording
            and file_config.recording.export_spans
            and not effective_api_key
        ):
            logger.error(
                "In record mode and export_spans is true, but API key not provided. API key is required to export spans to Tusk backend. Please provide an API key in the initialization parameters."
            )
            return instance

        effective_observable_service_id = None
        if file_config and file_config.service:
            effective_observable_service_id = file_config.service.id

        export_spans_enabled = file_config.recording.export_spans if file_config and file_config.recording else False

        if export_spans_enabled and not effective_observable_service_id:
            logger.error(
                "Observable service ID not provided. Please provide an observable service ID in the configuration file."
            )
            return instance

        if instance.mode == TuskDriftMode.DISABLED:
            logger.debug("SDK disabled via environment variable")
            return instance

        instance._log_rust_core_startup_status()
        logger.debug(f"Initializing in {instance.mode} mode")

        effective_transforms = transforms
        if effective_transforms is None and file_config and file_config.transforms:
            effective_transforms = file_config.transforms
        instance._transform_configs = effective_transforms

        instance.config = TuskConfig(
            api_key=effective_api_key,
            env=env,
            sampling_rate=instance._sampling_rate,
            transforms=effective_transforms,
        )

        effective_export_directory = None
        if file_config and file_config.traces and file_config.traces.dir:
            effective_export_directory = file_config.traces.dir

        effective_backend_url = "https://api.usetusk.ai"
        if file_config and file_config.tusk_api and file_config.tusk_api.url:
            effective_backend_url = file_config.tusk_api.url

        base_dir = Path(effective_export_directory) if effective_export_directory else Path.cwd() / ".tusk" / "traces"

        logger.debug(f"Config: {file_config}")
        logger.debug(f"Base directory: {base_dir}")

        use_remote_export = bool(
            export_spans_enabled and effective_api_key is not None and effective_observable_service_id is not None
        )

        exporter_config = TdSpanExporterConfig(
            base_directory=base_dir,
            mode=instance.mode,
            observable_service_id=effective_observable_service_id,
            use_remote_export=use_remote_export,
            api_key=effective_api_key,
            tusk_backend_base_url=effective_backend_url,
            environment=env,
            sdk_version=SDK_VERSION,
            sdk_instance_id=instance._sdk_instance_id,
        )
        instance.span_exporter = TdSpanExporter(exporter_config)

        # Initialize OpenTelemetry TracerProvider
        service_name = effective_observable_service_id or "drift-python-sdk"
        resource = Resource.create(
            {
                "service.name": service_name,
                "service.version": SDK_VERSION,
                "deployment.environment": env,
            }
        )

        instance._tracer_provider = TracerProvider(resource=resource)

        # Create and add our custom span processor
        instance._td_span_processor = TdSpanProcessor(
            exporter=instance.span_exporter,
            mode=instance.mode,
            sampling_rate=instance._sampling_rate,
            app_ready=instance.app_ready,
            environment=env,
        )
        instance._td_span_processor.start()
        instance._tracer_provider.add_span_processor(instance._td_span_processor)

        # Set as global tracer provider
        trace.set_tracer_provider(instance._tracer_provider)

        logger.debug("OpenTelemetry TracerProvider initialized")

        if instance.mode == TuskDriftMode.REPLAY:
            # Initialize communicator early for replay mode (matches Node SDK pattern)
            try:
                instance._init_communicator_for_replay()
                logger.debug("Replay communicator initialized during SDK initialization")
            except Exception as e:
                logger.warning(f"Failed to initialize replay communicator: {e}")
                logger.warning(
                    "Continuing with SDK initialization. Mock responses will not be available until CLI is running."
                )

        install_hooks()

        instance._init_auto_instrumentations()
        instance._start_adaptive_sampling_control_loop()

        # Create env vars snapshot if enabled (matches Node SDK behavior)
        instance.create_env_vars_snapshot()

        # Register shutdown handler for graceful cleanup
        atexit.register(instance.shutdown)

        cls._initialized = True
        instance._log_startup_summary(env=env, use_remote_export=use_remote_export)

        return instance

    def _determine_sampling_config(self, init_param: float | None) -> ResolvedSamplingConfig:
        """Determine the effective sampling config from init params, env, and file config."""
        recording_config = self.file_config.recording if self.file_config else None
        config_sampling = recording_config.sampling if recording_config else None

        mode: SamplingMode = "fixed"
        if config_sampling and config_sampling.mode in {"fixed", "adaptive"}:
            mode = "adaptive" if config_sampling.mode == "adaptive" else "fixed"
        elif config_sampling and config_sampling.mode:
            logger.warning(
                "Invalid sampling mode from config file: %s. Must be 'fixed' or 'adaptive'. Ignoring.",
                config_sampling.mode,
            )

        base_rate: float | None = None
        if init_param is not None:
            validated = validate_sampling_rate(init_param, "init params")
            if validated is not None:
                logger.debug(f"Using sampling rate from init params: {validated}")
                base_rate = validated

        if base_rate is None:
            env_rate = os.environ.get("TUSK_SAMPLING_RATE")
            if env_rate is not None:
                try:
                    parsed = float(env_rate)
                    validated = validate_sampling_rate(parsed, "TUSK_SAMPLING_RATE env var")
                    if validated is not None:
                        logger.debug(f"Using sampling rate from env var: {validated}")
                        base_rate = validated
                except ValueError:
                    logger.warning(f"Invalid TUSK_SAMPLING_RATE env var: {env_rate}")

        if base_rate is None and config_sampling and config_sampling.base_rate is not None:
            validated = validate_sampling_rate(config_sampling.base_rate, "config file recording.sampling.base_rate")
            if validated is not None:
                logger.debug(f"Using sampling rate from config file recording.sampling.base_rate: {validated}")
                base_rate = validated

        if base_rate is None and recording_config and recording_config.sampling_rate is not None:
            validated = validate_sampling_rate(recording_config.sampling_rate, "config file recording.sampling_rate")
            if validated is not None:
                logger.debug(f"Using sampling rate from config file recording.sampling_rate: {validated}")
                base_rate = validated

        if base_rate is None:
            logger.debug("Using default sampling rate: 1.0")
            base_rate = 1.0

        min_rate = 0.0
        if mode == "adaptive":
            validated_min_rate = validate_sampling_rate(
                config_sampling.min_rate if config_sampling else None,
                "config file recording.sampling.min_rate",
            )
            min_rate = validated_min_rate if validated_min_rate is not None else 0.001
            min_rate = min(base_rate, min_rate)

        return ResolvedSamplingConfig(
            mode=mode,
            base_rate=base_rate,
            min_rate=min_rate,
        )

    def _determine_sampling_rate(self, init_param: float | None) -> float:
        """Backward-compatible helper that returns only the effective base sampling rate."""
        return self._determine_sampling_config(init_param).base_rate

    def _start_adaptive_sampling_control_loop(self) -> None:
        if self.mode != TuskDriftMode.RECORD or self._sampling_mode != "adaptive":
            return

        self._adaptive_sampling_controller = AdaptiveSamplingController(
            ResolvedSamplingConfig(
                mode="adaptive",
                base_rate=self._sampling_rate,
                min_rate=self._min_sampling_rate,
            )
        )
        self._effective_memory_limit_bytes = self._detect_effective_memory_limit_bytes()
        self._adaptive_sampling_stop_event.clear()

        self._adaptive_sampling_thread = threading.Thread(
            target=self._adaptive_sampling_loop,
            daemon=True,
            name="drift-adaptive-sampling",
        )
        self._adaptive_sampling_thread.start()
        self._safe_update_adaptive_sampling_health()

    def _adaptive_sampling_loop(self) -> None:
        while not self._adaptive_sampling_stop_event.wait(timeout=2.0):
            self._safe_update_adaptive_sampling_health()

    def _safe_update_adaptive_sampling_health(self) -> None:
        try:
            self._update_adaptive_sampling_health()
        except Exception:
            logger.error("Adaptive sampling health update failed; keeping previous controller state.", exc_info=True)

    def _update_adaptive_sampling_health(self) -> None:
        if self._adaptive_sampling_controller is None:
            return

        batch_processor = self._td_span_processor._batch_processor if self._td_span_processor else None
        queue_fill_ratio = None
        dropped_span_count = 0
        if batch_processor is not None and batch_processor.max_queue_size > 0:
            queue_fill_ratio = batch_processor.queue_size / batch_processor.max_queue_size
            dropped_span_count = batch_processor.dropped_span_count

        export_failure_count = 0
        export_circuit_open = False
        if self.span_exporter is not None:
            for adapter in self.span_exporter.get_adapters():
                spans_failed = getattr(adapter, "spans_failed", 0)
                export_failure_count += int(spans_failed)
                export_circuit_open = export_circuit_open or getattr(adapter, "circuit_state", "") == "open"

        self._adaptive_sampling_controller.update(
            AdaptiveSamplingHealthSnapshot(
                queue_fill_ratio=queue_fill_ratio,
                dropped_span_count=dropped_span_count,
                export_failure_count=export_failure_count,
                export_circuit_open=export_circuit_open,
                memory_pressure_ratio=self._get_memory_pressure_ratio(),
            )
        )

    def _detect_effective_memory_limit_bytes(self) -> int | None:
        candidates = (
            "/sys/fs/cgroup/memory.max",
            "/sys/fs/cgroup/memory/memory.limit_in_bytes",
        )
        for path in candidates:
            parsed = self._read_numeric_control_file(path)
            if parsed is None:
                continue
            if parsed <= 0 or parsed > 1_000_000_000_000_000:
                continue
            return parsed
        return None

    def _get_memory_pressure_ratio(self) -> float | None:
        if self._effective_memory_limit_bytes is None or self._effective_memory_limit_bytes <= 0:
            return None

        cgroup_current = self._read_numeric_control_file("/sys/fs/cgroup/memory.current")
        if cgroup_current is not None:
            return cgroup_current / self._effective_memory_limit_bytes

        cgroup_v1_current = self._read_numeric_control_file("/sys/fs/cgroup/memory/memory.usage_in_bytes")
        if cgroup_v1_current is not None:
            return cgroup_v1_current / self._effective_memory_limit_bytes

        current_rss_bytes = self._read_current_rss_bytes()
        if current_rss_bytes is not None:
            return current_rss_bytes / self._effective_memory_limit_bytes

        return None

    @staticmethod
    def _parse_proc_status_rss_bytes(raw_status: str) -> int | None:
        for line in raw_status.splitlines():
            if not line.startswith("VmRSS:"):
                continue

            parts = line.split()
            if len(parts) < 3 or parts[2].lower() != "kb":
                return None

            return int(parts[1]) * 1024

        return None

    @staticmethod
    def _parse_proc_statm_rss_bytes(raw_statm: str, page_size: int) -> int | None:
        fields = raw_statm.split()
        if len(fields) < 2:
            return None

        return int(fields[1]) * page_size

    def _read_current_rss_bytes(self) -> int | None:
        try:
            proc_status_path = Path("/proc/self/status")
            if proc_status_path.exists():
                parsed = self._parse_proc_status_rss_bytes(proc_status_path.read_text())
                if parsed is not None:
                    return parsed
        except Exception:
            pass

        try:
            proc_statm_path = Path("/proc/self/statm")
            if proc_statm_path.exists():
                return self._parse_proc_statm_rss_bytes(proc_statm_path.read_text(), int(os.sysconf("SC_PAGE_SIZE")))
        except Exception:
            pass

        return None

    def _read_numeric_control_file(self, path: str) -> int | None:
        try:
            if not os.path.exists(path):
                return None
            raw_value = Path(path).read_text().strip()
            if not raw_value or raw_value == "max":
                return None
            return int(raw_value)
        except Exception:
            return None

    def _detect_mode(self) -> TuskDriftMode:
        """Detect the SDK mode from environment variable."""
        mode_env = os.environ.get("TUSK_DRIFT_MODE", "").upper()

        if mode_env == "RECORD":
            return TuskDriftMode.RECORD
        elif mode_env == "REPLAY":
            return TuskDriftMode.REPLAY
        elif mode_env in ("DISABLED", "DISABLE"):
            return TuskDriftMode.DISABLED
        else:
            # Default to DISABLED if no mode specified (matches Node SDK)
            return TuskDriftMode.DISABLED

    def _init_communicator_for_replay(self) -> None:
        """Initialize CLI communicator for REPLAY mode."""
        mock_host = os.environ.get("TUSK_MOCK_HOST")
        mock_port = os.environ.get("TUSK_MOCK_PORT")
        mock_socket = os.environ.get("TUSK_MOCK_SOCKET")

        connection_info: dict[str, Any]

        if mock_host and mock_port:
            connection_info = {
                "host": mock_host,
                "port": int(mock_port),
            }
            logger.debug(f"Using TCP connection to CLI: {mock_host}:{mock_port}")
        else:
            socket_path = mock_socket or "/tmp/tusk-connect.sock"

            socket_file = Path(socket_path)
            if not socket_file.exists():
                raise FileNotFoundError(f"Socket not found at {socket_path}. Make sure Tusk CLI is running.")

            file_stat = socket_file.stat()
            if not stat.S_ISSOCK(file_stat.st_mode):
                raise ValueError(f"Path exists but is not a socket: {socket_path}")

            logger.debug(f"Socket found and verified at {socket_path}")
            connection_info = {"socketPath": socket_path}

        self.communicator = ProtobufCommunicator(CommunicatorConfig.from_env())

        service_id: str = (
            self.file_config.service.id
            if self.file_config and self.file_config.service and self.file_config.service.id
            else "unknown"
        )

        # Connect to CLI during initialization
        # Use synchronous connection to keep socket alive
        try:
            if self.communicator:
                self.communicator.connect_sync(connection_info, service_id)
                self._is_connected_with_cli = True
                logger.debug("SDK successfully connected to CLI synchronously")
        except Exception as e:
            logger.warning(f"Failed to initialize replay communicator: {e}")
            logger.info(
                "Continuing with SDK initialization. Mock responses will not be available until CLI is running."
            )
            self._is_connected_with_cli = False
            # Don't raise - allow Django to start even if CLI isn't ready yet

    def _init_auto_instrumentations(self) -> None:
        """Initialize instrumentations."""
        try:
            import flask

            from ..instrumentation.flask import FlaskInstrumentation

            _ = FlaskInstrumentation()
            logger.debug("Flask instrumentation initialized")
        except ImportError:
            pass

        try:
            import fastapi

            from ..instrumentation.fastapi import FastAPIInstrumentation

            _ = FastAPIInstrumentation()
            logger.debug("FastAPI instrumentation initialized")
        except ImportError:
            pass

        try:
            import requests

            from ..instrumentation.requests import RequestsInstrumentation

            _ = RequestsInstrumentation()
            logger.debug("Requests instrumentation initialized")
        except ImportError:
            pass

        try:
            import httpx

            from ..instrumentation.httpx import HttpxInstrumentation

            _ = HttpxInstrumentation()
            logger.debug("httpx instrumentation initialized")
        except ImportError:
            pass

        try:
            import urllib3

            from ..instrumentation.urllib3 import Urllib3Instrumentation

            _ = Urllib3Instrumentation()
            logger.debug("urllib3 instrumentation initialized")
        except ImportError:
            pass

        try:
            import urllib.request

            from ..instrumentation.urllib import UrllibInstrumentation

            _ = UrllibInstrumentation()
            logger.debug("urllib instrumentation initialized")
        except ImportError:
            pass

        try:
            import sqlalchemy

            from ..instrumentation.sqlalchemy import SqlAlchemyInstrumentation

            _ = SqlAlchemyInstrumentation()
            logger.debug("SQLAlchemy instrumentation initialized")
        except ImportError:
            pass

        # Initialize PostgreSQL instrumentation before Django
        # Instrument BOTH psycopg2 and psycopg if available
        # This allows apps to use either or both
        psycopg2_available = False
        psycopg_available = False

        # Try psycopg2 first
        try:
            import psycopg2

            from ..instrumentation.psycopg2 import Psycopg2Instrumentation

            _ = Psycopg2Instrumentation()
            logger.debug("Psycopg2 instrumentation initialized")
            psycopg2_available = True
        except ImportError:
            pass

        # Try psycopg (v3)
        try:
            import psycopg

            from ..instrumentation.psycopg import PsycopgInstrumentation

            _ = PsycopgInstrumentation()
            logger.debug("Psycopg instrumentation initialized")
            psycopg_available = True
        except ImportError:
            pass

        if not psycopg2_available and not psycopg_available:
            logger.debug("No PostgreSQL client library found (psycopg2 or psycopg)")
        elif psycopg2_available and psycopg_available:
            logger.debug("Both psycopg2 and psycopg available - instrumented both")

        try:
            import redis

            from ..instrumentation.redis import RedisInstrumentation

            _ = RedisInstrumentation()
            logger.debug("Redis instrumentation initialized")
        except ImportError:
            pass

        try:
            import grpc

            from ..instrumentation.grpc import GrpcInstrumentation

            _ = GrpcInstrumentation()
            logger.debug("gRPC instrumentation initialized")
        except ImportError:
            pass

        try:
            import django

            from ..instrumentation.django import DjangoInstrumentation

            _ = DjangoInstrumentation()
            logger.debug("Django instrumentation initialized")
        except ImportError:
            pass

        # REPLAY mode only instrumentations
        if self.mode == TuskDriftMode.REPLAY:
            # Kinde instrumentation for auth replay - registers hook for when kinde_sdk is imported
            try:
                from ..instrumentation.kinde import KindeInstrumentation

                _ = KindeInstrumentation(mode=self.mode)
                logger.debug("Kinde instrumentation registered (REPLAY mode)")
            except Exception as e:
                logger.debug(f"Kinde instrumentation registration failed: {e}")

            # Socket instrumentation for detecting unpatched dependencies
            try:
                from ..instrumentation.socket import SocketInstrumentation

                _ = SocketInstrumentation(mode=self.mode)
                logger.debug("Socket instrumentation initialized (REPLAY mode - unpatched dependency detection)")
            except Exception as e:
                logger.debug(f"Socket instrumentation initialization failed: {e}")

            # PyJWT instrumentation for JWT verification bypass
            try:
                from ..instrumentation.pyjwt import PyJWTInstrumentation

                _ = PyJWTInstrumentation(mode=self.mode)
                logger.debug("PyJWT instrumentation registered (REPLAY mode)")
            except Exception as e:
                logger.debug(f"PyJWT instrumentation registration failed: {e}")

    def create_env_vars_snapshot(self) -> None:
        """Create a span capturing all environment variables.

        Only creates the snapshot in RECORD mode when enable_env_var_recording is True.
        This matches the Node SDK behavior for capturing env vars at initialization.
        """
        if self.mode != TuskDriftMode.RECORD:
            return

        if not self.file_config or not self.file_config.recording:
            return

        if not self.file_config.recording.enable_env_var_recording:
            return

        # Get all environment variables
        env_vars = dict(os.environ)

        tracer = self.get_tracer()
        span = tracer.start_span(
            name="ENV_VARS_SNAPSHOT",
            kind=OTelSpanKind.INTERNAL,
            attributes={
                TdSpanAttributes.NAME: "ENV_VARS_SNAPSHOT",
                TdSpanAttributes.PACKAGE_NAME: "os.environ",
                TdSpanAttributes.IS_PRE_APP_START: True,
                TdSpanAttributes.INPUT_VALUE: "{}",
                TdSpanAttributes.OUTPUT_VALUE: json.dumps({"ENV_VARS": env_vars}),
            },
        )
        span.end()
        logger.debug(f"Created ENV_VARS_SNAPSHOT span with {len(env_vars)} environment variables")

    def get_tracer(self, name: str | None = None, version: str = "") -> Tracer:
        """Get OpenTelemetry tracer.

        Args:
            name: Tracer name (default: TD_INSTRUMENTATION_LIBRARY_NAME)
            version: Tracer version (default: SDK version)

        Returns:
            OpenTelemetry Tracer instance
        """
        if name is None:
            name = TD_INSTRUMENTATION_LIBRARY_NAME

        if not version:
            from ..version import SDK_VERSION

            version = SDK_VERSION

        if self._tracer_provider:
            return self._tracer_provider.get_tracer(name, version)
        else:
            # Fallback to global tracer if provider not initialized
            return trace.get_tracer(name, version)

    def mark_app_as_ready(self) -> None:
        """Mark the application as ready (started listening)."""
        if not self._initialized:
            if self.mode != TuskDriftMode.DISABLED:
                logger.error("mark_app_as_ready() called before initialize(). Call initialize() first.")
            return

        self.app_ready = True

        # Update span processor with app_ready flag
        if self._td_span_processor:
            self._td_span_processor.update_app_ready(True)

        if self.mode == TuskDriftMode.REPLAY:
            logger.debug("Replay mode active - ready to serve mocked responses")
        elif self.mode == TuskDriftMode.RECORD:
            logger.debug("Record mode active - capturing inbound requests and responses")

        logger.info("App marked as ready")

    def collect_span(self, span: CleanSpanData) -> None:
        """[DEPRECATED] Collect and export a span.

        This method is deprecated and maintained for backward compatibility only.
        New instrumentations should use OpenTelemetry spans via SpanUtils instead.
        """
        if self.mode == TuskDriftMode.DISABLED:
            return

        trace_blocking_manager = TraceBlockingManager.get_instance()
        if trace_blocking_manager.is_trace_blocked(span.trace_id):
            return

        if should_block_span(span):
            return

        if not should_sample(self._sampling_rate, self.app_ready):
            return

        try:
            span.to_proto()
        except Exception as e:
            logger.error(f"Failed to serialize span to protobuf: {e}")
            return

        if self.mode == TuskDriftMode.REPLAY and span.kind == SpanKind.SERVER:
            try:
                asyncio.create_task(self.send_inbound_span_for_replay(span))
            except RuntimeError:
                pass

        if self.mode == TuskDriftMode.RECORD:
            # Use TdSpanProcessor's internal batch processor
            if self._td_span_processor and self._td_span_processor._batch_processor:
                self._td_span_processor._batch_processor.add_span(span)
            elif self.span_exporter:
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        asyncio.create_task(self.span_exporter.export_spans([span]))
                    else:
                        loop.run_until_complete(self.span_exporter.export_spans([span]))
                except RuntimeError:
                    asyncio.run(self.span_exporter.export_spans([span]))

    async def request_mock_async(self, mock_request: MockRequestInput) -> MockResponseOutput:
        """Request mocked response data from CLI (async)."""
        if self.mode != TuskDriftMode.REPLAY:
            logger.error("Cannot request mock: not in replay mode")
            return MockResponseOutput(found=False, error="Not in replay mode")

        if not self.communicator:
            logger.error("Cannot request mock: communicator not initialized")
            return MockResponseOutput(found=False, error="Communicator not initialized")

        if self._cli_connection_task and not self._is_connected_with_cli:
            logger.debug("Waiting for CLI connection to be established")
            try:
                await self._cli_connection_task
            except Exception as e:
                logger.error(f"Failed to connect to CLI: {e}")
                return MockResponseOutput(found=False, error=f"Connection failed: {e}")

        if not self._is_connected_with_cli:
            return MockResponseOutput(found=False, error="Not connected to CLI")

        try:
            logger.debug(f"Sending protobuf request to CLI (async) {mock_request.test_id}")
            response = await self.communicator.request_mock_async(mock_request)
            logger.debug(f"Received protobuf response from CLI {response.found}")
            return response
        except Exception as e:
            logger.error(f"Error sending protobuf request to CLI: {e}")
            return MockResponseOutput(found=False, error=f"Request failed: {e}")

    def request_mock_sync(self, mock_request: MockRequestInput) -> MockResponseOutput:
        """Request mocked response data from CLI (synchronous)."""
        if self.mode != TuskDriftMode.REPLAY:
            logger.error("Cannot request mock: not in replay mode")
            return MockResponseOutput(found=False, error="Not in replay mode")

        if not self.communicator:
            logger.error("Cannot request mock: communicator not initialized")
            return MockResponseOutput(found=False, error="Communicator not initialized")

        if not self._is_connected_with_cli:
            logger.error("Requesting sync mock but CLI is not ready yet")
            return MockResponseOutput(found=False, error="CLI not connected yet")

        try:
            logger.debug(f"Sending protobuf request to CLI (sync) {mock_request.test_id}")
            response = self.communicator.request_mock_sync(mock_request)
            logger.debug(f"Received protobuf response from CLI {response.found}")
            return response
        except Exception as e:
            logger.error(f"Error sending protobuf request to CLI: {e}")
            return MockResponseOutput(found=False, error=f"Request failed: {e}")

    async def send_inbound_span_for_replay(self, span: CleanSpanData) -> None:
        """Send an inbound span to CLI for replay validation."""
        if self.mode != TuskDriftMode.REPLAY or not self.communicator:
            return

        if not self._is_connected_with_cli:
            return

        try:
            await self.communicator.send_inbound_span_for_replay(span)
        except Exception as e:
            logger.error(f"Failed to send inbound replay span: {e}")

    async def send_instrumentation_version_mismatch_alert(
        self,
        module_name: str,
        requested_version: str | None,
        supported_versions: list[str],
    ) -> None:
        """Send instrumentation version mismatch alert to CLI."""
        if not self.communicator or not self._is_connected_with_cli:
            return

        try:
            await self.communicator.send_instrumentation_version_mismatch_alert(
                module_name, requested_version, supported_versions
            )
        except Exception as e:
            logger.debug(f"Failed to send version mismatch alert: {e}")

    async def send_unpatched_dependency_alert(
        self,
        stack_trace: str,
        trace_test_server_span_id: str,
    ) -> None:
        """Send unpatched dependency alert to CLI."""
        if not self.communicator or not self._is_connected_with_cli:
            return

        try:
            await self.communicator.send_unpatched_dependency_alert(stack_trace, trace_test_server_span_id)
        except Exception as e:
            logger.debug(f"Failed to send unpatched dependency alert: {e}")

    def should_record_root_request(self, *, is_pre_app_start: bool) -> RootSamplingDecision:
        if self._adaptive_sampling_controller is not None:
            return self._adaptive_sampling_controller.get_decision(is_pre_app_start=is_pre_app_start)

        if is_pre_app_start:
            return RootSamplingDecision(
                should_record=True,
                reason="pre_app_start",
                mode="fixed",
                state="fixed",
                base_rate=self._sampling_rate,
                min_rate=self._min_sampling_rate,
                effective_rate=1.0,
                admission_multiplier=1.0,
            )

        should_record = should_sample(self._sampling_rate, True)
        return RootSamplingDecision(
            should_record=should_record,
            reason="sampled" if should_record else "not_sampled",
            mode="fixed",
            state="fixed",
            base_rate=self._sampling_rate,
            min_rate=self._min_sampling_rate,
            effective_rate=self._sampling_rate,
            admission_multiplier=1.0,
        )

    def get_sampling_rate(self) -> float:
        """Get the current sampling rate."""
        return self._sampling_rate

    def get_mode(self) -> TuskDriftMode:
        """Get the current mode."""
        return self.mode

    def is_initialized(self) -> bool:
        """Check if SDK is initialized."""
        return self._initialized

    def is_app_ready(self) -> bool:
        """Check if app is ready."""
        return self.app_ready

    def get_config(self) -> TuskConfig:
        """Get the runtime config."""
        return self.config

    def get_init_params(self) -> dict[str, Any]:
        """Get the init parameters."""
        return self._init_params

    def get_protobuf_communicator(self) -> ProtobufCommunicator | None:
        """Get the communicator instance."""
        return self.communicator

    def shutdown(self) -> None:
        """Shutdown the SDK."""
        import asyncio

        from .coverage_server import stop_coverage_collection

        self._adaptive_sampling_stop_event.set()
        if self._adaptive_sampling_thread is not None:
            self._adaptive_sampling_thread.join(timeout=5.0)
            self._adaptive_sampling_thread = None

        # Shutdown OpenTelemetry tracer provider
        if self._td_span_processor is not None:
            self._td_span_processor.shutdown()

        if self._tracer_provider is not None:
            self._tracer_provider.shutdown()

        if self.span_exporter is not None:
            asyncio.run(self.span_exporter.shutdown())

        if self.communicator:
            try:
                self.communicator.disconnect()
            except Exception as e:
                logger.error(f"Error disconnecting from CLI: {e}")

        try:
            TraceBlockingManager.get_instance().shutdown()
        except Exception as e:
            logger.error(f"Error shutting down trace blocking manager: {e}")

        stop_coverage_collection()
