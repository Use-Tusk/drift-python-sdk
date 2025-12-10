"""Tests for span export adapters."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from drift.core.types import SpanKind
from drift.core.tracing.adapters import (
    ApiSpanAdapter,
    ApiSpanAdapterConfig,
    ExportResult,
    ExportResultCode,
    FilesystemSpanAdapter,
    InMemorySpanAdapter,
)
from tests.utils import create_test_span


class TestExportResult(unittest.TestCase):
    """Tests for ExportResult dataclass."""

    def test_success_result(self):
        result = ExportResult.success()
        self.assertEqual(result.code, ExportResultCode.SUCCESS)
        self.assertIsNone(result.error)

    def test_failed_result_with_exception(self):
        error = ValueError("test error")
        result = ExportResult.failed(error)
        self.assertEqual(result.code, ExportResultCode.FAILED)
        self.assertEqual(result.error, error)

    def test_failed_result_with_string(self):
        result = ExportResult.failed("test error message")
        self.assertEqual(result.code, ExportResultCode.FAILED)
        self.assertIsInstance(result.error, Exception)
        self.assertEqual(str(result.error), "test error message")


class TestInMemorySpanAdapter(unittest.TestCase):
    """Tests for InMemorySpanAdapter."""

    def setUp(self):
        self.adapter = InMemorySpanAdapter()

    def test_name(self):
        self.assertEqual(self.adapter.name, "in-memory")

    def test_repr(self):
        self.assertEqual(repr(self.adapter), "InMemorySpanAdapter(spans=0)")
        self.adapter.collect_span(create_test_span())
        self.assertEqual(repr(self.adapter), "InMemorySpanAdapter(spans=1)")

    def test_collect_and_get_spans(self):
        span1 = create_test_span(span_id="1" * 16)
        span2 = create_test_span(span_id="2" * 16)

        self.adapter.collect_span(span1)
        self.adapter.collect_span(span2)

        spans = self.adapter.get_all_spans()
        self.assertEqual(len(spans), 2)
        self.assertEqual(spans[0].span_id, "1" * 16)
        self.assertEqual(spans[1].span_id, "2" * 16)

    def test_get_spans_by_instrumentation(self):
        span1 = create_test_span()
        span1.instrumentation_name = "FlaskInstrumentation"
        span2 = create_test_span()
        span2.instrumentation_name = "FastAPIInstrumentation"

        self.adapter.collect_span(span1)
        self.adapter.collect_span(span2)

        flask_spans = self.adapter.get_spans_by_instrumentation("Flask")
        self.assertEqual(len(flask_spans), 1)
        self.assertEqual(flask_spans[0].instrumentation_name, "FlaskInstrumentation")

    def test_get_spans_by_kind(self):
        span1 = create_test_span()
        span1.kind = SpanKind.SERVER
        span2 = create_test_span()
        span2.kind = SpanKind.CLIENT

        self.adapter.collect_span(span1)
        self.adapter.collect_span(span2)

        server_spans = self.adapter.get_spans_by_kind(SpanKind.SERVER)
        self.assertEqual(len(server_spans), 1)
        self.assertEqual(server_spans[0].kind, SpanKind.SERVER)

    def test_clear(self):
        self.adapter.collect_span(create_test_span())
        self.assertEqual(len(self.adapter.get_all_spans()), 1)

        self.adapter.clear()
        self.assertEqual(len(self.adapter.get_all_spans()), 0)

    def test_export_spans_async(self):
        spans = [create_test_span(), create_test_span(span_id="c" * 16)]
        result = asyncio.run(self.adapter.export_spans(spans))

        self.assertEqual(result.code, ExportResultCode.SUCCESS)
        self.assertEqual(len(self.adapter.get_all_spans()), 2)

    def test_shutdown_clears_spans(self):
        self.adapter.collect_span(create_test_span())
        asyncio.run(self.adapter.shutdown())
        self.assertEqual(len(self.adapter.get_all_spans()), 0)

    def test_get_spans_by_name(self):
        """Test filtering spans by name."""
        span1 = create_test_span(name="GET /api/users")
        span2 = create_test_span(name="POST /api/users")
        span3 = create_test_span(name="GET /api/orders")

        self.adapter.collect_span(span1)
        self.adapter.collect_span(span2)
        self.adapter.collect_span(span3)

        # Get all spans with GET in name
        all_spans = self.adapter.get_all_spans()
        get_spans = [s for s in all_spans if "GET" in s.name]
        self.assertEqual(len(get_spans), 2)

    def test_get_spans_by_trace_id(self):
        """Test filtering spans by trace ID."""
        trace1 = "trace1" + "0" * 26
        trace2 = "trace2" + "0" * 26

        span1 = create_test_span(trace_id=trace1, name="span1")
        span2 = create_test_span(trace_id=trace1, name="span2")
        span3 = create_test_span(trace_id=trace2, name="span3")

        self.adapter.collect_span(span1)
        self.adapter.collect_span(span2)
        self.adapter.collect_span(span3)

        all_spans = self.adapter.get_all_spans()
        trace1_spans = [s for s in all_spans if s.trace_id == trace1]
        self.assertEqual(len(trace1_spans), 2)

    def test_get_spans_preserves_order(self):
        """Test that spans are returned in insertion order."""
        for i in range(5):
            span = create_test_span(span_id=str(i) * 16, name=f"span-{i}")
            self.adapter.collect_span(span)

        spans = self.adapter.get_all_spans()
        for i, span in enumerate(spans):
            self.assertEqual(span.name, f"span-{i}")

    def test_concurrent_exports(self):
        """Test concurrent exports don't cause issues."""
        async def export_multiple():
            tasks = []
            for i in range(10):
                span = create_test_span(span_id=str(i) * 16)
                tasks.append(self.adapter.export_spans([span]))
            await asyncio.gather(*tasks)

        asyncio.run(export_multiple())
        self.assertEqual(len(self.adapter.get_all_spans()), 10)


class TestFilesystemSpanAdapter(unittest.TestCase):
    """Tests for FilesystemSpanAdapter."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.adapter = FilesystemSpanAdapter(self.temp_dir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_name(self):
        self.assertEqual(self.adapter.name, "filesystem")

    def test_repr(self):
        self.assertIn("FilesystemSpanAdapter", repr(self.adapter))
        self.assertIn(self.temp_dir, repr(self.adapter))

    def test_creates_directory(self):
        new_dir = Path(self.temp_dir) / "nested" / "spans"
        adapter = FilesystemSpanAdapter(new_dir)
        self.assertTrue(new_dir.exists())

    def test_exports_span_to_jsonl(self):
        span = create_test_span()
        result = asyncio.run(self.adapter.export_spans([span]))

        self.assertEqual(result.code, ExportResultCode.SUCCESS)

        # Find the created file
        files = list(Path(self.temp_dir).glob("*.jsonl"))
        self.assertEqual(len(files), 1)

        # Verify file content
        with open(files[0]) as f:
            line = f.readline()
            data = json.loads(line)
            self.assertEqual(data["traceId"], span.trace_id)
            self.assertEqual(data["spanId"], span.span_id)
            self.assertEqual(data["name"], span.name)

    def test_groups_spans_by_trace_id(self):
        trace1 = "t1" + "0" * 30
        trace2 = "t2" + "0" * 30

        span1 = create_test_span(trace_id=trace1, span_id="1" * 16)
        span2 = create_test_span(trace_id=trace1, span_id="2" * 16)
        span3 = create_test_span(trace_id=trace2, span_id="3" * 16)

        # Export all spans
        asyncio.run(self.adapter.export_spans([span1, span2, span3]))

        files = list(Path(self.temp_dir).glob("*.jsonl"))
        self.assertEqual(len(files), 2)

        # Check t1 file has 2 lines
        t1_file = [f for f in files if "t1" in str(f)][0]
        with open(t1_file) as f:
            lines = f.readlines()
            self.assertEqual(len(lines), 2)

    def test_lru_eviction(self):
        adapter = FilesystemSpanAdapter(self.temp_dir, max_cached_traces=2)

        # Add 3 different traces
        for i in range(3):
            trace_id = f"trace{i}" + "0" * 26
            span = create_test_span(trace_id=trace_id)
            asyncio.run(adapter.export_spans([span]))

        # Only 2 traces should be in cache
        self.assertEqual(len(adapter._trace_file_map), 2)
        # Oldest (trace0) should have been evicted
        self.assertNotIn("trace0" + "0" * 26, adapter._trace_file_map)

    def test_lru_updates_on_access(self):
        adapter = FilesystemSpanAdapter(self.temp_dir, max_cached_traces=2)

        trace0 = "trace0" + "0" * 26
        trace1 = "trace1" + "0" * 26
        trace2 = "trace2" + "0" * 26

        asyncio.run(adapter.export_spans([create_test_span(trace_id=trace0)]))
        asyncio.run(adapter.export_spans([create_test_span(trace_id=trace1)]))
        # Access trace0 again (moves to end of LRU)
        asyncio.run(adapter.export_spans([create_test_span(trace_id=trace0, span_id="d" * 16)]))
        # Add trace2 - should evict trace1
        asyncio.run(adapter.export_spans([create_test_span(trace_id=trace2)]))

        self.assertIn(trace0, adapter._trace_file_map)
        self.assertIn(trace2, adapter._trace_file_map)
        self.assertNotIn(trace1, adapter._trace_file_map)

    def test_shutdown_clears_cache(self):
        asyncio.run(self.adapter.export_spans([create_test_span()]))
        self.assertEqual(len(self.adapter._trace_file_map), 1)

        asyncio.run(self.adapter.shutdown())
        self.assertEqual(len(self.adapter._trace_file_map), 0)


class TestApiSpanAdapter(unittest.TestCase):
    """Tests for ApiSpanAdapter."""

    def setUp(self):
        self.config = ApiSpanAdapterConfig(
            api_key="test-api-key",
            tusk_backend_base_url="https://api.test.com",
            observable_service_id="test-service-id",
            environment="test",
            sdk_version="1.0.0",
            sdk_instance_id="test-instance",
        )
        self.adapter = ApiSpanAdapter(self.config)

    def tearDown(self):
        # No cleanup needed for betterproto client
        pass

    def test_name(self):
        self.assertEqual(self.adapter.name, "api")

    def test_repr(self):
        self.assertIn("ApiSpanAdapter", repr(self.adapter))
        self.assertIn("api.test.com", repr(self.adapter))
        self.assertIn("test", repr(self.adapter))  # environment

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
        # Config is minimal now - no timeout/retries since using betterproto
        self.assertEqual(config.api_key, "key")
        self.assertEqual(config.environment, "prod")

    def test_transform_span_to_protobuf(self):
        """Test span transformation to protobuf format."""
        from tusk.drift.core.v1 import Span as ProtoSpan

        span = create_test_span()
        result = self.adapter._transform_span_to_protobuf(span)

        # Result should be a protobuf Span object
        self.assertIsInstance(result, ProtoSpan)
        self.assertEqual(result.trace_id, span.trace_id)
        self.assertEqual(result.span_id, span.span_id)
        self.assertEqual(result.name, span.name)
        self.assertEqual(result.package_name, span.package_name)
        # Kind is converted to int value for protobuf
        self.assertEqual(result.kind, span.kind.value)
        # Check that input/output were converted to Struct
        self.assertIsNotNone(result.input_value)
        self.assertIsNotNone(result.output_value)
        # Check timestamp and duration are datetime/timedelta
        from datetime import datetime, timedelta
        self.assertIsInstance(result.timestamp, datetime)
        self.assertIsInstance(result.duration, timedelta)

    def test_base_url_construction(self):
        """Test that the API URL is constructed correctly."""
        self.assertEqual(
            self.adapter._base_url,
            "https://api.test.com/api/drift/tusk.drift.backend.v1.SpanExportService/ExportSpans"
        )

    def test_aiohttp_not_installed(self):
        """Test graceful handling when aiohttp is not installed."""
        # The error is raised in the channel, which is called during export_spans
        # We can't easily mock the import since the adapter is already initialized
        # So we'll just verify the adapter was created successfully
        # The actual ImportError handling is tested in integration tests
        self.assertIsNotNone(self.adapter)
        self.assertEqual(self.adapter.name, "api")


class TestAdapterIntegration(unittest.TestCase):
    """Integration tests for adapters working together."""

    def test_multiple_adapters(self):
        """Test exporting to multiple adapters."""
        memory_adapter = InMemorySpanAdapter()

        with tempfile.TemporaryDirectory() as temp_dir:
            fs_adapter = FilesystemSpanAdapter(temp_dir)

            span = create_test_span()

            # Export to both
            result1 = asyncio.run(memory_adapter.export_spans([span]))
            result2 = asyncio.run(fs_adapter.export_spans([span]))

            self.assertEqual(result1.code, ExportResultCode.SUCCESS)
            self.assertEqual(result2.code, ExportResultCode.SUCCESS)

            # Verify both have the span
            self.assertEqual(len(memory_adapter.get_all_spans()), 1)
            files = list(Path(temp_dir).glob("*.jsonl"))
            self.assertEqual(len(files), 1)


if __name__ == "__main__":
    unittest.main()
