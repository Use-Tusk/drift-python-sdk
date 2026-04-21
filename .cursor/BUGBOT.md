# BUGBOT Notes

## Instrumentation Guidelines

- When adding a new instrumentation, the README must be updated to document the new instrumentation.

## Logging Levels

The SDK's default log level is `info` (see `drift/core/logger.py`). Customers initializing with `TuskDrift.initialize(env=...)` and no logger overrides will see every `logger.info(...)` line in their application output.

**Rules:**

1. **Per-call / per-request / per-query log lines MUST use `logger.debug(...)`, never `logger.info(...)`.** This includes anything inside a patched function body that runs on every invocation (e.g. `patched_connect`, middleware `__call__`, request hooks, cursor wrappers). One INFO line per DB connect or HTTP request floods customer logs and looks like a bug to them.

2. **"X instrumented" startup lines MUST be `logger.debug(...)`.** The convention (see `drift/instrumentation/redis/instrumentation.py` and the sync paths in `drift/instrumentation/psycopg/instrumentation.py`) is debug. Even though these fire once per process, they appear in every customer's startup logs and read as internal SDK noise. Do not include the patched function's repr (`module.connect is now: ...`) at any level — it leaks implementation detail and confuses customers.

3. **`logger.info(...)` is reserved for events the customer genuinely benefits from seeing once:** "App marked as ready", circuit-breaker state transitions, Rust-core enable/disable at startup, mode-change events. If the line can fire more than ~once per process lifetime in normal operation, it must be `debug`.

4. **When adding a sibling instrumentation (e.g. psycopg2 alongside psycopg3), match the existing level conventions across both.** The psycopg2/psycopg3 split caused exactly this bug — psycopg3 used `debug` for "RECORD mode: Connected", psycopg2 used `info`, and customers saw 4 noise lines per connect.

5. **Do not log raw header dicts, query args, or connection arguments at INFO** even once — these can be high-cardinality and contain sensitive values. Use `debug` and prefer logging only the keys, not values.

## REPLAY Mode: Context Ordering and OUTPUT_VALUE on Inbound Spans

When implementing or modifying a REPLAY mode handler (e.g., `_handle_replay_request`) in any framework instrumentation (Django, FastAPI, Flask, WSGI, etc.):

1. **`span.end()` MUST be called BEFORE `replay_trace_id_context.reset()`.**
   `TdSpanProcessor.on_end()` is triggered synchronously by `span.end()` and reads `replay_trace_id_context.get()` to route the inbound replay span to the CLI. If the context is reset first, the processor silently drops the span and the CLI never receives it.

2. **`OUTPUT_VALUE` MUST be set on the server span before `span.end()` in REPLAY mode.**
   The CLI determines pass/fail using the raw HTTP response it receives directly, but the backend/UI uses the `OUTPUT_VALUE` from the inbound replay span to populate `span_result_recording` for the "Expected vs Actual" diff view. If OUTPUT_VALUE is missing, the UI shows expected data on the left and empty `{}` on the right — appearing as a deviation even though the test passed.

3. **E2E tests will NOT catch these bugs** because they only check the CLI's `passed` boolean, which is based on the raw HTTP response comparison — not the inbound replay span data.
