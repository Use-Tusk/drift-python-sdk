"""Instrumentation for aiohttp HTTP client library."""

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
from ...core.tracing.span_utils import CreateSpanOptions, SpanInfo, SpanUtils
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


class AiohttpInstrumentation(InstrumentationBase):
    """Instrumentation for the aiohttp HTTP client library.

    Patches aiohttp.ClientSession._request() to:
    - Intercept HTTP requests in REPLAY mode and return mocked responses
    - Capture request/response data as CLIENT spans in RECORD mode

    aiohttp is an async HTTP client library commonly used for making
    async HTTP requests in Python asyncio applications.
    """

    def __init__(self, enabled: bool = True, transforms: dict[str, Any] | None = None) -> None:
        self._transform_engine = HttpTransformEngine(self._resolve_http_transforms(transforms))
        super().__init__(
            name="AiohttpInstrumentation",
            module_name="aiohttp",
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
        """Patch the aiohttp module."""
        # Patch ClientSession._request
        if hasattr(module, "ClientSession"):
            self._patch_client_session(module)
        else:
            logger.warning("aiohttp.ClientSession not found, skipping instrumentation")

    def _get_default_response(self, aiohttp_module: Any, method: str, url: str) -> Any:
        """Return default response for background requests in REPLAY mode.

        Background requests (health checks, metrics, etc.) that happen outside
        of any trace context should return a default response instead of failing.
        """
        # Create a minimal mock response
        return _MockClientResponse(
            method=method,
            url=url,
            status=200,
            headers={},
            body=b"",
        )

    def _create_client_span(self, method: str, url: str, is_pre_app_start: bool) -> SpanInfo | None:
        """Create a client span for HTTP requests.

        Args:
            method: HTTP method
            url: Request URL
            is_pre_app_start: Whether this is before app start

        Returns:
            SpanInfo if successful, None if span creation failed
        """
        parsed_url = urlparse(url)
        span_name = f"{method} {parsed_url.path or '/'}"

        return SpanUtils.create_span(
            CreateSpanOptions(
                name=span_name,
                kind=OTelSpanKind.CLIENT,
                attributes={
                    TdSpanAttributes.NAME: span_name,
                    TdSpanAttributes.PACKAGE_NAME: parsed_url.scheme,
                    TdSpanAttributes.INSTRUMENTATION_NAME: "AiohttpInstrumentation",
                    TdSpanAttributes.SUBMODULE_NAME: method,
                    TdSpanAttributes.PACKAGE_TYPE: PackageType.HTTP.name,
                    TdSpanAttributes.IS_PRE_APP_START: is_pre_app_start,
                },
                is_pre_app_start=is_pre_app_start,
            )
        )

    def _patch_client_session(self, module: Any) -> None:
        """Patch aiohttp.ClientSession._request for async HTTP calls."""
        original_request = module.ClientSession._request
        instrumentation_self = self

        async def patched_request(
            client_self,
            method: str,
            str_or_url,
            **kwargs,
        ):
            """Patched ClientSession._request method."""
            # Convert URL to string
            url_str = str(str_or_url)
            sdk = TuskDrift.get_instance()

            # Pass through if SDK is disabled
            if sdk.mode == TuskDriftMode.DISABLED:
                return await original_request(client_self, method, str_or_url, **kwargs)

            # Set calling_library_context to suppress socket instrumentation warnings
            # for internal socket calls (e.g., aiohappyeyeballs connection management)
            context_token = calling_library_context.set("aiohttp")
            try:

                async def original_call():
                    return await original_request(client_self, method, str_or_url, **kwargs)

                # REPLAY mode
                if sdk.mode == TuskDriftMode.REPLAY:
                    return await handle_replay_mode(
                        replay_mode_handler=lambda: instrumentation_self._handle_replay_request(
                            sdk, module, method, url_str, **kwargs
                        ),
                        no_op_request_handler=lambda: instrumentation_self._get_default_response(
                            module, method, url_str
                        ),
                        is_server_request=False,
                    )

                # RECORD mode
                return await handle_record_mode(
                    original_function_call=original_call,
                    record_mode_handler=lambda is_pre_app_start: instrumentation_self._handle_record_request(
                        client_self,
                        method,
                        str_or_url,
                        is_pre_app_start,
                        original_request,
                        **kwargs,
                    ),
                    span_kind=OTelSpanKind.CLIENT,
                )
            finally:
                calling_library_context.reset(context_token)

        # Apply patch
        module.ClientSession._request = patched_request
        logger.info("aiohttp.ClientSession._request instrumented")

    async def _handle_replay_request(
        self,
        sdk: TuskDrift,
        aiohttp_module: Any,
        method: str,
        url: str,
        **kwargs,
    ) -> Any:
        """Handle request in REPLAY mode (async).

        Creates a span, fetches mock response.
        Raises RuntimeError if no mock is found.
        """
        span_info = self._create_client_span(method, url, not sdk.app_ready)
        if not span_info:
            raise RuntimeError(f"Error creating span in replay mode for {method} {url}")

        try:
            with SpanUtils.with_span(span_info):
                mock_response = await self._try_get_mock(
                    sdk,
                    aiohttp_module,
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

    async def _handle_record_request(
        self,
        client_self: Any,
        method: str,
        str_or_url: Any,
        is_pre_app_start: bool,
        original_request: Any,
        **kwargs,
    ) -> Any:
        """Handle request in RECORD mode (async).

        Creates a span, makes the real request, and records the response.
        """
        url_str = str(str_or_url)

        span_info = self._create_client_span(method, url_str, is_pre_app_start)
        if not span_info:
            # Span creation failed (trace blocked, etc.) - just make the request
            return await original_request(client_self, method, str_or_url, **kwargs)

        try:
            with SpanUtils.with_span(span_info):
                # Check drop transforms BEFORE making the request
                headers = dict(kwargs.get("headers", {}))
                if self._transform_engine and self._transform_engine.should_drop_outbound_request(
                    method, url_str, headers
                ):
                    # Request should be dropped - mark span and raise exception
                    span_info.span.set_attribute(
                        TdSpanAttributes.OUTPUT_VALUE,
                        json.dumps({"bodyProcessingError": "dropped"}),
                    )
                    span_info.span.set_status(Status(OTelStatusCode.ERROR, "Dropped by transform"))
                    raise RequestDroppedByTransform(
                        f"Outbound request to {url_str} was dropped by transform rule",
                        method,
                        url_str,
                    )

                # Capture request body before send
                pre_captured_body = self._get_request_body_safely(kwargs)

                # Make the real request
                error = None
                response = None

                try:
                    response = await original_request(client_self, method, str_or_url, **kwargs)
                    return response
                except Exception as e:
                    error = e
                    raise
                finally:
                    # Finalize span with request/response data
                    await self._finalize_span(
                        span_info.span,
                        method,
                        url_str,
                        kwargs,
                        response,
                        error,
                        pre_captured_body,
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

        # Common content type mappings
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

    def _get_request_body_safely(self, kwargs: dict) -> bytes | None:
        """Safely get request body content from kwargs.

        aiohttp supports various body formats:
        - data: bytes, str, or dict (form data)
        - json: dict (will be JSON-encoded)

        Args:
            kwargs: Request kwargs

        Returns:
            bytes content or None if unavailable
        """
        try:
            # Check for json parameter first
            if "json" in kwargs:
                json_data = kwargs["json"]
                if json_data is not None:
                    return json.dumps(json_data).encode("utf-8")

            # Check for data parameter
            if "data" in kwargs:
                data = kwargs["data"]
                if data is None:
                    return None
                if isinstance(data, bytes):
                    return data
                if isinstance(data, str):
                    return data.encode("utf-8")
                if isinstance(data, dict):
                    # Form data - encode as url-encoded
                    from urllib.parse import urlencode

                    return urlencode(data).encode("utf-8")

            return None
        except Exception:
            return None

    async def _try_get_mock(
        self,
        sdk: TuskDrift,
        aiohttp_module: Any,
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
            parsed_url = urlparse(url)

            # Extract headers from kwargs
            headers = dict(kwargs.get("headers", {}))
            # Strip auth-related headers for consistent matching
            headers.pop("authorization", None)
            headers.pop("Authorization", None)
            headers.pop("cookie", None)
            headers.pop("Cookie", None)

            # Extract query params from URL
            params = {}
            if parsed_url.query:
                from urllib.parse import parse_qs

                params = {k: v[0] if len(v) == 1 else v for k, v in parse_qs(parsed_url.query).items()}

            # Get body from kwargs
            body_base64 = None
            body_size = 0
            request_body = self._get_request_body_safely(kwargs)
            if request_body:
                body_base64, body_size = self._encode_body_to_base64(request_body)

            raw_input_value = {
                "method": method,
                "url": url,
                "protocol": parsed_url.scheme,
                "hostname": parsed_url.hostname,
                "port": parsed_url.port,
                "path": parsed_url.path or "/",
                "headers": headers,
                "query": params,
            }

            # Add body fields only if body exists
            if body_base64 is not None:
                raw_input_value["body"] = body_base64
                raw_input_value["bodySize"] = body_size

            input_value = create_mock_input_value(raw_input_value)

            # Create schema merge hints for input
            input_schema_merges = {
                "headers": SchemaMerge(match_importance=0.0),
            }
            if body_base64 is not None:
                request_content_type = self._get_content_type_header(headers)
                input_schema_merges["body"] = SchemaMerge(
                    encoding=EncodingType.BASE64,
                    decoded_type=self._get_decoded_type_from_content_type(request_content_type),
                )

            # Use centralized mock finding utility
            from ...core.mock_utils import find_mock_response_sync

            mock_response_output = find_mock_response_sync(
                sdk=sdk,
                trace_id=trace_id,
                span_id=span_id,
                name=f"{method} {parsed_url.path or '/'}",
                package_name=parsed_url.scheme,
                package_type=PackageType.HTTP,
                instrumentation_name="AiohttpInstrumentation",
                submodule_name=method,
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
            return self._create_mock_response(aiohttp_module, mock_response_output.response, method, url)

        except Exception as e:
            logger.error(f"Error getting mock for {method} {url}: {e}")
            return None

    def _create_mock_response(self, aiohttp_module: Any, mock_data: dict[str, Any], method: str, url: str) -> Any:
        """Create a mocked aiohttp.ClientResponse-like object.

        Args:
            aiohttp_module: The aiohttp module
            mock_data: Mock response data from CLI
            method: HTTP method
            url: Request URL

        Returns:
            Mocked response object
        """
        # Get status code and headers
        status_code = mock_data.get("statusCode", 200)
        headers = dict(mock_data.get("headers", {}))

        # Remove content-encoding and transfer-encoding headers since the body
        # was already decompressed when recorded
        headers_to_remove = []
        for key in headers:
            if key.lower() in ("content-encoding", "transfer-encoding"):
                headers_to_remove.append(key)
        for key in headers_to_remove:
            del headers[key]

        # Get body - decode from base64 if needed
        body = mock_data.get("body", "")
        content = b""
        if isinstance(body, str):
            try:
                # Try to decode as base64
                decoded = base64.b64decode(body.encode("ascii"), validate=True)
                if base64.b64encode(decoded).decode("ascii") == body:
                    content = decoded
                else:
                    content = body.encode("utf-8")
            except Exception:
                content = body.encode("utf-8")
        elif isinstance(body, bytes):
            content = body
        else:
            content = json.dumps(body).encode("utf-8")

        # Determine final URL - use from mock data if present (for redirect handling)
        final_url = mock_data.get("finalUrl", url)

        # Create mock response
        response = _MockClientResponse(
            method=method,
            url=final_url,
            status=status_code,
            headers=headers,
            body=content,
        )

        logger.debug(f"Created mock aiohttp response: {status_code} for {final_url}")
        return response

    async def _finalize_span(
        self,
        span: Span,
        method: str,
        url: str,
        kwargs: dict,
        response: Any,
        error: Exception | None,
        pre_captured_body: bytes | None = None,
    ) -> None:
        """Finalize span with request/response data.

        Args:
            span: The OpenTelemetry span to finalize
            method: HTTP method
            url: Request URL
            kwargs: Request kwargs
            response: Response object (if successful)
            error: Exception (if failed)
            pre_captured_body: Pre-captured request body bytes
        """
        try:
            parsed_url = urlparse(url)

            # ===== BUILD INPUT VALUE =====
            headers = dict(kwargs.get("headers", {}))
            # Strip auth-related headers for consistent matching
            headers.pop("authorization", None)
            headers.pop("Authorization", None)
            headers.pop("cookie", None)
            headers.pop("Cookie", None)

            # Extract query params from URL
            params = {}
            if parsed_url.query:
                from urllib.parse import parse_qs

                params = {k: v[0] if len(v) == 1 else v for k, v in parse_qs(parsed_url.query).items()}

            # Get request body
            body_base64 = None
            body_size = 0
            request_body = pre_captured_body if pre_captured_body is not None else self._get_request_body_safely(kwargs)
            if request_body:
                body_base64, body_size = self._encode_body_to_base64(request_body)

            input_value = {
                "method": method,
                "url": url,
                "protocol": parsed_url.scheme,
                "hostname": parsed_url.hostname,
                "port": parsed_url.port,
                "path": parsed_url.path or "/",
                "headers": headers,
                "query": params,
            }

            # Add body fields only if body exists
            if body_base64 is not None:
                input_value["body"] = body_base64
                input_value["bodySize"] = body_size

            # ===== BUILD OUTPUT VALUE =====
            output_value = {}
            status = SpanStatus(code=StatusCode.OK, message="")
            response_body_base64 = None

            if error:
                output_value = {
                    "errorName": type(error).__name__,
                    "errorMessage": str(error),
                }
                status = SpanStatus(code=StatusCode.ERROR, message=str(error))
            elif response:
                # Extract response data
                response_headers = {}
                if hasattr(response, "headers"):
                    response_headers = dict(response.headers)

                response_body_size = 0

                try:
                    # aiohttp ClientResponse requires reading the body
                    # Check if body has already been read
                    if hasattr(response, "_body") and response._body is not None:
                        response_bytes = response._body
                    elif hasattr(response, "read"):
                        # Read the response body
                        response_bytes = await response.read()
                    else:
                        response_bytes = b""

                    response_body_base64, response_body_size = self._encode_body_to_base64(response_bytes)
                except Exception:
                    response_body_base64 = None
                    response_body_size = 0

                status_code = response.status if hasattr(response, "status") else 200

                output_value = {
                    "statusCode": status_code,
                    "statusMessage": response.reason if hasattr(response, "reason") else "",
                    "headers": response_headers,
                }

                # Add body fields only if body exists
                if response_body_base64 is not None:
                    output_value["body"] = response_body_base64
                    output_value["bodySize"] = response_body_size

                # Capture redirect information
                if hasattr(response, "url"):
                    final_url = str(response.url)
                    if final_url != url:  # Only store if redirects occurred
                        output_value["finalUrl"] = final_url

                if hasattr(response, "history") and response.history:
                    output_value["historyCount"] = len(response.history)

                if status_code >= 400:
                    status = SpanStatus(
                        code=StatusCode.ERROR,
                        message=f"HTTP {status_code}",
                    )

                # Check if response content type should block the trace
                from ...core.content_type_utils import get_decoded_type, should_block_content_type
                from ...core.trace_blocking_manager import TraceBlockingManager

                response_content_type = response_headers.get("content-type") or response_headers.get("Content-Type")
                decoded_type = get_decoded_type(response_content_type)

                if should_block_content_type(decoded_type):
                    span_context = span.get_span_context()
                    trace_id = format(span_context.trace_id, "032x")

                    blocking_mgr = TraceBlockingManager.get_instance()
                    blocking_mgr.block_trace(
                        trace_id, reason=f"outbound_binary:{decoded_type.name if decoded_type else 'unknown'}"
                    )
                    logger.warning(
                        f"Blocking trace {trace_id} - outbound request returned binary response: {response_content_type}"
                    )
                    return
            else:
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

                input_value = span_data.input_value or input_value
                output_value = span_data.output_value or output_value
                transform_metadata = span_data.transform_metadata

            # ===== CREATE SCHEMA MERGE HINTS =====
            request_content_type = self._get_content_type_header(headers)
            response_content_type = None
            if response and hasattr(response, "headers"):
                response_content_type = self._get_content_type_header(dict(response.headers))

            input_schema_merges = {
                "headers": SchemaMerge(match_importance=0.0),
            }
            if body_base64 is not None:
                input_schema_merges["body"] = SchemaMerge(
                    encoding=EncodingType.BASE64,
                    decoded_type=self._get_decoded_type_from_content_type(request_content_type),
                )

            output_schema_merges = {
                "headers": SchemaMerge(match_importance=0.0),
            }
            if response_body_base64 is not None:
                output_schema_merges["body"] = SchemaMerge(
                    encoding=EncodingType.BASE64,
                    decoded_type=self._get_decoded_type_from_content_type(response_content_type),
                )

            # ===== SET SPAN ATTRIBUTES =====
            normalized_input = remove_none_values(input_value)
            normalized_output = remove_none_values(output_value)
            span.set_attribute(TdSpanAttributes.INPUT_VALUE, json.dumps(normalized_input))
            span.set_attribute(TdSpanAttributes.OUTPUT_VALUE, json.dumps(normalized_output))

            from ..wsgi.utilities import _schema_merges_to_dict

            input_schema_merges_dict = _schema_merges_to_dict(input_schema_merges)
            output_schema_merges_dict = _schema_merges_to_dict(output_schema_merges)

            span.set_attribute(TdSpanAttributes.INPUT_SCHEMA_MERGES, json.dumps(input_schema_merges_dict))
            span.set_attribute(TdSpanAttributes.OUTPUT_SCHEMA_MERGES, json.dumps(output_schema_merges_dict))

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


class _MockClientResponse:
    """Mock aiohttp ClientResponse for REPLAY mode.

    This class mimics the aiohttp.ClientResponse interface to provide
    a mock response that can be used in place of a real response.
    """

    def __init__(
        self,
        method: str,
        url: str,
        status: int,
        headers: dict,
        body: bytes,
    ):
        self.method = method
        self._url = url
        self.status = status
        self._headers = headers
        self._body = body
        self.reason = "OK" if status < 400 else "Error"
        self.history: list = []

    @property
    def url(self):
        """Return URL as a yarl.URL-like object."""
        from urllib.parse import urlparse

        parsed = urlparse(self._url)
        return _MockURL(self._url, parsed)

    @property
    def headers(self):
        """Return headers as a dict-like object."""
        return self._headers

    @property
    def content_type(self) -> str:
        """Return content type from headers."""
        return self._headers.get("content-type", "application/octet-stream")

    async def read(self) -> bytes:
        """Read response body."""
        return self._body

    async def text(self, encoding: str = "utf-8") -> str:
        """Read response body as text."""
        return self._body.decode(encoding)

    async def json(self, **kwargs) -> Any:
        """Read response body as JSON."""
        return json.loads(self._body)

    def release(self) -> None:
        """Release the response (no-op for mock)."""
        pass

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        self.release()


class _MockURL:
    """Mock yarl.URL-like object for mock responses."""

    def __init__(self, url_str: str, parsed):
        self._url_str = url_str
        self._parsed = parsed

    def __str__(self):
        return self._url_str

    @property
    def scheme(self):
        return self._parsed.scheme

    @property
    def host(self):
        return self._parsed.hostname

    @property
    def port(self):
        return self._parsed.port

    @property
    def path(self):
        return self._parsed.path

    @property
    def query_string(self):
        return self._parsed.query
