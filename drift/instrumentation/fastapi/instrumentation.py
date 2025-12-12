from __future__ import annotations

import base64
import json
import time
import uuid
from functools import wraps
from types import ModuleType
from typing import TYPE_CHECKING, Any, Callable, override

if TYPE_CHECKING:
    from collections.abc import Awaitable

    Scope = dict[str, Any]
    Receive = Callable[[], Awaitable[dict[str, Any]]]
    Send = Callable[[dict[str, Any]], Awaitable[None]]

from ...core.drift_sdk import TuskDrift
from ...core.json_schema_helper import JsonSchemaHelper, SchemaMerge
from ...core.types import (
    CleanSpanData,
    Duration,
    PackageType,
    SpanKind,
    SpanStatus,
    StatusCode,
    Timestamp,
    current_trace_id_context,
    current_span_id_context,
)
from ..base import InstrumentationBase
from ..http import HttpSpanData, HttpTransformEngine


HEADER_SCHEMA_MERGES = {
    "headers": SchemaMerge(match_importance=0.0),
}

MAX_BODY_SIZE = 10000  # 10KB limit


class FastAPIInstrumentation(InstrumentationBase):
    def __init__(self, enabled: bool = True, transforms: dict[str, Any] | None = None):
        self._transform_engine = HttpTransformEngine(
            self._resolve_http_transforms(transforms)
        )
        super().__init__(
            name="FastAPIInstrumentation",
            module_name="fastapi",
            supported_versions=">=0.68.0",
            enabled=enabled,
        )

    def _resolve_http_transforms(
        self, provided: dict[str, Any] | list[dict[str, Any]] | None
    ) -> list[dict[str, Any]] | None:
        if isinstance(provided, list):
            return provided
        if isinstance(provided, dict) and isinstance(provided.get("http"), list):
            return provided["http"]

        sdk = TuskDrift.get_instance()
        transforms = getattr(sdk.config, "transforms", None)
        if isinstance(transforms, dict) and isinstance(transforms.get("http"), list):
            return transforms["http"]
        return None

    @override
    def patch(self, module: ModuleType) -> None:
        """Patch FastAPI to capture HTTP requests/responses"""
        fastapi_class = getattr(module, "FastAPI", None)
        if not fastapi_class:
            print("Warning: FastAPI.FastAPI class not found")
            return

        original_call = fastapi_class.__call__
        transform_engine = self._transform_engine

        @wraps(original_call)
        async def instrumented_call(
            self: Any, scope: Scope, receive: Receive, send: Send
        ) -> None:
            # Only instrument HTTP requests, pass through websocket/lifespan
            if scope.get("type") != "http":
                return await original_call(self, scope, receive, send)

            return await _handle_request(
                self,
                scope,
                receive,
                send,
                original_call,
                transform_engine,
            )

        fastapi_class.__call__ = instrumented_call
        print("FastAPI instrumentation applied")


async def _handle_replay_request(
    app: Any,
    scope: Scope,
    receive: Receive,
    send: Send,
    original_call: Callable[..., Any],
    transform_engine: HttpTransformEngine | None,
    method: str,
    raw_path: str,
    target: str,
) -> None:
    """Handle FastAPI request in REPLAY mode.

    In replay mode, server requests:
    - Extract trace context from headers (x-td-trace-id)
    - Fetch environment variables if requested (x-td-fetch-env-vars)
    - Execute the request normally (NOT mocked!)
    - Create SERVER span for tracking
    """
    import logging
    from ...core.types import replay_trace_id_context

    logger = logging.getLogger(__name__)
    sdk = TuskDrift.get_instance()

    # Extract trace ID from headers (case-insensitive lookup)
    request_headers = _extract_headers(scope)
    # Convert headers to lowercase for case-insensitive lookup
    headers_lower = {k.lower(): v for k, v in request_headers.items()}
    replay_trace_id = headers_lower.get("x-td-trace-id")

    if not replay_trace_id:
        logger.debug(f"[FastAPIInstrumentation] No trace ID found in headers for {method} {raw_path}")
        # No trace context; proceed without span
        return await original_call(app, scope, receive, send)

    logger.debug(f"[FastAPIInstrumentation] Setting replay trace ID: {replay_trace_id}")

    # Fetch env vars from CLI if requested
    should_fetch_env_vars = headers_lower.get("x-td-fetch-env-vars") == "true"
    if should_fetch_env_vars:
        try:
            env_vars = sdk.request_env_vars_sync(replay_trace_id)

            # Store in tracker for env instrumentation to use
            from ..env import EnvVarTracker
            tracker = EnvVarTracker.get_instance()
            tracker.set_env_vars(replay_trace_id, env_vars)

            logger.debug(f"[FastAPIInstrumentation] Fetched {len(env_vars)} env vars from CLI for trace {replay_trace_id}")
        except Exception as e:
            logger.error(f"[FastAPIInstrumentation] Failed to fetch env vars from CLI: {e}")

    # Remove accept-encoding header to prevent compression during replay
    # (responses are stored decompressed, compression would double-compress)
    if "accept-encoding" in headers_lower:
        # Modify headers in scope
        headers_list = scope.get("headers", [])
        scope["headers"] = [
            (k, v) for k, v in headers_list
            if k.decode("utf-8", errors="replace").lower() != "accept-encoding"
        ]

    # Set replay trace context using context variable
    replay_token = replay_trace_id_context.set(replay_trace_id)

    try:
        # Generate span IDs
        start_time_ns = time.time_ns()
        trace_id = replay_trace_id
        span_id = str(uuid.uuid4()).replace("-", "")[:16]

        # Set trace context for child spans (e.g., outbound HTTP calls)
        trace_token = current_trace_id_context.set(trace_id)
        span_token = current_span_id_context.set(span_id)

        response_data: dict[str, Any] = {}
        request_body_parts: list[bytes] = []
        total_body_size = 0
        body_truncated = False
        response_body_parts: list[bytes] = []
        response_body_size = 0
        response_body_truncated = False

        # Wrap receive to capture request body
        async def wrapped_receive() -> dict[str, Any]:
            nonlocal total_body_size, body_truncated
            message = await receive()
            if message.get("type") == "http.request":
                body_chunk = message.get("body", b"")
                if body_chunk:
                    if total_body_size >= MAX_BODY_SIZE:
                        body_truncated = True
                    else:
                        remaining_space = MAX_BODY_SIZE - total_body_size
                        if len(body_chunk) > remaining_space:
                            body_chunk = body_chunk[:remaining_space]
                            body_truncated = True
                        request_body_parts.append(body_chunk)
                        total_body_size += len(body_chunk)
            return message

        # Wrap send to capture response status, headers, and body
        async def wrapped_send(message: dict[str, Any]) -> None:
            nonlocal response_body_size, response_body_truncated
            if message.get("type") == "http.response.start":
                response_data["status_code"] = message.get("status", 200)
                response_data["status_message"] = _get_status_message(message.get("status", 200))
                raw_headers = message.get("headers", [])
                response_data["headers"] = {
                    k.decode("utf-8", errors="replace") if isinstance(k, bytes) else k: v.decode("utf-8", errors="replace") if isinstance(v, bytes) else v
                    for k, v in raw_headers
                }
            elif message.get("type") == "http.response.body":
                body_chunk = message.get("body", b"")
                if body_chunk:
                    if response_body_size >= MAX_BODY_SIZE:
                        response_body_truncated = True
                    else:
                        remaining_space = MAX_BODY_SIZE - response_body_size
                        if len(body_chunk) > remaining_space:
                            body_chunk = body_chunk[:remaining_space]
                            response_body_truncated = True
                        response_body_parts.append(body_chunk)
                        response_body_size += len(body_chunk)
            await send(message)

        await original_call(app, scope, wrapped_receive, wrapped_send)
        request_body = b"".join(request_body_parts) if request_body_parts else None
        response_body = b"".join(response_body_parts) if response_body_parts else None
        _capture_span(
            scope,
            response_data,
            request_body,
            body_truncated,
            response_body,
            response_body_truncated,
            start_time_ns,
            trace_id,
            span_id,
            transform_engine,
        )
    finally:
        # Reset context
        replay_trace_id_context.reset(replay_token)
        current_trace_id_context.reset(trace_token)
        current_span_id_context.reset(span_token)


async def _handle_request(
    app: Any,
    scope: Scope,
    receive: Receive,
    send: Send,
    original_call: Callable[..., Any],
    transform_engine: HttpTransformEngine | None,
) -> None:
    """Handle a single FastAPI request by capturing request/response data"""
    sdk = TuskDrift.get_instance()

    method = scope.get("method", "GET")
    raw_path = scope.get("path", "/")
    query_bytes = scope.get("query_string", b"")
    if isinstance(query_bytes, bytes):
        query_string = query_bytes.decode("utf-8", errors="replace")
    else:
        query_string = str(query_bytes)
    target_for_drop = f"{raw_path}?{query_string}" if query_string else raw_path
    headers_for_drop = _extract_headers(scope)

    if transform_engine and transform_engine.should_drop_inbound_request(
        method,
        target_for_drop,
        headers_for_drop,
    ):
        return await original_call(app, scope, receive, send)

    # Handle REPLAY mode
    if sdk.mode == "REPLAY":
        return await _handle_replay_request(
            app, scope, receive, send, original_call, transform_engine, method, raw_path, target_for_drop
        )

    # RECORD mode or DISABLED mode
    start_time_ns = time.time_ns()
    trace_id = str(uuid.uuid4()).replace("-", "")
    span_id = str(uuid.uuid4()).replace("-", "")[:16]

    # Set trace context for child spans (e.g., outbound HTTP calls)
    trace_token = current_trace_id_context.set(trace_id)
    span_token = current_span_id_context.set(span_id)

    response_data: dict[str, Any] = {}
    request_body_parts: list[bytes] = []
    total_body_size = 0
    body_truncated = False
    response_body_parts: list[bytes] = []
    response_body_size = 0
    response_body_truncated = False

    # Wrap receive to capture request body
    async def wrapped_receive() -> dict[str, Any]:
        nonlocal total_body_size, body_truncated
        message = await receive()
        if message.get("type") == "http.request":
            body_chunk = message.get("body", b"")
            if body_chunk:  # Only process non-empty chunks
                if total_body_size >= MAX_BODY_SIZE:
                    # Already at limit, flag that we're dropping data
                    body_truncated = True
                else:
                    # Calculate remaining space and truncate if necessary
                    remaining_space = MAX_BODY_SIZE - total_body_size
                    if len(body_chunk) > remaining_space:
                        body_chunk = body_chunk[:remaining_space]
                        body_truncated = True
                    request_body_parts.append(body_chunk)
                    total_body_size += len(body_chunk)
        return message

    # Wrap send to capture response status, headers, and body
    async def wrapped_send(message: dict[str, Any]) -> None:
        nonlocal response_body_size, response_body_truncated
        if message.get("type") == "http.response.start":
            response_data["status_code"] = message.get("status", 200)
            # ASGI doesn't provide status message directly, derive from status code
            response_data["status_message"] = _get_status_message(message.get("status", 200))
            # Convert headers from list of tuples to dict
            raw_headers = message.get("headers", [])
            response_data["headers"] = {
                k.decode("utf-8", errors="replace") if isinstance(k, bytes) else k: v.decode("utf-8", errors="replace") if isinstance(v, bytes) else v
                for k, v in raw_headers
            }
        elif message.get("type") == "http.response.body":
            body_chunk = message.get("body", b"")
            if body_chunk:
                if response_body_size >= MAX_BODY_SIZE:
                    response_body_truncated = True
                else:
                    remaining_space = MAX_BODY_SIZE - response_body_size
                    if len(body_chunk) > remaining_space:
                        body_chunk = body_chunk[:remaining_space]
                        response_body_truncated = True
                    response_body_parts.append(body_chunk)
                    response_body_size += len(body_chunk)
        await send(message)

    try:
        await original_call(app, scope, wrapped_receive, wrapped_send)
        request_body = b"".join(request_body_parts) if request_body_parts else None
        response_body = b"".join(response_body_parts) if response_body_parts else None
        _capture_span(
            scope,
            response_data,
            request_body,
            body_truncated,
            response_body,
            response_body_truncated,
            start_time_ns,
            trace_id,
            span_id,
            transform_engine,
        )
    except Exception as e:
        response_data["status_code"] = 500
        response_data["error"] = str(e)
        response_data["error_type"] = type(e).__name__
        request_body = b"".join(request_body_parts) if request_body_parts else None
        response_body = b"".join(response_body_parts) if response_body_parts else None
        _capture_span(
            scope,
            response_data,
            request_body,
            body_truncated,
            response_body,
            response_body_truncated,
            start_time_ns,
            trace_id,
            span_id,
            transform_engine,
        )
        raise
    finally:
        # Reset trace context
        current_trace_id_context.reset(trace_token)
        current_span_id_context.reset(span_token)


def _capture_span(
    scope: Scope,
    response_data: dict[str, Any],
    request_body: bytes | None,
    request_body_truncated: bool,
    response_body: bytes | None,
    response_body_truncated: bool,
    start_time_ns: int,
    trace_id: str,
    span_id: str,
    transform_engine: HttpTransformEngine | None,
) -> None:
    """Create and collect a span from request/response data"""
    end_time_ns = time.time_ns()
    duration_ns = end_time_ns - start_time_ns

    method = scope.get("method", "GET")
    path = scope.get("path", "/")
    query_string = scope.get("query_string", b"")
    if isinstance(query_string, bytes):
        query_string = query_string.decode("utf-8", errors="replace")

    # Build target (path + query string) to match Node SDK
    target = f"{path}?{query_string}" if query_string else path

    # Get HTTP version from scope
    http_version = scope.get("http_version", "1.1")

    # Get remote address info from scope
    client = scope.get("client")
    remote_address = client[0] if client else None
    remote_port = client[1] if client and len(client) > 1 else None

    input_value: dict[str, Any] = {
        "method": method,
        "url": _build_url(scope),
        "target": target,  # Path + query string combined, matches Node SDK
        "headers": _extract_headers(scope),
        "httpVersion": http_version,
    }
    # Add optional fields only if present
    if remote_address:
        input_value["remoteAddress"] = remote_address
    if remote_port:
        input_value["remotePort"] = remote_port

    if request_body:
        # Store body as Base64 encoded string to match Node SDK behavior
        input_value["body"] = base64.b64encode(request_body).decode("ascii")
        input_value["bodySize"] = len(request_body)
        if request_body_truncated:
            input_value["bodyProcessingError"] = "truncated"  # Match Node SDK field name

    output_value: dict[str, Any] = {
        "statusCode": response_data.get("status_code", 200),  # camelCase to match Node SDK
        "statusMessage": response_data.get("status_message", ""),
        "headers": response_data.get("headers", {}),
    }

    if response_body:
        # Store body as Base64 encoded string to match Node SDK behavior
        output_value["body"] = base64.b64encode(response_body).decode("ascii")
        output_value["bodySize"] = len(response_body)
        if response_body_truncated:
            output_value["bodyProcessingError"] = "truncated"  # Match Node SDK field name

    if "error" in response_data:
        output_value["errorMessage"] = response_data["error"]  # Match Node SDK field name
    if "error_type" in response_data:
        output_value["errorName"] = response_data["error_type"]  # Match Node SDK field name

    # Check if content type should block the trace
    from ...core.content_type_utils import get_decoded_type, should_block_content_type
    from ...core.trace_blocking_manager import TraceBlockingManager
    import logging

    logger = logging.getLogger(__name__)
    response_headers = response_data.get("headers", {})
    content_type = response_headers.get("content-type") or response_headers.get("Content-Type")
    decoded_type = get_decoded_type(content_type)

    if should_block_content_type(decoded_type):
        blocking_mgr = TraceBlockingManager.get_instance()
        blocking_mgr.block_trace(
            trace_id,
            reason=f"binary_content:{decoded_type.name if decoded_type else 'unknown'}"
        )
        logger.warning(
            f"Blocking trace {trace_id} - binary response: {content_type} "
            f"(decoded as {decoded_type.name if decoded_type else 'unknown'})"
        )
        return  # Skip span creation

    transform_metadata = None
    if transform_engine:
        span_data = HttpSpanData(
            kind=SpanKind.SERVER,
            input_value=input_value,
            output_value=output_value,
        )
        transform_engine.apply_transforms(span_data)
        input_value = span_data.input_value or input_value
        output_value = span_data.output_value or output_value
        transform_metadata = span_data.transform_metadata

    sdk = TuskDrift.get_instance()
    # Derive timestamp from start_time_ns (not datetime.now() which would be end time)
    timestamp_seconds = start_time_ns // 1_000_000_000
    timestamp_nanos = start_time_ns % 1_000_000_000
    duration_seconds = duration_ns // 1_000_000_000
    duration_nanos = duration_ns % 1_000_000_000

    status_code = response_data.get("status_code", 200)
    if status_code >= 400:
        status = SpanStatus(code=StatusCode.ERROR, message=f"HTTP {status_code}")
    else:
        status = SpanStatus(code=StatusCode.OK, message="")

    # Build schema merge hints including body encoding and truncation flags
    input_schema_merges = dict(HEADER_SCHEMA_MERGES)
    if "body" in input_value:
        from ...core.json_schema_helper import EncodingType
        input_schema_merges["body"] = SchemaMerge(encoding=EncodingType.BASE64)
    # Add bodyProcessingError to schema merges if truncated (matches Node SDK)
    if request_body_truncated:
        input_schema_merges["bodyProcessingError"] = SchemaMerge(match_importance=1.0)

    output_schema_merges = dict(HEADER_SCHEMA_MERGES)
    if "body" in output_value:
        from ...core.json_schema_helper import EncodingType
        output_schema_merges["body"] = SchemaMerge(encoding=EncodingType.BASE64)
    # Add bodyProcessingError to schema merges if truncated (matches Node SDK)
    if response_body_truncated:
        output_schema_merges["bodyProcessingError"] = SchemaMerge(match_importance=1.0)

    input_schema_info = JsonSchemaHelper.generate_schema_and_hash(
        input_value, input_schema_merges
    )
    output_schema_info = JsonSchemaHelper.generate_schema_and_hash(
        output_value, output_schema_merges
    )

    # Use route template if available (e.g., "/greet/{name}") to avoid cardinality explosion
    # Falls back to literal path if route not set (e.g., 404 responses)
    route = scope.get("route")
    route_path = getattr(route, "path", None) if route else None
    span_name = f"{method} {route_path or path}"

    # Attach env vars to metadata if present
    from ..env import EnvVarTracker
    from ...core.types import MetadataObject
    tracker = EnvVarTracker.get_instance()
    env_vars = tracker.get_env_vars(trace_id)
    metadata = None
    if env_vars:
        metadata = MetadataObject(ENV_VARS=env_vars)

    span = CleanSpanData(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id="",
        name=span_name,
        package_name="fastapi",
        instrumentation_name="FastAPIInstrumentation",
        submodule_name=method,
        package_type=PackageType.HTTP,
        kind=SpanKind.SERVER,
        input_value=input_value,
        output_value=output_value,
        input_schema=input_schema_info.schema,
        output_schema=output_schema_info.schema,
        input_value_hash=input_schema_info.decoded_value_hash,
        output_value_hash=output_schema_info.decoded_value_hash,
        input_schema_hash=input_schema_info.decoded_schema_hash,
        output_schema_hash=output_schema_info.decoded_schema_hash,
        status=status,
        is_pre_app_start=not sdk.app_ready,
        is_root_span=True,
        timestamp=Timestamp(seconds=timestamp_seconds, nanos=timestamp_nanos),
        duration=Duration(seconds=duration_seconds, nanos=duration_nanos),
        transform_metadata=transform_metadata,
        metadata=metadata,
    )

    sdk.collect_span(span)

    # Clear tracker after span collection
    tracker.clear_env_vars(trace_id)


def _build_url(scope: Scope) -> str:
    """Build full URL from ASGI scope"""
    scheme = scope.get("scheme", "http")

    # Get host from headers or server
    host = None
    headers = scope.get("headers", [])
    for key, value in headers:
        if key == b"host" or key == "host":
            host = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
            break

    if not host:
        server = scope.get("server")
        if server:
            host_name, port = server
            if (scheme == "http" and port != 80) or (scheme == "https" and port != 443):
                host = f"{host_name}:{port}"
            else:
                host = host_name
        else:
            host = "localhost"

    path = scope.get("path", "/")
    query_string = scope.get("query_string", b"")
    if isinstance(query_string, bytes):
        query_string = query_string.decode("utf-8", errors="replace")

    url = f"{scheme}://{host}{path}"
    if query_string:
        url += f"?{query_string}"
    return url


def _extract_headers(scope: Scope) -> dict[str, str]:
    """Extract HTTP headers from ASGI scope"""
    headers: dict[str, str] = {}
    for key, value in scope.get("headers", []):
        # ASGI headers are bytes tuples - use errors="replace" for safety
        header_name = key.decode("utf-8", errors="replace") if isinstance(key, bytes) else key
        header_value = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
        # Convert to title case for consistency with Flask
        headers[header_name.title()] = header_value
    return headers


# HTTP status code to message mapping (standard codes)
_HTTP_STATUS_MESSAGES: dict[int, str] = {
    100: "Continue",
    101: "Switching Protocols",
    200: "OK",
    201: "Created",
    202: "Accepted",
    204: "No Content",
    301: "Moved Permanently",
    302: "Found",
    304: "Not Modified",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    422: "Unprocessable Entity",
    429: "Too Many Requests",
    500: "Internal Server Error",
    501: "Not Implemented",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}


def _get_status_message(status_code: int) -> str:
    """Get HTTP status message for a status code."""
    return _HTTP_STATUS_MESSAGES.get(status_code, "")
