"""Filesystem span adapter for exporting spans to local JSONL files."""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, override

from .base import ExportResult, SpanExportAdapter

if TYPE_CHECKING:
    from ...types import CleanSpanData

logger = logging.getLogger(__name__)

DEFAULT_MAX_CACHED_TRACES = 1000


class FilesystemSpanAdapter(SpanExportAdapter):
    """
    Exports spans to local JSONL files organized by trace ID.

    Each trace gets its own file named: {timestamp}_trace_{traceId}.jsonl
    Spans are appended as JSON lines (one span per line).
    """

    def __init__(
        self,
        base_directory: str | Path,
        max_cached_traces: int = DEFAULT_MAX_CACHED_TRACES,
    ) -> None:
        """
        Initialize the filesystem adapter.

        Args:
            base_directory: Directory where span files will be written
            max_cached_traces: Maximum number of trace-to-file mappings to cache (LRU)
        """
        self._base_directory = Path(base_directory)
        self._max_cached_traces = max_cached_traces
        self._trace_file_map: OrderedDict[str, Path] = OrderedDict()

        # Create directory if it doesn't exist
        self._base_directory.mkdir(parents=True, exist_ok=True)

    def __repr__(self) -> str:
        return f"FilesystemSpanAdapter(directory={self._base_directory})"

    @property
    @override
    def name(self) -> str:
        return "filesystem"

    def _get_or_create_file_path(self, trace_id: str) -> Path:
        """Get or create file path for a trace ID with LRU eviction."""
        if trace_id in self._trace_file_map:
            # Move to end (most recently used)
            self._trace_file_map.move_to_end(trace_id)
            return self._trace_file_map[trace_id]

        # Create new file with timestamp prefix
        iso_timestamp = datetime.now(UTC).isoformat().replace(":", "-").replace(".", "-")
        file_path = self._base_directory / f"{iso_timestamp}_trace_{trace_id}.jsonl"
        self._trace_file_map[trace_id] = file_path

        # Evict oldest entries if over limit
        while len(self._trace_file_map) > self._max_cached_traces:
            self._trace_file_map.popitem(last=False)

        return file_path

    @override
    async def export_spans(self, spans: list[CleanSpanData]) -> ExportResult:
        """Export spans to trace-specific JSONL files."""
        try:
            import aiofiles
        except ImportError:
            logger.warning("aiofiles not installed, falling back to sync I/O")
            return await self._export_spans_sync(spans)

        try:
            for span in spans:
                file_path = self._get_or_create_file_path(span.trace_id)

                # Serialize span to JSON
                span_dict = self._span_to_dict(span)
                json_line = json.dumps(span_dict, default=str, ensure_ascii=False) + "\n"

                async with aiofiles.open(file_path, "a", encoding="utf-8") as f:
                    await f.write(json_line)

            return ExportResult.success()

        except Exception as e:
            logger.error("Failed to export spans to filesystem: %s", e)
            return ExportResult.failed(e)

    async def _export_spans_sync(self, spans: list[CleanSpanData]) -> ExportResult:
        """Fallback synchronous export when aiofiles is not available."""
        try:
            for span in spans:
                file_path = self._get_or_create_file_path(span.trace_id)

                span_dict = self._span_to_dict(span)
                json_line = json.dumps(span_dict, default=str, ensure_ascii=False) + "\n"

                with open(file_path, "a", encoding="utf-8") as f:
                    f.write(json_line)

            return ExportResult.success()

        except Exception as e:
            logger.error("Failed to export spans to filesystem: %s", e)
            return ExportResult.failed(e)

    @override
    async def shutdown(self) -> None:
        """Shutdown and clear the trace file map."""
        self._trace_file_map.clear()

    def _span_to_dict(self, span: CleanSpanData) -> dict[str, Any]:
        """Convert CleanSpanData to a JSON-serializable dictionary."""
        from ...types import PackageType, SpanKind, StatusCode

        result: dict[str, Any] = {
            "traceId": span.trace_id,
            "spanId": span.span_id,
            "parentSpanId": span.parent_span_id,
            "name": span.name,
            "packageName": span.package_name,
            "instrumentationName": span.instrumentation_name,
            "submoduleName": span.submodule_name,
            "inputValue": span.input_value,
            "outputValue": span.output_value,
            "inputSchema": self._schema_to_dict(span.input_schema),
            "outputSchema": self._schema_to_dict(span.output_schema),
            "inputSchemaHash": span.input_schema_hash,
            "outputSchemaHash": span.output_schema_hash,
            "inputValueHash": span.input_value_hash,
            "outputValueHash": span.output_value_hash,
            "kind": span.kind.value if isinstance(span.kind, SpanKind) else span.kind,
            "status": {
                "code": span.status.code.value if isinstance(span.status.code, StatusCode) else span.status.code,
                "message": span.status.message,
            },
            "isPreAppStart": span.is_pre_app_start,
            "timestamp": {
                "seconds": span.timestamp.seconds,
                "nanos": span.timestamp.nanos,
            },
            "duration": {
                "seconds": span.duration.seconds,
                "nanos": span.duration.nanos,
            },
            "isRootSpan": span.is_root_span,
        }

        # Add optional fields
        if span.package_type is not None:
            result["packageType"] = (
                span.package_type.value if isinstance(span.package_type, PackageType) else span.package_type
            )

        if span.environment is not None:
            result["environment"] = span.environment

        if span.metadata is not None:
            result["metadata"] = (
                asdict(span.metadata) if hasattr(span.metadata, "__dataclass_fields__") else span.metadata
            )

        if span.transform_metadata is not None:
            result["transformMetadata"] = asdict(span.transform_metadata)

        if span.is_used is not None:
            result["isUsed"] = span.is_used

        if span.stack_trace is not None:
            result["stackTrace"] = span.stack_trace

        return result

    def _schema_to_dict(self, schema: Any) -> dict[str, Any]:
        """Convert JsonSchema to a dictionary."""
        from ...json_schema_helper import JsonSchema

        if schema is None:
            return {"type": 0, "properties": {}}

        if isinstance(schema, JsonSchema):
            return schema.to_primitive()

        # Already a dict
        return schema
