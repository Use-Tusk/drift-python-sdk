"""Tracing infrastructure for the Drift SDK."""

from .otel_converter import (
    format_span_id,
    format_trace_id,
    otel_span_to_clean_span_data,
)
from .span_exporter import TdSpanExporter, TdSpanExporterConfig
from .span_utils import (
    AddSpanAttributesOptions,
    CreateSpanOptions,
    SpanExecutorOptions,
    SpanInfo,
    SpanUtils,
)
from .td_attributes import TdSpanAttributes
from .td_span_processor import TdSpanProcessor

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
