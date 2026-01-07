# Context Propagation in Python

This document covers how the Drift Python SDK handles tracing context propagation across different execution contexts, including edge cases and recommended patterns.

## Overview

The SDK uses OpenTelemetry for distributed tracing, which relies on Python's `contextvars` module for context propagation. Understanding when context propagates automatically vs. when it requires explicit handling is crucial for correct trace hierarchies.

## Context Propagation Behavior

| Scenario | Auto-propagates? | Notes |
|----------|------------------|-------|
| `async/await` chains | ✅ Yes | Native `contextvars` support |
| `ThreadPoolExecutor` | ❌ No | Requires explicit propagation |
| `ProcessPoolExecutor` | ❌ No | Context cannot cross process boundaries |
| `asyncio.run_in_executor()` | ❌ No | Same as ThreadPoolExecutor |
| `asyncio.to_thread()` (Python 3.9+) | ✅ Yes | Recommended for blocking calls |
| Callback-based libraries | ❌ No | Context lost when callback executes |

## Stack Trace Capture

The SDK captures stack traces for debugging and mock matching. Different components use different truncation levels:

| Component | Max Frames | Use Case |
|-----------|------------|----------|
| Socket instrumentation (unpatched alerts) | Unlimited | Full debugging info |
| `SpanUtils.capture_stack_trace()` | 10 (default) | Span metadata |
| Communicator debug traces | 20 | Internal debugging |

## ThreadPoolExecutor Pattern

Context does **not** automatically propagate to thread pool workers. Use the explicit propagation pattern:

```python
from opentelemetry import context as otel_context

def _run_with_context(ctx, fn, *args, **kwargs):
    """Run function with OpenTelemetry context in a thread pool."""
    token = otel_context.attach(ctx)
    try:
        return fn(*args, **kwargs)
    finally:
        otel_context.detach(token)

# Usage
ctx = otel_context.get_current()
with ThreadPoolExecutor(max_workers=4) as executor:
    future = executor.submit(_run_with_context, ctx, my_function, arg1)
```

### Alternative: asyncio.to_thread() (Python 3.9+)

For async code needing to run blocking operations, `asyncio.to_thread()` automatically propagates context:

```python
# Context propagates automatically - no wrapper needed
result = await asyncio.to_thread(blocking_function, arg1)
```

## Possible SDK-Level Solutions

### Option 1: Manual Helper (Current Approach)

**What:** Provide documented `_run_with_context()` pattern.

**Pros:** Explicit, no magic, works everywhere  
**Cons:** Requires user code changes

### Option 2: ContextAwareThreadPoolExecutor

**What:** SDK provides a drop-in executor that auto-propagates context.

```python
from concurrent.futures import ThreadPoolExecutor
import contextvars

class ContextAwareThreadPoolExecutor(ThreadPoolExecutor):
    def submit(self, fn, *args, **kwargs):
        ctx = contextvars.copy_context()
        return super().submit(ctx.run, fn, *args, **kwargs)
```

**Pros:** Clean API, opt-in  
**Cons:** User must change imports

### Option 3: Monkey-patch ThreadPoolExecutor

**What:** SDK patches `ThreadPoolExecutor.submit()` globally at initialization.

**Pros:** Zero user code changes  
**Cons:**

- High risk of breaking other libraries
- Hidden global side effects
- Performance overhead for all executors (even unrelated ones)
- Debugging becomes harder

**Recommendation:** Not recommended for tracing SDKs.

## Comparison with Node.js SDK

| Aspect | Python | Node.js |
|--------|--------|---------|
| Async context mechanism | `contextvars` (native) | `AsyncLocalStorage` via OpenTelemetry |
| `async/await` propagation | ✅ Automatic | ❌ Requires `context.with()` |
| Thread pools | ❌ Manual propagation | N/A (single-threaded) |
| Callbacks | ❌ Context lost | ❌ Requires `context.bind()` |

Python's native `contextvars` makes async code simpler—no explicit binding needed for `await` chains. However, thread pools and callbacks still require explicit handling in both languages.

## Testing Context Propagation

The FastAPI e2e tests include endpoints that verify context propagation:

- `GET /api/test-async-context` - Verifies context across concurrent async calls
- `GET /api/test-thread-context` - Verifies explicit thread pool propagation

Run the e2e tests to validate:

```bash
cd drift/instrumentation/fastapi/e2e-tests
./run.sh
```

## Edge Cases to Watch For

1. **Libraries using internal thread pools** (e.g., some HTTP clients, database drivers) - May lose context unless the library explicitly supports it

2. **Fire-and-forget async tasks** - `asyncio.create_task()` preserves context, but if the task outlives the parent span, relationships may be unclear

3. **Gevent/eventlet** - Green threads have different context semantics; not currently tested

4. **Multiprocessing** - Context cannot be serialized across process boundaries; each process needs independent tracing setup
