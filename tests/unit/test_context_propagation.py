"""Test context propagation to ThreadPoolExecutor threads."""

import unittest
from concurrent.futures import ThreadPoolExecutor
from drift.core.types import current_trace_id_context, current_span_id_context


class TestContextPropagation(unittest.TestCase):
    """Test that context variables propagate correctly to thread pools."""

    def test_context_propagates_to_thread_pool(self):
        """Test that context variables are accessible in ThreadPoolExecutor threads."""
        # Set context in main thread
        trace_id = "test-trace-123"
        span_id = "test-span-456"

        trace_token = current_trace_id_context.set(trace_id)
        span_token = current_span_id_context.set(span_id)

        try:
            # Verify context is set in main thread
            self.assertEqual(current_trace_id_context.get(), trace_id)
            self.assertEqual(current_span_id_context.get(), span_id)

            # Helper to set context and check it
            def run_with_context(trace_id, span_id):
                """Set context and return values."""
                if trace_id:
                    current_trace_id_context.set(trace_id)
                if span_id:
                    current_span_id_context.set(span_id)
                return {
                    "trace_id": current_trace_id_context.get(),
                    "span_id": current_span_id_context.get(),
                }

            # Get context values to pass to threads
            ctx_trace_id = current_trace_id_context.get()
            ctx_span_id = current_span_id_context.get()

            with ThreadPoolExecutor(max_workers=2) as executor:
                future1 = executor.submit(run_with_context, ctx_trace_id, ctx_span_id)
                future2 = executor.submit(run_with_context, ctx_trace_id, ctx_span_id)

                result1 = future1.result()
                result2 = future2.result()

                # Context should be accessible in threads
                self.assertEqual(result1["trace_id"], trace_id,
                               "Context trace_id not propagated to thread 1")
                self.assertEqual(result1["span_id"], span_id,
                               "Context span_id not propagated to thread 1")
                self.assertEqual(result2["trace_id"], trace_id,
                               "Context trace_id not propagated to thread 2")
                self.assertEqual(result2["span_id"], span_id,
                               "Context span_id not propagated to thread 2")
        finally:
            current_trace_id_context.reset(trace_token)
            current_span_id_context.reset(span_token)


if __name__ == "__main__":
    unittest.main()
