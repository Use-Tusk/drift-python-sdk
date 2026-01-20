"""Tests for trace_blocking_manager.py - Trace blocking to prevent memory exhaustion."""

from __future__ import annotations

import json
import threading
import time

import pytest

from drift.core.trace_blocking_manager import (
    MAX_SPAN_SIZE_BYTES,
    MAX_SPAN_SIZE_MB,
    METADATA_BUFFER_BYTES,
    TraceBlockingManager,
    estimate_span_size,
    should_block_span,
)
from drift.core.types import SpanKind, SpanStatus, StatusCode
from tests.utils import create_test_span


class TestTraceBlockingManagerSingleton:
    """Tests for TraceBlockingManager singleton behavior."""

    def test_get_instance_returns_singleton(self):
        """Should return the same instance on multiple calls."""
        instance1 = TraceBlockingManager.get_instance()
        instance2 = TraceBlockingManager.get_instance()
        assert instance1 is instance2

    def test_singleton_starts_cleanup_thread(self):
        """Should start cleanup thread on first instance."""
        instance = TraceBlockingManager.get_instance()
        assert instance._cleanup_thread is not None
        assert instance._cleanup_thread.is_alive()


class TestTraceBlockingManagerBlockTrace:
    """Tests for trace blocking operations."""

    @pytest.fixture(autouse=True)
    def setup_manager(self):
        """Setup clean manager for each test."""
        manager = TraceBlockingManager.get_instance()
        manager.clear_all()
        yield manager
        manager.clear_all()

    def test_block_trace(self, setup_manager):
        """Should block a trace ID."""
        manager = setup_manager
        trace_id = "abc123" + "0" * 26

        manager.block_trace(trace_id, reason="test")

        assert manager.is_trace_blocked(trace_id)
        assert manager.get_blocked_count() == 1

    def test_is_trace_blocked_returns_false_for_unblocked(self, setup_manager):
        """Should return False for unblocked traces."""
        manager = setup_manager
        assert manager.is_trace_blocked("not_blocked" + "0" * 22) is False

    def test_get_block_reason(self, setup_manager):
        """Should return the block reason."""
        manager = setup_manager
        trace_id = "reason_test" + "0" * 21

        manager.block_trace(trace_id, reason="size_limit:1.5MB")

        assert manager.get_block_reason(trace_id) == "size_limit:1.5MB"

    def test_get_block_reason_returns_none_for_unblocked(self, setup_manager):
        """Should return None for unblocked traces."""
        manager = setup_manager
        assert manager.get_block_reason("not_blocked" + "0" * 22) is None

    def test_unblock_trace(self, setup_manager):
        """Should unblock a trace ID."""
        manager = setup_manager
        trace_id = "unblock_test" + "0" * 20

        manager.block_trace(trace_id)
        assert manager.is_trace_blocked(trace_id)

        manager.unblock_trace(trace_id)
        assert manager.is_trace_blocked(trace_id) is False

    def test_unblock_nonexistent_trace_is_safe(self, setup_manager):
        """Should not raise when unblocking nonexistent trace."""
        manager = setup_manager
        # Should not raise
        manager.unblock_trace("nonexistent" + "0" * 21)

    def test_clear_all(self, setup_manager):
        """Should clear all blocked traces."""
        manager = setup_manager

        for i in range(5):
            manager.block_trace(f"trace{i}" + "0" * 26)

        assert manager.get_blocked_count() == 5

        manager.clear_all()

        assert manager.get_blocked_count() == 0

    def test_blocking_same_trace_twice_does_not_duplicate(self, setup_manager):
        """Should not duplicate when blocking same trace twice."""
        manager = setup_manager
        trace_id = "duplicate" + "0" * 23

        manager.block_trace(trace_id, reason="first")
        manager.block_trace(trace_id, reason="second")

        assert manager.get_blocked_count() == 1
        # First reason should be preserved
        assert manager.get_block_reason(trace_id) == "first"


class TestTraceBlockingManagerThreadSafety:
    """Tests for thread-safe operations."""

    @pytest.fixture(autouse=True)
    def setup_manager(self):
        """Setup clean manager for each test."""
        manager = TraceBlockingManager.get_instance()
        manager.clear_all()
        yield manager
        manager.clear_all()

    def test_concurrent_block_and_check(self, setup_manager):
        """Should handle concurrent blocking and checking safely."""
        manager = setup_manager
        trace_ids = [f"concurrent{i}" + "0" * 21 for i in range(100)]
        results = []

        def blocker():
            for trace_id in trace_ids[:50]:
                manager.block_trace(trace_id)

        def checker():
            for trace_id in trace_ids[:50]:
                results.append(manager.is_trace_blocked(trace_id))

        threads = [
            threading.Thread(target=blocker),
            threading.Thread(target=checker),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All traces should be blocked after threads complete
        for trace_id in trace_ids[:50]:
            assert manager.is_trace_blocked(trace_id)


class TestTraceBlockingManagerCleanup:
    """Tests for TTL-based cleanup."""

    @pytest.fixture(autouse=True)
    def setup_manager(self):
        """Setup clean manager for each test."""
        manager = TraceBlockingManager.get_instance()
        manager.clear_all()
        yield manager
        manager.clear_all()

    def test_cleanup_removes_old_traces(self, setup_manager):
        """Should remove traces older than TTL."""
        manager = setup_manager
        trace_id = "old_trace" + "0" * 23

        # Block trace and manually set old timestamp
        manager.block_trace(trace_id)
        # Set timestamp to be older than TTL
        old_time = (time.time() * 1000) - manager.DEFAULT_TTL_MS - 1000
        manager._trace_timestamps[trace_id] = old_time

        # Trigger cleanup
        manager._cleanup_old_traces()

        # Trace should be removed
        assert manager.is_trace_blocked(trace_id) is False

    def test_cleanup_keeps_recent_traces(self, setup_manager):
        """Should keep traces newer than TTL."""
        manager = setup_manager
        trace_id = "recent_trace" + "0" * 20

        manager.block_trace(trace_id)

        # Trigger cleanup
        manager._cleanup_old_traces()

        # Trace should still be blocked
        assert manager.is_trace_blocked(trace_id)


class TestEstimateSpanSize:
    """Tests for estimate_span_size function."""

    def test_estimates_empty_span(self):
        """Should estimate size for span with empty values."""
        span = create_test_span(input_value={}, output_value={})

        size = estimate_span_size(span)

        # Should include metadata buffer
        assert size >= METADATA_BUFFER_BYTES

    def test_estimates_span_with_data(self):
        """Should estimate size including input and output."""
        input_data = {"key": "value" * 100}
        output_data = {"result": "data" * 200}
        span = create_test_span(input_value=input_data, output_value=output_data)

        size = estimate_span_size(span)

        expected_input_size = len(json.dumps(input_data).encode("utf-8"))
        expected_output_size = len(json.dumps(output_data).encode("utf-8"))
        expected_total = expected_input_size + expected_output_size + METADATA_BUFFER_BYTES

        assert size == expected_total

    def test_handles_non_serializable_input(self, mocker):
        """Should handle non-serializable input gracefully."""
        span = mocker.MagicMock()
        span.input_value = {"func": lambda x: x}  # Not JSON serializable
        span.output_value = {}

        # Should not raise
        size = estimate_span_size(span)
        assert size >= METADATA_BUFFER_BYTES

    def test_handles_none_values(self, mocker):
        """Should handle None input/output values."""
        span = mocker.MagicMock()
        span.input_value = None
        span.output_value = None

        size = estimate_span_size(span)
        assert size == METADATA_BUFFER_BYTES


class TestShouldBlockSpan:
    """Tests for should_block_span function."""

    @pytest.fixture(autouse=True)
    def setup_manager(self):
        """Setup clean manager for each test."""
        manager = TraceBlockingManager.get_instance()
        manager.clear_all()
        yield manager
        manager.clear_all()

    def test_blocks_server_span_with_error_status(self, setup_manager):
        """Should block trace when SERVER span has ERROR status."""
        span = create_test_span()
        span.kind = SpanKind.SERVER
        span.status = SpanStatus(code=StatusCode.ERROR, message="Internal Server Error")
        span.trace_id = "error_trace" + "0" * 21

        result = should_block_span(span)

        assert result is True
        manager = TraceBlockingManager.get_instance()
        assert manager.is_trace_blocked(span.trace_id)
        assert manager.get_block_reason(span.trace_id) == "server_error"

    def test_does_not_block_server_span_with_ok_status(self, setup_manager):
        """Should not block trace when SERVER span has OK status."""
        span = create_test_span()
        span.kind = SpanKind.SERVER
        span.status = SpanStatus(code=StatusCode.OK)

        result = should_block_span(span)

        assert result is False

    def test_does_not_block_client_span_with_error_status(self, setup_manager):
        """Should not block trace when CLIENT span has ERROR status."""
        span = create_test_span()
        span.kind = SpanKind.CLIENT
        span.status = SpanStatus(code=StatusCode.ERROR)

        result = should_block_span(span)

        assert result is False

    def test_blocks_span_exceeding_size_limit(self, setup_manager):
        """Should block trace when span exceeds size limit."""
        # Create large input that exceeds 1MB
        large_data = {"data": "x" * (MAX_SPAN_SIZE_BYTES + 1000)}
        span = create_test_span(input_value=large_data, output_value={})
        span.trace_id = "large_trace" + "0" * 21

        result = should_block_span(span)

        assert result is True
        manager = TraceBlockingManager.get_instance()
        assert manager.is_trace_blocked(span.trace_id)
        reason = manager.get_block_reason(span.trace_id)
        assert reason is not None and "size_limit" in reason

    def test_does_not_block_span_within_size_limit(self, setup_manager):
        """Should not block trace when span is within size limit."""
        span = create_test_span()

        result = should_block_span(span)

        assert result is False

    def test_server_error_takes_precedence_over_size(self, setup_manager):
        """Should block for server error even if size is within limit."""
        span = create_test_span()
        span.kind = SpanKind.SERVER
        span.status = SpanStatus(code=StatusCode.ERROR)
        span.trace_id = "precedence" + "0" * 22

        result = should_block_span(span)

        assert result is True
        manager = TraceBlockingManager.get_instance()
        assert manager.get_block_reason(span.trace_id) == "server_error"


class TestConstants:
    """Tests for module constants."""

    def test_max_span_size_is_1mb(self):
        """Should have 1MB as max span size."""
        assert MAX_SPAN_SIZE_MB == 1
        assert MAX_SPAN_SIZE_BYTES == 1 * 1024 * 1024

    def test_metadata_buffer_is_50kb(self):
        """Should have 50KB metadata buffer."""
        assert METADATA_BUFFER_BYTES == 50 * 1024
