"""Base adapter interface for span export."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...core.types import CleanSpanData


class ExportResultCode(Enum):
    """Result codes for span export operations."""

    SUCCESS = 0
    FAILED = 1


@dataclass
class ExportResult:
    """Result of a span export operation."""

    code: ExportResultCode
    error: Exception | None = None

    @classmethod
    def success(cls) -> "ExportResult":
        """Create a successful export result."""
        return cls(code=ExportResultCode.SUCCESS)

    @classmethod
    def failed(cls, error: Exception | str) -> "ExportResult":
        """Create a failed export result."""
        if isinstance(error, str):
            error = Exception(error)
        return cls(code=ExportResultCode.FAILED, error=error)


class SpanExportAdapter(ABC):
    """
    Abstract base class for span export adapters.

    Adapters implement different export strategies:
    - InMemorySpanAdapter: Stores spans in memory (for testing)
    - FilesystemSpanAdapter: Exports spans to local JSONL files
    - ApiSpanAdapter: Exports spans to Tusk backend API
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique name identifying this adapter."""
        ...

    @abstractmethod
    async def export_spans(self, spans: list["CleanSpanData"]) -> ExportResult:
        """
        Export a batch of spans.

        Args:
            spans: List of CleanSpanData instances to export

        Returns:
            ExportResult indicating success or failure
        """
        ...

    @abstractmethod
    async def shutdown(self) -> None:
        """
        Shutdown the adapter and release any resources.

        Called when the SDK is shutting down.
        """
        ...
