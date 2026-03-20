# urllib3 Instrumentation Notes

---

## 1. `preload_content=False` and Streaming Responses

### Background

In urllib3, when making HTTP requests:

- **`preload_content=True` (default)**: Response body is immediately read into memory and cached in `response._body`
- **`preload_content=False`**: Response body is NOT automatically read; the underlying stream (`_fp`) remains open for the caller to read manually via `response.read()` or `response.stream()`

`preload_content=False` is commonly used by:

- **botocore/boto3** — always uses this to control the read lifecycle, validate CRC32 checksums, and handle retries
- Large file downloads (avoid loading GB into memory)
- Streaming responses (process chunks as they arrive)

### How It Works (Recording)

When recording a `preload_content=False` response, the instrumentation captures the body in `_get_response_body_safely()` by:

1. Reading the raw bytes from `response._fp` (the underlying socket/`http.client.HTTPResponse`)
2. Replacing `response._fp` with an `io.BytesIO` containing those same bytes
3. Returning the bytes for inclusion in the span's `outputValue.body`

This is safe because `_finalize_span` runs in the `finally` block **before** the response is returned to the caller. The caller then reads from the BytesIO exactly as it would have read from the socket — urllib3's `read()` pipeline (content decoding, connection release, etc.) processes the same bytes.

**Important**: We read from `_fp` directly rather than calling `response.read()` because urllib3's `read()` consumes `_fp` and does **not** check `_body` on subsequent calls. Only the `.data` property checks `_body`. Since botocore uses `read()`, a second `read()` call would return `b""` and break CRC32 checksum validation.

A size guard (`_MAX_CAPTURE_BYTES = 1 MB`) prevents buffering very large responses into memory. Spans exceeding 1 MB are blocked at export time anyway (`MAX_SPAN_SIZE_BYTES`), so there's no value in reading more than that from the socket.

### How It Works (Replay)

When replaying, `_create_mock_response()` constructs a urllib3 `HTTPResponse` with `preload_content=False` and a `BytesIO` containing the recorded body. This ensures:

- Callers using `read()` (botocore) read from the fresh BytesIO and get the body
- Callers using `.data` (requests library) trigger `read(cache_content=True)`, which reads from the BytesIO and caches in `_body`
- Callers using `stream()` read from the BytesIO in chunks

`preload_content` must be `False` for mock responses — with `True`, the constructor exhausts the BytesIO during initialization, and subsequent `read()` calls return `b""`.

### Code Location

The safe body retrieval logic is in `_get_response_body_safely()` in [`instrumentation.py`](./instrumentation.py).  
Mock response construction is in `_create_mock_response()` in the same file.

### Test Coverage

The e2e tests include endpoints for the `preload_content=False` pattern:

- `/test/preload-content-false-read` — manual `read()` (botocore pattern)
- `/test/preload-content-false-crc32` — `read()` + CRC32 checksum validation (DynamoDB pattern)
- `/test/preload-content-false-stream` — chunked `stream()` reading

These are included in the automated REPLAY test suite.
