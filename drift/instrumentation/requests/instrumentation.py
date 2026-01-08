"""Instrumentation for requests HTTP client library."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any
from urllib.parse import urlparse

from opentelemetry.trace import Span, Status
from opentelemetry.trace import SpanKind as OTelSpanKind
from opentelemetry.trace import StatusCode as OTelStatusCode


class RequestDroppedByTransform(Exception):
    """Exception raised when an outbound HTTP request is dropped by a transform rule.

    This matches Node SDK behavior where drop transforms prevent the HTTP call
    and raise an error rather than returning a fake response.

    Attributes:
        message: Error message explaining the drop
        method: HTTP method (GET, POST, etc.)
        url: Request URL that was dropped
    """

    def __init__(self, message: str, method: str, url: str):
        self.message = message
        self.method = method
        self.url = url
        super().__init__(message)


from ...core.data_normalization import create_mock_input_value, remove_none_values
from ...core.drift_sdk import TuskDrift
from ...core.json_schema_helper import DecodedType, EncodingType, SchemaMerge
from ...core.mode_utils import handle_record_mode, handle_replay_mode
from ...core.tracing import TdSpanAttributes
from ...core.tracing.span_utils import CreateSpanOptions, SpanUtils
from ...core.types import (
    PackageType,
    SpanKind,
    SpanStatus,
    StatusCode,
    TuskDriftMode,
    calling_library_context,
)
from ..base import InstrumentationBase
from ..http import HttpSpanData, HttpTransformEngine

logger = logging.getLogger(__name__)

# Schema merge hints for headers (low match importance)
HEADER_SCHEMA_MERGES = {
    "headers": SchemaMerge(match_importance=0.0),
}


class RequestsInstrumentation(InstrumentationBase):
    """Instrumentation for the requests HTTP client library.

    Patches requests.Session.request to:
    - Intercept HTTP requests in REPLAY mode and return mocked responses
    - Capture request/response data as CLIENT spans in RECORD mode
    """

    def __init__(self, enabled: bool = True, transforms: dict[str, Any] | None = None) -> None:
        self._transform_engine = HttpTransformEngine(self._resolve_http_transforms(transforms))
        super().__init__(
            name="RequestsInstrumentation",
            module_name="requests",
            supported_versions="*",
            enabled=enabled,
        )

    def _resolve_http_transforms(
        self, provided: dict[str, Any] | list[dict[str, Any]] | None
    ) -> list[dict[str, Any]] | None:
        """Resolve HTTP transforms from provided config or SDK config."""
        if isinstance(provided, list):
            return provided
        if isinstance(provided, dict) and isinstance(provided.get("http"), list):
            return provided["http"]

        sdk = TuskDrift.get_instance()
        transforms = getattr(sdk.config, "transforms", None)
        if isinstance(transforms, dict) and isinstance(transforms.get("http"), list):
            return transforms["http"]
        return None

    def patch(self, module: Any) -> None:
        """Patch the requests module."""
        if not hasattr(module, "Session"):
            logger.warning("requests.Session not found, skipping instrumentation")
            return

        # Store original method
        original_request = module.Session.request
        instrumentation_self = self

        def patched_request(session_self, method: str, url: str, **kwargs):
            """Patched Session.request method."""
            sdk = TuskDrift.get_instance()

            # Pass through if SDK is disabled
            if sdk.mode == TuskDriftMode.DISABLED:
                return original_request(session_self, method, url, **kwargs)

            def original_call():
                return original_request(session_self, method, url, **kwargs)

            # REPLAY mode: Use handle_replay_mode for proper background request handling
            if sdk.mode == TuskDriftMode.REPLAY:
                return handle_replay_mode(
                    replay_mode_handler=lambda: instrumentation_self._handle_replay(
                        sdk, session_self, method, url, original_request, **kwargs
                    ),
                    no_op_request_handler=lambda: instrumentation_self._get_default_response(url),
                    is_server_request=False,
                )

            # RECORD mode: Use handle_record_mode for proper is_pre_app_start handling
            return handle_record_mode(
                original_function_call=original_call,
                record_mode_handler=lambda is_pre_app_start: instrumentation_self._handle_record(
                    sdk, session_self, method, url, is_pre_app_start, original_request, **kwargs
                ),
                span_kind=OTelSpanKind.CLIENT,
            )

        # Apply patch
        module.Session.request = patched_request
        logger.info("requests.Session.request instrumented")

    def _get_default_response(self, url: str) -> Any:
        """Return default response for background requests in REPLAY mode.

        Background requests (health checks, metrics, etc.) that happen outside
        of any trace context should return a default response instead of failing.
        """
        import requests

        response = requests.Response()
        response.status_code = 200
        response.reason = "OK"
        response.url = url
        response._content = b""
        response.encoding = "utf-8"
        logger.debug(f"[RequestsInstrumentation] Returning default response for background request to {url}")
        return response

    def _handle_replay(
        self,
        sdk: TuskDrift,
        session_self: Any,
        method: str,
        url: str,
        original_request: Any,
        **kwargs,
    ) -> Any:
        """Handle request in REPLAY mode.

        Creates a span, fetches mock response, and returns it.
        Raises RuntimeError if no mock is found.
        """
        parsed_url = urlparse(url)
        span_name = f"{method.upper()} {parsed_url.path or '/'}"

        # Create span using SpanUtils
        span_info = SpanUtils.create_span(
            CreateSpanOptions(
                name=span_name,
                kind=OTelSpanKind.CLIENT,
                attributes={
                    TdSpanAttributes.NAME: span_name,
                    TdSpanAttributes.PACKAGE_NAME: parsed_url.scheme,
                    TdSpanAttributes.INSTRUMENTATION_NAME: "RequestsInstrumentation",
                    TdSpanAttributes.SUBMODULE_NAME: method.upper(),
                    TdSpanAttributes.PACKAGE_TYPE: PackageType.HTTP.name,
                    TdSpanAttributes.IS_PRE_APP_START: not sdk.app_ready,
                },
                is_pre_app_start=not sdk.app_ready,
            )
        )

        if not span_info:
            raise RuntimeError(f"Error creating span in replay mode for {method} {url}")

        try:
            with SpanUtils.with_span(span_info):
                # Use IDs from SpanInfo (already formatted)
                mock_response = self._try_get_mock(
                    sdk,
                    method,
                    url,
                    span_info.trace_id,
                    span_info.span_id,
                    **kwargs,
                )

                if mock_response is not None:
                    return mock_response

                # No mock found - raise error in REPLAY mode
                raise RuntimeError(f"No mock found for {method} {url} in REPLAY mode")
        finally:
            span_info.span.end()

    def _handle_record(
        self,
        sdk: TuskDrift,
        session_self: Any,
        method: str,
        url: str,
        is_pre_app_start: bool,
        original_request: Any,
        **kwargs,
    ) -> Any:
        """Handle request in RECORD mode.

        Creates a span, makes the real request, and records the response.
        """
        parsed_url = urlparse(url)
        span_name = f"{method.upper()} {parsed_url.path or '/'}"

        # Create span using SpanUtils
        span_info = SpanUtils.create_span(
            CreateSpanOptions(
                name=span_name,
                kind=OTelSpanKind.CLIENT,
                attributes={
                    TdSpanAttributes.NAME: span_name,
                    TdSpanAttributes.PACKAGE_NAME: parsed_url.scheme,
                    TdSpanAttributes.INSTRUMENTATION_NAME: "RequestsInstrumentation",
                    TdSpanAttributes.SUBMODULE_NAME: method.upper(),
                    TdSpanAttributes.PACKAGE_TYPE: PackageType.HTTP.name,
                    TdSpanAttributes.IS_PRE_APP_START: is_pre_app_start,
                },
                is_pre_app_start=is_pre_app_start,
            )
        )

        if not span_info:
            # Span creation failed (trace blocked, etc.) - just make the request
            return original_request(session_self, method, url, **kwargs)

        try:
            with SpanUtils.with_span(span_info):
                # Check drop transforms BEFORE making the request
                if self._transform_engine and self._transform_engine.should_drop_outbound_request(
                    method.upper(), url, kwargs.get("headers", {})
                ):
                    # Request should be dropped - mark span and raise exception
                    span_info.span.set_attribute(
                        TdSpanAttributes.OUTPUT_VALUE,
                        json.dumps({"bodyProcessingError": "dropped"}),
                    )
                    span_info.span.set_status(Status(OTelStatusCode.ERROR, "Dropped by transform"))
                    raise RequestDroppedByTransform(
                        f"Outbound request to {url} was dropped by transform rule",
                        method.upper(),
                        url,
                    )

                # Make the real request
                error = None
                response = None

                # Set calling_library_context to prevent socket instrumentation warnings
                calling_lib_token = calling_library_context.set("RequestsInstrumentation")
                try:
                    response = original_request(session_self, method, url, **kwargs)
                    return response
                except Exception as e:
                    error = e
                    raise
                finally:
                    calling_library_context.reset(calling_lib_token)
                    # Finalize span with request/response data
                    self._finalize_span(
                        span_info.span,
                        method,
                        url,
                        response,
                        error,
                        kwargs,
                    )
        finally:
            span_info.span.end()

    def _encode_body_to_base64(self, body_data: Any) -> tuple[str | None, int]:
        """Encode body data to base64 string.

        Args:
            body_data: Body data (str, bytes, dict, or other)

        Returns:
            Tuple of (base64_encoded_string, original_byte_size)
        """
        if body_data is None:
            return None, 0

        # Convert to bytes
        if isinstance(body_data, bytes):
            body_bytes = body_data
        elif isinstance(body_data, str):
            body_bytes = body_data.encode("utf-8")
        elif isinstance(body_data, dict):
            # JSON data
            body_bytes = json.dumps(body_data).encode("utf-8")
        else:
            # Fallback: convert to string then encode
            body_bytes = str(body_data).encode("utf-8")

        # Encode to base64
        base64_body = base64.b64encode(body_bytes).decode("ascii")

        return base64_body, len(body_bytes)

    def _get_decoded_type_from_content_type(self, content_type: str | None) -> DecodedType | None:
        """Determine decoded type from Content-Type header.

        Args:
            content_type: Content-Type header value

        Returns:
            DecodedType enum value or None
        """
        if not content_type:
            return None

        # Extract main type (before semicolon)
        main_type = content_type.lower().split(";")[0].strip()

        # Common content type mappings (subset from Node.js httpBodyEncoder.ts)
        CONTENT_TYPE_MAP = {
            "application/json": DecodedType.JSON,
            "text/plain": DecodedType.PLAIN_TEXT,
            "text/html": DecodedType.HTML,
            "application/x-www-form-urlencoded": DecodedType.FORM_DATA,
            "multipart/form-data": DecodedType.MULTIPART_FORM,
            "application/xml": DecodedType.XML,
            "text/xml": DecodedType.XML,
        }

        return CONTENT_TYPE_MAP.get(main_type)

    def _get_content_type_header(self, headers: dict) -> str | None:
        """Get content-type header (case-insensitive lookup)."""
        for key, value in headers.items():
            if key.lower() == "content-type":
                return value
        return None

    def _try_get_mock(
        self,
        sdk: TuskDrift,
        method: str,
        url: str,
        trace_id: str,
        span_id: str,
        **kwargs,
    ) -> Any:
        """Try to get a mocked response from CLI.

        Returns:
            Mocked response object if found, None otherwise
        """
        try:
            # Build request input value
            parsed_url = urlparse(url)

            # Extract request data
            headers = kwargs.get("headers", {})
            params = kwargs.get("params", {})

            # Handle request body - encode to base64
            data = kwargs.get("data")
            json_data = kwargs.get("json")
            body_base64 = None
            body_size = 0

            if json_data is not None:
                body_base64, body_size = self._encode_body_to_base64(json_data)
            elif data is not None:
                body_base64, body_size = self._encode_body_to_base64(data)

            raw_input_value = {
                "method": method.upper(),
                "url": url,
                "protocol": parsed_url.scheme,
                "hostname": parsed_url.hostname,
                "port": parsed_url.port,
                "path": parsed_url.path or "/",
                "headers": dict(headers),
                "query": params,
            }

            # Add body fields only if body exists
            if body_base64 is not None:
                raw_input_value["body"] = body_base64
                raw_input_value["bodySize"] = body_size

            input_value = create_mock_input_value(raw_input_value)

            # Create schema merge hints for input (centralized schema generation)
            input_schema_merges = {
                "headers": SchemaMerge(match_importance=0.0),
            }
            if body_base64 is not None:
                request_content_type = self._get_content_type_header(headers)
                input_schema_merges["body"] = SchemaMerge(
                    encoding=EncodingType.BASE64,
                    decoded_type=self._get_decoded_type_from_content_type(request_content_type),
                )

            # Use centralized mock finding utility (matches Node SDK pattern)
            from ...core.mock_utils import find_mock_response_sync

            mock_response_output = find_mock_response_sync(
                sdk=sdk,
                trace_id=trace_id,
                span_id=span_id,
                name=f"{method.upper()} {parsed_url.path or '/'}",
                package_name=parsed_url.scheme,
                package_type=PackageType.HTTP,
                instrumentation_name="RequestsInstrumentation",
                submodule_name=method.upper(),
                input_value=input_value,
                kind=SpanKind.CLIENT,
                input_schema_merges=input_schema_merges,
            )

            if not mock_response_output or not mock_response_output.found:
                logger.debug(f"No mock found for {method} {url} (trace_id={trace_id})")
                return None

            # Create mocked response object
            if mock_response_output.response is None:
                logger.debug(f"Mock found but response data is None for {method} {url}")
                return None
            return self._create_mock_response(mock_response_output.response, url)

        except Exception as e:
            logger.error(f"Error getting mock for {method} {url}: {e}")
            return None

    def _create_mock_response(self, mock_data: dict[str, Any], url: str) -> Any:
        """Create a mocked requests.Response object.

        Args:
            mock_data: Mock response data from CLI
            url: Request URL

        Returns:
            Mocked Response object
        """
        import requests

        # Create a mock response
        response = requests.Response()
        response.status_code = mock_data.get("statusCode", 200)
        response.reason = mock_data.get("statusMessage", "OK")
        response.url = url

        # Set headers
        headers = mock_data.get("headers", {})
        response.headers.update(headers)

        # Set body - decode from base64 if needed
        body = mock_data.get("body", "")
        if isinstance(body, str):
            # Try to decode as base64 first (expected format from CLI)
            try:
                # Check if it looks like base64 (only contains base64 chars)
                # and can be successfully decoded and re-encoded to match
                decoded = base64.b64decode(body.encode("ascii"), validate=True)
                # Verify round-trip works (confirms it's valid base64)
                if base64.b64encode(decoded).decode("ascii") == body:
                    response._content = decoded
                else:
                    # Not valid base64, treat as plain text
                    response._content = body.encode("utf-8")
            except Exception:
                # Fall back to treating as plain text
                response._content = body.encode("utf-8")
        elif isinstance(body, bytes):
            response._content = body
        else:
            # JSON or other object - serialize
            response._content = json.dumps(body).encode("utf-8")

        response.encoding = "utf-8"

        logger.debug(f"Created mock response: {response.status_code} for {url}")
        return response

    def _finalize_span(
        self,
        span: Span,
        method: str,
        url: str,
        response: Any,
        error: Exception | None,
        request_kwargs: dict[str, Any],
    ) -> None:
        """Finalize span with request/response data.

        Args:
            span: The OpenTelemetry span to finalize
            method: HTTP method
            url: Request URL
            response: Response object (if successful)
            error: Exception (if failed)
            request_kwargs: Original request kwargs
        """
        try:
            parsed_url = urlparse(url)

            # ===== BUILD INPUT VALUE =====
            headers = request_kwargs.get("headers", {})
            params = request_kwargs.get("params", {})

            # Get request body and encode to base64
            data = request_kwargs.get("data")
            json_data = request_kwargs.get("json")
            body_base64 = None
            body_size = 0

            if json_data is not None:
                body_base64, body_size = self._encode_body_to_base64(json_data)
            elif data is not None:
                body_base64, body_size = self._encode_body_to_base64(data)

            input_value = {
                "method": method.upper(),
                "url": url,
                "protocol": parsed_url.scheme,
                "hostname": parsed_url.hostname,
                "port": parsed_url.port,
                "path": parsed_url.path or "/",
                "headers": dict(headers),
                "query": params,
            }

            # Add body fields only if body exists
            if body_base64 is not None:
                input_value["body"] = body_base64
                input_value["bodySize"] = body_size

            # ===== BUILD OUTPUT VALUE =====
            output_value = {}
            status = SpanStatus(code=StatusCode.OK, message="")
            response_body_base64 = None  # Initialize for later use in schema merges

            if error:
                output_value = {
                    "errorName": type(error).__name__,
                    "errorMessage": str(error),
                }
                status = SpanStatus(code=StatusCode.ERROR, message=str(error))
            elif response:
                # Extract response data
                response_headers = dict(response.headers)
                response_body_size = 0

                try:
                    # Get response content as bytes (respects encoding)
                    # No truncation at capture time - span-level 1MB blocking at export handles oversized spans
                    response_bytes = response.content

                    # Encode to base64
                    response_body_base64, response_body_size = self._encode_body_to_base64(response_bytes)
                except Exception:
                    response_body_base64 = None
                    response_body_size = 0

                output_value = {
                    "statusCode": response.status_code,
                    "statusMessage": response.reason,
                    "headers": response_headers,
                }

                # Add body fields only if body exists
                if response_body_base64 is not None:
                    output_value["body"] = response_body_base64
                    output_value["bodySize"] = response_body_size

                if response.status_code >= 400:
                    status = SpanStatus(
                        code=StatusCode.ERROR,
                        message=f"HTTP {response.status_code}",
                    )

                # Check if response content type should block the trace
                from ...core.content_type_utils import get_decoded_type, should_block_content_type
                from ...core.trace_blocking_manager import TraceBlockingManager

                response_content_type = response_headers.get("content-type") or response_headers.get("Content-Type")
                decoded_type = get_decoded_type(response_content_type)

                if should_block_content_type(decoded_type):
                    # Block PARENT trace for outbound requests with binary responses
                    span_context = span.get_span_context()
                    trace_id = format(span_context.trace_id, "032x")

                    blocking_mgr = TraceBlockingManager.get_instance()
                    blocking_mgr.block_trace(
                        trace_id, reason=f"outbound_binary:{decoded_type.name if decoded_type else 'unknown'}"
                    )
                    logger.warning(
                        f"Blocking trace {trace_id} - outbound request returned binary response: {response_content_type} "
                        f"(decoded as {decoded_type.name if decoded_type else 'unknown'})"
                    )
                    return  # Skip finalizing span
            else:
                # No response and no error
                output_value = {}

            # ===== APPLY TRANSFORMS =====
            transform_metadata = None
            if self._transform_engine:
                span_data = HttpSpanData(
                    kind=SpanKind.CLIENT,
                    input_value=input_value,
                    output_value=output_value,
                )
                self._transform_engine.apply_transforms(span_data)

                # Update values with transformed data
                input_value = span_data.input_value or input_value
                output_value = span_data.output_value or output_value
                transform_metadata = span_data.transform_metadata

            # ===== CREATE SCHEMA MERGE HINTS =====
            # Determine decoded types from content-type headers
            request_content_type = self._get_content_type_header(headers)
            response_content_type = None
            if response and hasattr(response, "headers"):
                response_content_type = self._get_content_type_header(dict(response.headers))

            # Create schema merge hints for input
            input_schema_merges = {
                "headers": SchemaMerge(match_importance=0.0),
            }
            if body_base64 is not None:
                input_schema_merges["body"] = SchemaMerge(
                    encoding=EncodingType.BASE64,
                    decoded_type=self._get_decoded_type_from_content_type(request_content_type),
                )

            # Create schema merge hints for output
            output_schema_merges = {
                "headers": SchemaMerge(match_importance=0.0),
            }
            if response_body_base64 is not None:
                output_schema_merges["body"] = SchemaMerge(
                    encoding=EncodingType.BASE64,
                    decoded_type=self._get_decoded_type_from_content_type(response_content_type),
                )

            # ===== SET SPAN ATTRIBUTES =====
            # Normalize values to remove None fields (matches REPLAY path behavior)
            normalized_input = remove_none_values(input_value)
            normalized_output = remove_none_values(output_value)
            span.set_attribute(TdSpanAttributes.INPUT_VALUE, json.dumps(normalized_input))
            span.set_attribute(TdSpanAttributes.OUTPUT_VALUE, json.dumps(normalized_output))

            # Set schema merges (schemas will be generated at export time)
            from ..wsgi.utilities import _schema_merges_to_dict

            input_schema_merges_dict = _schema_merges_to_dict(input_schema_merges)
            output_schema_merges_dict = _schema_merges_to_dict(output_schema_merges)

            span.set_attribute(TdSpanAttributes.INPUT_SCHEMA_MERGES, json.dumps(input_schema_merges_dict))
            span.set_attribute(TdSpanAttributes.OUTPUT_SCHEMA_MERGES, json.dumps(output_schema_merges_dict))

            # Set transform metadata if present
            if transform_metadata:
                span.set_attribute(TdSpanAttributes.TRANSFORM_METADATA, json.dumps(transform_metadata))

            # Set status
            if status.code == StatusCode.ERROR:
                span.set_status(Status(OTelStatusCode.ERROR, status.message))
            else:
                span.set_status(Status(OTelStatusCode.OK))

        except Exception as e:
            logger.error(f"Error finalizing span for {method} {url}: {e}")
            span.set_status(Status(OTelStatusCode.ERROR, str(e)))
