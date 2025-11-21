"""Drift Python SDK for distributed tracing and instrumentation."""

from .core import (
    TuskDrift,
    CleanSpanData,
    PackageType,
    SpanKind,
    StatusCode,
    DriftMode,
    BatchSpanProcessorConfig,
)
from .instrumentation.flask import FlaskInstrumentation
from .instrumentation.fastapi import FastAPIInstrumentation
from .tracing.adapters import (
    SpanExportAdapter,
    ExportResult,
    ExportResultCode,
    InMemorySpanAdapter,
    FilesystemSpanAdapter,
    ApiSpanAdapter,
    ApiSpanAdapterConfig,
    create_api_adapter,
)

__version__ = "0.1.0"

__all__ = [
    # Core
    "TuskDrift",
    "CleanSpanData",
    "PackageType",
    "SpanKind",
    "StatusCode",
    "DriftMode",
    "BatchSpanProcessorConfig",
    # Instrumentations
    "FlaskInstrumentation",
    "FastAPIInstrumentation",
    # Adapters
    "SpanExportAdapter",
    "ExportResult",
    "ExportResultCode",
    "InMemorySpanAdapter",
    "FilesystemSpanAdapter",
    "ApiSpanAdapter",
    "ApiSpanAdapterConfig",
    "create_api_adapter",
]
