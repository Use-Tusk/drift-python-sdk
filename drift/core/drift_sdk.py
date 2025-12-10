"""Core TuskDrift SDK singleton and lifecycle management."""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import random
import stat
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..instrumentation.registry import install_hooks
from .communication.communicator import CommunicatorConfig, ProtobufCommunicator
from .communication.types import MockRequestInput, MockResponseOutput
from .config import TuskConfig, TuskFileConfig, load_tusk_config
from .sampling import should_sample, validate_sampling_rate
from .trace_blocking_manager import TraceBlockingManager, should_block_span
from .tracing import TdSpanExporter, TdSpanExporterConfig
from .types import CleanSpanData, DriftMode, SpanKind

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# List of instrumentation module names that should not be imported before SDK initialization
# Mirrors TuskDriftInstrumentationModuleNames in Node SDK
INSTRUMENTATION_MODULE_NAMES = [
    "flask",
    "fastapi",
    "starlette",
    "requests",
    "django",
    "httpx",
    # Add more as instrumentations are developed
]


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
        self.config = TuskConfig()
        self.file_config: TuskFileConfig | None = None
        self.app_ready = False
        self._sdk_instance_id = self._generate_sdk_instance_id()
        self._sampling_rate: float = 1.0
        self._transform_configs: dict[str, Any] | None = None
        self._init_params: dict[str, Any] = {}

        self.span_exporter: TdSpanExporter | None = None

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
        random_suffix = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=9))
        return f"sdk-{timestamp_ms}-{random_suffix}"

    def _already_imported_modules(self) -> set[str]:
        """
        Check which instrumentation modules are already imported.
        Mirrors alreadyRequiredModules() in Node SDK.
        """
        already_imported = set()

        for module_name in INSTRUMENTATION_MODULE_NAMES:
            if module_name in sys.modules:
                already_imported.add(module_name)

        return already_imported

    @classmethod
    def initialize(
        cls,
        api_key: str | None = None,
        env: str | None = None,
        sampling_rate: float | None = None,
        observable_service_id: str | None = None,
        export_directory: str | Path | None = None,
        tusk_backend_base_url: str = "https://api.usetusk.ai",
        sdk_version: str = "0.1.0",
        transforms: dict[str, Any] | None = None,
    ) -> TuskDrift:
        """
        Initialize the TuskDrift SDK.

        Args:
            api_key: Tusk API key for remote export
            env: Environment name (e.g., "development", "production")
            sampling_rate: Fraction of traces to capture (0.0 to 1.0)
            observable_service_id: ID of the observable service in Tusk (optional, will read from config if not provided)
            export_directory: Directory for filesystem export (optional)
            tusk_backend_base_url: Base URL for the Tusk backend
            sdk_version: Version of the SDK
            transforms: Optional transform configuration matching the Node SDK schema

        Returns:
            The initialized TuskDrift instance
        """
        instance = cls.get_instance()

        # Store init params
        instance._init_params = {
            "api_key": api_key,
            "env": env,
            "sampling_rate": sampling_rate,
            "transforms": transforms,
        }

        # Determine sampling rate early (needed for logging)
        instance._sampling_rate = instance._determine_sampling_rate(sampling_rate)

        # Handle env fallback to environment variable
        if not env:
            env_from_var = os.environ.get("PYTHON_ENV") or os.environ.get("ENV") or "development"
            logger.warning(
                f"Environment not provided in initialization parameters. Using '{env_from_var}' as the environment."
            )
            env = env_from_var

        if cls._initialized:
            logger.debug("Already initialized, skipping...")
            return instance

        file_config = instance.file_config

        # Validate mode and configuration early
        if (
            instance.mode == "RECORD"
            and file_config
            and file_config.recording
            and file_config.recording.export_spans
            and not api_key
        ):
            logger.error(
                "In record mode and export_spans is true, but API key not provided. "
                "API key is required to export spans to Tusk backend. "
                "Please provide an API key in the initialization parameters."
            )
            return instance

        # Read observable_service_id from config if not provided (matches Node SDK)
        effective_observable_service_id = observable_service_id
        if not effective_observable_service_id and file_config and file_config.service:
            effective_observable_service_id = file_config.service.id

        # Validate observable_service_id if export_spans is enabled
        export_spans_enabled = (
            file_config.recording.export_spans if file_config and file_config.recording else False
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

        # Check if any instrumentation modules are already imported (matches Node SDK)
        already_imported = instance._already_imported_modules()
        if already_imported:
            module_list = ", ".join(sorted(already_imported))
            message = (
                f"TuskDrift must be initialized before any other modules are imported. "
                f"This ensures TuskDrift is able to instrument the imported modules. "
                f"{module_list} {'are' if len(already_imported) > 1 else 'is'} already imported."
            )

            if instance.mode == "RECORD":
                logger.warning(f"{message} TuskDrift is now disabled and will continue in disabled mode.")
                instance.mode = "DISABLED"
                return instance
            elif instance.mode == "REPLAY":
                logger.error(f"{message} TuskDrift will not run in replay mode. Stopping the app.")
                import sys
                sys.exit(1)

        # Transform config precedence: config file > init param (matches Node SDK)
        effective_transforms = file_config.transforms if file_config and file_config.transforms else transforms
        instance._transform_configs = effective_transforms

        instance.config = TuskConfig(
            api_key=api_key,
            env=env,
            sampling_rate=sampling_rate or 1.0,
            transforms=effective_transforms,
        )

        effective_export_directory = export_directory
        if (
            effective_export_directory is None
            and file_config
            and file_config.traces
            and file_config.traces.dir
        ):
            effective_export_directory = file_config.traces.dir

        # Determine tusk backend URL: init param > config file > default
        effective_backend_url = tusk_backend_base_url
        if (
            tusk_backend_base_url == "https://api.usetusk.ai"
            and file_config
            and file_config.tusk_api
            and file_config.tusk_api.url
        ):
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
        use_remote_export = (
            export_spans_enabled and api_key is not None and effective_observable_service_id is not None
        )

        exporter_config = TdSpanExporterConfig(
            base_directory=base_dir,
            mode=instance.mode,
            observable_service_id=effective_observable_service_id,
            use_remote_export=use_remote_export,
            api_key=api_key,
            tusk_backend_base_url=effective_backend_url,
            environment=env,
            sdk_version=sdk_version,
            sdk_instance_id=instance._sdk_instance_id,
        )
        instance.span_exporter = TdSpanExporter(exporter_config)

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
        # Check for TCP or Unix socket mode
        mock_host = os.environ.get("TUSK_MOCK_HOST")
        mock_port = os.environ.get("TUSK_MOCK_PORT")
        mock_socket = os.environ.get("TUSK_MOCK_SOCKET")

        connection_info: dict[str, Any]

        if mock_host and mock_port:
            # TCP mode (Docker)
            connection_info = {
                "host": mock_host,
                "port": int(mock_port),
            }
            logger.debug(f"Using TCP connection to CLI: {mock_host}:{mock_port}")
        else:
            # Unix socket mode (default)
            socket_path = mock_socket or "/tmp/tusk-connect.sock"

            # Check if socket exists and is ready
            socket_file = Path(socket_path)
            if not socket_file.exists():
                raise FileNotFoundError(
                    f"Socket not found at {socket_path}. Make sure Tusk CLI is running."
                )

            # Check if it's actually a socket
            file_stat = socket_file.stat()
            if not stat.S_ISSOCK(file_stat.st_mode):
                raise ValueError(f"Path exists but is not a socket: {socket_path}")

            logger.debug(f"Socket found and verified at {socket_path}")
            connection_info = {"socketPath": socket_path}

        # Initialize communicator
        self.communicator = ProtobufCommunicator(CommunicatorConfig.from_env())

        # Start connection early (will be awaited on first mock request)
        async def connect_to_cli():
            try:
                service_id = (
                    self.file_config.service.id
                    if self.file_config and self.file_config.service
                    else "unknown"
                )
                await self.communicator.connect(connection_info, service_id)
                self._is_connected_with_cli = True
                logger.debug("SDK successfully connected to CLI")
            except Exception as e:
                logger.error(f"Failed to connect to CLI: {e}")
                self._is_connected_with_cli = False

        # Start connection task
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                self._cli_connection_task = loop.create_task(connect_to_cli())
            else:
                # Event loop exists but not running - run connection synchronously
                loop.run_until_complete(connect_to_cli())
        except RuntimeError:
            # No event loop running yet - create one and connect synchronously
            # This is critical for tusk CLI which expects immediate acknowledgement
            logger.debug("No event loop found, creating one to connect to CLI")
            asyncio.run(connect_to_cli())

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

    def mark_app_as_ready(self) -> None:
        """Mark the application as ready (started listening)."""
        if not self._initialized:
            if self.mode != "DISABLED":
                logger.error("mark_app_as_ready() called before initialize(). Call initialize() first.")
            return

        self.app_ready = True
        logger.debug("Application marked as ready")

        if self.mode == "REPLAY":
            logger.debug("Replay mode active - ready to serve mocked responses")
        elif self.mode == "RECORD":
            logger.debug("Record mode active - capturing inbound requests and responses")

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
            return

        # REPLAY mode: Send inbound SERVER spans to CLI
        if self.mode == "REPLAY" and span.kind == SpanKind.SERVER:
            try:
                asyncio.create_task(self.send_inbound_span_for_replay(span))
            except RuntimeError:
                # No event loop running, skip CLI communication
                pass

        # Export to all adapters via span exporter
        if self.span_exporter:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # Event loop is running, create task
                    asyncio.create_task(self.span_exporter.export_spans([span]))
                else:
                    # Event loop exists but not running, run synchronously
                    loop.run_until_complete(self.span_exporter.export_spans([span]))
            except RuntimeError:
                # No event loop, create one and run synchronously
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
            logger.error(f"Error sending mock request to CLI: {e}")
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
