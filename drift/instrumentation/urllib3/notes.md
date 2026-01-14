# urllib3 Instrumentation Notes

---

## 1. `preload_content=False` and Streaming Responses

### Background

In urllib3, when making HTTP requests:

- **`preload_content=True` (default)**: Response body is immediately read into memory and cached in `response._body`
- **`preload_content=False`**: Response body is NOT automatically read; the underlying stream remains open for the application to read manually via `response.read()` or `response.stream()`

This is commonly used for:

- Large file downloads (avoid loading GB into memory)
- Streaming responses (process chunks as they arrive)
- Memory-efficient processing

### Bug Found & Fixed

**Problem:** The instrumentation broke applications using `preload_content=False` because accessing `response.data` in `_finalize_span()` consumed the response stream before the application could read it.

**Root Cause:** The original code used `hasattr(response, "data")` to check for the attribute. However, Python's `hasattr()` internally calls `getattr()`, which triggers property getters. urllib3's `.data` is a property that reads the entire body when accessed, consuming the stream.

**Fix:** Check for the internal `_body` attribute directly using `getattr(response, "_body", sentinel)` instead of `hasattr(response, "data")`.

**Logging**: Instead of silently skipping body capture, the SDK will emit a warning in application logs to inform users that replay may not work correctly for this request. Example:

```text
[TuskDrift] Response body not captured for GET https://example.com/api - request used preload_content=False or streaming. Replay may return an empty body.
```

### Trade-off: RECORD vs REPLAY

For `preload_content=False` responses, we face an inherent trade-off:

| Mode | Behavior |
|------|----------|
| **RECORD** | ✅ Works correctly - we skip capturing the body, so the application can read/stream normally |
| **REPLAY** | ⚠️ Response body will be empty - we didn't capture it, so there's nothing to return |

This is intentional. We prioritize not breaking the application during RECORD over capturing data for REPLAY.

### Mock Matching Impact

Mock matching is not affected because the CLI's matching algorithm is entirely input-based. It matches on request URL, method, headers, body (InputValue), and uses InputValueHash, InputSchemaHash, and similarity scoring on InputValue. OutputValue (response) is not used for matching.

Mock **response** is affected:

- The matched span's `OutputValue.body` will be empty/missing
- Status code and headers are still captured correctly
- Application may fail if it expects actual response data

### Code Location

The safe body retrieval logic is in `_get_response_body_safely()` method in [`instrumentation.py`](./instrumentation.py):

```python
def _get_response_body_safely(self, response: Any) -> bytes | None:
    # Use getattr with a sentinel to avoid triggering property getters
    _sentinel = object()
    body = getattr(response, "_body", _sentinel)
    
    if body is _sentinel:
        return b""  # Not a urllib3 HTTPResponse
    
    if body is not None:
        return body  # Body already cached, safe to return
    
    # Body hasn't been read yet (preload_content=False)
    # Skip to avoid consuming the stream
    return None
```

### Test Coverage

The e2e tests include endpoints for `preload_content=False` and streaming responses (`/test/bug/preload-content-false`, `/test/bug/streaming-response`), but these are excluded from the automated REPLAY test suite since they're incompatible with replay. They can be tested manually to verify RECORD mode doesn't break the application.

### Future Considerations

**Potential improvements to support replay for streaming responses:**

1. **Response wrapper approach**: Create a wrapper around the urllib3 response that intercepts `read()` and `stream()` calls, capturing bytes as the application reads them. After the application finishes reading, store the accumulated body in the span. This would require:
   - Implementing a custom response wrapper class
   - Deferring span finalization until the response is fully consumed
   - Handling edge cases like partial reads, connection errors mid-stream

2. **Post-read body capture**: After the application calls `response.read()`, the body becomes cached in `response._body`. We could potentially capture it at that point. However, this requires knowing *when* the application has finished reading, which is non-trivial.

This may not be worth implementing at the moment because:

- Applications using `preload_content=False` are typically downloading large files or streaming data - scenarios that aren't good candidates for unit test mocking anyway
- The complexity of implementing response wrappers could introduce subtle bugs
- Most API calls use the default `preload_content=True` and work correctly
