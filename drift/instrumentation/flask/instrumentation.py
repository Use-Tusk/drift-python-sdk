from __future__ import annotations

import logging
import time
import uuid
from types import ModuleType
from typing import TYPE_CHECKING, Any, override

logger = logging.getLogger(__name__)

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
    current_span_id_context,
    current_trace_id_context,
    replay_trace_id_context,
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
    """Handle a single Flask request by capturing request/response data"""
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

    start_time_ns = time.time_ns()

    # Use replay trace ID if in REPLAY mode, otherwise generate new IDs
    if replay_trace_id:
        trace_id = replay_trace_id
    else:
        trace_id = str(uuid.uuid4()).replace("-", "")

    span_id = str(uuid.uuid4()).replace("-", "")[:16]

    # Set trace context for child spans (e.g., outbound HTTP calls)
    trace_token = current_trace_id_context.set(trace_id)
    span_token = current_span_id_context.set(span_id)

    response_data: dict[str, Any] = {}  # pyright: ignore[reportExplicitAny]

    # Capture request body using WSGI utilities
    request_body, body_truncated = capture_request_body(environ)
    environ["_drift_request_body"] = request_body
    environ["_drift_request_body_truncated"] = body_truncated

    # Check if request should be dropped
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "")
    query_string = environ.get("QUERY_STRING", "")
    target = f"{path}?{query_string}" if query_string else path
    request_headers = extract_headers(environ)

    if transform_engine and transform_engine.should_drop_inbound_request(
        method, target, request_headers
    ):
        # Reset trace context before early return (prevents context leak)
        current_trace_id_context.reset(trace_token)
        current_span_id_context.reset(span_token)
        return original_wsgi_app(app, environ, start_response)

    def wrapped_start_response(
        status: str,
        response_headers: list[tuple[str, str]],
        exc_info: OptExcInfo | None = None,
    ):
        # Parse status using WSGI utilities
        status_code, status_message = parse_status_line(status)
        response_data["status_code"] = status_code
        response_data["status_message"] = status_message
        response_data["headers"] = dict(response_headers)
        return start_response(status, response_headers, exc_info)

    # Store metadata in environ for span creation
    environ["_drift_start_time_ns"] = start_time_ns
    environ["_drift_trace_id"] = trace_id
    environ["_drift_span_id"] = span_id
    environ["_drift_trace_token"] = trace_token
    environ["_drift_span_token"] = span_token

    # Store replay token in environ for cleanup
    environ["_drift_replay_token"] = replay_token

    # Create callback for response completion
    def on_response_complete(env: WSGIEnvironment, resp_data: dict[str, Any]) -> None:
        """Callback when response is complete - creates span"""
        _capture_span(env, resp_data, transform_engine)
        # Reset trace context after span is captured
        r_token = env.get("_drift_replay_token")
        t_token = env.get("_drift_trace_token")
        s_token = env.get("_drift_span_token")
        if r_token:
            replay_trace_id_context.reset(r_token)
        if t_token:
            current_trace_id_context.reset(t_token)
        if s_token:
            current_span_id_context.reset(s_token)

    try:
        response = original_wsgi_app(app, environ, wrapped_start_response)
        # Wrap response to capture body and defer span creation
        return ResponseBodyCapture(
            response, environ, response_data, on_response_complete
        )
    except Exception as e:
        response_data["status_code"] = 500
        response_data["error"] = str(e)
        _capture_span(environ, response_data, transform_engine)
        # Reset trace context on error
        if replay_token:
            replay_trace_id_context.reset(replay_token)
        current_trace_id_context.reset(trace_token)
        current_span_id_context.reset(span_token)
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

    # Build input_value using WSGI utilities
    request_body = environ.get("_drift_request_body")
    request_body_truncated = environ.get("_drift_request_body_truncated", False)
    input_value = build_input_value(environ, request_body, request_body_truncated)

    # Build output_value using WSGI utilities
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
        blocking_mgr = TraceBlockingManager.get_instance()
        blocking_mgr.block_trace(
            trace_id,
            reason=f"binary_content:{decoded_type.name if decoded_type else 'unknown'}",
        )
        logger.warning(
            f"Blocking trace {trace_id} - binary response: {content_type} "
            f"(decoded as {decoded_type.name if decoded_type else 'unknown'})"
        )
        return  # Skip span creation

    # Apply transforms if present
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

    # Generate schemas using WSGI utilities
    input_schema_info = generate_input_schema_info(input_value, request_body_truncated)
    output_schema_info = generate_output_schema_info(
        output_value, response_body_truncated, decoded_type
    )

    sdk = TuskDrift.get_instance()
    # Derive timestamp from start_time_ns (not datetime.now() which would be end time)
    timestamp_seconds = start_time_ns // 1_000_000_000
    timestamp_nanos = start_time_ns % 1_000_000_000
    duration_seconds = duration_ns // 1_000_000_000
    duration_nanos = duration_ns % 1_000_000_000

    if status_code >= 400:
        status = SpanStatus(code=StatusCode.ERROR, message=f"HTTP {status_code}")
    else:
        status = SpanStatus(code=StatusCode.OK, message="")

    # Flask-specific: use literal path for span name
    method = environ.get("REQUEST_METHOD", "")
    path = environ.get("PATH_INFO", "")
    span_name = f"{method} {path}"

    # Attach env vars to metadata if present
    from ...core.types import MetadataObject
    from ..env import EnvVarTracker

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
        metadata=metadata,
    )

    sdk.collect_span(span)

    # Clear tracker after span collection
    tracker.clear_env_vars(trace_id)
