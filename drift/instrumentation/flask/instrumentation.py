from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Iterable
from datetime import datetime
from functools import wraps
from types import ModuleType
from typing import TYPE_CHECKING, Any, override

if TYPE_CHECKING:
    from _typeshed import OptExcInfo
    from _typeshed.wsgi import StartResponse, WSGIApplication, WSGIEnvironment

from ...core.drift_sdk import TuskDrift
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


class FlaskInstrumentation(InstrumentationBase):
    def __init__(self, enabled: bool = True):
        super().__init__(
            name="FlaskInstrumentation",
            module_name="flask",
            supported_versions=">=2.0.0",
            enabled=enabled,
        )

    @override
    def patch(self, module: ModuleType) -> None:
        """Patch Flask to capture HTTP requests/responses"""
        flask_class = getattr(module, "Flask", None)
        if not flask_class:
            print("Warning: Flask.Flask class not found")
            return

        original_wsgi_app = flask_class.wsgi_app

        @wraps(original_wsgi_app)
        def instrumented_wsgi_app(
            self, environ: WSGIEnvironment, start_response: StartResponse
        ) -> Iterable[bytes]:
            return _handle_request(self, environ, start_response, original_wsgi_app)

        flask_class.wsgi_app = instrumented_wsgi_app  # type: ignore
        print("Flask instrumentation applied")


def _handle_request(
    app: Any,
    environ: WSGIEnvironment,
    start_response: StartResponse,
    original_wsgi_app: WSGIApplication,
) -> Iterable[bytes]:
    """Handle a single Flask request by capturing request/response data"""
    start_time_ns = time.time_ns()
    trace_id = str(uuid.uuid4()).replace("-", "")
    span_id = str(uuid.uuid4()).replace("-", "")[:16]
    response_data: dict[str, Any] = {}

    request_body = None
    if environ.get("REQUEST_METHOD") in ("POST", "PUT", "PATCH"):
        try:
            content_length = int(environ.get("CONTENT_LENGTH", 0))
            if content_length > 0 and content_length <= 10000:  # 10KB limit
                wsgi_input = environ.get("wsgi.input")
                if wsgi_input:
                    request_body = wsgi_input.read(content_length)
                    # Create a new BytesIO so Flask can read it
                    from io import BytesIO

                    environ["wsgi.input"] = BytesIO(request_body)
        except Exception:
            pass

    environ["_drift_request_body"] = request_body

    def wrapped_start_response(
        status: str,
        response_headers: list[tuple[str, str]],
        exc_info: OptExcInfo | None = None,
    ):
        status_code = int(status.split()[0])
        response_data["status_code"] = status_code
        response_data["headers"] = dict(response_headers)
        return start_response(status, response_headers, exc_info)

    environ["_drift_start_time_ns"] = start_time_ns
    environ["_drift_trace_id"] = trace_id
    environ["_drift_span_id"] = span_id

    try:
        response = original_wsgi_app(app, environ, wrapped_start_response)
        _capture_span(environ, response_data)
        return response
    except Exception as e:
        response_data["status_code"] = 500
        response_data["error"] = str(e)
        _capture_span(environ, response_data)
        raise


def _capture_span(environ: WSGIEnvironment, response_data: dict[str, Any]) -> None:
    """Create and collect a span from request/response data"""
    start_time_ns = environ.get("_drift_start_time_ns", 0)
    trace_id = environ.get("_drift_trace_id", "")
    span_id = environ.get("_drift_span_id", "")

    if not all([start_time_ns, trace_id, span_id]):
        return

    end_time_ns = time.time_ns()
    duration_ns = end_time_ns - start_time_ns

    input_value = {
        "method": environ.get("REQUEST_METHOD", ""),
        "path": environ.get("PATH_INFO", ""),
        "url": _build_url(environ),
        "query": environ.get("QUERY_STRING", ""),
        "headers": _extract_headers(environ),
    }

    request_body = environ.get("_drift_request_body")
    if request_body:
        try:
            input_value["body"] = json.loads(request_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            input_value["body"] = request_body.decode("utf-8", errors="replace")

    output_value = {
        "status_code": response_data.get("status_code", 200),
        "headers": response_data.get("headers", {}),
    }

    if "error" in response_data:
        output_value["error"] = response_data["error"]

    sdk = TuskDrift.get_instance()
    timestamp_dt = datetime.now()
    timestamp_seconds = int(timestamp_dt.timestamp())
    timestamp_nanos = int((timestamp_dt.timestamp() % 1) * 1_000_000_000)
    duration_seconds = duration_ns // 1_000_000_000
    duration_nanos = duration_ns % 1_000_000_000

    status_code = response_data.get("status_code", 200)
    if status_code >= 400:
        status = SpanStatus(code=StatusCode.ERROR, message=f"HTTP {status_code}")
    else:
        status = SpanStatus(code=StatusCode.OK, message="")

    input_hash = hashlib.sha256(json.dumps(input_value, sort_keys=True).encode()).hexdigest()[:16]
    output_hash = hashlib.sha256(json.dumps(output_value, sort_keys=True).encode()).hexdigest()[:16]

    method = environ.get("REQUEST_METHOD", "")
    path = environ.get("PATH_INFO", "")
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
        input_value_hash=input_hash,
        output_value_hash=output_hash,
        status=status,
        is_pre_app_start=not sdk.app_ready,
        is_root_span=True,
        timestamp=Timestamp(seconds=timestamp_seconds, nanos=timestamp_nanos),
        duration=Duration(seconds=duration_seconds, nanos=duration_nanos),
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
