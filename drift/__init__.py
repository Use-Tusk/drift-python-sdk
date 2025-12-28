"""Drift Python SDK for distributed tracing and instrumentation."""

from .core import (
    TuskDrift,
    CleanSpanData,
    PackageType,
    SpanKind,
    StatusCode,
    DriftMode,
    BatchSpanProcessorConfig,
    # Config
    TuskConfig,
    TuskFileConfig,
    ServiceConfig,
    RecordingConfig,
    TracesConfig,
    TuskApiConfig,
    load_tusk_config,
    find_project_root,
)
from .core.logger import LogLevel, set_log_level, get_log_level
from .instrumentation.flask import FlaskInstrumentation
from .instrumentation.fastapi import FastAPIInstrumentation
from .instrumentation.requests import RequestsInstrumentation
from .core.tracing.adapters import (
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
    # Config
    "TuskConfig",
    "TuskFileConfig",
    "ServiceConfig",
    "RecordingConfig",
    "TracesConfig",
    "TuskApiConfig",
    "load_tusk_config",
    "find_project_root",
    # Logger
    "LogLevel",
    "set_log_level",
    "get_log_level",
    # Instrumentations
    "FlaskInstrumentation",
    "FastAPIInstrumentation",
    "RequestsInstrumentation",
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
