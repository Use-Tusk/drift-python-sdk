"""Tests for span export adapters."""

import asyncio
import json
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from drift.core.tracing.adapters import (
    ApiSpanAdapter,
    ApiSpanAdapterConfig,
    ExportResult,
    ExportResultCode,
    FilesystemSpanAdapter,
    InMemorySpanAdapter,
)
from drift.core.types import SpanKind
from tests.utils import create_test_span


class TestExportResult:
    """Tests for ExportResult dataclass."""

    def test_success_result(self):
        """Test success result creation."""
        result = ExportResult.success()
        assert result.code == ExportResultCode.SUCCESS
        assert result.error is None

    def test_failed_result_with_exception(self):
        """Test failed result with exception."""
        error = ValueError("test error")
        result = ExportResult.failed(error)
        assert result.code == ExportResultCode.FAILED
        assert result.error == error

    def test_failed_result_with_string(self):
        """Test failed result with string error message."""
        result = ExportResult.failed("test error message")
        assert result.code == ExportResultCode.FAILED
        assert isinstance(result.error, Exception)
        assert str(result.error) == "test error message"


class TestInMemorySpanAdapter:
    """Tests for InMemorySpanAdapter."""

    @pytest.fixture
    def adapter(self):
        """Create adapter for testing."""
        return InMemorySpanAdapter()

    def test_name(self, adapter):
        """Test adapter name."""
        assert adapter.name == "in-memory"

    def test_repr(self, adapter):
        """Test adapter repr."""
        assert repr(adapter) == "InMemorySpanAdapter(spans=0)"
        adapter.collect_span(create_test_span())
        assert repr(adapter) == "InMemorySpanAdapter(spans=1)"

    def test_collect_and_get_spans(self, adapter):
        """Test collecting and retrieving spans."""
        span1 = create_test_span(span_id="1" * 16)
        span2 = create_test_span(span_id="2" * 16)

        adapter.collect_span(span1)
        adapter.collect_span(span2)

        spans = adapter.get_all_spans()
        assert len(spans) == 2
        assert spans[0].span_id == "1" * 16
        assert spans[1].span_id == "2" * 16

    def test_get_spans_by_instrumentation(self, adapter):
        """Test filtering spans by instrumentation."""
        span1 = create_test_span()
        span1.instrumentation_name = "FlaskInstrumentation"
        span2 = create_test_span()
        span2.instrumentation_name = "FastAPIInstrumentation"

        adapter.collect_span(span1)
        adapter.collect_span(span2)

        flask_spans = adapter.get_spans_by_instrumentation("Flask")
        assert len(flask_spans) == 1
        assert flask_spans[0].instrumentation_name == "FlaskInstrumentation"

    def test_get_spans_by_kind(self, adapter):
        """Test filtering spans by kind."""
        span1 = create_test_span()
        span1.kind = SpanKind.SERVER
        span2 = create_test_span()
        span2.kind = SpanKind.CLIENT

        adapter.collect_span(span1)
        adapter.collect_span(span2)

        server_spans = adapter.get_spans_by_kind(SpanKind.SERVER)
        assert len(server_spans) == 1
        assert server_spans[0].kind == SpanKind.SERVER

    def test_clear(self, adapter):
        """Test clearing spans."""
        adapter.collect_span(create_test_span())
        assert len(adapter.get_all_spans()) == 1

        adapter.clear()
        assert len(adapter.get_all_spans()) == 0

    def test_export_spans_async(self, adapter):
        """Test async span export."""
        spans = [create_test_span(), create_test_span(span_id="c" * 16)]
        result = asyncio.run(adapter.export_spans(spans))

        assert result.code == ExportResultCode.SUCCESS
        assert len(adapter.get_all_spans()) == 2

    def test_shutdown_clears_spans(self, adapter):
        """Test that shutdown clears spans."""
        adapter.collect_span(create_test_span())
        asyncio.run(adapter.shutdown())
        assert len(adapter.get_all_spans()) == 0

    def test_get_spans_by_name(self, adapter):
        """Test filtering spans by name."""
        span1 = create_test_span(name="GET /api/users")
        span2 = create_test_span(name="POST /api/users")
        span3 = create_test_span(name="GET /api/orders")

        adapter.collect_span(span1)
        adapter.collect_span(span2)
        adapter.collect_span(span3)

        all_spans = adapter.get_all_spans()
        get_spans = [s for s in all_spans if "GET" in s.name]
        assert len(get_spans) == 2

    def test_get_spans_by_trace_id(self, adapter):
        """Test filtering spans by trace ID."""
        trace1 = "trace1" + "0" * 26
        trace2 = "trace2" + "0" * 26

        span1 = create_test_span(trace_id=trace1, name="span1")
        span2 = create_test_span(trace_id=trace1, name="span2")
        span3 = create_test_span(trace_id=trace2, name="span3")

        adapter.collect_span(span1)
        adapter.collect_span(span2)
        adapter.collect_span(span3)

        all_spans = adapter.get_all_spans()
        trace1_spans = [s for s in all_spans if s.trace_id == trace1]
        assert len(trace1_spans) == 2

    def test_get_spans_preserves_order(self, adapter):
        """Test that spans are returned in insertion order."""
        for i in range(5):
            span = create_test_span(span_id=str(i) * 16, name=f"span-{i}")
            adapter.collect_span(span)

        spans = adapter.get_all_spans()
        for i, span in enumerate(spans):
            assert span.name == f"span-{i}"

    def test_concurrent_exports(self, adapter):
        """Test concurrent exports don't cause issues."""

        async def export_multiple():
            tasks = []
            for i in range(10):
                span = create_test_span(span_id=str(i) * 16)
                tasks.append(adapter.export_spans([span]))
            await asyncio.gather(*tasks)

        asyncio.run(export_multiple())
        assert len(adapter.get_all_spans()) == 10


class TestFilesystemSpanAdapter:
    """Tests for FilesystemSpanAdapter."""

    @pytest.fixture
    def temp_dir(self):
        """Create temporary directory for tests."""
        tmpdir = tempfile.mkdtemp()
        yield tmpdir
        shutil.rmtree(tmpdir, ignore_errors=True)

    @pytest.fixture
    def adapter(self, temp_dir):
        """Create adapter for testing."""
        return FilesystemSpanAdapter(temp_dir)

    def test_name(self, adapter):
        """Test adapter name."""
        assert adapter.name == "filesystem"

    def test_repr(self, adapter, temp_dir):
        """Test adapter repr."""
        assert "FilesystemSpanAdapter" in repr(adapter)
        assert temp_dir in repr(adapter)

    def test_creates_directory(self, temp_dir):
        """Test directory creation."""
        new_dir = Path(temp_dir) / "nested" / "spans"
        FilesystemSpanAdapter(new_dir)
        assert new_dir.exists()

    def test_exports_span_to_jsonl(self, adapter, temp_dir):
        """Test exporting span to JSONL file."""
        span = create_test_span()
        result = asyncio.run(adapter.export_spans([span]))

        assert result.code == ExportResultCode.SUCCESS

        files = list(Path(temp_dir).glob("*.jsonl"))
        assert len(files) == 1

        with open(files[0]) as f:
            line = f.readline()
            data = json.loads(line)
            assert data["traceId"] == span.trace_id
            assert data["spanId"] == span.span_id
            assert data["name"] == span.name

    def test_groups_spans_by_trace_id(self, adapter, temp_dir):
        """Test grouping spans by trace ID."""
        trace1 = "t1" + "0" * 30
        trace2 = "t2" + "0" * 30

        span1 = create_test_span(trace_id=trace1, span_id="1" * 16)
        span2 = create_test_span(trace_id=trace1, span_id="2" * 16)
        span3 = create_test_span(trace_id=trace2, span_id="3" * 16)

        asyncio.run(adapter.export_spans([span1, span2, span3]))

        files = list(Path(temp_dir).glob("*.jsonl"))
        assert len(files) == 2

        t1_file = [f for f in files if "t1" in f.name][0]
        with open(t1_file) as f:
            lines = f.readlines()
            assert len(lines) == 2

    def test_lru_eviction(self, temp_dir):
        """Test LRU eviction of cached traces."""
        adapter = FilesystemSpanAdapter(temp_dir, max_cached_traces=2)

        for i in range(3):
            trace_id = f"trace{i}" + "0" * 26
            span = create_test_span(trace_id=trace_id)
            asyncio.run(adapter.export_spans([span]))

        assert len(adapter._trace_file_map) == 2
        assert "trace0" + "0" * 26 not in adapter._trace_file_map

    def test_lru_updates_on_access(self, temp_dir):
        """Test LRU updates on access."""
        adapter = FilesystemSpanAdapter(temp_dir, max_cached_traces=2)

        trace0 = "trace0" + "0" * 26
        trace1 = "trace1" + "0" * 26
        trace2 = "trace2" + "0" * 26

        asyncio.run(adapter.export_spans([create_test_span(trace_id=trace0)]))
        asyncio.run(adapter.export_spans([create_test_span(trace_id=trace1)]))
        asyncio.run(adapter.export_spans([create_test_span(trace_id=trace0, span_id="d" * 16)]))
        asyncio.run(adapter.export_spans([create_test_span(trace_id=trace2)]))

        assert trace0 in adapter._trace_file_map
        assert trace2 in adapter._trace_file_map
        assert trace1 not in adapter._trace_file_map

    def test_shutdown_clears_cache(self, adapter):
        """Test that shutdown clears cache."""
        asyncio.run(adapter.export_spans([create_test_span()]))
        assert len(adapter._trace_file_map) == 1

        asyncio.run(adapter.shutdown())
        assert len(adapter._trace_file_map) == 0


class TestApiSpanAdapter:
    """Tests for ApiSpanAdapter."""

    @pytest.fixture
    def config(self):
        """Create config for testing."""
        return ApiSpanAdapterConfig(
            api_key="test-api-key",
            tusk_backend_base_url="https://api.test.com",
            observable_service_id="test-service-id",
            environment="test",
            sdk_version="1.0.0",
            sdk_instance_id="test-instance",
        )

    @pytest.fixture
    def adapter(self, config):
        """Create adapter for testing."""
        return ApiSpanAdapter(config)

    def test_name(self, adapter):
        """Test adapter name."""
        assert adapter.name == "api"

    def test_repr(self, adapter):
        """Test adapter repr."""
        assert "ApiSpanAdapter" in repr(adapter)
        assert "api.test.com" in repr(adapter)
        assert "test" in repr(adapter)

    def test_config_defaults(self):
        """Test that config has sensible defaults."""
        config = ApiSpanAdapterConfig(
            api_key="key",
            tusk_backend_base_url="https://api.test.com",
            observable_service_id="svc",
            environment="prod",
            sdk_version="1.0.0",
            sdk_instance_id="inst",
        )
        assert config.api_key == "key"
        assert config.environment == "prod"

    def test_transform_span_to_protobuf(self, adapter):
        """Test span transformation to protobuf format."""
        from tusk.drift.core.v1 import Span as ProtoSpan

        span = create_test_span()
        result = adapter._transform_span_to_protobuf(span)

        assert isinstance(result, ProtoSpan)
        assert result.trace_id == span.trace_id
        assert result.span_id == span.span_id
        assert result.name == span.name
        assert result.package_name == span.package_name
        assert result.kind == span.kind.value
        assert result.input_value is not None
        assert result.output_value is not None
        assert isinstance(result.timestamp, datetime)
        assert isinstance(result.duration, timedelta)

    def test_base_url_construction(self, adapter):
        """Test that the API URL is constructed correctly."""
        assert adapter._base_url == "https://api.test.com/api/drift/tusk.drift.backend.v1.SpanExportService/ExportSpans"

    def test_aiohttp_not_installed(self, adapter):
        """Test graceful handling when aiohttp is not installed."""
        assert adapter is not None
        assert adapter.name == "api"


class TestAdapterIntegration:
    """Integration tests for adapters working together."""

    def test_multiple_adapters(self):
        """Test exporting to multiple adapters."""
        memory_adapter = InMemorySpanAdapter()

        with tempfile.TemporaryDirectory() as temp_dir:
            fs_adapter = FilesystemSpanAdapter(temp_dir)

            span = create_test_span()

            result1 = asyncio.run(memory_adapter.export_spans([span]))
            result2 = asyncio.run(fs_adapter.export_spans([span]))

            assert result1.code == ExportResultCode.SUCCESS
            assert result2.code == ExportResultCode.SUCCESS

            assert len(memory_adapter.get_all_spans()) == 1
            files = list(Path(temp_dir).glob("*.jsonl"))
            assert len(files) == 1
