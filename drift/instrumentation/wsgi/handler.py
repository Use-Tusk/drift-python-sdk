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
from opentelemetry.trace import Status, set_span_in_context
from opentelemetry.trace import StatusCode as OTelStatusCode

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from _typeshed import OptExcInfo
    from _typeshed.wsgi import StartResponse, WSGIApplication, WSGIEnvironment
    from opentelemetry.trace import Span


from ...core.tracing import TdSpanAttributes
from ...core.types import (
    PackageType,
    SpanKind,
    replay_trace_id_context,
    span_kind_context,
    TuskDriftMode,
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
    original_wsgi_app: WSGIApplication,
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

    # Check if we're in REPLAY mode and handle trace ID extraction
    replay_trace_id = None
    replay_token = None
    if sdk.mode == TuskDriftMode.REPLAY:
        # Extract trace ID from headers (case-insensitive lookup)
        request_headers = extract_headers(environ)
        headers_lower = {k.lower(): v for k, v in request_headers.items()}
        replay_trace_id = headers_lower.get("x-td-trace-id")

        if not replay_trace_id:
            # No trace context in REPLAY mode; proceed without span
            return original_wsgi_app(app, environ, start_response)

        # Set replay trace context
        replay_token = replay_trace_id_context.set(replay_trace_id)

    # Extract request info for span name and drop check
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "")
    query_string = environ.get("QUERY_STRING", "")
    target = f"{path}?{query_string}" if query_string else path

    # Capture request body
    request_body = capture_request_body(environ)
    environ["_drift_request_body"] = request_body

    # Check if request should be dropped
    request_headers = extract_headers(environ)
    if transform_engine and transform_engine.should_drop_inbound_request(method, target, request_headers):
        if replay_token:
            replay_trace_id_context.reset(replay_token)
        return original_wsgi_app(app, environ, start_response)

    # Get tracer and prepare to start span
    tracer = sdk.get_tracer()
    span_name = f"{method} {path}"

    # Build input value before starting span
    input_value = build_input_value(environ, request_body)

    # Store start time for duration calculation
    start_time_ns = time.time_ns()

    # Start the span and make it current
    # Note: We manually manage context because span needs to stay open across WSGI response iterator
    span = tracer.start_span(
        name=span_name,
        kind=OTelSpanKind.SERVER,
        attributes={
            TdSpanAttributes.NAME: span_name,
            TdSpanAttributes.PACKAGE_NAME: framework_name,
            TdSpanAttributes.INSTRUMENTATION_NAME: instrumentation_name,
            TdSpanAttributes.SUBMODULE_NAME: method,
            TdSpanAttributes.PACKAGE_TYPE: PackageType.HTTP.name,
            TdSpanAttributes.IS_PRE_APP_START: not sdk.app_ready,
            TdSpanAttributes.IS_ROOT_SPAN: True,
            TdSpanAttributes.INPUT_VALUE: json.dumps(input_value),
        },
    )

    # Make span active by attaching to context (manual management)
    ctx = otel_context.get_current()
    ctx_with_span = set_span_in_context(span, ctx)
    token = otel_context.attach(ctx_with_span)

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
