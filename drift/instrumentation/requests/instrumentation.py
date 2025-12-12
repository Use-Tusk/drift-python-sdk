"""Instrumentation for requests HTTP client library."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
import traceback
from typing import Any, Dict, Optional
from urllib.parse import urlparse


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

from ..base import InstrumentationBase
from ..http import HttpSpanData, HttpTransformEngine
from ...core.communication.types import MockRequestInput
from ...core.data_normalization import create_mock_input_value
from ...core.drift_sdk import TuskDrift
from ...core.json_schema_helper import JsonSchemaHelper, SchemaMerge, EncodingType, DecodedType
from ...core.types import (
    CleanSpanData,
    PackageType,
    SpanKind,
    SpanStatus,
    StatusCode,
    replay_trace_id_context,
    current_trace_id_context,
    current_span_id_context,
)

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

    def __init__(self, enabled: bool = True, transforms: Dict[str, Any] | None = None) -> None:
        self._transform_engine = HttpTransformEngine(
            self._resolve_http_transforms(transforms)
        )
        super().__init__(
            name="RequestsInstrumentation",
            module_name="requests",
            supported_versions="*",
            enabled=enabled,
        )

    def _resolve_http_transforms(
        self, provided: Dict[str, Any] | list[Dict[str, Any]] | None
    ) -> list[Dict[str, Any]] | None:
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

        def patched_request(session_self, method: str, url: str, **kwargs):
            """Patched Session.request method."""
            sdk = TuskDrift.get_instance()

            # Pass through if SDK is disabled
            if sdk.mode == "DISABLED":
                return original_request(session_self, method, url, **kwargs)

            # Get trace context from parent span (if exists) or generate new IDs for root span
            parent_trace_id = current_trace_id_context.get()
            parent_span_id = current_span_id_context.get()

            if parent_trace_id:
                # Child span: inherit parent's trace_id
                trace_id = parent_trace_id
            else:
                # Root span: generate new trace_id
                trace_id = self._generate_trace_id()

            # Always generate a new span_id for this CLIENT span
            span_id = self._generate_span_id()

            # Set this span as the current context for any nested children
            trace_token = current_trace_id_context.set(trace_id)
            span_token = current_span_id_context.set(span_id)

            try:
                stack_trace = self._get_stack_trace()

                # REPLAY mode: Try to get mock
                if sdk.mode == "REPLAY":
                    mock_response = self._try_get_mock(
                        sdk, method, url, trace_id, span_id, stack_trace, **kwargs
                    )
                    if mock_response is not None:
                        return mock_response

                # ===== CHECK DROP TRANSFORMS (matches Node SDK) =====
                # Check BEFORE making the HTTP request to prevent network traffic
                if self._transform_engine and self._transform_engine.should_drop_outbound_request(
                    method.upper(), url, kwargs.get("headers", {})
                ):
                    # Request should be dropped - create span and raise exception (matches Node SDK)
                    start_time = time.time()
                    duration_ms = (time.time() - start_time) * 1000

                    # Create dropped span (will be marked as dropped by transforms)
                    self._create_span(
                        sdk,
                        method,
                        url,
                        trace_id,
                        span_id,
                        parent_span_id,
                        duration_ms,
                        None,  # No response
                        None,  # No error
                        kwargs,
                        is_dropped_by_transform=True,  # Mark as dropped (matches Node SDK)
                    )

                    # Raise exception to indicate drop (matches Node SDK behavior)
                    # The application receives an error, not a fake response
                    raise RequestDroppedByTransform(
                        f"Outbound request to {url} was dropped by transform rule",
                        method.upper(),
                        url,
                    )

                # RECORD mode or mock not found: Make real request
                start_time = time.time()
                error = None
                response = None

                try:
                    response = original_request(session_self, method, url, **kwargs)
                    return response
                except Exception as e:
                    error = e
                    raise
                finally:
                    # Create span in RECORD mode or if mock was not found
                    if sdk.mode == "RECORD" or (sdk.mode == "REPLAY" and response is not None):
                        duration_ms = (time.time() - start_time) * 1000
                        self._create_span(
                            sdk,
                            method,
                            url,
                            trace_id,
                            span_id,
                            parent_span_id,  # Pass parent span ID
                            duration_ms,
                            response,
                            error,
                            kwargs,
                        )
            finally:
                # Reset trace context
                current_trace_id_context.reset(trace_token)
                current_span_id_context.reset(span_token)

        # Apply patch
        module.Session.request = patched_request
        logger.info("requests.Session.request instrumented")

    def _encode_body_to_base64(self, body_data: Any) -> tuple[Optional[str], int]:
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

    def _get_decoded_type_from_content_type(self, content_type: Optional[str]) -> Optional[DecodedType]:
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

    def _get_content_type_header(self, headers: dict) -> Optional[str]:
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
        stack_trace: str,
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

            # ===== GENERATE SCHEMAS AND HASHES (matches Node SDK) =====
            # Create schema merge hints for input (same as in _create_span)
            input_schema_merges = {
                "headers": SchemaMerge(match_importance=0.0),
            }
            if body_base64 is not None:
                request_content_type = self._get_content_type_header(headers)
                input_schema_merges["body"] = SchemaMerge(
                    encoding=EncodingType.BASE64,
                    decoded_type=self._get_decoded_type_from_content_type(request_content_type),
                )

            # Generate schema and hashes for CLI matching
            input_result = JsonSchemaHelper.generate_schema_and_hash(input_value, input_schema_merges)

            # Create mock span for matching
            # Convert timestamp from milliseconds to Timestamp object
            timestamp_ms = time.time() * 1000
            timestamp_seconds = int(timestamp_ms // 1000)
            timestamp_nanos = int((timestamp_ms % 1000) * 1_000_000)

            from ...core.types import Timestamp, Duration

            mock_span = CleanSpanData(
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=None,
                name=f"{method.upper()} {parsed_url.path or '/'}",
                package_name=parsed_url.scheme,
                package_type=PackageType.HTTP,
                instrumentation_name="RequestsInstrumentation",
                submodule_name=method.upper(),
                input_value=input_value,
                output_value=None,
                # ===== ADD SCHEMA/HASH METADATA (matches Node SDK) =====
                input_schema=input_result.schema,
                input_schema_hash=input_result.decoded_schema_hash,
                input_value_hash=input_result.decoded_value_hash,
                # ===== INCLUDE STACK TRACE FOR CLI ALERTS =====
                stack_trace=stack_trace,
                kind=SpanKind.CLIENT,
                status=SpanStatus(code=StatusCode.UNSPECIFIED, message=""),
                timestamp=Timestamp(seconds=timestamp_seconds, nanos=timestamp_nanos),
                duration=Duration(seconds=0, nanos=0),
                is_root_span=False,
                is_pre_app_start=False,
            )

            # Request mock from CLI (synchronous for requests library)
            # Get replay trace ID from context (matches Node.js behavior)
            replay_trace_id = replay_trace_id_context.get()

            mock_request = MockRequestInput(
                test_id=replay_trace_id or "",  # ✅ Uses replay trace ID from context
                outbound_span=mock_span,
            )

            mock_response_output = sdk.request_mock_sync(mock_request)

            if not mock_response_output.found:
                logger.debug(
                    f"No mock found for {method} {url} "
                    f"(replay_trace_id={replay_trace_id}, span_trace_id={trace_id})"
                )
                return None

            # Create mocked response object
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

    def _create_span(
        self,
        sdk: TuskDrift,
        method: str,
        url: str,
        trace_id: str,
        span_id: str,
        parent_span_id: Optional[str],
        duration_ms: float,
        response: Any,
        error: Exception | None,
        request_kwargs: dict[str, Any],
        is_dropped_by_transform: bool = False,
    ) -> None:
        """Create and collect a CLIENT span for the HTTP request.

        Args:
            sdk: TuskDrift instance
            method: HTTP method
            url: Request URL
            trace_id: Trace ID (from parent or generated)
            span_id: Span ID (newly generated for this span)
            parent_span_id: Parent span ID (from context, or None for root span)
            duration_ms: Request duration in milliseconds
            response: Response object (if successful)
            error: Exception (if failed)
            request_kwargs: Original request kwargs
            is_dropped_by_transform: If True, request was dropped by transform (matches Node SDK)
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
            response_body_truncated = False  # Track if response body was truncated

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
                    response_bytes = response.content

                    # Truncate if needed (10KB limit) - matches Node SDK behavior
                    if len(response_bytes) > 10240:
                        response_bytes = response_bytes[:10240]
                        response_body_truncated = True

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
                    # Flag truncated bodies (matches Node SDK httpBodyEncoder)
                    if response_body_truncated:
                        output_value["bodyProcessingError"] = "truncated"

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
                    blocking_mgr = TraceBlockingManager.get_instance()
                    blocking_mgr.block_trace(
                        trace_id,
                        reason=f"outbound_binary:{decoded_type.name if decoded_type else 'unknown'}"
                    )
                    logger.warning(
                        f"Blocking trace {trace_id} - outbound request returned binary response: {response_content_type} "
                        f"(decoded as {decoded_type.name if decoded_type else 'unknown'})"
                    )
                    return  # Skip span creation
            else:
                # No response and no error - this happens when request is dropped by transforms
                output_value = {}

            # ===== APPLY TRANSFORMS OR MARK AS DROPPED =====
            transform_metadata = None
            if is_dropped_by_transform:
                # Request was dropped - create metadata showing drop action (matches Node SDK)
                from ...core.types import TransformMetadata, TransformAction

                # Set output to indicate drop
                output_value = {
                    "bodyProcessingError": "dropped",  # Matches Node SDK marker
                }

                # Set status to ERROR to indicate drop
                status = SpanStatus(code=StatusCode.ERROR, message="Dropped by transform")

                # Create transform metadata with drop action
                transform_metadata = TransformMetadata(
                    transformed=True,
                    actions=[
                        TransformAction(
                            type="drop",
                            field="entire_span",
                            reason="transforms",
                            description="Request dropped by outbound transform rule",
                        )
                    ],
                )
            elif self._transform_engine:
                # Normal transform application (not dropped)
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
            # Add bodyProcessingError to schema merges if truncated (matches Node SDK)
            if response_body_truncated:
                output_schema_merges["bodyProcessingError"] = SchemaMerge(match_importance=1.0)

            # ===== GENERATE SCHEMAS AND HASHES =====
            input_result = JsonSchemaHelper.generate_schema_and_hash(input_value, input_schema_merges)
            output_result = JsonSchemaHelper.generate_schema_and_hash(output_value, output_schema_merges)

            # ===== CREATE TIMESTAMP AND DURATION =====
            from ...core.types import Timestamp, Duration

            timestamp_ms = time.time() * 1000
            timestamp_seconds = int(timestamp_ms // 1000)
            timestamp_nanos = int((timestamp_ms % 1000) * 1_000_000)

            duration_seconds = int(duration_ms // 1000)
            duration_nanos = int((duration_ms % 1000) * 1_000_000)

            # ===== CREATE SPAN =====
            span = CleanSpanData(
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id,  # Use parent from context
                name=f"{method.upper()} {parsed_url.path or '/'}",
                package_name=parsed_url.scheme,
                package_type=PackageType.HTTP,
                instrumentation_name="RequestsInstrumentation",
                submodule_name=method.upper(),
                input_value=input_value,
                output_value=output_value,
                input_schema=input_result.schema,
                output_schema=output_result.schema,
                input_schema_hash=input_result.decoded_schema_hash,
                output_schema_hash=output_result.decoded_schema_hash,
                input_value_hash=input_result.decoded_value_hash,
                output_value_hash=output_result.decoded_value_hash,
                kind=SpanKind.CLIENT,
                status=status,
                timestamp=Timestamp(seconds=timestamp_seconds, nanos=timestamp_nanos),
                duration=Duration(seconds=duration_seconds, nanos=duration_nanos),
                is_root_span=parent_span_id is None,  # Root span if no parent
                is_pre_app_start=not sdk.app_ready,
                transform_metadata=transform_metadata,  # ✅ Include transform metadata
                is_used=False if is_dropped_by_transform else None,  # Mark dropped spans as unused (matches Node SDK)
            )

            sdk.collect_span(span)

        except Exception as e:
            logger.error(f"Error creating span for {method} {url}: {e}")

    def _generate_trace_id(self) -> str:
        """Generate a random trace ID."""
        import secrets
        return secrets.token_hex(16)

    def _generate_span_id(self) -> str:
        """Generate a random span ID."""
        import secrets
        return secrets.token_hex(8)

    def _get_stack_trace(self) -> str:
        """Get the current stack trace."""
        stack = traceback.format_stack()
        # Filter out instrumentation frames
        filtered = [
            line for line in stack
            if "instrumentation" not in line and "drift" not in line
        ]
        return "".join(filtered[-10:])  # Last 10 frames
