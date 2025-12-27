from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from types import ModuleType
from typing import TYPE_CHECKING, Any, override

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import SpanKind as OTelSpanKind
from opentelemetry.trace import Status, StatusCode as OTelStatusCode, set_span_in_context

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from _typeshed import OptExcInfo
    from _typeshed.wsgi import StartResponse, WSGIApplication, WSGIEnvironment
    from opentelemetry.trace import Span

from ...core.drift_sdk import TuskDrift
from ...core.tracing import TdSpanAttributes
from ...core.types import (
    PackageType,
    replay_trace_id_context,
    SpanKind,
)
from ..base import InstrumentationBase
from ..http import HttpSpanData, HttpTransformEngine
from ..wsgi import (
    ResponseBodyCapture,
    build_input_value,
    build_output_value,
    capture_request_body,
    extract_headers,
    generate_input_schema_info,
    generate_output_schema_info,
    parse_status_line,
)


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
            logger.warning("Flask.Flask class not found")
            return

        original_wsgi_app: WSGIApplication = flask_class.wsgi_app  # pyright: ignore[reportAny]
        transform_engine = self._transform_engine

        # wraps(original) = functools.update_wrapper(instrumented, original)
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


def _handle_request(
    app: WSGIApplication,
    environ: WSGIEnvironment,
    start_response: StartResponse,
    original_wsgi_app: WSGIApplication,
    transform_engine: HttpTransformEngine | None,
) -> Iterable[bytes]:
    """Handle a single Flask request using OpenTelemetry spans"""
    sdk = TuskDrift.get_instance()

    # Check if we're in REPLAY mode and handle trace ID extraction
    replay_trace_id = None
    replay_token = None
    if sdk.mode == "REPLAY":
        # Extract trace ID from headers (case-insensitive lookup)
        request_headers = extract_headers(environ)
        headers_lower = {k.lower(): v for k, v in request_headers.items()}
        replay_trace_id = headers_lower.get("x-td-trace-id")

        if not replay_trace_id:
            # No trace context in REPLAY mode; proceed without span
            return original_wsgi_app(app, environ, start_response)

        # Fetch env vars from CLI if requested
        should_fetch_env_vars = headers_lower.get("x-td-fetch-env-vars") == "true"
        if should_fetch_env_vars:
            try:
                env_vars = sdk.request_env_vars_sync(replay_trace_id)

                # Store in tracker for env instrumentation to use
                from ..env import EnvVarTracker

                tracker = EnvVarTracker.get_instance()
                tracker.set_env_vars(replay_trace_id, env_vars)

                logger.debug(
                    f"[FlaskInstrumentation] Fetched {len(env_vars)} env vars from CLI for trace {replay_trace_id}"
                )
            except Exception as e:
                logger.error(
                    f"[FlaskInstrumentation] Failed to fetch env vars from CLI: {e}"
                )

        # Set replay trace context
        replay_token = replay_trace_id_context.set(replay_trace_id)

    # Extract request info for span name and drop check
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "")
    query_string = environ.get("QUERY_STRING", "")
    target = f"{path}?{query_string}" if query_string else path

    # Capture request body
    request_body, body_truncated = capture_request_body(environ)
    environ["_drift_request_body"] = request_body
    environ["_drift_request_body_truncated"] = body_truncated

    # Check if request should be dropped
    request_headers = extract_headers(environ)
    if transform_engine and transform_engine.should_drop_inbound_request(
        method, target, request_headers
    ):
        if replay_token:
            replay_trace_id_context.reset(replay_token)
        return original_wsgi_app(app, environ, start_response)

    # Get tracer and prepare to start span
    tracer = sdk.get_tracer()
    span_name = f"{method} {path}"

    # Build input value before starting span
    input_value = build_input_value(environ, request_body, body_truncated)

    # Store start time for duration calculation
    start_time_ns = time.time_ns()

    # Start the span and make it current
    # Note: We manually manage context because span needs to stay open across WSGI response iterator
    span = tracer.start_span(
        name=span_name,
        kind=OTelSpanKind.SERVER,
        attributes={
            TdSpanAttributes.NAME: span_name,
            TdSpanAttributes.PACKAGE_NAME: "flask",
            TdSpanAttributes.INSTRUMENTATION_NAME: "FlaskInstrumentation",
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
    environ["_drift_start_time_ns"] = start_time_ns

    def on_response_complete(env: WSGIEnvironment, resp_data: dict[str, Any]) -> None:
        """Callback when response is complete - finalize and end span"""
        _finalize_span(env, resp_data, transform_engine)

    try:
        response = original_wsgi_app(app, environ, wrapped_start_response)
        # Wrap response to capture body and defer span finalization
        return ResponseBodyCapture(
            response, environ, response_data, on_response_complete
        )
    except Exception as e:
        response_data["status_code"] = 500
        response_data["error"] = str(e)
        _finalize_span(environ, response_data, transform_engine)
        raise


def _finalize_span(
    environ: WSGIEnvironment,
    response_data: dict[str, Any],
    transform_engine: HttpTransformEngine | None,
) -> None:
    """Finalize span with response data and end it"""
    span: Span | None = environ.get("_drift_span")
    token = environ.get("_drift_context_token")
    replay_token = environ.get("_drift_replay_token")
    start_time_ns = environ.get("_drift_start_time_ns", 0)

    if not span:
        return

    try:
        # Calculate duration
        end_time_ns = time.time_ns()
        duration_ms = (end_time_ns - start_time_ns) / 1_000_000

        # Build output_value
        status_code = response_data.get("status_code", 200)
        status_message = response_data.get("status_message", "")
        response_headers = response_data.get("headers", {})
        response_body = response_data.get("body")
        response_body_truncated = response_data.get("body_truncated", False)
        error = response_data.get("error")

        output_value = build_output_value(
            status_code,
            status_message,
            response_headers,
            response_body,
            response_body_truncated,
            error,
        )

        # Check if content type should block the trace
        from ...core.content_type_utils import get_decoded_type, should_block_content_type
        from ...core.trace_blocking_manager import TraceBlockingManager

        content_type = response_headers.get("content-type") or response_headers.get(
            "Content-Type"
        )
        decoded_type = get_decoded_type(content_type)

        if should_block_content_type(decoded_type):
            # Get trace ID from span
            span_context = span.get_span_context()
            trace_id = format(span_context.trace_id, '032x')

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
        request_body_truncated = environ.get("_drift_request_body_truncated", False)
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

        # Generate schemas and hashes
        input_schema_info = generate_input_schema_info(input_value_dict, request_body_truncated)
        output_schema_info = generate_output_schema_info(
            output_value, response_body_truncated, decoded_type
        )

        # Set schema attributes (convert JsonSchema to dict first)
        span.set_attribute(TdSpanAttributes.INPUT_SCHEMA, json.dumps(input_schema_info.schema.to_primitive()))
        span.set_attribute(TdSpanAttributes.OUTPUT_SCHEMA, json.dumps(output_schema_info.schema.to_primitive()))
        span.set_attribute(TdSpanAttributes.INPUT_SCHEMA_HASH, input_schema_info.decoded_schema_hash)
        span.set_attribute(TdSpanAttributes.OUTPUT_SCHEMA_HASH, output_schema_info.decoded_schema_hash)
        span.set_attribute(TdSpanAttributes.INPUT_VALUE_HASH, input_schema_info.decoded_value_hash)
        span.set_attribute(TdSpanAttributes.OUTPUT_VALUE_HASH, output_schema_info.decoded_value_hash)

        # Set transform metadata if present
        if transform_metadata:
            span.set_attribute(TdSpanAttributes.TRANSFORM_METADATA, json.dumps(transform_metadata))

        # Attach env vars to metadata if present
        from ...core.types import MetadataObject
        from ..env import EnvVarTracker

        tracker = EnvVarTracker.get_instance()
        span_context = span.get_span_context()
        trace_id = format(span_context.trace_id, '032x')
        env_vars = tracker.get_env_vars(trace_id)
        if env_vars:
            metadata = MetadataObject(ENV_VARS=env_vars)
            span.set_attribute(TdSpanAttributes.METADATA, json.dumps(metadata))
            # Clear tracker after setting
            tracker.clear_env_vars(trace_id)

        # Set status based on HTTP status code
        if status_code >= 400:
            span.set_status(Status(OTelStatusCode.ERROR, f"HTTP {status_code}"))
        else:
            span.set_status(Status(OTelStatusCode.OK))

    except Exception as e:
        logger.error(f"Error finalizing Flask span: {e}", exc_info=True)
        span.set_status(Status(OTelStatusCode.ERROR, str(e)))
    finally:
        # End the span - this triggers TdSpanProcessor to convert and export
        span.end()

        # Detach context
        if token:
            otel_context.detach(token)

        # Reset replay context
        if replay_token:
            replay_trace_id_context.reset(replay_token)
