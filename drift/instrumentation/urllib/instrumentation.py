"""Instrumentation for urllib.request HTTP client library."""

from __future__ import annotations

import base64
import email
import json
import logging
import socket
from io import BytesIO
from typing import Any
from urllib.parse import parse_qs, urlparse

# socket._GLOBAL_DEFAULT_TIMEOUT is a sentinel object used by urllib
# The type checker doesn't recognize it, so we access it via getattr
_GLOBAL_DEFAULT_TIMEOUT: object = getattr(socket, "_GLOBAL_DEFAULT_TIMEOUT")  # noqa: B009

from opentelemetry.trace import Span, Status
from opentelemetry.trace import SpanKind as OTelSpanKind
from opentelemetry.trace import StatusCode as OTelStatusCode


class RequestDroppedByTransform(Exception):
    """Exception raised when an outbound HTTP request is dropped by a transform rule.

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
)
from ..base import InstrumentationBase
from ..http import HttpSpanData, HttpTransformEngine

logger = logging.getLogger(__name__)

# Schema merge hints for headers (low match importance)
HEADER_SCHEMA_MERGES = {
    "headers": SchemaMerge(match_importance=0.0),
}


class ResponseWrapper:
    """Wrapper for urllib response that caches the body for re-reading.

    This wrapper reads and caches the entire response body on first access,
    then provides the cached body to all subsequent reads. This allows the
    instrumentation to capture the response body while still allowing the
    application code to read the response normally.
    """

    def __init__(self, response: Any):
        self._response = response
        self._body: bytes | None = None
        self._fp: BytesIO | None = None

        # Copy attributes from the original response
        self.status = getattr(response, "status", getattr(response, "code", 200))
        self.code = self.status
        self.reason = getattr(response, "reason", getattr(response, "msg", "OK"))
        self.msg = self.reason
        self.url = getattr(response, "url", "")

    def _ensure_body_cached(self) -> None:
        """Read and cache the body from the original response if not already done."""
        if self._body is None:
            self._body = self._response.read()
            self._fp = BytesIO(self._body)

    def read(self, amt: int | None = None) -> bytes:
        """Read response body from cache."""
        self._ensure_body_cached()
        assert self._fp is not None  # Guaranteed by _ensure_body_cached
        if amt is None:
            return self._fp.read()
        return self._fp.read(amt)

    def readline(self) -> bytes:
        """Read a line from response body."""
        self._ensure_body_cached()
        assert self._fp is not None  # Guaranteed by _ensure_body_cached
        return self._fp.readline()

    def readlines(self) -> list[bytes]:
        """Read all lines from response body."""
        self._ensure_body_cached()
        assert self._fp is not None  # Guaranteed by _ensure_body_cached
        return self._fp.readlines()

    def info(self) -> Any:
        """Return headers from original response."""
        return self._response.info()

    def geturl(self) -> str:
        """Return the URL of the response."""
        return self.url

    def getcode(self) -> int:
        """Return the HTTP status code."""
        return self.status

    def getheaders(self) -> list[tuple[str, str]]:
        """Return headers as list of tuples."""
        if hasattr(self._response, "getheaders"):
            return self._response.getheaders()
        info = self.info()
        if info:
            return list(info.items())
        return []

    def getheader(self, name: str, default: str | None = None) -> str | None:
        """Get a specific header value."""
        if hasattr(self._response, "getheader"):
            return self._response.getheader(name, default)
        info = self.info()
        if info:
            return info.get(name, default)
        return default

    def fileno(self) -> int:
        """Return file descriptor."""
        if hasattr(self._response, "fileno"):
            return self._response.fileno()
        return -1

    def get_cached_body(self) -> bytes:
        """Get the cached body bytes (for instrumentation use)."""
        self._ensure_body_cached()
        assert self._body is not None  # Guaranteed by _ensure_body_cached
        return self._body

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, *args):
        """Context manager exit."""
        self.close()

    def close(self):
        """Close the response."""
        if hasattr(self._response, "close"):
            self._response.close()

    def __iter__(self):
        """Iterate over response lines."""
        self._ensure_body_cached()
        return self

    def __next__(self) -> bytes:
        """Get next line."""
        self._ensure_body_cached()
        assert self._fp is not None  # Guaranteed by _ensure_body_cached
        line = self._fp.readline()
        if not line:
            raise StopIteration
        return line

    def __getattr__(self, name: str) -> Any:
        """Forward any other attributes to the wrapped response."""
        return getattr(self._response, name)


class MockHTTPResponse:
    """Mock HTTP response compatible with urllib.request expectations.

    This class mimics http.client.HTTPResponse with the modifications
    that urllib.request makes (adding .url and .msg attributes).
    """

    def __init__(
        self,
        status_code: int,
        reason: str,
        headers: dict[str, str],
        body: bytes,
        url: str,
    ):
        self.status = status_code
        self.code = status_code  # urllib uses .code
        self.reason = reason
        self.msg = reason  # urllib sets .msg = .reason
        self.url = url
        self._headers = headers
        self._body = body
        self._fp = BytesIO(body)

    def read(self, amt: int | None = None) -> bytes:
        """Read response body."""
        if amt is None:
            return self._fp.read()
        return self._fp.read(amt)

    def readline(self) -> bytes:
        """Read a line from response body."""
        return self._fp.readline()

    def readlines(self) -> list[bytes]:
        """Read all lines from response body."""
        return self._fp.readlines()

    def info(self) -> email.message.Message:
        """Return headers as email.message.Message (urllib convention)."""
        header_str = "\r\n".join(f"{k}: {v}" for k, v in self._headers.items())
        return email.message_from_string(header_str)

    def geturl(self) -> str:
        """Return the URL of the response."""
        return self.url

    def getcode(self) -> int:
        """Return the HTTP status code."""
        return self.status

    def getheaders(self) -> list[tuple[str, str]]:
        """Return headers as list of tuples."""
        return list(self._headers.items())

    def getheader(self, name: str, default: str | None = None) -> str | None:
        """Get a specific header value (case-insensitive)."""
        name_lower = name.lower()
        for key, value in self._headers.items():
            if key.lower() == name_lower:
                return value
        return default

    def fileno(self) -> int:
        """Return -1 as there's no real file descriptor."""
        return -1

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, *args):
        """Context manager exit."""
        self.close()

    def close(self):
        """Close the response (no-op for mock)."""
        pass

    def __iter__(self):
        """Iterate over response lines."""
        return self

    def __next__(self) -> bytes:
        """Get next line."""
        line = self._fp.readline()
        if not line:
            raise StopIteration
        return line


class UrllibInstrumentation(InstrumentationBase):
    """Instrumentation for the urllib.request HTTP client library.

    Patches OpenerDirector.open() to:
    - Intercept HTTP requests in REPLAY mode and return mocked responses
    - Capture request/response data as CLIENT spans in RECORD mode

    We patch OpenerDirector.open() instead of urlopen() because all HTTP calls
    flow through OpenerDirector.open(), including custom opener usage via
    build_opener(). This ensures complete coverage.
    """

    def __init__(self, enabled: bool = True, transforms: dict[str, Any] | None = None) -> None:
        self._transform_engine = HttpTransformEngine(self._resolve_http_transforms(transforms))
        super().__init__(
            name="UrllibInstrumentation",
            module_name="urllib.request",
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
        """Patch the urllib.request module.

        Patches OpenerDirector.open() to intercept all HTTP requests.
        All urllib HTTP calls (urlopen(), custom openers, etc.) flow through
        OpenerDirector.open(), making it the ideal patching point.
        """
        if not hasattr(module, "OpenerDirector"):
            logger.warning("urllib.request.OpenerDirector not found, skipping instrumentation")
            return

        # Store original method
        original_open = module.OpenerDirector.open
        instrumentation_self = self

        def patched_open(opener_self, fullurl, data=None, timeout=_GLOBAL_DEFAULT_TIMEOUT):
            """Patched OpenerDirector.open method.

            Args:
                opener_self: OpenerDirector instance
                fullurl: URL string or Request object
                data: Optional request body data
                timeout: Request timeout
            """
            sdk = TuskDrift.get_instance()

            # Pass through if SDK is disabled
            if sdk.mode == TuskDriftMode.DISABLED:
                return original_open(opener_self, fullurl, data, timeout)

            # Set calling_library_context to suppress socket instrumentation warnings
            # context_token = calling_library_context.set("urllib")
            try:
                # Extract URL for default response handler
                if isinstance(fullurl, str):
                    url = fullurl
                else:
                    url = fullurl.full_url

                def original_call():
                    return original_open(opener_self, fullurl, data, timeout)

                # REPLAY mode: Use handle_replay_mode for proper background request handling
                if sdk.mode == TuskDriftMode.REPLAY:
                    return handle_replay_mode(
                        replay_mode_handler=lambda: instrumentation_self._handle_replay_open(
                            sdk, fullurl, data, timeout
                        ),
                        no_op_request_handler=lambda: instrumentation_self._get_default_response(url),
                        is_server_request=False,
                    )

                # RECORD mode: Use handle_record_mode for proper is_pre_app_start handling
                return handle_record_mode(
                    original_function_call=original_call,
                    record_mode_handler=lambda is_pre_app_start: instrumentation_self._handle_record_open(
                        opener_self, fullurl, data, timeout, is_pre_app_start, original_open
                    ),
                    span_kind=OTelSpanKind.CLIENT,
                )
            finally:
                # calling_library_context.reset(context_token)
                pass

        # Apply patch
        module.OpenerDirector.open = patched_open
        logger.info("urllib.request.OpenerDirector.open instrumented")

    def _extract_request_info(self, fullurl: Any, data: bytes | None) -> dict[str, Any]:
        """Extract request information from urlopen arguments.

        Args:
            fullurl: Either a URL string or a Request object
            data: Optional request body

        Returns:
            Dict with method, url, headers, body, etc.
        """
        from urllib.request import Request

        if isinstance(fullurl, str):
            req = Request(fullurl, data)
        else:
            req = fullurl
            if data is not None:
                req.data = data

        method = req.get_method()
        url = req.full_url
        # Combine both header dicts (unredirected_hdrs has precedence in urllib)
        headers = {**req.headers, **req.unredirected_hdrs}
        body = req.data

        return {
            "method": method,
            "url": url,
            "headers": headers,
            "body": body,
        }

    def _get_default_response(self, url: str) -> MockHTTPResponse:
        """Return default response for background requests in REPLAY mode.

        Background requests (health checks, metrics, etc.) that happen outside
        of any trace context should return a default response instead of failing.
        """
        logger.debug(f"[UrllibInstrumentation] Returning default response for background request to {url}")
        return MockHTTPResponse(
            status_code=200,
            reason="OK",
            headers={},
            body=b"",
            url=url,
        )

    def _handle_record_open(
        self,
        opener_self: Any,
        fullurl: Any,
        data: bytes | None,
        timeout: Any,
        is_pre_app_start: bool,
        original_open: Any,
    ) -> Any:
        """Handle OpenerDirector.open() in RECORD mode.

        Args:
            opener_self: OpenerDirector instance
            fullurl: URL string or Request object
            data: Optional request body
            timeout: Request timeout
            is_pre_app_start: Whether this is before app start
            original_open: Original OpenerDirector.open method
        """
        request_info = self._extract_request_info(fullurl, data)
        method = request_info["method"]
        url = request_info["url"]
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
                    TdSpanAttributes.INSTRUMENTATION_NAME: "UrllibInstrumentation",
                    TdSpanAttributes.SUBMODULE_NAME: method.upper(),
                    TdSpanAttributes.PACKAGE_TYPE: PackageType.HTTP.name,
                    TdSpanAttributes.IS_PRE_APP_START: is_pre_app_start,
                },
                is_pre_app_start=is_pre_app_start,
            )
        )

        if not span_info:
            # Span creation failed (trace blocked, etc.) - just make the request
            return original_open(opener_self, fullurl, data, timeout)

        try:
            with SpanUtils.with_span(span_info):
                # Check drop transforms BEFORE making the request
                headers = request_info.get("headers", {})
                if self._transform_engine and self._transform_engine.should_drop_outbound_request(
                    method.upper(), url, headers
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
                wrapped_response = None

                try:
                    response = original_open(opener_self, fullurl, data, timeout)
                    # Wrap the response to allow body caching for instrumentation
                    # while still allowing the caller to read it
                    wrapped_response = ResponseWrapper(response)
                    return wrapped_response
                except Exception as e:
                    error = e
                    raise
                finally:
                    # Finalize span with request/response data
                    # Use wrapped_response if available (it caches the body)
                    self._finalize_span(
                        span_info.span,
                        method,
                        url,
                        wrapped_response if wrapped_response else response,
                        error,
                        request_info,
                    )
        finally:
            span_info.span.end()

    def _handle_replay_open(
        self,
        sdk: TuskDrift,
        fullurl: Any,
        data: bytes | None,
        timeout: Any,
    ) -> MockHTTPResponse:
        """Handle OpenerDirector.open() in REPLAY mode.

        Args:
            sdk: TuskDrift instance
            fullurl: URL string or Request object
            data: Optional request body
            timeout: Request timeout
        """
        request_info = self._extract_request_info(fullurl, data)
        method = request_info["method"]
        url = request_info["url"]
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
                    TdSpanAttributes.INSTRUMENTATION_NAME: "UrllibInstrumentation",
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
                    request_info,
                )

                if mock_response is not None:
                    return mock_response

                # No mock found - raise error in REPLAY mode
                raise RuntimeError(f"No mock found for {method} {url} in REPLAY mode")
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

    def _build_input_value(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
    ) -> tuple[dict[str, Any], str | None, int]:
        """Build the input value dictionary for HTTP requests.

        Args:
            method: HTTP method (GET, POST, etc.)
            url: Full request URL
            headers: Request headers dictionary
            body: Request body bytes (or None)

        Returns:
            Tuple of (input_value dict, body_base64 string or None, body_size int)
        """
        parsed_url = urlparse(url)

        # Parse query params from URL
        params = {}
        if parsed_url.query:
            params = {k: v[0] if len(v) == 1 else v for k, v in parse_qs(parsed_url.query).items()}

        # Encode body to base64
        body_base64 = None
        body_size = 0

        if body is not None:
            body_base64, body_size = self._encode_body_to_base64(body)

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

        return input_value, body_base64, body_size

    def _build_input_schema_merges(
        self,
        headers: dict[str, str],
        body_base64: str | None,
    ) -> dict[str, SchemaMerge]:
        """Build schema merge hints for input value.

        Args:
            headers: Request headers dictionary
            body_base64: Base64-encoded body string (or None if no body)

        Returns:
            Dictionary of schema merge hints
        """
        input_schema_merges: dict[str, SchemaMerge] = {
            "headers": SchemaMerge(match_importance=0.0),
        }

        if body_base64 is not None:
            request_content_type = self._get_content_type_header(headers)
            input_schema_merges["body"] = SchemaMerge(
                encoding=EncodingType.BASE64,
                decoded_type=self._get_decoded_type_from_content_type(request_content_type),
            )

        return input_schema_merges

    def _try_get_mock(
        self,
        sdk: TuskDrift,
        method: str,
        url: str,
        trace_id: str,
        span_id: str,
        request_info: dict[str, Any],
    ) -> MockHTTPResponse | None:
        """Try to get a mocked response from CLI.

        Returns:
            Mocked response object if found, None otherwise

        Raises:
            urllib.error.HTTPError: If the recorded response was an HTTPError
        """
        try:
            parsed_url = urlparse(url)

            # Extract request data
            headers = request_info.get("headers", {})
            body = request_info.get("body")

            # Build input value using shared helper
            raw_input_value, body_base64, _ = self._build_input_value(method, url, headers, body)
            input_value = create_mock_input_value(raw_input_value)

            # Build schema merge hints using shared helper
            input_schema_merges = self._build_input_schema_merges(headers, body_base64)

            # Use centralized mock finding utility
            from ...core.mock_utils import find_mock_response_sync

            mock_response_output = find_mock_response_sync(
                sdk=sdk,
                trace_id=trace_id,
                span_id=span_id,
                name=f"{method.upper()} {parsed_url.path or '/'}",
                package_name=parsed_url.scheme,
                package_type=PackageType.HTTP,
                instrumentation_name="UrllibInstrumentation",
                submodule_name=method.upper(),
                input_value=input_value,
                kind=SpanKind.CLIENT,
                input_schema_merges=input_schema_merges,
                is_pre_app_start=not sdk.app_ready,
            )

            if not mock_response_output or not mock_response_output.found:
                logger.debug(f"No mock found for {method} {url} (trace_id={trace_id})")
                return None

            # Create mocked response object
            if mock_response_output.response is None:
                logger.debug(f"Mock found but response data is None for {method} {url}")
                return None

            # Check if the recorded response was an error (HTTPError, URLError, etc.)
            response_data = mock_response_output.response
            error_name = response_data.get("errorName")

            if error_name == "HTTPError":
                # The original request raised HTTPError - we need to raise it too
                self._raise_http_error_from_mock(response_data, url)

            return self._create_mock_response(response_data, url)

        except Exception as e:
            # Re-raise HTTPError (and other urllib errors) so they propagate correctly
            from urllib.error import HTTPError, URLError
            if isinstance(e, (HTTPError, URLError)):
                raise
            logger.error(f"Error getting mock for {method} {url}: {e}")
            return None

    def _create_mock_response(self, mock_data: dict[str, Any], url: str) -> MockHTTPResponse:
        """Create a mocked urllib-compatible response object.

        Args:
            mock_data: Mock response data from CLI
            url: Request URL (original request URL)

        Returns:
            MockHTTPResponse object
        """
        status_code = mock_data.get("statusCode", 200)
        reason = mock_data.get("statusMessage", "OK")
        headers = dict(mock_data.get("headers", {}))

        # Use finalUrl from recorded response if present (indicates redirect occurred)
        # Otherwise fall back to the original request URL
        response_url = mock_data.get("finalUrl", url)

        # Remove content-encoding and transfer-encoding headers since the body
        # was already decompressed when recorded
        headers_to_remove = []
        for key in headers:
            if key.lower() in ("content-encoding", "transfer-encoding"):
                headers_to_remove.append(key)
        for key in headers_to_remove:
            del headers[key]

        # Decode body from base64 if needed
        body = mock_data.get("body", "")
        if isinstance(body, str):
            # Try to decode as base64 first (expected format from CLI)
            try:
                decoded = base64.b64decode(body.encode("ascii"), validate=True)
                # Verify round-trip works (confirms it's valid base64)
                if base64.b64encode(decoded).decode("ascii") == body:
                    body_bytes = decoded
                else:
                    body_bytes = body.encode("utf-8")
            except Exception:
                body_bytes = body.encode("utf-8")
        elif isinstance(body, bytes):
            body_bytes = body
        else:
            body_bytes = json.dumps(body).encode("utf-8")

        logger.debug(f"Created mock response: {status_code} for {response_url}")
        return MockHTTPResponse(
            status_code=status_code,
            reason=reason,
            headers=headers,
            body=body_bytes,
            url=response_url,
        )

    def _raise_http_error_from_mock(self, mock_data: dict[str, Any], url: str) -> None:
        """Raise an HTTPError from mocked error response data.

        When the original request resulted in an HTTPError (4xx/5xx status codes),
        we need to raise the same error during replay so application code that
        catches HTTPError behaves the same way.

        Args:
            mock_data: Mock response data containing errorName and errorMessage
            url: Request URL

        Raises:
            urllib.error.HTTPError: Always raises this exception
        """
        from email.message import Message
        from urllib.error import HTTPError

        error_message = mock_data.get("errorMessage", "")

        # Parse status code and reason from error message
        # Format: "HTTP Error 404: NOT FOUND"
        status_code = 500  # Default
        reason = "Internal Server Error"

        if error_message.startswith("HTTP Error "):
            try:
                # Extract "404: NOT FOUND" part
                parts = error_message[len("HTTP Error "):].split(":", 1)
                status_code = int(parts[0].strip())
                if len(parts) > 1:
                    reason = parts[1].strip()
            except (ValueError, IndexError):
                pass

        # Create empty headers (HTTPError requires headers object)
        headers = Message()

        # Create empty body
        body_fp = BytesIO(b"")

        logger.debug(f"Raising HTTPError {status_code} for {url} (replayed from recording)")
        raise HTTPError(url, status_code, reason, headers, body_fp)

    def _finalize_span(
        self,
        span: Span,
        method: str,
        url: str,
        response: Any,
        error: Exception | None,
        request_info: dict[str, Any],
    ) -> None:
        """Finalize span with request/response data.

        Args:
            span: The OpenTelemetry span to finalize
            method: HTTP method
            url: Request URL
            response: Response object (if successful)
            error: Exception (if failed)
            request_info: Original request info dict
        """
        try:
            # ===== BUILD INPUT VALUE =====
            headers = request_info.get("headers", {})
            body = request_info.get("body")

            # Build input value using shared helper
            input_value, body_base64, _ = self._build_input_value(method, url, headers, body)

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
                # urllib responses are file-like, need to read carefully
                response_status = getattr(response, "status", getattr(response, "code", 200))
                response_reason = getattr(response, "reason", getattr(response, "msg", "OK"))

                # Get headers - urllib uses info() which returns email.message.Message
                response_headers = {}
                try:
                    info = response.info()
                    if info:
                        response_headers = dict(info.items())
                except Exception:
                    pass

                response_body_size = 0

                try:
                    # Read response content
                    # Use get_cached_body() for ResponseWrapper (which caches the body)
                    # This allows the caller to still read the response after we capture it
                    if hasattr(response, "get_cached_body"):
                        # ResponseWrapper - use cached body
                        response_bytes = response.get_cached_body()
                    else:
                        # Raw response - read directly (body will be consumed)
                        response_bytes = response.read()

                    # Encode to base64
                    response_body_base64, response_body_size = self._encode_body_to_base64(response_bytes)
                except Exception:
                    response_body_base64 = None
                    response_body_size = 0

                # Capture final URL (important for redirect scenarios)
                # urllib sets response.url to the final URL after all redirects
                final_url = getattr(response, "url", None)
                if callable(final_url):
                    # Some response objects have geturl() method
                    final_url = final_url()

                output_value = {
                    "statusCode": response_status,
                    "statusMessage": response_reason,
                    "headers": response_headers,
                }

                # Store final URL if different from request URL (indicates redirect)
                if final_url and final_url != url:
                    output_value["finalUrl"] = final_url

                # Add body fields only if body exists
                if response_body_base64 is not None:
                    output_value["body"] = response_body_base64
                    output_value["bodySize"] = response_body_size

                if response_status >= 400:
                    status = SpanStatus(
                        code=StatusCode.ERROR,
                        message=f"HTTP {response_status}",
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
            # Build input schema merges using shared helper
            input_schema_merges = self._build_input_schema_merges(headers, body_base64)

            # Get response content type for output schema merges
            response_content_type = None
            if response:
                try:
                    info = response.info()
                    if info:
                        response_content_type = info.get("content-type")
                except Exception:
                    pass

            # Create schema merge hints for output
            output_schema_merges: dict[str, SchemaMerge] = {
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

            # Set schema merges
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
