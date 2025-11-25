"""Core TuskDrift SDK singleton and lifecycle management."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .batch_processor import BatchSpanProcessor, BatchSpanProcessorConfig
from .config import TuskConfig, TuskFileConfig, load_tusk_config
from .sampling import should_sample, validate_sampling_rate
from .types import CleanSpanData, DriftMode
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
        """Setup export adapters based on configuration."""
        # Filesystem adapter
        if export_directory is not None:
            from ..tracing.adapters.filesystem import FilesystemSpanAdapter

            filesystem_adapter = FilesystemSpanAdapter(export_directory)
            self._adapters.append(filesystem_adapter)
            logger.info(f"Filesystem adapter enabled, exporting to: {export_directory}")

        # API adapter (requires api_key and observable_service_id)
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

    def mark_app_as_ready(self) -> None:
        """Mark the application as ready (started listening)."""
        self.app_ready = True
        print("Application marked as ready")

    def collect_span(self, span: CleanSpanData) -> None:
        """
        Collect a span and export to all configured adapters.

        Respects sampling rate and uses batching if enabled.
        """
        if self.mode == "DISABLED":
            return

        # Check sampling
        if not should_sample(self._sampling_rate, self.app_ready):
            logger.debug(f"Span {span.name} not sampled")
            return

        # Validate span can be serialized to protobuf
        span.to_proto()

        # Export via batch processor or directly
        if self._batch_processor is not None:
            self._batch_processor.add_span(span)
        else:
            self._export_to_adapters_sync([span])

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
