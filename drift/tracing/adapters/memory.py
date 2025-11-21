"""In-memory span adapter for testing and development."""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from .base import ExportResult, SpanExportAdapter

if TYPE_CHECKING:
    from ...core.types import CleanSpanData, SpanKind


class InMemorySpanAdapter(SpanExportAdapter):
    """
    Stores spans in memory - useful for testing and development.

    Provides helper methods to query spans by instrumentation name or kind.
    """

    def __init__(self) -> None:
        self._spans: list[CleanSpanData] = []

    def __repr__(self) -> str:
        return f"InMemorySpanAdapter(spans={len(self._spans)})"

    @property
    @override
    def name(self) -> str:
        return "in-memory"

    def collect_span(self, span: "CleanSpanData") -> None:
        """Add a single span to the in-memory store."""
        self._spans.append(span)

    def get_all_spans(self) -> list["CleanSpanData"]:
        """Get all stored spans."""
        return list(self._spans)

    def get_spans_by_instrumentation(self, instrumentation_name: str) -> list["CleanSpanData"]:
        """Get spans matching an instrumentation name (partial match)."""
        return [
            span for span in self._spans if instrumentation_name in span.instrumentation_name
        ]

    def get_spans_by_kind(self, kind: "SpanKind") -> list["CleanSpanData"]:
        """Get spans of a specific kind."""
        return [span for span in self._spans if span.kind == kind]

    def clear(self) -> None:
        """Clear all stored spans."""
        self._spans.clear()

    @override
    async def export_spans(self, spans: list["CleanSpanData"]) -> ExportResult:
        """Export spans by storing them in memory."""
        for span in spans:
            self.collect_span(span)
        return ExportResult.success()

    @override
    async def shutdown(self) -> None:
        """Shutdown by clearing all spans."""
        self.clear()


# Re-export for backwards compatibility
__all__ = ["InMemorySpanAdapter", "ExportResult", "ExportResultCode"]

from .base import ExportResultCode
