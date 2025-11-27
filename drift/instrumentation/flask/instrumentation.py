from __future__ import annotations

import base64
import time
import uuid
from collections.abc import Iterable, Iterator
from functools import wraps
from types import ModuleType
from typing import TYPE_CHECKING, Any, override

if TYPE_CHECKING:
    from _typeshed import OptExcInfo
    from _typeshed.wsgi import StartResponse, WSGIApplication, WSGIEnvironment

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
from ..http import HttpSpanData, HttpTransformEngine

HEADER_SCHEMA_MERGES = {
    "headers": SchemaMerge(match_importance=0.0),
}

MAX_BODY_SIZE = 10000  # 10KB limit


class FlaskInstrumentation(InstrumentationBase):
    def __init__(self, enabled: bool = True, transforms: dict[str, Any] | None = None):
        self._transform_engine = HttpTransformEngine(
            self._resolve_http_transforms(transforms)
        )
        super().__init__(
            name="FlaskInstrumentation",
            module_name="flask",
            supported_versions=">=2.0.0",
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
        """Patch Flask to capture HTTP requests/responses"""
        flask_class = getattr(module, "Flask", None)
        if not flask_class:
            print("Warning: Flask.Flask class not found")
            return

        original_wsgi_app: WSGIApplication = flask_class.wsgi_app  # pyright: ignore[reportAny]
        transform_engine = self._transform_engine

        # wraps(original) = functools.update_wrapper(instrumented, original)
        # copies some metadata over to instrumented
        @wraps(original_wsgi_app)
        def instrumented_wsgi_app(
            self: WSGIApplication,
            environ: WSGIEnvironment,
            start_response: StartResponse,
        ) -> Iterable[bytes]:
            return _handle_request(
                self,
                environ,
                start_response,
                original_wsgi_app,
                transform_engine,
            )

        flask_class.wsgi_app = instrumented_wsgi_app  # type: ignore
        print("Flask instrumentation applied")


class _ResponseBodyCapture(Iterable[bytes]):
    """
    Wrapper for WSGI response iterable that captures the response body.

    Captures body chunks up to MAX_BODY_SIZE and calls the span capture
    function when the response is closed.
    """

    def __init__(
        self,
        response: Iterable[bytes],
        environ: WSGIEnvironment,
        response_data: dict[str, Any],
        transform_engine: HttpTransformEngine | None,
    ):
        self._response = response
        self._environ = environ
        self._response_data = response_data
        self._transform_engine = transform_engine
        self._body_parts: list[bytes] = []
        self._body_size = 0
        self._body_truncated = False
        self._closed = False

    def __iter__(self) -> Iterator[bytes]:
        try:
            for chunk in self._response:
                # Capture chunk for body
                if chunk and not self._body_truncated:
                    if self._body_size >= MAX_BODY_SIZE:
                        self._body_truncated = True
                    else:
                        remaining = MAX_BODY_SIZE - self._body_size
                        if len(chunk) > remaining:
                            self._body_parts.append(chunk[:remaining])
                            self._body_size += remaining
                            self._body_truncated = True
                        else:
                            self._body_parts.append(chunk)
                            self._body_size += len(chunk)
                # Pass chunk through to actual response
                yield chunk
        finally:
            # Capture span when iteration completes
            self._finalize()

    def close(self) -> None:
        """Called by WSGI server when response is done."""
        self._finalize()
        # Call close on wrapped response if it has one
        if hasattr(self._response, "close"):
            self._response.close()  # type: ignore

    def _finalize(self) -> None:
        """Capture the span with collected response body."""
        if self._closed:
            return
        self._closed = True

        # Add response body to response_data
        if self._body_parts:
            body = b"".join(self._body_parts)
            self._response_data["body"] = body
            self._response_data["body_size"] = len(body)
            if self._body_truncated:
                self._response_data["body_truncated"] = True

        _capture_span(self._environ, self._response_data, self._transform_engine)


def _handle_request(
    app: WSGIApplication,
    environ: WSGIEnvironment,
    start_response: StartResponse,
    original_wsgi_app: WSGIApplication,
    transform_engine: HttpTransformEngine | None,
) -> Iterable[bytes]:
    """Handle a single Flask request by capturing request/response data"""
    start_time_ns = time.time_ns()
    trace_id = str(uuid.uuid4()).replace("-", "")
    span_id = str(uuid.uuid4()).replace("-", "")[:16]
    response_data: dict[str, Any] = {}  # pyright: ignore[reportExplicitAny]

    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "")
    query_string = environ.get("QUERY_STRING", "")
    target = f"{path}?{query_string}" if query_string else path

    request_body = None
    if environ.get("REQUEST_METHOD") in ("POST", "PUT", "PATCH"):
        try:
            content_length = int(environ.get("CONTENT_LENGTH", 0))
            if content_length > 0 and content_length <= MAX_BODY_SIZE:
                wsgi_input = environ.get("wsgi.input")
                if wsgi_input:
                    request_body = wsgi_input.read(content_length)
                    # Create a new BytesIO so Flask can read it
                    from io import BytesIO

                    environ["wsgi.input"] = BytesIO(request_body)
        except Exception:
            pass

    environ["_drift_request_body"] = request_body

    request_headers_for_drop = _extract_headers(environ)
    if transform_engine and transform_engine.should_drop_inbound_request(
        method, target, request_headers_for_drop
    ):
        return original_wsgi_app(app, environ, start_response)

    def wrapped_start_response(
        status: str,
        response_headers: list[tuple[str, str]],
        exc_info: OptExcInfo | None = None,
    ):
        # Parse status like "200 OK" -> (200, "OK")
        status_parts = status.split(None, 1)
        status_code = int(status_parts[0])
        status_message = status_parts[1] if len(status_parts) > 1 else ""
        response_data["status_code"] = status_code
        response_data["status_message"] = status_message
        response_data["headers"] = dict(response_headers)
        return start_response(status, response_headers, exc_info)

    environ["_drift_start_time_ns"] = start_time_ns
    environ["_drift_trace_id"] = trace_id
    environ["_drift_span_id"] = span_id

    try:
        response = original_wsgi_app(app, environ, wrapped_start_response)
        # Wrap response to capture body and defer span creation
        return _ResponseBodyCapture(response, environ, response_data, transform_engine)
    except Exception as e:
        response_data["status_code"] = 500
        response_data["error"] = str(e)
        _capture_span(environ, response_data, transform_engine)
        raise


def _capture_span(
    environ: WSGIEnvironment,
    response_data: dict[str, Any],
    transform_engine: HttpTransformEngine | None,
) -> None:
    """Create and collect a span from request/response data"""
    start_time_ns = environ.get("_drift_start_time_ns", 0)
    trace_id = environ.get("_drift_trace_id", "")
    span_id = environ.get("_drift_span_id", "")

    if not all([start_time_ns, trace_id, span_id]):
        return

    end_time_ns = time.time_ns()
    duration_ns = end_time_ns - start_time_ns

    # Build target (path + query string) to match Node SDK
    path = environ.get("PATH_INFO", "")
    query_string = environ.get("QUERY_STRING", "")
    target = f"{path}?{query_string}" if query_string else path

    # Get HTTP version from SERVER_PROTOCOL (e.g., "HTTP/1.1" -> "1.1")
    server_protocol = environ.get("SERVER_PROTOCOL", "HTTP/1.1")
    http_version = (
        server_protocol.replace("HTTP/", "")
        if server_protocol.startswith("HTTP/")
        else "1.1"
    )

    input_value = {
        "method": environ.get("REQUEST_METHOD", ""),
        "url": _build_url(environ),
        "target": target,  # Path + query string combined, matches Node SDK
        "headers": _extract_headers(environ),
        "httpVersion": http_version,
        "remoteAddress": environ.get("REMOTE_ADDR"),
        "remotePort": int(environ["REMOTE_PORT"])
        if environ.get("REMOTE_PORT")
        else None,
    }
    # Remove None values to keep span clean
    input_value = {k: v for k, v in input_value.items() if v is not None}

    request_body = environ.get("_drift_request_body")
    if request_body:
        # Store body as Base64 encoded string to match Node SDK behavior
        input_value["body"] = base64.b64encode(request_body).decode("ascii")
        input_value["bodySize"] = len(request_body)

    output_value: dict[str, Any] = {
        "statusCode": response_data.get(
            "status_code", 200
        ),  # camelCase to match Node SDK
        "statusMessage": response_data.get("status_message", ""),
        "headers": response_data.get("headers", {}),
    }

    # Add response body if captured
    response_body = response_data.get("body")
    if response_body:
        output_value["body"] = base64.b64encode(response_body).decode("ascii")
        output_value["bodySize"] = response_data.get("body_size", len(response_body))
        if response_data.get("body_truncated"):
            output_value["bodyProcessingError"] = "truncated"

    if "error" in response_data:
        output_value["errorMessage"] = response_data[
            "error"
        ]  # Match Node SDK field name

    transform_metadata = None
    if transform_engine:
        span_data = HttpSpanData(
            kind=SpanKind.SERVER,
            input_value=input_value,
            output_value=output_value,
        )
        transform_engine.apply_transforms(span_data)
        transform_metadata = span_data.transform_metadata
        input_value = span_data.input_value or input_value
        output_value = span_data.output_value or output_value

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

    method = environ.get("REQUEST_METHOD", "")
    span_name = f"{method} {path}"

    span = CleanSpanData(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id="",
        name=span_name,
        package_name="flask",
        instrumentation_name="FlaskInstrumentation",
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
    )

    sdk.collect_span(span)


def _build_url(environ: WSGIEnvironment) -> str:
    """Build full URL from WSGI environ"""
    scheme = environ.get("wsgi.url_scheme", "http")
    host = environ.get("HTTP_HOST") or environ.get("SERVER_NAME", "localhost")
    path = environ.get("PATH_INFO", "")
    query = environ.get("QUERY_STRING", "")

    url = f"{scheme}://{host}{path}"
    if query:
        url += f"?{query}"
    return url


def _extract_headers(environ: WSGIEnvironment) -> dict[str, str]:
    """Extract HTTP headers from WSGI environ"""
    headers = {}
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            header_name = key[5:].replace("_", "-").title()
            headers[header_name] = str(value)
    return headers
