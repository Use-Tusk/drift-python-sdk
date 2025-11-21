from __future__ import annotations

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
)
from ..base import InstrumentationBase


HEADER_SCHEMA_MERGES = {
    "headers": SchemaMerge(match_importance=0.0),
}

MAX_BODY_SIZE = 10000  # 10KB limit


class FastAPIInstrumentation(InstrumentationBase):
    def __init__(self, enabled: bool = True):
        super().__init__(
            name="FastAPIInstrumentation",
            module_name="fastapi",
            supported_versions=">=0.68.0",
            enabled=enabled,
        )

    @override
    def patch(self, module: ModuleType) -> None:
        """Patch FastAPI to capture HTTP requests/responses"""
        fastapi_class = getattr(module, "FastAPI", None)
        if not fastapi_class:
            print("Warning: FastAPI.FastAPI class not found")
            return

        original_call = fastapi_class.__call__

        @wraps(original_call)
        async def instrumented_call(
            self: Any, scope: Scope, receive: Receive, send: Send
        ) -> None:
            # Only instrument HTTP requests, pass through websocket/lifespan
            if scope.get("type") != "http":
                return await original_call(self, scope, receive, send)

            return await _handle_request(self, scope, receive, send, original_call)

        fastapi_class.__call__ = instrumented_call
        print("FastAPI instrumentation applied")


async def _handle_request(
    app: Any,
    scope: Scope,
    receive: Receive,
    send: Send,
    original_call: Callable[..., Any],
) -> None:
    """Handle a single FastAPI request by capturing request/response data"""
    start_time_ns = time.time_ns()
    trace_id = str(uuid.uuid4()).replace("-", "")
    span_id = str(uuid.uuid4()).replace("-", "")[:16]
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
            scope, response_data, request_body, body_truncated,
            response_body, response_body_truncated,
            start_time_ns, trace_id, span_id
        )
    except Exception as e:
        response_data["status_code"] = 500
        response_data["error"] = str(e)
        response_data["error_type"] = type(e).__name__
        request_body = b"".join(request_body_parts) if request_body_parts else None
        response_body = b"".join(response_body_parts) if response_body_parts else None
        _capture_span(
            scope, response_data, request_body, body_truncated,
            response_body, response_body_truncated,
            start_time_ns, trace_id, span_id
        )
        raise


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
) -> None:
    """Create and collect a span from request/response data"""
    end_time_ns = time.time_ns()
    duration_ns = end_time_ns - start_time_ns

    method = scope.get("method", "GET")
    path = scope.get("path", "/")
    query_string = scope.get("query_string", b"")
    if isinstance(query_string, bytes):
        query_string = query_string.decode("utf-8", errors="replace")

    input_value: dict[str, Any] = {
        "method": method,
        "path": path,
        "url": _build_url(scope),
        "query": query_string,
        "headers": _extract_headers(scope),
    }

    if request_body:
        try:
            input_value["body"] = json.loads(request_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            input_value["body"] = request_body.decode("utf-8", errors="replace")
        input_value["body_size"] = len(request_body)
        if request_body_truncated:
            input_value["body_truncated"] = True

    output_value: dict[str, Any] = {
        "status_code": response_data.get("status_code", 200),
        "headers": response_data.get("headers", {}),
    }

    if response_body:
        try:
            output_value["body"] = json.loads(response_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            output_value["body"] = response_body.decode("utf-8", errors="replace")
        output_value["body_size"] = len(response_body)
        if response_body_truncated:
            output_value["body_truncated"] = True

    if "error" in response_data:
        output_value["error"] = response_data["error"]
    if "error_type" in response_data:
        output_value["error_type"] = response_data["error_type"]

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

    input_schema_info = JsonSchemaHelper.generate_schema_and_hash(
        input_value, HEADER_SCHEMA_MERGES
    )
    output_schema_info = JsonSchemaHelper.generate_schema_and_hash(
        output_value, HEADER_SCHEMA_MERGES
    )

    # Use route template if available (e.g., "/greet/{name}") to avoid cardinality explosion
    # Falls back to literal path if route not set (e.g., 404 responses)
    route = scope.get("route")
    route_path = getattr(route, "path", None) if route else None
    span_name = f"{method} {route_path or path}"

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
    )

    sdk.collect_span(span)


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
