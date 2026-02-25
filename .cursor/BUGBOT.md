# BUGBOT Notes

## Instrumentation Guidelines

- When adding a new instrumentation, the README must be updated to document the new instrumentation.

## REPLAY Mode: Context Ordering and OUTPUT_VALUE on Inbound Spans

When implementing or modifying a REPLAY mode handler (e.g., `_handle_replay_request`) in any framework instrumentation (Django, FastAPI, Flask, WSGI, etc.):

1. **`span.end()` MUST be called BEFORE `replay_trace_id_context.reset()`.**
   `TdSpanProcessor.on_end()` is triggered synchronously by `span.end()` and reads `replay_trace_id_context.get()` to route the inbound replay span to the CLI. If the context is reset first, the processor silently drops the span and the CLI never receives it.

2. **`OUTPUT_VALUE` MUST be set on the server span before `span.end()` in REPLAY mode.**
   The CLI determines pass/fail using the raw HTTP response it receives directly, but the backend/UI uses the `OUTPUT_VALUE` from the inbound replay span to populate `span_result_recording` for the "Expected vs Actual" diff view. If OUTPUT_VALUE is missing, the UI shows expected data on the left and empty `{}` on the right — appearing as a deviation even though the test passed.

3. **E2E tests will NOT catch these bugs** because they only check the CLI's `passed` boolean, which is based on the raw HTTP response comparison — not the inbound replay span data.
