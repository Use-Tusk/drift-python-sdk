"""Span export adapters for the Drift SDK."""

from .base import ExportResult, ExportResultCode, SpanExportAdapter
from .memory import InMemorySpanAdapter
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
]
