"""In-memory span adapter for testing and development."""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from .base import ExportResult, SpanExportAdapter

if TYPE_CHECKING:
    from ...core.types import CleanSpanData, SpanKind
    from ...core.tracing.span_exporter import TdSpanExporter

# Track registered adapters
_registered_adapters: list[InMemorySpanAdapter] = []


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
__all__ = [
    "InMemorySpanAdapter",
    "ExportResult",
    "ExportResultCode",
    "register_in_memory_adapter",
    "unregister_in_memory_adapter",
    "clear_registered_in_memory_adapters",
]

from .base import ExportResultCode


def _get_span_exporter() -> TdSpanExporter | None:
    """Get the span exporter from the SDK instance."""
    from ...core.drift_sdk import TuskDrift

    sdk = TuskDrift.get_instance()
    return getattr(sdk, "span_exporter", None)


def register_in_memory_adapter(adapter: InMemorySpanAdapter) -> None:
    """
    Clear all other adapters and register the in-memory adapter.

    This is primarily useful for testing - it removes all filesystem/API
    adapters and only keeps the in-memory adapter for fast span access.

    Args:
        adapter: The in-memory adapter instance to register

    Example:
        ```python
        from drift import TuskDrift
        from drift.tracing.adapters import InMemorySpanAdapter, register_in_memory_adapter

        TuskDrift.initialize()  # Initialize SDK first
        adapter = InMemorySpanAdapter()
        register_in_memory_adapter(adapter)

        # Now only this adapter will receive spans
        spans = adapter.get_all_spans()
        ```
    """
    from ...core.drift_sdk import TuskDrift

    sdk = TuskDrift.get_instance()

    # Initialize if not already initialized
    if not TuskDrift._initialized:
        TuskDrift.initialize()

    span_exporter = _get_span_exporter()
    if span_exporter is None:
        raise RuntimeError("Span exporter not available.")

    span_exporter.clear_adapters()
    _registered_adapters.append(adapter)
    span_exporter.add_adapter(adapter)


def unregister_in_memory_adapter(adapter: InMemorySpanAdapter) -> None:
    """
    Unregister a specific in-memory adapter.

    Args:
        adapter: The adapter to unregister
    """
    span_exporter = _get_span_exporter()

    if adapter not in _registered_adapters:
        return

    _registered_adapters.remove(adapter)

    if span_exporter:
        span_exporter.remove_adapter(adapter)


def clear_registered_in_memory_adapters() -> None:
    """Clear all registered in-memory adapters."""
    span_exporter = _get_span_exporter()

    if span_exporter:
        for adapter in _registered_adapters:
            span_exporter.remove_adapter(adapter)

    _registered_adapters.clear()
