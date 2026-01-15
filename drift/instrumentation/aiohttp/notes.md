# aiohttp Instrumentation Notes

## 1. Socket Instrumentation Warnings from `aiohappyeyeballs`

### Background

aiohttp uses `aiohappyeyeballs` internally for connection management. This library implements the "Happy Eyeballs" algorithm (RFC 8305) for fast and reliable connection establishment by racing IPv4 and IPv6 connections.

When making HTTP requests, aiohttp's connection flow is:

1. Application calls `session.get()` or similar method
2. This calls `ClientSession._request()` (which we patch)
3. aiohttp internally uses `aiohappyeyeballs` to establish the TCP connection
4. `aiohappyeyeballs` uses low-level asyncio APIs like `loop.sock_connect()`
5. These schedule callbacks via the asyncio event loop

### Known Limitation

**Problem:** In REPLAY mode, socket instrumentation warnings are still logged for aiohttp requests, even though the HTTP layer is properly instrumented.

**Root Cause:** Python's `contextvars` propagate through `async/await` chains, but NOT through all asyncio internal callback mechanisms. When `aiohappyeyeballs` schedules socket operations via `loop.sock_connect()`, these run as event loop callbacks that don't inherit the `calling_library_context` we set in the patched `_request()` method.

The stack trace shows the context boundary:

```text
File "asyncio/events.py", line 88, in _run
    self._context.run(self._callback, *self._args)
File "aiohappyeyeballs/_staggered.py", line 132, in run_one_coro
    result = await coro_fn()
File "aiohappyeyeballs/impl.py", line 208, in _connect_sock
    await loop.sock_connect(sock, address)
```

The `_context.run()` call creates a new context for the callback, which doesn't include our `calling_library_context`.

### Why This Happens Only with aiohttp

Other HTTP client libraries like httpx don't have this issue because:

| Library | Connection Layer | Context Propagation |
|---------|-----------------|---------------------|
| **httpx** | Uses `httpcore` which maintains context through async/await | ✅ Works correctly |
| **requests** | Synchronous, no callbacks | ✅ Works correctly |
| **urllib3** | Synchronous, no callbacks | ✅ Works correctly |
| **aiohttp** | Uses `aiohappyeyeballs` with asyncio callbacks | ⚠️ Context lost in callbacks |

### Impact

| Aspect | Impact |
|--------|--------|
| **Functionality** | ✅ No impact - instrumentation works correctly |
| **E2E Tests** | ✅ All tests pass |
| **Logs** | ⚠️ Spurious "Unpatched dependency alert" warnings in REPLAY mode |
| **CLI Alerts** | ⚠️ False positive alerts sent to CLI |

The warnings are cosmetic - they don't indicate actual unpatched dependencies since we successfully intercept at the higher `ClientSession._request()` level.

### Mitigation

The e2e test framework already handles this by skipping socket instrumentation warning checks for aiohttp:

```python
# Note: The socket instrumentation will detect and warn about these calls, but this
# is expected behavior. The warnings indicate that there are low-level socket calls
# happening that are not directly instrumented by our aiohttp instrumentation.
# This is normal for aiohappyeyeballs and doesn't indicate an instrumentation bug
# since we successfully intercept at the higher ClientSession._request() level.
```

### Code Location

The `calling_library_context` is set in `_patch_client_session()` in [`instrumentation.py`](./instrumentation.py):

```python
async def patched_request(client_self, method: str, str_or_url, **kwargs):
    # ...
    # Set calling_library_context to suppress socket instrumentation warnings
    # for internal socket calls (e.g., aiohappyeyeballs connection management)
    context_token = calling_library_context.set("aiohttp")
    try:
        # ... handle REPLAY or RECORD mode ...
    finally:
        calling_library_context.reset(context_token)
```

### Future Considerations

**Potential approaches to eliminate the warnings:**

1. **Stack trace inspection**: Modify socket instrumentation to check the stack trace for known libraries like `aiohappyeyeballs` and suppress warnings. However, this adds overhead to every socket call and is fragile if library internals change.

2. **asyncio context propagation**: Python's `contextvars` module has `copy_context()` which could theoretically be used, but we don't control how `aiohappyeyeballs` or asyncio schedules callbacks.

3. **Library-specific skip list**: Add a configuration to socket instrumentation to skip warnings for known libraries. This is the most practical approach but requires maintenance as new libraries are added.

**Current approach:** Accept the warnings for aiohttp as expected behavior. The e2e test framework already handles this correctly, and the warnings don't affect functionality. Users who see these warnings in their logs can be informed they're expected for aiohttp.

### Test Coverage

The e2e tests verify that:

- All aiohttp request types work correctly in RECORD and REPLAY modes
- The socket instrumentation warning check is skipped for aiohttp
- Traces are captured correctly despite the warnings
