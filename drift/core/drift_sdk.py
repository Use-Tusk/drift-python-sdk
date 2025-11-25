"""Core TuskDrift SDK singleton and lifecycle management."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .batch_processor import BatchSpanProcessor, BatchSpanProcessorConfig
from .communication.communicator import CommunicatorConfig, ProtobufCommunicator
from .communication.types import MockRequestInput, MockResponseOutput
from .config import TuskConfig, TuskFileConfig, load_tusk_config
from .sampling import should_sample, validate_sampling_rate
from .trace_blocking_manager import TraceBlockingManager, should_block_span
from .types import CleanSpanData, DriftMode, SpanKind
from ..instrumentation.registry import install_hooks
from ..tracing.adapters.base import SpanExportAdapter
from ..tracing.adapters.memory import InMemorySpanAdapter

if TYPE_CHECKING:
    pass

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
        self.config = TuskConfig()
        self.file_config: TuskFileConfig | None = None
        self.app_ready = False
        self._adapters: list[SpanExportAdapter] = []
        self._in_memory_adapter = InMemorySpanAdapter()
        self._sdk_instance_id = str(uuid.uuid4())
        self._sampling_rate: float = 1.0
        self._batch_processor: BatchSpanProcessor | None = None
        self._use_batching: bool = True
        self._transform_configs: dict[str, Any] | None = None

        # CLI communication for replay mode
        self.communicator: ProtobufCommunicator | None = None
        self._cli_connection_task: asyncio.Task | None = None
        self._is_connected_with_cli = False

        # Load config file early
        self.file_config = load_tusk_config()

        # Always add in-memory adapter for testing/debugging
        self._adapters.append(self._in_memory_adapter)

    @classmethod
    def get_instance(cls) -> TuskDrift:
        """Get the singleton TuskDrift instance."""
        if cls._instance is None:
            cls._instance = TuskDrift()
        return cls._instance

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
        use_batching: bool = True,
        batch_config: BatchSpanProcessorConfig | None = None,
        transforms: dict[str, Any] | None = None,
    ) -> TuskDrift:
        """
        Initialize the TuskDrift SDK.

        Args:
            api_key: Tusk API key for remote export
            env: Environment name (e.g., "development", "production")
            sampling_rate: Fraction of traces to capture (0.0 to 1.0)
            observable_service_id: ID of the observable service in Tusk (required for API export)
            export_directory: Directory for filesystem export (optional)
            tusk_backend_base_url: Base URL for the Tusk backend
            sdk_version: Version of the SDK
            use_batching: Whether to batch spans before export (default: True)
            batch_config: Optional batch processor configuration
            transforms: Optional transform configuration matching the Node SDK schema

        Returns:
            The initialized TuskDrift instance
        """
        instance = cls.get_instance()

        if cls._initialized:
            logger.warning("TuskDrift already initialized")
            return instance

        # Get file config (already loaded in __init__)
        file_config = instance.file_config

        # Merge transforms: init param takes precedence over config file
        effective_transforms = transforms
        if effective_transforms is None and file_config and file_config.transforms:
            effective_transforms = file_config.transforms

        instance.config = TuskConfig(
            api_key=api_key,
            env=env,
            sampling_rate=sampling_rate or 1.0,
            transforms=effective_transforms,
        )
        instance._transform_configs = effective_transforms

        # Determine sampling rate (includes config file as a source)
        instance._sampling_rate = instance._determine_sampling_rate(sampling_rate)
        instance._use_batching = use_batching

        # Determine export directory: init param > config file > None
        effective_export_directory = export_directory
        if effective_export_directory is None and file_config and file_config.traces and file_config.traces.dir:
            effective_export_directory = file_config.traces.dir

        # Determine tusk backend URL: init param > config file > default
        effective_backend_url = tusk_backend_base_url
        if tusk_backend_base_url == "https://api.usetusk.ai" and file_config and file_config.tusk_api and file_config.tusk_api.url:
            effective_backend_url = file_config.tusk_api.url

        # Setup adapters based on configuration
        instance._setup_adapters(
            api_key=api_key,
            observable_service_id=observable_service_id,
            environment=env or "development",
            export_directory=effective_export_directory,
            tusk_backend_base_url=effective_backend_url,
            sdk_version=sdk_version,
        )

        # Initialize communicator for REPLAY mode
        if instance.mode == "REPLAY":
            instance._init_communicator_for_replay()

        # Setup batch processor if batching is enabled
        if use_batching:
            instance._batch_processor = BatchSpanProcessor(
                adapters=instance._adapters,
                config=batch_config,
            )
            instance._batch_processor.start()
            logger.debug("Batch span processor started")

        # Install instrumentation hooks
        install_hooks()

        cls._initialized = True
        print(f"Drift SDK initialized in {instance.mode} mode (sampling: {instance._sampling_rate * 100:.0f}%)")

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

    def _setup_adapters(
        self,
        api_key: str | None,
        observable_service_id: str | None,
        environment: str,
        export_directory: str | Path | None,
        tusk_backend_base_url: str,
        sdk_version: str,
    ) -> None:
        """Setup export adapters based on configuration and mode.

        - RECORD mode: Filesystem and/or API adapters (+ in-memory)
        - REPLAY mode: Only in-memory adapter
        - DISABLED mode: Only in-memory adapter
        """
        # Only setup filesystem/API adapters in RECORD mode
        if self.mode != "RECORD":
            logger.debug(f"Mode is {self.mode}, skipping filesystem/API adapters")
            return

        # Filesystem adapter (RECORD mode only)
        if export_directory is not None:
            from ..tracing.adapters.filesystem import FilesystemSpanAdapter

            filesystem_adapter = FilesystemSpanAdapter(export_directory)
            self._adapters.append(filesystem_adapter)
            logger.info(f"Filesystem adapter enabled, exporting to: {export_directory}")

        # API adapter (RECORD mode only, requires api_key and observable_service_id)
        if api_key and observable_service_id:
            from ..tracing.adapters.api import ApiSpanAdapter, ApiSpanAdapterConfig

            api_config = ApiSpanAdapterConfig(
                api_key=api_key,
                tusk_backend_base_url=tusk_backend_base_url,
                observable_service_id=observable_service_id,
                environment=environment,
                sdk_version=sdk_version,
                sdk_instance_id=self._sdk_instance_id,
            )
            api_adapter = ApiSpanAdapter(api_config)
            self._adapters.append(api_adapter)
            logger.info("API adapter enabled")

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
            return "RECORD"

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
            self._cli_connection_task = loop.create_task(connect_to_cli())
        except RuntimeError:
            # No event loop running yet, will connect on first request
            logger.debug("No event loop running, will connect on first mock request")

    def mark_app_as_ready(self) -> None:
        """Mark the application as ready (started listening)."""
        self.app_ready = True
        print("Application marked as ready")

    def collect_span(self, span: CleanSpanData) -> None:
        """
        Collect a span and export to configured adapters (mode-aware).

        Behavior by mode:
        - DISABLED: Skip collection
        - RECORD: Export to filesystem/API adapters (respects sampling + trace blocking)
        - REPLAY: Send inbound SERVER spans to CLI, store in memory

        Respects sampling rate and uses batching if enabled.
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
            logger.warning(f"Span {span.name} blocked due to size - blocking trace {span.trace_id}")
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
            asyncio.create_task(self.send_inbound_span_for_replay(span))

        # Export via batch processor or directly
        if self._batch_processor is not None:
            self._batch_processor.add_span(span)
        else:
            self._export_to_adapters_sync([span])

    # ========== Mock Request Methods (REPLAY mode) ==========

    async def request_mock_async(self, mock_request: MockRequestInput) -> MockResponseOutput:
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
            logger.debug(f"Sending mock request to CLI (async), testId: {mock_request.test_id}")
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
            logger.debug(f"Sending mock request to CLI (sync), testId: {mock_request.test_id}")
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
            logger.debug(f"Requesting env vars (sync) for trace: {trace_test_server_span_id}")
            env_vars = self.communicator.request_env_vars_sync(trace_test_server_span_id)
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

    def _export_to_adapters_sync(self, spans: list[CleanSpanData]) -> None:
        """Export spans to all configured adapters synchronously."""
        import asyncio

        for adapter in self._adapters:
            try:
                # Handle async adapters
                if asyncio.iscoroutinefunction(adapter.export_spans):
                    try:
                        loop = asyncio.get_running_loop()
                        task = asyncio.create_task(
                            self._export_with_error_handling(adapter, spans)
                        )
                        task.add_done_callback(self._handle_export_task_done)
                    except RuntimeError:
                        asyncio.run(adapter.export_spans(spans))
                else:
                    adapter.export_spans(spans)  # type: ignore
            except Exception as e:
                logger.error("Failed to export spans via %s: %s", adapter.name, e)

    async def _export_with_error_handling(
        self, adapter: SpanExportAdapter, spans: list[CleanSpanData]
    ) -> None:
        """Wrap adapter export with error handling."""
        try:
            result = await adapter.export_spans(spans)
            if result.code.value != 0:  # Not SUCCESS
                logger.error(
                    "Adapter %s failed to export spans: %s",
                    adapter.name,
                    result.error,
                )
        except Exception as e:
            logger.error("Adapter %s raised exception: %s", adapter.name, e)

    def _handle_export_task_done(self, task: "asyncio.Task[None]") -> None:
        """Handle completed export task, logging any unhandled exceptions."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error("Export task failed with unhandled exception: %s", exc)

    def add_adapter(self, adapter: SpanExportAdapter) -> None:
        """Add a custom span export adapter."""
        self._adapters.append(adapter)
        logger.info(f"Added adapter: {adapter.name}")

    def remove_adapter(self, adapter: SpanExportAdapter) -> None:
        """Remove a span export adapter."""
        if adapter in self._adapters:
            self._adapters.remove(adapter)
            logger.info(f"Removed adapter: {adapter.name}")

    def clear_adapters(self) -> None:
        """Remove all adapters except the in-memory adapter."""
        self._adapters = [self._in_memory_adapter]
        logger.info("Cleared all adapters (keeping in-memory)")

    def get_in_memory_spans(self) -> list[CleanSpanData]:
        """Get all spans from the in-memory adapter."""
        return self._in_memory_adapter.get_all_spans()

    def clear_in_memory_spans(self) -> None:
        """Clear all spans from the in-memory adapter."""
        self._in_memory_adapter.clear()

    @property
    def adapters(self) -> list[SpanExportAdapter]:
        """Get the list of configured adapters."""
        return list(self._adapters)

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

    @property
    def batch_processor(self) -> BatchSpanProcessor | None:
        """Get the batch processor (if batching is enabled)."""
        return self._batch_processor

    def get_file_config(self) -> TuskFileConfig | None:
        """Get the loaded config file (if any)."""
        return self.file_config

    def shutdown(self) -> None:
        """Shutdown the SDK and all adapters."""
        import asyncio

        # Stop batch processor first (flushes remaining spans)
        if self._batch_processor is not None:
            self._batch_processor.stop()
            logger.debug("Batch processor stopped")

        # Disconnect from CLI if connected
        if self.communicator:
            try:
                asyncio.run(self.communicator.disconnect())
                logger.debug("Disconnected from CLI")
            except Exception as e:
                logger.error(f"Error disconnecting from CLI: {e}")

        # Shutdown trace blocking manager
        try:
            TraceBlockingManager.get_instance().shutdown()
        except Exception as e:
            logger.error(f"Error shutting down trace blocking manager: {e}")

        # Shutdown adapters
        for adapter in self._adapters:
            try:
                if asyncio.iscoroutinefunction(adapter.shutdown):
                    try:
                        loop = asyncio.get_running_loop()
                        asyncio.create_task(adapter.shutdown())
                    except RuntimeError:
                        asyncio.run(adapter.shutdown())
                else:
                    adapter.shutdown()  # type: ignore
            except Exception as e:
                logger.error(f"Error shutting down adapter {adapter.name}: {e}")

        print("Drift SDK shutdown complete")
