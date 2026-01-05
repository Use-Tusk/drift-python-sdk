from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import random
import stat
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import SpanKind as OTelSpanKind

from ..instrumentation.registry import install_hooks
from .communication.communicator import CommunicatorConfig, ProtobufCommunicator
from .communication.types import MockRequestInput, MockResponseOutput
from .config import TuskConfig, TuskFileConfig, load_tusk_config
from .logger import LogLevel, configure_logger
from .sampling import should_sample, validate_sampling_rate
from .trace_blocking_manager import TraceBlockingManager, should_block_span
from .tracing import TdSpanAttributes, TdSpanExporter, TdSpanExporterConfig
from .tracing.td_span_processor import TdSpanProcessor
from .types import CleanSpanData, DriftMode, SpanKind

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer

logger = logging.getLogger(__name__)


class TuskDrift:
    """Main SDK singleton managing the Drift SDK lifecycle."""

    _instance: TuskDrift | None = None
    _initialized = False

    def __init__(self) -> None:
        self.mode: DriftMode = self._detect_mode()
        self.config = TuskConfig()
        self.file_config: TuskFileConfig | None = None
        self.app_ready = False
        self._sdk_instance_id = self._generate_sdk_instance_id()
        self._sampling_rate: float = 1.0
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
        if self.mode == "REPLAY" and "SENTRY_DSN" in os.environ:
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

        instance._sampling_rate = instance._determine_sampling_rate(sampling_rate)

        effective_api_key = api_key or os.environ.get("TUSK_API_KEY")

        if not env:
            env_from_var = os.environ.get("NODE_ENV") or "development"
            logger.warning(
                f"Environment not provided in initialization parameters. Using '{env_from_var}' as the environment."
            )
            env = env_from_var

        if cls._initialized:
            logger.debug("Already initialized, skipping...")
            return instance

        file_config = instance.file_config

        if (
            instance.mode == "RECORD"
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

        if instance.mode == "DISABLED":
            logger.debug("SDK disabled via environment variable")
            return instance

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

        from ..version import SDK_VERSION as sdk_version

        exporter_config = TdSpanExporterConfig(
            base_directory=base_dir,
            mode=instance.mode,
            observable_service_id=effective_observable_service_id,
            use_remote_export=use_remote_export,
            api_key=effective_api_key,
            tusk_backend_base_url=effective_backend_url,
            environment=env,
            sdk_version=sdk_version,
            sdk_instance_id=instance._sdk_instance_id,
        )
        instance.span_exporter = TdSpanExporter(exporter_config)

        # Initialize OpenTelemetry TracerProvider
        service_name = effective_observable_service_id or "drift-python-sdk"
        resource = Resource.create(
            {
                "service.name": service_name,
                "service.version": sdk_version,
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

        if instance.mode == "REPLAY":
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

        # Create env vars snapshot if enabled (matches Node SDK behavior)
        instance.create_env_vars_snapshot()

        # Register shutdown handler for graceful cleanup
        atexit.register(instance.shutdown)

        cls._initialized = True
        logger.info("SDK initialized successfully")

        return instance

    def _determine_sampling_rate(self, init_param: float | None) -> float:
        """Determine the sampling rate from various sources (precedence order)."""
        # 1. Init param takes precedence
        if init_param is not None:
            validated = validate_sampling_rate(init_param, "init params")
            if validated is not None:
                logger.debug(f"Using sampling rate from init params: {validated}")
                return validated

        # 2. Environment variable
        env_rate = os.environ.get("TUSK_SAMPLING_RATE")
        if env_rate is not None:
            try:
                parsed = float(env_rate)
                validated = validate_sampling_rate(parsed, "TUSK_SAMPLING_RATE env var")
                if validated is not None:
                    logger.debug(f"Using sampling rate from env var: {validated}")
                    return validated
            except ValueError:
                logger.warning(f"Invalid TUSK_SAMPLING_RATE env var: {env_rate}")

        # 3. Config file
        if self.file_config and self.file_config.recording and self.file_config.recording.sampling_rate is not None:
            config_rate = self.file_config.recording.sampling_rate
            validated = validate_sampling_rate(config_rate, "config file")
            if validated is not None:
                logger.debug(f"Using sampling rate from config file: {validated}")
                return validated

        # 4. Default
        logger.debug("Using default sampling rate: 1.0")
        return 1.0

    def _detect_mode(self) -> DriftMode:
        """Detect the SDK mode from environment variable."""
        mode_env = os.environ.get("TUSK_DRIFT_MODE", "").upper()

        if mode_env == "RECORD":
            return "RECORD"
        elif mode_env == "REPLAY":
            return "REPLAY"
        elif mode_env in ("DISABLED", "DISABLE"):
            return "DISABLED"
        else:
            # Default to DISABLED if no mode specified (matches Node SDK)
            return "DISABLED"

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

        logger.info(f"[INIT_COMPLETE] SDK initialization finished. Connected to CLI: {self._is_connected_with_cli}")
        if self.communicator and self.communicator.is_connected:
            logger.info("[INIT_COMPLETE] Communicator reports connected: True")
        else:
            logger.info("[INIT_COMPLETE] Communicator reports connected: False")

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

        # Initialize PostgreSQL instrumentation before Django
        # Instrument BOTH psycopg2 and psycopg if available
        # This allows apps to use either or both
        psycopg2_available = False
        psycopg_available = False

        # Try psycopg2 first
        try:
            import psycopg2  # type: ignore[unresolved-import]

            from ..instrumentation.psycopg2 import Psycopg2Instrumentation

            _ = Psycopg2Instrumentation()
            logger.debug("Psycopg2 instrumentation initialized")
            psycopg2_available = True
        except ImportError:
            pass

        # Try psycopg (v3)
        try:
            import psycopg  # type: ignore[unresolved-import]

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
            import redis  # type: ignore[unresolved-import]

            from ..instrumentation.redis import RedisInstrumentation

            _ = RedisInstrumentation()
            logger.debug("Redis instrumentation initialized")
        except ImportError:
            pass

        try:
            import django  # type: ignore[unresolved-import]

            from ..instrumentation.django import DjangoInstrumentation

            _ = DjangoInstrumentation()
            logger.debug("Django instrumentation initialized")
        except ImportError:
            pass

        if self.file_config and self.file_config.recording and self.file_config.recording.enable_env_var_recording:
            # try:
            #     from ..instrumentation.env import EnvInstrumentation

            #     _ = EnvInstrumentation(enabled=True)
            #     logger.debug("Env instrumentation initialized")
            # except Exception:
            #     pass
            pass

    def create_env_vars_snapshot(self) -> None:
        """Create a span capturing all environment variables.

        Only creates the snapshot in RECORD mode when enable_env_var_recording is True.
        This matches the Node SDK behavior for capturing env vars at initialization.
        """
        if self.mode != "RECORD":
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

    def get_tracer(self, name: str = "drift", version: str = "") -> Tracer:
        """Get OpenTelemetry tracer.

        Args:
            name: Tracer name (default: "drift")
            version: Tracer version (default: SDK version)

        Returns:
            OpenTelemetry Tracer instance
        """
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
            if self.mode != "DISABLED":
                logger.error("mark_app_as_ready() called before initialize(). Call initialize() first.")
            return

        self.app_ready = True

        # Update span processor with app_ready flag
        if self._td_span_processor:
            self._td_span_processor.update_app_ready(True)

        logger.debug("Application marked as ready")

        if self.mode == "REPLAY":
            logger.debug("Replay mode active - ready to serve mocked responses")
        elif self.mode == "RECORD":
            logger.debug("Record mode active - capturing inbound requests and responses")

    def collect_span(self, span: CleanSpanData) -> None:
        """[DEPRECATED] Collect and export a span.

        This method is deprecated and maintained for backward compatibility only.
        New instrumentations should use OpenTelemetry spans via SpanUtils instead.
        """
        if self.mode == "DISABLED":
            return

        trace_blocking_manager = TraceBlockingManager.get_instance()
        if trace_blocking_manager.is_trace_blocked(span.trace_id):
            return

        if should_block_span(span):
            logger.warning(
                f"Blocking trace {span.trace_id} - span '{span.name}' exceeds size limit. Future spans for this trace will be prevented."
            )
            return

        if not should_sample(self._sampling_rate, self.app_ready):
            return

        try:
            span.to_proto()
        except Exception as e:
            logger.error(f"Failed to serialize span to protobuf: {e}")
            return

        if self.mode == "REPLAY" and span.kind == SpanKind.SERVER:
            try:
                asyncio.create_task(self.send_inbound_span_for_replay(span))
            except RuntimeError:
                pass

        if self.mode == "RECORD":
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
        if self.mode != "REPLAY":
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
        if self.mode != "REPLAY":
            logger.error("Cannot request mock: not in replay mode")
            return MockResponseOutput(found=False, error="Not in replay mode")

        if not self.communicator:
            logger.error("Cannot request mock: communicator not initialized")
            return MockResponseOutput(found=False, error="Communicator not initialized")

        if not self._is_connected_with_cli:
            logger.error("Requesting sync mock but CLI is not ready yet")
            return MockResponseOutput(found=False, error="CLI not connected yet")

        if not self._is_connected_with_cli:
            logger.error("Requesting sync mock but CLI is not ready yet")
            raise RuntimeError("Requesting sync mock but CLI is not ready yet")

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
        if self.mode != "REPLAY" or not self.communicator:
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
        except Exception:
            pass

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
        except Exception:
            pass

    def get_sampling_rate(self) -> float:
        """Get the current sampling rate."""
        return self._sampling_rate

    def get_mode(self) -> DriftMode:
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

        # Shutdown OpenTelemetry tracer provider
        if self._td_span_processor is not None:
            self._td_span_processor.shutdown()

        if self._tracer_provider is not None:
            self._tracer_provider.shutdown()

        if self.span_exporter is not None:
            asyncio.run(self.span_exporter.shutdown())

        if self.communicator:
            try:
                asyncio.run(self.communicator.disconnect())
            except Exception as e:
                logger.error(f"Error disconnecting from CLI: {e}")

        try:
            TraceBlockingManager.get_instance().shutdown()
        except Exception as e:
            logger.error(f"Error shutting down trace blocking manager: {e}")
