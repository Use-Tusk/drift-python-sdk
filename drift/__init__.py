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
from .core.metrics import SDKMetrics, get_sdk_metrics
from .core.resilience import CircuitBreaker, CircuitBreakerConfig, RetryConfig
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
from .version import SDK_VERSION

__version__ = SDK_VERSION

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
    # Metrics
    "SDKMetrics",
    "get_sdk_metrics",
    # Resilience
    "RetryConfig",
    "CircuitBreaker",
    "CircuitBreakerConfig",
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
