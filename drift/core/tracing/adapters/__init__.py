"""Span export adapters for the Drift SDK."""

from .base import ExportResult, ExportResultCode, SpanExportAdapter
from .memory import (
    InMemorySpanAdapter,
    register_in_memory_adapter,
    unregister_in_memory_adapter,
    clear_registered_in_memory_adapters,
)
from .filesystem import FilesystemSpanAdapter
from .api import ApiSpanAdapter, ApiSpanAdapterConfig, create_api_adapter

__all__ = [
    # Base
    "SpanExportAdapter",
    "ExportResult",
    "ExportResultCode",
    # Adapters
    "InMemorySpanAdapter",
    "FilesystemSpanAdapter",
    "ApiSpanAdapter",
    "ApiSpanAdapterConfig",
    # Helpers
    "create_api_adapter",
    "register_in_memory_adapter",
    "unregister_in_memory_adapter",
    "clear_registered_in_memory_adapters",
]
