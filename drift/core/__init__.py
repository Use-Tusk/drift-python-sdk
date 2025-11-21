"""Core module for the Drift SDK."""

from .drift_sdk import TuskDrift
from .types import DriftMode, CleanSpanData, PackageType, SpanKind, StatusCode
from .config import TuskConfig
from .batch_processor import BatchSpanProcessor, BatchSpanProcessorConfig
from .sampling import should_sample, validate_sampling_rate

__all__ = [
    # Main SDK
    "TuskDrift",
    "TuskConfig",
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
]
