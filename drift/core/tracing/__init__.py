"""Tracing infrastructure for the Drift SDK."""

from .span_exporter import TdSpanExporter, TdSpanExporterConfig

__all__ = ["TdSpanExporter", "TdSpanExporterConfig"]
