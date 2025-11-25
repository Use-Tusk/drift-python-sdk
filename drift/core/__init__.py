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
]
