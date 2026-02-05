"""Generic WSGI request/response handler for HTTP tracing.

This module provides framework-agnostic WSGI instrumentation logic that can be
reused by different WSGI frameworks (Flask, Bottle, Pyramid, etc.).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from opentelemetry import context as otel_context
from opentelemetry.trace import SpanKind as OTelSpanKind
from opentelemetry.trace import Status
from opentelemetry.trace import StatusCode as OTelStatusCode

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable

    from _typeshed import OptExcInfo
    from _typeshed.wsgi import StartResponse, WSGIApplication, WSGIEnvironment
    from opentelemetry.trace import Span

    # Type for unbound WSGI method like Flask.wsgi_app that takes (self, environ, start_response)
    WsgiAppMethod = Callable[[WSGIApplication, WSGIEnvironment, StartResponse], "Iterable[bytes]"]


from ...core.mode_utils import handle_record_mode, should_record_inbound_http_request
from ...core.tracing import TdSpanAttributes
from ...core.tracing.span_utils import CreateSpanOptions, SpanUtils
from ...core.types import (
    PackageType,
    SpanKind,
    TuskDriftMode,
    replay_trace_id_context,
    span_kind_context,
)
from ..http import HttpSpanData, HttpTransformEngine
from .response_capture import ResponseBodyCapture
from .utilities import (
    build_input_schema_merges,
    build_input_value,
    build_output_schema_merges,
    build_output_value,
    capture_request_body,
    extract_headers,
    parse_status_line,
)


def handle_wsgi_request(
    app: WSGIApplication,
    environ: WSGIEnvironment,
    start_response: StartResponse,
    original_wsgi_app: WsgiAppMethod,
    framework_name: str = "wsgi",
    instrumentation_name: str | None = None,
    transform_engine: HttpTransformEngine | None = None,
) -> Iterable[bytes]:
    """Handle a single WSGI request with OpenTelemetry tracing.

    Args:
        app: The WSGI application instance
        environ: WSGI environ dictionary
        start_response: WSGI start_response callable
        original_wsgi_app: Original unwrapped WSGI application
        framework_name: Name of framework for span attribution (default: "wsgi")
        instrumentation_name: Name of instrumentation (default: auto-generated from framework_name)
        transform_engine: Optional HTTP transform engine

    Returns:
        WSGI response iterable
    """
    from ...core.drift_sdk import TuskDrift

    sdk = TuskDrift.get_instance()

    # Auto-generate instrumentation name if not provided
    if instrumentation_name is None:
        instrumentation_name = f"{framework_name.title()}Instrumentation"

    # Define passthrough function
    def original_call() -> Iterable[bytes]:
        return original_wsgi_app(app, environ, start_response)

    # DISABLED mode: pass through
    if sdk.mode == TuskDriftMode.DISABLED:
        return original_call()

    # REPLAY mode: requires trace ID header
    if sdk.mode == TuskDriftMode.REPLAY:
        return _handle_replay_request(
            app, environ, start_response, original_wsgi_app, framework_name, instrumentation_name, transform_engine, sdk
        )

    # RECORD mode: use mode_utils for decision logic
    # Note: For SERVER spans, handle_record_mode will determine is_pre_app_start
    # and whether to skip recording (e.g., if nested in a pre-app-start span)
    return handle_record_mode(
        original_function_call=original_call,
        record_mode_handler=lambda is_pre_app_start: _handle_record_request(
            app,
            environ,
            start_response,
            original_wsgi_app,
            framework_name,
            instrumentation_name,
            transform_engine,
            sdk,
            is_pre_app_start,
        ),
        span_kind=OTelSpanKind.SERVER,
    )


def _handle_replay_request(
    app: WSGIApplication,
    environ: WSGIEnvironment,
    start_response: StartResponse,
    original_wsgi_app: WsgiAppMethod,
    framework_name: str,
    instrumentation_name: str,
    transform_engine: HttpTransformEngine | None,
    sdk: Any,
) -> Iterable[bytes]:
    """Handle REPLAY mode request.

    In REPLAY mode, we require a trace ID header to proceed with instrumentation.
    If no trace ID is present, we pass through to the original app.
    """
    # Extract trace ID from headers (case-insensitive lookup)
    request_headers = extract_headers(environ)
    headers_lower = {k.lower(): v for k, v in request_headers.items()}
    replay_trace_id = headers_lower.get("x-td-trace-id")

    if not replay_trace_id:
        # No trace context in REPLAY mode; proceed without span
        return original_wsgi_app(app, environ, start_response)

    # Start time travel at inbound request start when provided by CLI.
    # This prevents time drift before the first outbound span is matched (critical for cache keys).
    root_timestamp = headers_lower.get("x-td-root-timestamp")
    if root_timestamp:
        try:
            from drift.instrumentation.datetime.instrumentation import start_time_travel

            start_time_travel(root_timestamp, trace_id=replay_trace_id)
        except Exception as e:
            logger.debug(f"[WSGI] Failed to start time travel from x-td-root-timestamp: {e}")

    # Set replay trace context
    replay_token = replay_trace_id_context.set(replay_trace_id)

    # Continue with request handling (similar to RECORD but with replay context)
    return _create_and_handle_request(
        app,
        environ,
        start_response,
        original_wsgi_app,
        framework_name,
        instrumentation_name,
        transform_engine,
        sdk,
        is_pre_app_start=not sdk.app_ready,
        replay_token=replay_token,
    )


def _handle_record_request(
    app: WSGIApplication,
    environ: WSGIEnvironment,
    start_response: StartResponse,
    original_wsgi_app: WsgiAppMethod,
    framework_name: str,
    instrumentation_name: str,
    transform_engine: HttpTransformEngine | None,
    sdk: Any,
    is_pre_app_start: bool,
) -> Iterable[bytes]:
    """Handle RECORD mode request.

    The is_pre_app_start flag is determined by handle_record_mode based on
    app readiness and current span context.
    """
    return _create_and_handle_request(
        app,
        environ,
        start_response,
        original_wsgi_app,
        framework_name,
        instrumentation_name,
        transform_engine,
        sdk,
        is_pre_app_start=is_pre_app_start,
        replay_token=None,
    )


def _create_and_handle_request(
    app: WSGIApplication,
    environ: WSGIEnvironment,
    start_response: StartResponse,
    original_wsgi_app: WsgiAppMethod,
    framework_name: str,
    instrumentation_name: str,
    transform_engine: HttpTransformEngine | None,
    sdk: Any,
    is_pre_app_start: bool,
    replay_token: Any | None,
) -> Iterable[bytes]:
    """Create span and handle request with proper context management.

    This is the common path for both RECORD and REPLAY modes.
    We manually manage context because the span needs to stay open
    across the WSGI response iterator.
    """
    # Pre-flight check: drop transforms and sampling
    # NOTE: This is done before body capture to avoid unnecessary I/O
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "")
    query_string = environ.get("QUERY_STRING", "")
    target = f"{path}?{query_string}" if query_string else path
    request_headers = extract_headers(environ)

    if replay_token is None:
        should_record, skip_reason = should_record_inbound_http_request(
            method, target, request_headers, transform_engine, is_pre_app_start
        )
        if not should_record:
            logger.debug(f"[WSGI] Skipping request ({skip_reason}), path={path}")
            return original_wsgi_app(app, environ, start_response)

    # Capture request body
    request_body = capture_request_body(environ)
    environ["_drift_request_body"] = request_body

    span_name = f"{method} {path}"

    # Build input value before starting span
    input_value = build_input_value(environ, request_body)

    # Store start time for duration calculation
    start_time_ns = time.time_ns()

    # Create span using SpanUtils
    span_info = SpanUtils.create_span(
        CreateSpanOptions(
            name=span_name,
            kind=OTelSpanKind.SERVER,
            attributes={
                TdSpanAttributes.NAME: span_name,
                TdSpanAttributes.PACKAGE_NAME: framework_name,
                TdSpanAttributes.INSTRUMENTATION_NAME: instrumentation_name,
                TdSpanAttributes.SUBMODULE_NAME: method,
                TdSpanAttributes.PACKAGE_TYPE: PackageType.HTTP.name,
                TdSpanAttributes.IS_PRE_APP_START: is_pre_app_start,
                TdSpanAttributes.IS_ROOT_SPAN: True,
                TdSpanAttributes.INPUT_VALUE: json.dumps(input_value),
            },
            is_pre_app_start=is_pre_app_start,
        )
    )

    if not span_info:
        # Span creation failed (e.g., trace blocked), proceed without instrumentation
        if replay_token:
            replay_trace_id_context.reset(replay_token)
        return original_wsgi_app(app, environ, start_response)

    span = span_info.span

    # Manually attach context (required for WSGI streaming pattern)
    token = otel_context.attach(span_info.context)

    # Set span_kind_context for child spans and socket instrumentation to detect SERVER context
    span_kind_token = span_kind_context.set(SpanKind.SERVER)

    response_data: dict[str, Any] = {}

    def wrapped_start_response(
        status: str,
        response_headers: list[tuple[str, str]],
        exc_info: OptExcInfo | None = None,
    ):
        # Parse status
        status_code, status_message = parse_status_line(status)
        response_data["status_code"] = status_code
        response_data["status_message"] = status_message
        response_data["headers"] = dict(response_headers)
        return start_response(status, response_headers, exc_info)

    # Store span and context info in environ for response completion
    environ["_drift_span"] = span
    environ["_drift_context_token"] = token
    environ["_drift_replay_token"] = replay_token
    environ["_drift_span_kind_token"] = span_kind_token
    environ["_drift_start_time_ns"] = start_time_ns

    def on_response_complete(env: WSGIEnvironment, resp_data: dict[str, Any]) -> None:
        """Callback when response is complete - finalize and end span"""
        finalize_wsgi_span(env, resp_data, transform_engine)

    try:
        response = original_wsgi_app(app, environ, wrapped_start_response)
        # Wrap response to capture body and defer span finalization
        return ResponseBodyCapture(response, environ, response_data, on_response_complete)
    except Exception as e:
        response_data["status_code"] = 500
        response_data["error"] = str(e)
        finalize_wsgi_span(environ, response_data, transform_engine)
        raise


def finalize_wsgi_span(
    environ: WSGIEnvironment,
    response_data: dict[str, Any],
    transform_engine: HttpTransformEngine | None,
) -> None:
    """Finalize WSGI span with response data and end it.

    Args:
        environ: WSGI environ dictionary (contains span and context info)
        response_data: Response data dictionary (status, headers, body, etc.)
        transform_engine: Optional HTTP transform engine
    """
    span: Span | None = environ.get("_drift_span")
    token = environ.get("_drift_context_token")
    replay_token = environ.get("_drift_replay_token")
    span_kind_token = environ.get("_drift_span_kind_token")
    start_time_ns = environ.get("_drift_start_time_ns", 0)

    if not span:
        return

    try:
        # Calculate duration
        end_time_ns = time.time_ns()
        (end_time_ns - start_time_ns) / 1_000_000

        # Build output_value
        status_code = response_data.get("status_code", 200)
        status_message = response_data.get("status_message", "")
        response_headers = response_data.get("headers", {})
        response_body = response_data.get("body")
        error = response_data.get("error")

        output_value = build_output_value(
            status_code,
            status_message,
            response_headers,
            response_body,
            error,
        )

        # Check if content type should block the trace
        from ...core.content_type_utils import get_decoded_type, should_block_content_type
        from ...core.trace_blocking_manager import TraceBlockingManager

        content_type = response_headers.get("content-type") or response_headers.get("Content-Type")
        decoded_type = get_decoded_type(content_type)

        if should_block_content_type(decoded_type):
            # Get trace ID from span
            span_context = span.get_span_context()
            trace_id = format(span_context.trace_id, "032x")

            blocking_mgr = TraceBlockingManager.get_instance()
            blocking_mgr.block_trace(
                trace_id,
                reason=f"binary_content:{decoded_type.name if decoded_type else 'unknown'}",
            )
            logger.warning(
                f"Blocking trace {trace_id} - binary response: {content_type} "
                f"(decoded as {decoded_type.name if decoded_type else 'unknown'})"
            )
            # End span but don't export (trace is blocked)
            span.set_status(Status(OTelStatusCode.ERROR, "Binary content blocked"))
            span.end()
            return

        # Apply transforms if present
        input_value_dict = json.loads(span.attributes.get(TdSpanAttributes.INPUT_VALUE, "{}"))

        transform_metadata = None
        if transform_engine:
            span_data = HttpSpanData(
                kind=SpanKind.SERVER,
                input_value=input_value_dict,
                output_value=output_value,
            )
            transform_engine.apply_transforms(span_data)
            transform_metadata = span_data.transform_metadata
            input_value_dict = span_data.input_value or input_value_dict
            output_value = span_data.output_value or output_value

            # Update input value if transforms modified it
            span.set_attribute(TdSpanAttributes.INPUT_VALUE, json.dumps(input_value_dict))

        # Set output value
        span.set_attribute(TdSpanAttributes.OUTPUT_VALUE, json.dumps(output_value))

        # Build and set schema merge hints (schemas will be generated at export time)
        input_schema_merges = build_input_schema_merges(input_value_dict)
        output_schema_merges = build_output_schema_merges(output_value)

        span.set_attribute(TdSpanAttributes.INPUT_SCHEMA_MERGES, json.dumps(input_schema_merges))
        span.set_attribute(TdSpanAttributes.OUTPUT_SCHEMA_MERGES, json.dumps(output_schema_merges))

        # Set transform metadata if present
        if transform_metadata:
            span.set_attribute(TdSpanAttributes.TRANSFORM_METADATA, json.dumps(transform_metadata))

        # Set status based on HTTP status code
        if status_code >= 400:
            span.set_status(Status(OTelStatusCode.ERROR, f"HTTP {status_code}"))
        else:
            span.set_status(Status(OTelStatusCode.OK))

    except Exception as e:
        logger.error(f"Error finalizing WSGI span: {e}", exc_info=True)
        span.set_status(Status(OTelStatusCode.ERROR, str(e)))
    finally:
        # End the span - this triggers TdSpanProcessor to convert and export
        span.end()

        # Detach context
        if token:
            otel_context.detach(token)

        # Reset span kind context
        if span_kind_token:
            span_kind_context.reset(span_kind_token)

        # Reset replay context
        if replay_token:
            replay_trace_id_context.reset(replay_token)
