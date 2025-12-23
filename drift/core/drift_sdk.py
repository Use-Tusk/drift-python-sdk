"""Core TuskDrift SDK singleton and lifecycle management."""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import random
import stat
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..instrumentation.registry import install_hooks
from .communication.communicator import CommunicatorConfig, ProtobufCommunicator
from .communication.types import MockRequestInput, MockResponseOutput
from .config import TuskConfig, TuskFileConfig, load_tusk_config
from .logger import LogLevel, configure_logger
from .sampling import should_sample, validate_sampling_rate
from .trace_blocking_manager import TraceBlockingManager, should_block_span
from .tracing import TdSpanExporter, TdSpanExporterConfig
from .types import CleanSpanData, DriftMode, SpanKind

if TYPE_CHECKING:
    from .batch_processor import BatchSpanProcessor

logger = logging.getLogger(__name__)


class TuskDrift:
    """
    Main SDK singleton managing the Drift SDK lifecycle.

    Supports multiple span export adapters:
    - InMemorySpanAdapter: Always enabled for testing/debugging
    - FilesystemSpanAdapter: Enabled when export_directory is provided
    - ApiSpanAdapter: Enabled when api_key and observable_service_id are provided

    Features:
    - Sampling: Configurable sampling rate (0.0 to 1.0)
    - Batching: Spans are batched and exported periodically for efficiency
    """

    _instance: TuskDrift | None = None
    _initialized = False

    def __init__(self) -> None:
        self.mode: DriftMode = self._detect_mode()

        # Clear SENTRY_DSN in REPLAY mode to prevent replay traffic from hitting production Sentry
        # (matches Node SDK behavior: src/core/TuskDrift.ts:388-394)
        if self.mode == "REPLAY" and "SENTRY_DSN" in os.environ:
            logger.debug(
                "REPLAY mode detected - clearing SENTRY_DSN to prevent replay traffic from hitting Sentry"
            )
            del os.environ["SENTRY_DSN"]

        self.config = TuskConfig()
        self.file_config: TuskFileConfig | None = None
        self.app_ready = False
        self._sdk_instance_id = self._generate_sdk_instance_id()
        self._sampling_rate: float = 1.0
        self._transform_configs: dict[str, Any] | None = None
        self._init_params: dict[str, Any] = {}

        self.span_exporter: TdSpanExporter | None = None
        self.batch_processor: "BatchSpanProcessor | None" = None

        self.communicator: ProtobufCommunicator | None = None
        self._cli_connection_task: asyncio.Task | None = None
        self._is_connected_with_cli = False

        # Load config file early
        self.file_config = load_tusk_config()

    @classmethod
    def get_instance(cls) -> TuskDrift:
        """Get the singleton TuskDrift instance."""
        if cls._instance is None:
            cls._instance = TuskDrift()
        return cls._instance

    def _generate_sdk_instance_id(self) -> str:
        """Generate a unique SDK instance ID matching Node SDK pattern."""
        timestamp_ms = int(time.time() * 1000)
        random_suffix = "".join(
            random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=9)
        )
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
        """
        Initialize the TuskDrift SDK.

        Configuration precedence (highest to lowest):
        1. Initialization parameters (this function's arguments)
        2. Environment variables
        3. YAML configuration (.tusk/config.yaml)
        4. Built-in defaults

        Args:
            api_key: Tusk API key for remote export. Can also be set via TUSK_API_KEY env var.
            env: Environment name (e.g., "development", "production"). Defaults to NODE_ENV env var or "development".
            sampling_rate: Fraction of traces to capture (0.0 to 1.0). Can also be set via TUSK_SAMPLING_RATE env var.
            transforms: Optional transform configuration for PII redaction/masking. Overrides config file transforms.
            log_level: Logging level (silent, error, warn, info, debug). Default: info

        Returns:
            The initialized TuskDrift instance
        """
        instance = cls.get_instance()

        # Configure logger FIRST (before any logging calls)
        configure_logger(log_level=log_level, prefix="TuskDrift")

        # Store init params
        instance._init_params = {
            "api_key": api_key,
            "env": env,
            "sampling_rate": sampling_rate,
            "transforms": transforms,
            "log_level": log_level,
        }

        # Determine sampling rate early (needed for logging)
        instance._sampling_rate = instance._determine_sampling_rate(sampling_rate)

        # Determine API key with precedence: init param > env var
        effective_api_key = api_key or os.environ.get("TUSK_API_KEY")

        # Handle env fallback to environment variable
        # Precedence: init param > NODE_ENV env var > default
        if not env:
            env_from_var = os.environ.get("NODE_ENV") or "development"
            logger.debug(
                f"Environment not provided in initialization parameters. Using '{env_from_var}' from NODE_ENV or default."
            )
            env = env_from_var

        if cls._initialized:
            logger.debug("Already initialized, skipping...")
            return instance

        file_config = instance.file_config

        # Validate mode and configuration early
        # Validate API key when export_spans is enabled
        if (
            instance.mode == "RECORD"
            and file_config
            and file_config.recording
            and file_config.recording.export_spans
            and not effective_api_key
        ):
            logger.error(
                "In record mode and export_spans is true, but API key not provided. "
                "API key is required to export spans to Tusk backend. "
                "Please provide an API key via the 'api_key' parameter or TUSK_API_KEY environment variable."
            )
            return instance

        # Read service ID from config file only (not an init param per spec)
        effective_observable_service_id = None
        if file_config and file_config.service:
            effective_observable_service_id = file_config.service.id

        # Validate observable_service_id if export_spans is enabled
        export_spans_enabled = (
            file_config.recording.export_spans
            if file_config and file_config.recording
            else False
        )

        if export_spans_enabled and not effective_observable_service_id:
            logger.error(
                "Observable service ID not provided. "
                "Please provide an observable service ID in the configuration file."
            )
            return instance

        if instance.mode == "DISABLED":
            logger.debug("SDK disabled via environment variable")
            return instance

        logger.debug(f"Initializing in {instance.mode} mode")

        # Transform config precedence: init param > config file (per spec)
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

        # Read export directory from config file only (not an init param per spec)
        effective_export_directory = None
        if file_config and file_config.traces and file_config.traces.dir:
            effective_export_directory = file_config.traces.dir

        # Read tusk backend URL from config file (not an init param per spec)
        effective_backend_url = "https://api.usetusk.ai"
        if file_config and file_config.tusk_api and file_config.tusk_api.url:
            effective_backend_url = file_config.tusk_api.url

        # Base directory for local trace storage
        base_dir = (
            Path(effective_export_directory)
            if effective_export_directory
            else Path.cwd() / ".tusk" / "traces"
        )

        logger.debug(f"Config: {file_config}")
        logger.debug(f"Base directory: {base_dir}")

        # Determine if remote export should be used
        use_remote_export = bool(
            export_spans_enabled
            and effective_api_key is not None
            and effective_observable_service_id is not None
        )

        # Get SDK version from package metadata
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

        # Always enable batch processor for efficient export
        from .batch_processor import BatchSpanProcessor, BatchSpanProcessorConfig

        instance.batch_processor = BatchSpanProcessor(
            exporter=instance.span_exporter,
            config=BatchSpanProcessorConfig(),  # Uses defaults
        )
        instance.batch_processor.start()
        logger.info("Batch span processor started")

        if instance.mode == "REPLAY":
            instance._init_communicator_for_replay()

        install_hooks()

        instance._init_auto_instrumentations()

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
        if (
            self.file_config
            and self.file_config.recording
            and self.file_config.recording.sampling_rate is not None
        ):
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
        print("[SDK_STARTUP] Entering _init_communicator_for_replay()", flush=True)

        # Check for TCP or Unix socket mode
        mock_host = os.environ.get("TUSK_MOCK_HOST")
        mock_port = os.environ.get("TUSK_MOCK_PORT")
        mock_socket = os.environ.get("TUSK_MOCK_SOCKET")

        print(
            f"[SDK_STARTUP] Environment: TUSK_MOCK_HOST={mock_host}, TUSK_MOCK_PORT={mock_port}, TUSK_MOCK_SOCKET={mock_socket}",
            flush=True,
        )

        connection_info: dict[str, Any]

        if mock_host and mock_port:
            # TCP mode (Docker)
            connection_info = {
                "host": mock_host,
                "port": int(mock_port),
            }
            print(
                f"[SDK_STARTUP] Using TCP connection to CLI: {mock_host}:{mock_port}",
                flush=True,
            )
        else:
            # Unix socket mode (default)
            socket_path = mock_socket or "/tmp/tusk-connect.sock"

            # Check if socket exists and is ready
            socket_file = Path(socket_path)
            if not socket_file.exists():
                print(
                    f"[SDK_STARTUP] ERROR: Socket not found at {socket_path}",
                    flush=True,
                )
                raise FileNotFoundError(
                    f"Socket not found at {socket_path}. Make sure Tusk CLI is running."
                )

            # Check if it's actually a socket
            file_stat = socket_file.stat()
            if not stat.S_ISSOCK(file_stat.st_mode):
                print(
                    f"[SDK_STARTUP] ERROR: Path exists but is not a socket: {socket_path}",
                    flush=True,
                )
                raise ValueError(f"Path exists but is not a socket: {socket_path}")

            print(
                f"[SDK_STARTUP] Socket found and verified at {socket_path}", flush=True
            )
            connection_info = {"socketPath": socket_path}

        # Initialize communicator
        print("[SDK_STARTUP] Creating ProtobufCommunicator...", flush=True)
        self.communicator = ProtobufCommunicator(CommunicatorConfig.from_env())
        print("[SDK_STARTUP] ProtobufCommunicator created successfully", flush=True)

        # Connect synchronously - REPLAY mode doesn't need async performance
        # This avoids asyncio event loop conflicts with Django/Flask/FastAPI
        print("[SDK_STARTUP] Starting synchronous connection to CLI...", flush=True)

        def connect_to_cli_sync():
            try:
                print(
                    f"[SDK_STARTUP] In connect_to_cli_sync, connection_info={connection_info}",
                    flush=True,
                )
                service_id: str = (
                    self.file_config.service.id
                    if self.file_config
                    and self.file_config.service
                    and self.file_config.service.id
                    else "unknown"
                )
                print(f"[SDK_STARTUP] Service ID: {service_id}", flush=True)

                # Use asyncio.run for clean synchronous execution
                async def async_connect():
                    print("[SDK_STARTUP] Calling communicator.connect()...", flush=True)
                    if self.communicator:
                        await self.communicator.connect(connection_info, service_id)
                        print(
                            "[SDK_STARTUP] communicator.connect() completed", flush=True
                        )

                asyncio.run(async_connect())
                self._is_connected_with_cli = True
                print("[SDK_STARTUP] SDK successfully connected to CLI!", flush=True)
            except Exception as e:
                print(f"[SDK_STARTUP] ERROR: Failed to connect to CLI: {e}", flush=True)
                import traceback

                traceback.print_exc()
                self._is_connected_with_cli = False
                # Don't raise - allow SDK to continue without connection

        connect_to_cli_sync()
        print(
            f"[SDK_STARTUP] Exiting _init_communicator_for_replay(), connected={self._is_connected_with_cli}",
            flush=True,
        )

    def _init_auto_instrumentations(self) -> None:
        """Auto-detect and initialize all available instrumentations."""
        try:
            import flask  # pyright: ignore[reportUnusedImport]

            from ..instrumentation.flask import FlaskInstrumentation

            _ = FlaskInstrumentation()
            logger.info("initialized flask instrumentation")
        except ImportError:
            logger.warning("failed to initialize flask instrumentation")

        try:
            import fastapi  # pyright: ignore[reportUnusedImport]

            from ..instrumentation.fastapi import FastAPIInstrumentation

            _ = FastAPIInstrumentation()
            logger.info("initialized fastapi instrumentation")
        except ImportError:
            logger.warning("failed to initialize fastapi instrumentation")

        try:
            import requests  # pyright: ignore[reportUnusedImport]

            from ..instrumentation.requests import RequestsInstrumentation

            _ = RequestsInstrumentation()
            logger.info("initialized requests instrumentation")
        except ImportError:
            logger.warning("failed to initialize requests instrumentation")

        # Initialize PostgreSQL instrumentation BEFORE Django so Django can use the instrumented cursors
        # Try psycopg (v3) first (newer, pure Python), then fallback to psycopg2
        psycopg_initialized = False
        try:
            import psycopg  # pyright: ignore[reportUnusedImport]

            from ..instrumentation.psycopg import PsycopgInstrumentation

            _ = PsycopgInstrumentation()
            logger.info("initialized psycopg (v3) instrumentation")
            psycopg_initialized = True
        except ImportError:
            pass  # Try psycopg2 next
        
        if not psycopg_initialized:
            try:
                import psycopg2  # pyright: ignore[reportUnusedImport]

                from ..instrumentation.psycopg2 import Psycopg2Instrumentation

                _ = Psycopg2Instrumentation()
                logger.info("initialized psycopg2 instrumentation")
                psycopg_initialized = True
            except ImportError:
                pass
        
        if not psycopg_initialized:
            logger.warning("failed to initialize PostgreSQL instrumentation (neither psycopg nor psycopg2 found)")

        try:
            import django  # pyright: ignore[reportUnusedImport]

            from ..instrumentation.django import DjangoInstrumentation

            _ = DjangoInstrumentation()
            logger.info("initialized django instrumentation")
        except ImportError:
            logger.warning("failed to initialize django instrumentation")

        # Initialize env instrumentation if enabled
        if (
            self.file_config
            and self.file_config.recording
            and self.file_config.recording.enable_env_var_recording
        ):
            try:
                from ..instrumentation.env import EnvInstrumentation

                _ = EnvInstrumentation(enabled=True)
                logger.info("initialized env instrumentation")
            except Exception as e:
                logger.warning(f"failed to initialize env instrumentation: {e}")

    def mark_app_as_ready(self) -> None:
        """Mark the application as ready (started listening)."""
        if not self._initialized:
            if self.mode != "DISABLED":
                logger.error(
                    "mark_app_as_ready() called before initialize(). Call initialize() first."
                )
            return

        self.app_ready = True
        logger.debug("Application marked as ready")

        if self.mode == "REPLAY":
            logger.debug("Replay mode active - ready to serve mocked responses")
        elif self.mode == "RECORD":
            logger.debug(
                "Record mode active - capturing inbound requests and responses"
            )

    def collect_span(self, span: CleanSpanData) -> None:
        """
        Collect a span and export to configured adapters (mode-aware).

        Behavior by mode:
        - DISABLED: Skip collection
        - RECORD: Export to filesystem/API adapters (respects sampling + trace blocking)
        - REPLAY: Send inbound SERVER spans to CLI, store in memory
        """
        if self.mode == "DISABLED":
            return

        # Check if trace is blocked (exceeds size limit)
        trace_blocking_manager = TraceBlockingManager.get_instance()
        if trace_blocking_manager.is_trace_blocked(span.trace_id):
            logger.debug(f"Span {span.name} skipped - trace {span.trace_id} is blocked")
            return

        # Check for oversized spans and block entire trace if needed
        if should_block_span(span):
            logger.warning(
                f"Span {span.name} blocked due to size - blocking trace {span.trace_id}"
            )
            return

        # Check sampling
        if not should_sample(self._sampling_rate, self.app_ready):
            logger.debug(f"Span {span.name} not sampled")
            return

        # Validate span can be serialized to protobuf
        try:
            span.to_proto()
        except Exception as e:
            logger.error(f"Failed to serialize span to protobuf: {e}")
            logger.error(
                f"  Span name: {span.name}, package: {span.package_name}, trace_id: {span.trace_id}"
            )
            logger.error(
                f"  input_value type: {type(span.input_value)}, output_value type: {type(span.output_value)}"
            )
            logger.error(f"  input_value: {span.input_value}")
            logger.error(f"  output_value: {span.output_value}")
            import traceback

            logger.error(f"  Traceback: {traceback.format_exc()}")
            return

        # REPLAY mode: Send inbound SERVER spans to CLI
        if self.mode == "REPLAY" and span.kind == SpanKind.SERVER:
            try:
                asyncio.create_task(self.send_inbound_span_for_replay(span))
            except RuntimeError:
                # No event loop running, skip CLI communication
                pass

        # Export via batch processor (only in RECORD mode)
        if self.mode == "RECORD":
            if self.batch_processor:
                # Batched export (default, recommended)
                self.batch_processor.add_span(span)
            elif self.span_exporter:
                # Fallback to immediate export (when batch processor not initialized)
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        asyncio.create_task(self.span_exporter.export_spans([span]))
                    else:
                        loop.run_until_complete(self.span_exporter.export_spans([span]))
                except RuntimeError:
                    asyncio.run(self.span_exporter.export_spans([span]))

    async def request_mock_async(
        self, mock_request: MockRequestInput
    ) -> MockResponseOutput:
        """Request mocked response data from CLI (async).

        Args:
            mock_request: Mock request with test_id and outbound_span

        Returns:
            MockResponseOutput with found status and response data
        """
        if self.mode != "REPLAY":
            logger.error("Cannot request mock: not in replay mode")
            return MockResponseOutput(found=False, error="Not in replay mode")

        if not self.communicator:
            logger.error("Cannot request mock: communicator not initialized")
            return MockResponseOutput(found=False, error="Communicator not initialized")

        # Wait for CLI connection if not yet established
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
            logger.debug(
                f"Sending mock request to CLI (async), testId: {mock_request.test_id}"
            )
            response = await self.communicator.request_mock_async(mock_request)
            logger.debug(f"Received mock response from CLI: found={response.found}")
            return response
        except Exception as e:
            logger.error(f"Error sending mock request to CLI: {e}")
            return MockResponseOutput(found=False, error=f"Request failed: {e}")

    def request_mock_sync(self, mock_request: MockRequestInput) -> MockResponseOutput:
        """Request mocked response data from CLI (synchronous).

        This blocks the current thread. Use for instrumentations that
        require synchronous mock fetching.

        Args:
            mock_request: Mock request with test_id and outbound_span

        Returns:
            MockResponseOutput with found status and response data
        """
        if self.mode != "REPLAY":
            logger.error("Cannot request mock: not in replay mode")
            return MockResponseOutput(found=False, error="Not in replay mode")

        if not self.communicator:
            logger.error("Cannot request mock: communicator not initialized")
            return MockResponseOutput(found=False, error="Communicator not initialized")

        if not self._is_connected_with_cli:
            logger.error("Cannot request mock: not connected to CLI")
            return MockResponseOutput(found=False, error="Not connected to CLI")

        try:
            logger.debug(
                f"Sending mock request to CLI (sync), testId: {mock_request.test_id}"
            )
            response = self.communicator.request_mock_sync(mock_request)
            logger.debug(f"Received mock response from CLI: found={response.found}")
            return response
        except Exception as e:
            import traceback

            logger.error(
                f"Error sending mock request to CLI: {e}\n{traceback.format_exc()}"
            )
            return MockResponseOutput(found=False, error=f"Request failed: {e}")

    def request_env_vars_sync(self, trace_test_server_span_id: str) -> dict[str, str]:
        """Request environment variables from CLI (synchronous).

        Args:
            trace_test_server_span_id: Span ID for trace context

        Returns:
            Dictionary of environment variable names to values
        """
        if self.mode != "REPLAY":
            logger.debug("Cannot request env vars: not in replay mode")
            return {}

        if not self.communicator:
            logger.debug("Cannot request env vars: communicator not initialized")
            return {}

        if not self._is_connected_with_cli:
            logger.error("Cannot request env vars: not connected to CLI")
            return {}

        try:
            logger.debug(
                f"Requesting env vars (sync) for trace: {trace_test_server_span_id}"
            )
            env_vars = self.communicator.request_env_vars_sync(
                trace_test_server_span_id
            )
            logger.debug(f"Received env vars from CLI, count: {len(env_vars)}")
            return env_vars
        except Exception as e:
            logger.error(f"Error requesting env vars from CLI: {e}")
            return {}

    async def send_inbound_span_for_replay(self, span: CleanSpanData) -> None:
        """Send an inbound span to CLI for replay validation.

        Args:
            span: The inbound span data to send
        """
        if self.mode != "REPLAY" or not self.communicator:
            return

        if not self._is_connected_with_cli:
            logger.debug("Cannot send inbound span: not connected to CLI")
            return

        try:
            await self.communicator.send_inbound_span_for_replay(span)
        except Exception as e:
            logger.error(f"Error sending inbound span to CLI: {e}")

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
            await self.communicator.send_unpatched_dependency_alert(
                stack_trace, trace_test_server_span_id
            )
        except Exception:
            pass  # Alerts are non-critical

    @property
    def sampling_rate(self) -> float:
        """Get the current sampling rate."""
        return self._sampling_rate

    def get_sampling_rate(self) -> float:
        """Get the current sampling rate (method for compatibility)."""
        return self._sampling_rate

    def get_transform_configs(self) -> dict[str, Any] | None:
        """Return transform configuration passed during initialization (if any)."""
        return self._transform_configs

    def get_file_config(self) -> TuskFileConfig | None:
        """Get the loaded config file (if any)."""
        return self.file_config

    def shutdown(self) -> None:
        """Shutdown the SDK and all adapters."""
        import asyncio

        # Stop batch processor first (flushes remaining spans)
        if self.batch_processor is not None:
            self.batch_processor.stop(timeout=30.0)
            logger.debug("Batch processor stopped and flushed")

        if self.span_exporter is not None:
            asyncio.run(self.span_exporter.shutdown())
            logger.debug("Span exporter shut down")

        if self.communicator:
            try:
                asyncio.run(self.communicator.disconnect())
                logger.debug("Disconnected from CLI")
            except Exception as e:
                logger.error(f"Error disconnecting from CLI: {e}")

        try:
            TraceBlockingManager.get_instance().shutdown()
        except Exception as e:
            logger.error(f"Error shutting down trace blocking manager: {e}")

        print("Drift SDK shutdown complete")
