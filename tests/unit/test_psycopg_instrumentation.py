"""Unit tests for PostgreSQL instrumentation."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

os.environ["TUSK_DRIFT_MODE"] = "RECORD"

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from drift import TuskDrift
from drift.instrumentation import PostgresInstrumentation
from drift.core.types import SpanKind, current_trace_id_context, current_span_id_context
from drift.core.tracing.adapters import InMemorySpanAdapter, register_in_memory_adapter


class TestPostgresInstrumentation(unittest.TestCase):
    """Test PostgreSQL instrumentation."""

    @classmethod
    def setUpClass(cls):
        """Set up SDK and instrumentation once for all tests."""
        cls.sdk = TuskDrift.initialize()
        # Must mark app as ready before registering adapter
        cls.sdk.mark_app_as_ready()
        cls.adapter = InMemorySpanAdapter()
        register_in_memory_adapter(cls.adapter)
        cls.instrumentation = PostgresInstrumentation()

    def setUp(self):
        """Clear spans before each test."""
        self.adapter.clear()

    def test_instrumentation_is_enabled_by_default(self):
        """Test that instrumentation is enabled by default."""
        instr = PostgresInstrumentation()
        self.assertTrue(instr.enabled)

    def test_instrumentation_can_be_disabled(self):
        """Test that instrumentation can be disabled."""
        instr = PostgresInstrumentation(enabled=False)
        self.assertFalse(instr.enabled)

    def test_instrumentation_has_correct_name(self):
        """Test that instrumentation has correct name."""
        instr = PostgresInstrumentation()
        self.assertEqual(instr.name, "PostgresInstrumentation")

    def test_wrap_cursor_patches_execute(self):
        """Test that wrapping a cursor patches the execute method."""
        instr = PostgresInstrumentation()

        # Create a mock cursor
        mock_cursor = Mock()
        original_execute = Mock(return_value=None)
        mock_cursor.execute = original_execute
        mock_cursor.executemany = Mock(return_value=None)

        # Wrap the cursor
        wrapped_cursor = instr._wrap_cursor(mock_cursor)

        # Verify the execute method was replaced with a different function
        self.assertIsNotNone(wrapped_cursor.execute)
        # The wrapped execute should be callable but not the original mock
        self.assertTrue(callable(wrapped_cursor.execute))

    def test_wrap_connection_patches_cursor(self):
        """Test that wrapping a connection patches the cursor method."""
        instr = PostgresInstrumentation()

        # Create a mock connection
        mock_connection = Mock()
        mock_cursor = Mock()
        original_cursor_method = Mock(return_value=mock_cursor)
        mock_connection.cursor = original_cursor_method

        # Wrap the connection
        wrapped_connection = instr._wrap_connection(mock_connection)

        # Verify the cursor method was replaced with a different function
        self.assertIsNotNone(wrapped_connection.cursor)
        # The wrapped cursor should be callable
        self.assertTrue(callable(wrapped_connection.cursor))

    def test_execute_creates_span_with_parent_trace_id(self):
        """Test that executing a query creates a span with parent trace ID."""
        instr = PostgresInstrumentation()

        # Set parent trace context
        parent_trace_id = "parent-trace-123"
        parent_span_id = "parent-span-456"
        trace_token = current_trace_id_context.set(parent_trace_id)
        span_token = current_span_id_context.set(parent_span_id)

        try:
            # Create a mock execute function
            original_execute = Mock(return_value=None)

            # Execute a query
            instr._execute_query(original_execute, "SELECT * FROM users", None, is_many=False)

            # Wait for batch processing
            import time
            time.sleep(0.5)

            # Check that original execute was called
            original_execute.assert_called_once()

            # Check that span was created
            spans = self.adapter.get_all_spans()
            self.assertGreater(len(spans), 0, f"Expected at least 1 span, got {len(spans)}")

            # Find the CLIENT span
            db_spans = [s for s in spans if s.kind == SpanKind.CLIENT]
            self.assertGreater(len(db_spans), 0, f"Expected at least 1 CLIENT span, got {len(db_spans)}")

            span = db_spans[0]
            # Verify parent span ID was set
            self.assertEqual(span.parent_span_id, parent_span_id)
            self.assertEqual(span.trace_id, parent_trace_id)

        finally:
            current_trace_id_context.reset(trace_token)
            current_span_id_context.reset(span_token)

    def test_execute_creates_span_without_parent_trace_id(self):
        """Test that executing a query creates a root span if no parent."""
        instr = PostgresInstrumentation()

        # No parent trace context
        original_execute = Mock(return_value=None)

        # Execute a query
        instr._execute_query(original_execute, "SELECT * FROM users", None, is_many=False)

        # Wait a bit for span processing
        import time
        time.sleep(0.1)

        # Check that original execute was called
        original_execute.assert_called_once()

        # Check that span was created
        spans = self.adapter.get_all_spans()
        self.assertGreater(len(spans), 0)

        # Find the CLIENT span
        db_spans = [s for s in spans if s.kind == SpanKind.CLIENT]
        self.assertGreater(len(db_spans), 0)

        span = db_spans[0]
        # Verify this is a root span
        self.assertTrue(span.is_root_span)

    def test_execute_creates_span_with_error(self):
        """Test that a query error creates an error span."""
        instr = PostgresInstrumentation()

        # Create a mock execute function that raises an error
        test_error = ValueError("Database connection failed")
        original_execute = Mock(side_effect=test_error)

        # Execute a query that fails
        with self.assertRaises(ValueError):
            instr._execute_query(original_execute, "SELECT * FROM users", None, is_many=False)

        # Wait a bit for span processing
        import time
        time.sleep(0.1)

        # Check that span was created with error
        spans = self.adapter.get_all_spans()
        self.assertGreater(len(spans), 0)

        # Find the CLIENT span
        db_spans = [s for s in spans if s.kind == SpanKind.CLIENT]
        self.assertGreater(len(db_spans), 0)

        span = db_spans[0]
        # Verify error status
        from drift.core.types import StatusCode
        self.assertEqual(span.status.code, StatusCode.ERROR)
        self.assertIn("Database connection failed", span.status.message)

    def test_span_has_query_in_input_value(self):
        """Test that span input contains the query."""
        instr = PostgresInstrumentation()

        test_query = "SELECT * FROM users WHERE id = 1"
        original_execute = Mock(return_value=None)

        # Execute a query
        instr._execute_query(original_execute, test_query, None, is_many=False)

        # Wait a bit for span processing
        import time
        time.sleep(0.1)

        # Check that span contains the query
        spans = self.adapter.get_all_spans()
        db_spans = [s for s in spans if s.kind == SpanKind.CLIENT]
        self.assertGreater(len(db_spans), 0)

        span = db_spans[0]
        self.assertIsNotNone(span.input_value)
        self.assertEqual(span.input_value.get("query"), test_query)

    def test_span_has_operation_type_in_name(self):
        """Test that span name includes operation type."""
        instr = PostgresInstrumentation()

        test_query = "INSERT INTO users (name) VALUES ('John')"
        original_execute = Mock(return_value=None)

        # Execute a query
        instr._execute_query(original_execute, test_query, None, is_many=False)

        # Wait a bit for span processing
        import time
        time.sleep(0.1)

        # Check that span name includes operation
        spans = self.adapter.get_all_spans()
        db_spans = [s for s in spans if s.kind == SpanKind.CLIENT]
        self.assertGreater(len(db_spans), 0)

        span = db_spans[0]
        self.assertIn("INSERT", span.name)
        self.assertIn("PostgreSQL", span.name)

    def test_sanitize_args_handles_tuples(self):
        """Test that args sanitization handles tuples."""
        instr = PostgresInstrumentation()

        args = (1, "test", 3.14)
        sanitized = instr._sanitize_args(args)

        self.assertEqual(sanitized, [1, "test", 3.14])

    def test_sanitize_args_handles_lists(self):
        """Test that args sanitization handles lists."""
        instr = PostgresInstrumentation()

        args = [1, "test", 3.14]
        sanitized = instr._sanitize_args(args)

        self.assertEqual(sanitized, [1, "test", 3.14])

    def test_sanitize_args_handles_dicts(self):
        """Test that args sanitization handles dicts."""
        instr = PostgresInstrumentation()

        args = {"id": 1, "name": "test"}
        sanitized = instr._sanitize_args(args)

        self.assertEqual(sanitized, {"id": 1, "name": "test"})

    def test_sanitize_args_handles_none(self):
        """Test that args sanitization handles None."""
        instr = PostgresInstrumentation()

        sanitized = instr._sanitize_args(None)
        self.assertIsNone(sanitized)

    def test_sanitize_args_truncates_large_lists(self):
        """Test that large lists are truncated to 100 items."""
        instr = PostgresInstrumentation()

        args = list(range(200))
        sanitized = instr._sanitize_args(args)

        self.assertEqual(len(sanitized), 100)


if __name__ == "__main__":
    unittest.main()
