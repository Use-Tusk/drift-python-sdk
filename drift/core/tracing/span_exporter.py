"""Span exporter that manages multiple export adapters."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...tracing.adapters.base import SpanExportAdapter
    from ..types import CleanSpanData, DriftMode

logger = logging.getLogger(__name__)


@dataclass
class TdSpanExporterConfig:
    """Configuration for the span exporter."""

    base_directory: Path
    mode: DriftMode
    observable_service_id: str | None = None
    use_remote_export: bool = False
    api_key: str | None = None
    tusk_backend_base_url: str = "https://api.usetusk.ai"
    environment: str = "development"
    sdk_version: str = "0.1.0"
    sdk_instance_id: str = ""


class TdSpanExporter:
    """
    Manages span export adapters and handles exporting spans.

    The exporter filters adapters based on mode:
    - RECORD mode: All adapters run
    - REPLAY/DISABLED mode: Only in-memory and callback adapters run
    """

    def __init__(self, config: TdSpanExporterConfig) -> None:
        self.mode = config.mode
        self.adapters: list[SpanExportAdapter] = []

        self._setup_default_adapters(config)

        logger.debug(f"TdSpanExporter initialized with {len(self.adapters)} adapter(s)")

    def _setup_default_adapters(self, config: TdSpanExporterConfig) -> None:
        """Setup default adapters based on configuration."""
        from .adapters import (
            ApiSpanAdapter,
            ApiSpanAdapterConfig,
            FilesystemSpanAdapter,
        )

        if config.use_remote_export and config.api_key and config.observable_service_id:
            logger.debug("TdSpanExporter using API adapter")
            api_config = ApiSpanAdapterConfig(
                api_key=config.api_key,
                tusk_backend_base_url=config.tusk_backend_base_url,
                observable_service_id=config.observable_service_id,
                environment=config.environment,
                sdk_version=config.sdk_version,
                sdk_instance_id=config.sdk_instance_id,
            )
            self.add_adapter(ApiSpanAdapter(api_config))
        else:
            logger.debug("TdSpanExporter falling back to filesystem adapter")
            self.add_adapter(
                FilesystemSpanAdapter(base_directory=config.base_directory)
            )

    def get_adapters(self) -> list[SpanExportAdapter]:
        """Get all configured adapters."""
        return list(self.adapters)

    def add_adapter(self, adapter: SpanExportAdapter) -> None:
        """Add a custom export adapter."""
        self.adapters.append(adapter)
        logger.debug(
            f"Added {adapter.name} adapter. Total adapters: {len(self.adapters)}"
        )

    def remove_adapter(self, adapter: SpanExportAdapter) -> None:
        """Remove a specific adapter."""
        if adapter in self.adapters:
            self.adapters.remove(adapter)
            logger.debug(
                f"Removed {adapter.name} adapter. Total adapters: {len(self.adapters)}"
            )

    def clear_adapters(self) -> None:
        """Clear all adapters."""
        self.adapters = []
        logger.debug("All adapters cleared")

    def set_mode(self, mode: DriftMode) -> None:
        """Set the mode for determining which adapters to run."""
        self.mode = mode

    async def export_spans(self, spans: list[CleanSpanData]) -> None:
        """
        Export spans using all active adapters.

        Filters adapters based on mode:
        - RECORD: All adapters
        - REPLAY/DISABLED: Only in-memory and callback adapters
        """
        logger.debug(f"TdSpanExporter.export_spans() called with {len(spans)} span(s)")

        if len(self.adapters) == 0:
            logger.debug("No adapters configured")
            return

        # Filter adapters based on mode
        active_adapters = self._get_active_adapters()

        if len(active_adapters) == 0:
            logger.debug(f"No active adapters for mode: {self.mode}")
            return

        # Export to all active adapters
        for adapter in active_adapters:
            try:
                await adapter.export_spans(spans)
            except Exception as e:
                logger.error(f"Failed to export spans to {adapter.name}: {e}")

    def _get_active_adapters(self) -> list[SpanExportAdapter]:
        """Get active adapters based on current mode."""
        if self.mode != "RECORD":
            # In non-RECORD mode, only run in-memory and callback adapters
            return [
                adapter
                for adapter in self.adapters
                if adapter.name in ("in-memory", "callback")
            ]

        # In RECORD mode, run all adapters
        return self.adapters

    async def shutdown(self) -> None:
        """Shutdown all adapters."""
        for adapter in self.adapters:
            try:
                await adapter.shutdown()
            except Exception as e:
                logger.error(f"Failed to shutdown adapter {adapter.name}: {e}")

    async def force_flush(self) -> None:
        """Force flush any pending spans."""
        # Most adapters write immediately, so nothing to flush
        pass
