"""Django middleware for Drift span capture."""

from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING, Any, Callable

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse
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
from ..http import HttpSpanData, HttpTransformEngine
from ..wsgi import (
    build_input_value,
    build_output_value,
    capture_request_body,
    generate_input_schema_info,
    generate_output_schema_info,
    parse_status_line,
)


class DriftMiddleware:
    """Django middleware for Drift span capture.

    This middleware captures HTTP request/response data and creates spans.
    Uses WSGI utilities for all data extraction and schema generation.

    Args:
        get_response: The next middleware or view in the Django chain
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]):
        self.get_response = get_response
        self.transform_engine: HttpTransformEngine | None = None

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Process the request and response.

        Args:
            request: Django HttpRequest object

        Returns:
            Django HttpResponse object
        """
        from ...core.drift_sdk import TuskDrift
        sdk = TuskDrift.get_instance()

        # Check if we're in REPLAY mode and handle trace ID extraction
        replay_trace_id = None
        replay_token = None
        if sdk.mode == "REPLAY":
            # Extract trace ID from headers (case-insensitive lookup)
            # Django stores headers in request.META
            headers_lower = {k.lower(): v for k, v in request.META.items() if k.startswith("HTTP_")}
            # Convert HTTP_X_TD_TRACE_ID -> x-td-trace-id
            replay_trace_id = headers_lower.get("http_x_td_trace_id")

            if not replay_trace_id:
                # No trace context in REPLAY mode; proceed without span
                return self.get_response(request)

            # Fetch env vars from CLI if requested
            should_fetch_env_vars = headers_lower.get("http_x_td_fetch_env_vars") == "true"
            if should_fetch_env_vars:
                try:
                    env_vars = sdk.request_env_vars_sync(replay_trace_id)

                    # Store in tracker for env instrumentation to use
                    from ..env import EnvVarTracker
                    tracker = EnvVarTracker.get_instance()
                    tracker.set_env_vars(replay_trace_id, env_vars)

                    logger.debug(
                        f"[DjangoMiddleware] Fetched {len(env_vars)} env vars from CLI for trace {replay_trace_id}"
                    )
                except Exception as e:
                    logger.error(f"[DjangoMiddleware] Failed to fetch env vars from CLI: {e}")

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

        # Capture request body
        # Django provides request.body which handles reading and caching
        request_body = None
        body_truncated = False
        if request.method in ("POST", "PUT", "PATCH"):
            try:
                # Django's request.body automatically reads and caches the body
                body = request.body
                if body:
                    if len(body) > 10000:
                        request_body = body[:10000]
                        body_truncated = True
                    else:
                        request_body = body
            except Exception:
                pass

        # Check if request should be dropped
        method = request.method
        path = request.path
        query_string = request.META.get("QUERY_STRING", "")
        target = f"{path}?{query_string}" if query_string else path

        # Extract headers from META
        from ..wsgi import extract_headers
        request_headers = extract_headers(request.META)

        if self.transform_engine and self.transform_engine.should_drop_inbound_request(
            method, target, request_headers
        ):
            # Reset trace context before early return
            if replay_token:
                replay_trace_id_context.reset(replay_token)
            current_trace_id_context.reset(trace_token)
            current_span_id_context.reset(span_token)
            return self.get_response(request)

        # Store metadata on request for later use
        request._drift_start_time_ns = start_time_ns  # type: ignore
        request._drift_trace_id = trace_id  # type: ignore
        request._drift_span_id = span_id  # type: ignore
        request._drift_trace_token = trace_token  # type: ignore
        request._drift_span_token = span_token  # type: ignore
        request._drift_replay_token = replay_token  # type: ignore
        request._drift_request_body = request_body  # type: ignore
        request._drift_request_body_truncated = body_truncated  # type: ignore
        request._drift_route_template = None  # Will be set in process_view  # type: ignore

        try:
            # Call next middleware or view
            response = self.get_response(request)

            # Capture span after response is complete
            self._capture_span(request, response)

            return response
        except Exception as e:
            # Capture error span
            self._capture_error_span(request, e)
            raise
        finally:
            # Reset trace context
            if replay_token:
                replay_trace_id_context.reset(replay_token)
            current_trace_id_context.reset(trace_token)
            current_span_id_context.reset(span_token)

    def process_view(
        self,
        request: HttpRequest,
        view_func: Callable,
        view_args: tuple,
        view_kwargs: dict,
    ) -> None:
        """Called just before Django calls the view.

        Capture the route template from the resolver match.

        Args:
            request: Django HttpRequest object
            view_func: The view function about to be called
            view_args: Positional arguments for the view
            view_kwargs: Keyword arguments for the view
        """
        # Extract route template from resolver_match
        if hasattr(request, "resolver_match") and request.resolver_match:
            route = request.resolver_match.route
            if route:
                request._drift_route_template = route  # type: ignore

    def _capture_span(self, request: HttpRequest, response: HttpResponse) -> None:
        """Create and collect a span from request/response data.

        Args:
            request: Django HttpRequest object
            response: Django HttpResponse object
        """
        start_time_ns = getattr(request, "_drift_start_time_ns", None)
        trace_id = getattr(request, "_drift_trace_id", None)
        span_id = getattr(request, "_drift_span_id", None)

        if not all([start_time_ns, trace_id, span_id]):
            return

        end_time_ns = time.time_ns()
        duration_ns = end_time_ns - start_time_ns

        # Build input_value using WSGI utilities
        request_body = getattr(request, "_drift_request_body", None)
        request_body_truncated = getattr(request, "_drift_request_body_truncated", False)
        input_value = build_input_value(request.META, request_body, request_body_truncated)

        # Build output_value using WSGI utilities
        status_code = response.status_code
        status_message = response.reason_phrase if hasattr(response, "reason_phrase") else ""

        # Convert response headers to dict
        response_headers = dict(response.items()) if hasattr(response, "items") else {}

        # Capture response body if available
        response_body = None
        response_body_truncated = False
        if hasattr(response, "content"):
            content = response.content
            if isinstance(content, bytes) and len(content) > 0:
                if len(content) > 10000:
                    response_body = content[:10000]
                    response_body_truncated = True
                else:
                    response_body = content

        output_value = build_output_value(
            status_code,
            status_message,
            response_headers,
            response_body,
            response_body_truncated,
            None,
        )

        # Check if content type should block the trace
        from ...core.content_type_utils import get_decoded_type, should_block_content_type
        from ...core.trace_blocking_manager import TraceBlockingManager

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

        # Apply transforms if present
        transform_metadata = None
        if self.transform_engine:
            span_data = HttpSpanData(
                kind=SpanKind.SERVER,
                input_value=input_value,
                output_value=output_value,
            )
            self.transform_engine.apply_transforms(span_data)
            transform_metadata = span_data.transform_metadata
            input_value = span_data.input_value or input_value
            output_value = span_data.output_value or output_value

        # Generate schemas using WSGI utilities
        input_schema_info = generate_input_schema_info(input_value, request_body_truncated)
        output_schema_info = generate_output_schema_info(output_value, response_body_truncated)

        from ...core.drift_sdk import TuskDrift
        sdk = TuskDrift.get_instance()
        # Derive timestamp from start_time_ns
        timestamp_seconds = start_time_ns // 1_000_000_000
        timestamp_nanos = start_time_ns % 1_000_000_000
        duration_seconds = duration_ns // 1_000_000_000
        duration_nanos = duration_ns % 1_000_000_000

        if status_code >= 400:
            status = SpanStatus(code=StatusCode.ERROR, message=f"HTTP {status_code}")
        else:
            status = SpanStatus(code=StatusCode.OK, message="")

        # Django-specific: use route template for span name to avoid cardinality explosion
        method = request.method
        route_template = getattr(request, "_drift_route_template", None)
        if route_template:
            # Use route template (e.g., "users/<int:id>/")
            span_name = f"{method} {route_template}"
        else:
            # Fallback to literal path (e.g., for 404s)
            span_name = f"{method} {request.path}"

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
            package_name="django",
            instrumentation_name="DjangoInstrumentation",
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

    def _capture_error_span(self, request: HttpRequest, exception: Exception) -> None:
        """Create and collect an error span.

        Args:
            request: Django HttpRequest object
            exception: The exception that was raised
        """
        start_time_ns = getattr(request, "_drift_start_time_ns", None)
        trace_id = getattr(request, "_drift_trace_id", None)
        span_id = getattr(request, "_drift_span_id", None)

        if not all([start_time_ns, trace_id, span_id]):
            return

        end_time_ns = time.time_ns()
        duration_ns = end_time_ns - start_time_ns

        # Build input_value
        request_body = getattr(request, "_drift_request_body", None)
        request_body_truncated = getattr(request, "_drift_request_body_truncated", False)
        input_value = build_input_value(request.META, request_body, request_body_truncated)

        # Build error output_value
        output_value = build_output_value(
            500,
            "Internal Server Error",
            {},
            None,
            False,
            str(exception),
        )

        # Generate schemas
        input_schema_info = generate_input_schema_info(input_value, request_body_truncated)
        output_schema_info = generate_output_schema_info(output_value, False)

        from ...core.drift_sdk import TuskDrift
        sdk = TuskDrift.get_instance()
        timestamp_seconds = start_time_ns // 1_000_000_000
        timestamp_nanos = start_time_ns % 1_000_000_000
        duration_seconds = duration_ns // 1_000_000_000
        duration_nanos = duration_ns % 1_000_000_000

        method = request.method
        route_template = getattr(request, "_drift_route_template", None)
        span_name = f"{method} {route_template}" if route_template else f"{method} {request.path}"

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
            package_name="django",
            instrumentation_name="DjangoInstrumentation",
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
            status=SpanStatus(code=StatusCode.ERROR, message=f"Exception: {type(exception).__name__}"),
            is_pre_app_start=not sdk.app_ready,
            is_root_span=True,
            timestamp=Timestamp(seconds=timestamp_seconds, nanos=timestamp_nanos),
            duration=Duration(seconds=duration_seconds, nanos=duration_nanos),
            transform_metadata=None,
            metadata=metadata,
        )

        sdk.collect_span(span)

        # Clear tracker after span collection
        tracker.clear_env_vars(trace_id)
