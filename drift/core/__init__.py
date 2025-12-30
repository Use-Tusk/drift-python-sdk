"""Core module for the Drift SDK."""

from .batch_processor import BatchSpanProcessor, BatchSpanProcessorConfig
from .config import (
    RecordingConfig,
    ServiceConfig,
    TracesConfig,
    TuskApiConfig,
    TuskConfig,
    TuskFileConfig,
    find_project_root,
    load_tusk_config,
)
from .data_normalization import (
    create_mock_input_value,
    create_span_input_value,
    normalize_input_data,
    remove_none_values,
)
from .drift_sdk import TuskDrift
from .sampling import should_sample, validate_sampling_rate
from .trace_blocking_manager import (
    MAX_SPAN_SIZE_BYTES,
    MAX_SPAN_SIZE_MB,
    TraceBlockingManager,
    estimate_span_size,
    should_block_span,
)
from .types import CleanSpanData, DriftMode, PackageType, SpanKind, StatusCode

__all__ = [
    # Main SDK
    "TuskDrift",
    # Config
    "TuskConfig",
    "TuskFileConfig",
    "ServiceConfig",
    "RecordingConfig",
    "TracesConfig",
    "TuskApiConfig",
    "load_tusk_config",
    "find_project_root",
    # Types
    "DriftMode",
    "CleanSpanData",
    "PackageType",
    "SpanKind",
    "StatusCode",
    # Batching
    "BatchSpanProcessor",
    "BatchSpanProcessorConfig",
    # Sampling
    "should_sample",
    "validate_sampling_rate",
    # Data normalization
    "normalize_input_data",
    "remove_none_values",
    "create_span_input_value",
    "create_mock_input_value",
    # Trace blocking
    "TraceBlockingManager",
    "estimate_span_size",
    "should_block_span",
    "MAX_SPAN_SIZE_MB",
    "MAX_SPAN_SIZE_BYTES",
]
