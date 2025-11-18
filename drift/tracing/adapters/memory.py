from enum import Enum

from ...core.types import CleanSpanData, SpanKind


class ExportResultCode(Enum):
    SUCCESS = 0
    FAILED = 1


class ExportResult:
    code: ExportResultCode
    error: Exception | None

    def __init__(self, code: ExportResultCode, error: Exception | None = None):
        self.code = code
        self.error = error


class InMemorySpanAdapter:
    name: str = "in-memory"

    def __init__(self):
        self._spans: list[CleanSpanData] = []

    def collect_span(self, span: CleanSpanData) -> None:
        self._spans.append(span)

    def get_all_spans(self) -> list[CleanSpanData]:
        return list(self._spans)

    def get_spans_by_instrumentation(self, instrumentation_name: str) -> list[CleanSpanData]:
        return [span for span in self._spans if instrumentation_name in span.instrumentation_name]

    def get_spans_by_kind(self, kind: SpanKind) -> list[CleanSpanData]:
        return [span for span in self._spans if span.kind == kind]

    def clear(self) -> None:
        self._spans.clear()

    def export_spans(self, spans: list[CleanSpanData]) -> ExportResult:
        for span in spans:
            self.collect_span(span)
        return ExportResult(ExportResultCode.SUCCESS)

    def shutdown(self) -> None:
        self.clear()
