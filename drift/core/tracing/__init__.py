"""Tracing infrastructure for the Drift SDK."""

from .span_exporter import TdSpanExporter, TdSpanExporterConfig
from .td_attributes import TdSpanAttributes
from .td_span_processor import TdSpanProcessor
from .span_utils import (
    SpanUtils,
    SpanInfo,
    CreateSpanOptions,
    SpanExecutorOptions,
    AddSpanAttributesOptions,
)
from .otel_converter import (
    otel_span_to_clean_span_data,
    format_trace_id,
    format_span_id,
)

__all__ = [
    # Exporters
    "TdSpanExporter",
    "TdSpanExporterConfig",
    # OpenTelemetry integration
    "TdSpanAttributes",
    "TdSpanProcessor",
    "SpanUtils",
    "SpanInfo",
    "CreateSpanOptions",
    "SpanExecutorOptions",
    "AddSpanAttributesOptions",
    # Converters
    "otel_span_to_clean_span_data",
    "format_trace_id",
    "format_span_id",
]
