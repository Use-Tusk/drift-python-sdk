"""Django middleware for Drift span capture."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from opentelemetry.trace import SpanKind as OTelSpanKind

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse
from ...core.mode_utils import handle_record_mode, should_record_inbound_http_request
from ...core.tracing import TdSpanAttributes
from ...core.tracing.span_utils import CreateSpanOptions, SpanInfo, SpanUtils
from ...core.types import (
    CleanSpanData,
    Duration,
    PackageType,
    SpanKind,
    SpanStatus,
    StatusCode,
    Timestamp,
    TuskDriftMode,
    replay_trace_id_context,
    span_kind_context,
)
from ..http import HttpSpanData, HttpTransformEngine
from ..wsgi import (
    build_input_schema_merges,
    build_input_value,
    build_output_schema_merges,
    build_output_value,
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

        # DISABLED mode - just pass through
        if sdk.mode == TuskDriftMode.DISABLED:
            return self.get_response(request)

        # REPLAY mode - handle trace ID extraction and context setup
        if sdk.mode == TuskDriftMode.REPLAY:
            return self._handle_replay_request(request, sdk)

        # RECORD mode - use handle_record_mode for consistent is_pre_app_start logic
        return handle_record_mode(
            original_function_call=lambda: self.get_response(request),
            record_mode_handler=lambda is_pre_app_start: self._record_request(request, sdk, is_pre_app_start),
            span_kind=OTelSpanKind.SERVER,
        )

    def _handle_replay_request(self, request: HttpRequest, sdk) -> HttpResponse:
        """Handle request in REPLAY mode.

        Extracts trace ID from headers and sets up context for child spans.
        Does not record the root span in REPLAY mode.

        Args:
            request: Django HttpRequest object
            sdk: TuskDrift SDK instance

        Returns:
            Django HttpResponse object
        """
        # Extract trace ID from headers (case-insensitive lookup)
        # Django stores headers in request.META
        headers_lower = {k.lower(): v for k, v in request.META.items() if k.startswith("HTTP_")}
        logger.info(f"[DJANGO_MIDDLEWARE] REPLAY mode, headers: {list(headers_lower.keys())}")
        # Convert HTTP_X_TD_TRACE_ID -> x-td-trace-id
        replay_trace_id = headers_lower.get("http_x_td_trace_id")
        logger.info(f"[DJANGO_MIDDLEWARE] replay_trace_id from header: {replay_trace_id}")

        if not replay_trace_id:
            # No trace context in REPLAY mode; proceed without span
            logger.warning("[DJANGO_MIDDLEWARE] No replay_trace_id found in headers, proceeding without span")
            return self.get_response(request)

        # Start time travel as early as possible (inbound request start) to ensure deterministic
        # behavior for time-dependent code paths, especially cache key generation (e.g. django-cachalot).
        root_timestamp = headers_lower.get("http_x_td_root_timestamp")
        if root_timestamp:
            try:
                from drift.instrumentation.datetime.instrumentation import start_time_travel

                start_time_travel(root_timestamp, trace_id=replay_trace_id)
            except Exception as e:
                logger.debug(f"[DJANGO_MIDDLEWARE] Failed to start time travel from x-td-root-timestamp: {e}")

        # Set replay trace context
        replay_token = replay_trace_id_context.set(replay_trace_id)

        method = request.method
        path = request.path
        span_name = f"{method} {path}"

        # Create span using SpanUtils
        span_info = SpanUtils.create_span(
            CreateSpanOptions(
                name=span_name,
                kind=OTelSpanKind.SERVER,
                attributes={
                    TdSpanAttributes.NAME: span_name,
                    TdSpanAttributes.PACKAGE_NAME: "django",
                    TdSpanAttributes.INSTRUMENTATION_NAME: "DjangoInstrumentation",
                    TdSpanAttributes.SUBMODULE_NAME: method,
                    TdSpanAttributes.PACKAGE_TYPE: PackageType.HTTP.name,
                    TdSpanAttributes.IS_PRE_APP_START: False,
                    TdSpanAttributes.IS_ROOT_SPAN: True,
                },
                is_pre_app_start=False,
            )
        )

        if not span_info:
            # Failed to create span, just process the request
            replay_trace_id_context.reset(replay_token)
            return self.get_response(request)

        # Set span_kind_context for child spans
        span_kind_token = span_kind_context.set(SpanKind.SERVER)

        # Store metadata on request for later use
        request._drift_start_time_ns = time.time_ns()  # type: ignore
        request._drift_span = span_info.span  # type: ignore
        request._drift_route_template = None  # type: ignore

        try:
            with SpanUtils.with_span(span_info):
                response = self.get_response(request)
                # REPLAY mode: don't capture the span (it's already recorded)
                # But do normalize the response so comparison succeeds
                response = self._normalize_html_response(response)
                return response
        finally:
            # Reset context
            span_kind_context.reset(span_kind_token)
            replay_trace_id_context.reset(replay_token)
            span_info.span.end()

    def _record_request(self, request: HttpRequest, sdk, is_pre_app_start: bool) -> HttpResponse:
        """Handle request in RECORD mode.

        Creates a span, processes the request, and captures the span.

        Args:
            request: Django HttpRequest object
            sdk: TuskDrift SDK instance
            is_pre_app_start: Whether this request occurred before app was marked ready

        Returns:
            Django HttpResponse object
        """
        # Pre-flight check: drop transforms and sampling
        # NOTE: This is done before body capture to avoid unnecessary I/O
        method = request.method or ""
        path = request.path
        query_string = request.META.get("QUERY_STRING", "")
        target = f"{path}?{query_string}" if query_string else path

        from ..wsgi import extract_headers

        request_headers = extract_headers(request.META)

        should_record, skip_reason = should_record_inbound_http_request(
            method, target, request_headers, self.transform_engine, is_pre_app_start
        )
        if not should_record:
            logger.debug(f"[Django] Skipping request ({skip_reason}), path={path}")
            return self.get_response(request)

        start_time_ns = time.time_ns()
        span_name = f"{method} {path}"

        # Create span using SpanUtils
        span_info = SpanUtils.create_span(
            CreateSpanOptions(
                name=span_name,
                kind=OTelSpanKind.SERVER,
                attributes={
                    TdSpanAttributes.NAME: span_name,
                    TdSpanAttributes.PACKAGE_NAME: "django",
                    TdSpanAttributes.INSTRUMENTATION_NAME: "DjangoInstrumentation",
                    TdSpanAttributes.SUBMODULE_NAME: method,
                    TdSpanAttributes.PACKAGE_TYPE: PackageType.HTTP.name,
                    TdSpanAttributes.IS_PRE_APP_START: is_pre_app_start,
                    TdSpanAttributes.IS_ROOT_SPAN: True,
                },
                is_pre_app_start=is_pre_app_start,
            )
        )

        if not span_info:
            # Failed to create span, just process the request
            return self.get_response(request)

        # Set span_kind_context for child spans
        span_kind_token = span_kind_context.set(SpanKind.SERVER)

        # Capture request body
        request_body = None
        if request.method in ("POST", "PUT", "PATCH"):
            try:
                request_body = request.body
            except Exception:
                pass

        # Store metadata on request for later use
        request._drift_start_time_ns = start_time_ns  # type: ignore
        request._drift_span_info = span_info  # type: ignore
        request._drift_request_body = request_body  # type: ignore
        request._drift_route_template = None  # type: ignore

        try:
            with SpanUtils.with_span(span_info):
                response = self.get_response(request)
                self._capture_span(request, response, span_info)
                return response
        except Exception as e:
            self._capture_error_span(request, e, span_info)
            raise
        finally:
            span_kind_context.reset(span_kind_token)
            span_info.span.end()

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

    def _normalize_html_response(self, response: HttpResponse) -> HttpResponse:
        """Normalize HTML response body for REPLAY mode comparison.

        In REPLAY mode, we need the actual HTTP response to match the recorded
        response (which had CSRF tokens and class ordering normalized during recording).
        This modifies the response body in-place.

        Args:
            response: Django HttpResponse object

        Returns:
            Modified response with normalized body
        """
        from .html_utils import normalize_html_response

        return normalize_html_response(response)

    def _capture_span(self, request: HttpRequest, response: HttpResponse, span_info: SpanInfo) -> None:
        """Create and collect a span from request/response data.

        Args:
            request: Django HttpRequest object
            response: Django HttpResponse object
            span_info: SpanInfo containing trace/span IDs and span reference
        """
        start_time_ns = getattr(request, "_drift_start_time_ns", None)

        if not start_time_ns or not span_info.span.is_recording():
            return

        # Use trace_id and span_id from span_info
        trace_id = span_info.trace_id
        span_id = span_info.span_id

        end_time_ns = time.time_ns()
        duration_ns = end_time_ns - start_time_ns

        # Build input_value using WSGI utilities
        request_body = getattr(request, "_drift_request_body", None)
        input_value = build_input_value(request.META, request_body)

        # Build output_value using WSGI utilities
        status_code = response.status_code
        status_message = response.reason_phrase if hasattr(response, "reason_phrase") else ""

        # Convert response headers to dict
        response_headers = dict(response.items()) if hasattr(response, "items") else {}

        # Capture response body if available
        # No truncation at capture time - span-level 1MB blocking at export handles oversized spans
        response_body = None
        if hasattr(response, "content"):
            content = response.content
            if isinstance(content, bytes) and len(content) > 0:
                response_body = content

        # Normalize HTML responses for consistent record/replay comparison
        # This only affects what is stored in the span, not what the browser receives
        if response_body:
            from .html_utils import normalize_html_body

            content_type = response_headers.get("Content-Type", "")
            content_encoding = response_headers.get("Content-Encoding", "")
            response_body = normalize_html_body(response_body, content_type, content_encoding)

        output_value = build_output_value(
            status_code,
            status_message,
            response_headers,
            response_body,
            None,
        )

        # Check if content type should block the trace
        from ...core.content_type_utils import (
            get_decoded_type,
            should_block_content_type,
        )
        from ...core.trace_blocking_manager import TraceBlockingManager

        content_type = response_headers.get("content-type") or response_headers.get("Content-Type")
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

        # Build schema merges and generate schemas
        # Note: Django uses direct CleanSpanData creation instead of OTel spans,
        # so we need to generate schemas here instead of in the converter
        from ...core.json_schema_helper import JsonSchemaHelper

        input_schema_merges_dict = build_input_schema_merges(input_value)
        output_schema_merges_dict = build_output_schema_merges(output_value)

        # Convert dict back to SchemaMerge objects for JsonSchemaHelper
        from ...core.json_schema_helper import DecodedType, EncodingType, SchemaMerge

        def dict_to_schema_merges(merges_dict):
            result = {}
            for key, merge_data in merges_dict.items():
                encoding = EncodingType(merge_data["encoding"]) if "encoding" in merge_data else None
                decoded_type = DecodedType(merge_data["decoded_type"]) if "decoded_type" in merge_data else None
                match_importance = merge_data.get("match_importance")
                result[key] = SchemaMerge(
                    encoding=encoding, decoded_type=decoded_type, match_importance=match_importance
                )
            return result

        input_schema_merges = dict_to_schema_merges(input_schema_merges_dict)
        output_schema_merges = dict_to_schema_merges(output_schema_merges_dict)

        input_schema_info = JsonSchemaHelper.generate_schema_and_hash(input_value, input_schema_merges)
        output_schema_info = JsonSchemaHelper.generate_schema_and_hash(output_value, output_schema_merges)

        from ...core.drift_sdk import TuskDrift

        sdk = TuskDrift.get_instance()
        # Derive timestamp from start_time_ns
        timestamp_seconds = start_time_ns // 1_000_000_000
        timestamp_nanos = start_time_ns % 1_000_000_000
        duration_seconds = duration_ns // 1_000_000_000
        duration_nanos = duration_ns % 1_000_000_000

        # Match Node SDK: >= 400 is considered an error
        if status_code >= 400:
            status = SpanStatus(code=StatusCode.ERROR, message=f"HTTP {status_code}")
        else:
            status = SpanStatus(code=StatusCode.OK, message="")

        # Django-specific: use route template for span name to avoid cardinality explosion
        method = request.method or ""
        route_template = getattr(request, "_drift_route_template", None)
        if route_template:
            # Use route template (e.g., "users/<int:id>/")
            span_name = f"{method} {route_template}"
        else:
            # Fallback to literal path (e.g., for 404s)
            span_name = f"{method} {request.path}"

        # Only create and collect span in RECORD mode
        # In REPLAY mode, we only set up context for child spans but don't record the root span
        if sdk.mode == TuskDriftMode.RECORD:
            clean_span = CleanSpanData(
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
                is_pre_app_start=span_info.is_pre_app_start,
                is_root_span=True,
                timestamp=Timestamp(seconds=timestamp_seconds, nanos=timestamp_nanos),
                duration=Duration(seconds=duration_seconds, nanos=duration_nanos),
                transform_metadata=transform_metadata,
                metadata=None,
            )

            sdk.collect_span(clean_span)

    def _capture_error_span(self, request: HttpRequest, exception: Exception, span_info: SpanInfo) -> None:
        """Create and collect an error span.

        Args:
            request: Django HttpRequest object
            exception: The exception that was raised
            span_info: SpanInfo containing trace/span IDs and span reference
        """
        start_time_ns = getattr(request, "_drift_start_time_ns", None)

        if not start_time_ns or not span_info.span.is_recording():
            return

        # Use trace_id and span_id from span_info
        trace_id = span_info.trace_id
        span_id = span_info.span_id

        end_time_ns = time.time_ns()
        duration_ns = end_time_ns - start_time_ns

        # Build input_value
        request_body = getattr(request, "_drift_request_body", None)
        input_value = build_input_value(request.META, request_body)

        # Build error output_value
        output_value = build_output_value(
            500,
            "Internal Server Error",
            {},
            None,
            str(exception),
        )

        # Build schema merges and generate schemas
        from ...core.json_schema_helper import DecodedType, EncodingType, JsonSchemaHelper, SchemaMerge

        input_schema_merges_dict = build_input_schema_merges(input_value)
        output_schema_merges_dict = build_output_schema_merges(output_value)

        def dict_to_schema_merges(merges_dict):
            result = {}
            for key, merge_data in merges_dict.items():
                encoding = EncodingType(merge_data["encoding"]) if "encoding" in merge_data else None
                decoded_type = DecodedType(merge_data["decoded_type"]) if "decoded_type" in merge_data else None
                match_importance = merge_data.get("match_importance")
                result[key] = SchemaMerge(
                    encoding=encoding, decoded_type=decoded_type, match_importance=match_importance
                )
            return result

        input_schema_merges = dict_to_schema_merges(input_schema_merges_dict)
        output_schema_merges = dict_to_schema_merges(output_schema_merges_dict)

        input_schema_info = JsonSchemaHelper.generate_schema_and_hash(input_value, input_schema_merges)
        output_schema_info = JsonSchemaHelper.generate_schema_and_hash(output_value, output_schema_merges)

        from ...core.drift_sdk import TuskDrift

        sdk = TuskDrift.get_instance()
        timestamp_seconds = start_time_ns // 1_000_000_000
        timestamp_nanos = start_time_ns % 1_000_000_000
        duration_seconds = duration_ns // 1_000_000_000
        duration_nanos = duration_ns % 1_000_000_000

        method = request.method or ""
        route_template = getattr(request, "_drift_route_template", None)
        span_name = f"{method} {route_template}" if route_template else f"{method} {request.path}"

        clean_span = CleanSpanData(
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
            is_pre_app_start=span_info.is_pre_app_start,
            is_root_span=True,
            timestamp=Timestamp(seconds=timestamp_seconds, nanos=timestamp_nanos),
            duration=Duration(seconds=duration_seconds, nanos=duration_nanos),
            transform_metadata=None,
            metadata=None,
        )

        sdk.collect_span(clean_span)
