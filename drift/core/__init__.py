"""Core module for the Drift SDK."""

from .drift_sdk import TuskDrift
from .types import DriftMode, CleanSpanData, PackageType, SpanKind, StatusCode
from .config import (
    TuskConfig,
    TuskFileConfig,
    ServiceConfig,
    RecordingConfig,
    TracesConfig,
    TuskApiConfig,
    load_tusk_config,
    find_project_root,
)
from .batch_processor import BatchSpanProcessor, BatchSpanProcessorConfig
from .sampling import should_sample, validate_sampling_rate
from .data_normalization import (
    normalize_input_data,
    remove_none_values,
    create_span_input_value,
    create_mock_input_value,
)
from .trace_blocking_manager import (
    TraceBlockingManager,
    estimate_span_size,
    should_block_span,
    MAX_SPAN_SIZE_MB,
    MAX_SPAN_SIZE_BYTES,
)

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
