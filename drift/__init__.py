"""Drift Python SDK for distributed tracing and instrumentation."""

from .core import (
    BatchSpanProcessorConfig,
    CleanSpanData,
    PackageType,
    RecordingConfig,
    ServiceConfig,
    SpanKind,
    StatusCode,
    TracesConfig,
    TuskApiConfig,
    # Config
    TuskConfig,
    TuskDrift,
    TuskDriftMode,
    TuskFileConfig,
    find_project_root,
    load_tusk_config,
)
from .core.logger import LogLevel, get_log_level, set_log_level
from .core.tracing.adapters import (
    ApiSpanAdapter,
    ApiSpanAdapterConfig,
    ExportResult,
    ExportResultCode,
    FilesystemSpanAdapter,
    InMemorySpanAdapter,
    SpanExportAdapter,
    create_api_adapter,
)
from .instrumentation.fastapi import FastAPIInstrumentation
from .instrumentation.flask import FlaskInstrumentation
from .instrumentation.requests import RequestsInstrumentation
from .instrumentation.urllib3 import Urllib3Instrumentation

__version__ = "0.1.0"

__all__ = [
    # Core
    "TuskDrift",
    "CleanSpanData",
    "PackageType",
    "SpanKind",
    "StatusCode",
    "TuskDriftMode",
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
    "Urllib3Instrumentation",
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
