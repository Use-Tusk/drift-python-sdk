from __future__ import annotations

import base64
import json
import logging
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

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


from opentelemetry import trace

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

# Higher-level instrumentations that use urllib3 under the hood
# When these are active, urllib3 should skip creating duplicate spans
HIGHER_LEVEL_HTTP_INSTRUMENTATIONS = {"RequestsInstrumentation"}

HEADER_SCHEMA_MERGES = {
    "headers": SchemaMerge(match_importance=0.0),
}


class Urllib3Instrumentation(InstrumentationBase):
    """Instrumentation for the urllib3 HTTP client library.

    Patches urllib3.PoolManager.urlopen() and urllib3.HTTPConnectionPool.urlopen()
    to:
    - Intercept HTTP requests in REPLAY mode and return mocked responses
    - Capture request/response data as CLIENT spans in RECORD mode

    urllib3 is the underlying HTTP library used by the requests library.
    It provides connection pooling and thread safety.
    """

    def __init__(self, enabled: bool = True, transforms: dict[str, Any] | None = None) -> None:
        self._transform_engine = HttpTransformEngine(self._resolve_http_transforms(transforms))
        super().__init__(
            name="Urllib3Instrumentation",
            module_name="urllib3",
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

    def _is_already_instrumented_by_higher_level(self) -> bool:
        """Check if there's already an active client span from a higher-level HTTP instrumentation.

        This prevents double spans when urllib3 is used under the hood by libraries
        like 'requests' which have their own instrumentation.

        Returns:
            True if a higher-level instrumentation is already active, False otherwise.
        """
        current_span = trace.get_current_span()
        if current_span is None or not current_span.is_recording():
            return False

        span_context = current_span.get_span_context()
        if not span_context.is_valid:
            return False

        # Access attributes safely - the Span interface doesn't expose .attributes,
        # but SDK Span implementations do have it
        attributes = getattr(current_span, "attributes", None)
        if attributes is not None:
            instrumentation_name = attributes.get(TdSpanAttributes.INSTRUMENTATION_NAME)
            if instrumentation_name in HIGHER_LEVEL_HTTP_INSTRUMENTATIONS:
                logger.debug(f"Skipping urllib3 span creation - already instrumented by {instrumentation_name}")
                return True

        return False

    def patch(self, module: Any) -> None:
        """Patch the urllib3 module.

        Patches PoolManager.urlopen() and HTTPConnectionPool.urlopen() to intercept
        all HTTP requests made through urllib3.
        """
        # Patch PoolManager.urlopen
        if hasattr(module, "PoolManager"):
            self._patch_pool_manager(module)
        else:
            logger.warning("urllib3.PoolManager not found, skipping PoolManager instrumentation")

        # Patch HTTPConnectionPool.urlopen for direct pool usage
        if hasattr(module, "HTTPConnectionPool"):
            self._patch_connection_pool(module)
        else:
            logger.warning("urllib3.HTTPConnectionPool not found, skipping HTTPConnectionPool instrumentation")

    def _patch_pool_manager(self, module: Any) -> None:
        """Patch urllib3.PoolManager.urlopen for high-level API."""
        original_urlopen = module.PoolManager.urlopen
        instrumentation_self = self

        def patched_urlopen(
            pool_self,
            method: str,
            url: str,
            redirect: bool = True,
            **kw,
        ):
            """Patched PoolManager.urlopen method."""
            sdk = TuskDrift.get_instance()

            if sdk.mode == TuskDriftMode.DISABLED:
                return original_urlopen(pool_self, method, url, redirect=redirect, **kw)

            # Skip if already instrumented by higher-level library
            if instrumentation_self._is_already_instrumented_by_higher_level():
                return original_urlopen(pool_self, method, url, redirect=redirect, **kw)

            # Set calling_library_context to suppress socket instrumentation warnings
            # for internal socket calls made by urllib3
            context_token = calling_library_context.set("urllib3")
            try:

                def original_call():
                    return original_urlopen(pool_self, method, url, redirect=redirect, **kw)

                if sdk.mode == TuskDriftMode.REPLAY:
                    return handle_replay_mode(
                        replay_mode_handler=lambda: instrumentation_self._handle_replay_urlopen(
                            sdk, module, method, url, **kw
                        ),
                        no_op_request_handler=lambda: instrumentation_self._get_default_response(module, url),
                        is_server_request=False,
                    )

                return handle_record_mode(
                    original_function_call=original_call,
                    record_mode_handler=lambda is_pre_app_start: instrumentation_self._handle_record_urlopen(
                        pool_self, method, url, is_pre_app_start, original_urlopen, redirect=redirect, **kw
                    ),
                    span_kind=OTelSpanKind.CLIENT,
                )
            finally:
                calling_library_context.reset(context_token)

        module.PoolManager.urlopen = patched_urlopen
        logger.info("urllib3.PoolManager.urlopen instrumented")

    def _patch_connection_pool(self, module: Any) -> None:
        """Patch urllib3.HTTPConnectionPool.urlopen for direct pool usage."""
        original_urlopen = module.HTTPConnectionPool.urlopen
        instrumentation_self = self

        def patched_urlopen(
            pool_self,
            method: str,
            url: str,
            body=None,
            headers=None,
            retries=None,
            redirect=True,
            assert_same_host=True,
            timeout=None,
            pool_timeout=None,
            release_conn=None,
            chunked=False,
            body_pos=None,
            preload_content=True,
            decode_content=True,
            **response_kw,
        ):
            """Patched HTTPConnectionPool.urlopen method."""
            sdk = TuskDrift.get_instance()

            # Build full URL from pool's host/port and relative URL
            scheme = pool_self.scheme if hasattr(pool_self, "scheme") else "http"
            host = pool_self.host if hasattr(pool_self, "host") else "localhost"
            port = pool_self.port if hasattr(pool_self, "port") else None

            if url.startswith(("http://", "https://")):
                full_url = url
            else:
                port_str = f":{port}" if port and port not in (80, 443) else ""
                full_url = f"{scheme}://{host}{port_str}{url}"

            # Pass through if SDK is disabled
            if sdk.mode == TuskDriftMode.DISABLED:
                return original_urlopen(
                    pool_self,
                    method,
                    url,
                    body=body,
                    headers=headers,
                    retries=retries,
                    redirect=redirect,
                    assert_same_host=assert_same_host,
                    timeout=timeout,
                    pool_timeout=pool_timeout,
                    release_conn=release_conn,
                    chunked=chunked,
                    body_pos=body_pos,
                    preload_content=preload_content,
                    decode_content=decode_content,
                    **response_kw,
                )

            if instrumentation_self._is_already_instrumented_by_higher_level():
                return original_urlopen(
                    pool_self,
                    method,
                    url,
                    body=body,
                    headers=headers,
                    retries=retries,
                    redirect=redirect,
                    assert_same_host=assert_same_host,
                    timeout=timeout,
                    pool_timeout=pool_timeout,
                    release_conn=release_conn,
                    chunked=chunked,
                    body_pos=body_pos,
                    preload_content=preload_content,
                    decode_content=decode_content,
                    **response_kw,
                )

            # Set calling_library_context to suppress socket instrumentation warnings
            # for internal socket calls made by urllib3
            context_token = calling_library_context.set("urllib3")
            try:

                def original_call():
                    return original_urlopen(
                        pool_self,
                        method,
                        url,
                        body=body,
                        headers=headers,
                        retries=retries,
                        redirect=redirect,
                        assert_same_host=assert_same_host,
                        timeout=timeout,
                        pool_timeout=pool_timeout,
                        release_conn=release_conn,
                        chunked=chunked,
                        body_pos=body_pos,
                        preload_content=preload_content,
                        decode_content=decode_content,
                        **response_kw,
                    )

                # Import urllib3 module for mock response creation
                import urllib3 as urllib3_module

                # REPLAY mode
                if sdk.mode == TuskDriftMode.REPLAY:
                    return handle_replay_mode(
                        replay_mode_handler=lambda: instrumentation_self._handle_replay_urlopen(
                            sdk,
                            urllib3_module,
                            method,
                            full_url,
                            body=body,
                            headers=headers,
                        ),
                        no_op_request_handler=lambda: instrumentation_self._get_default_response(
                            urllib3_module, full_url
                        ),
                        is_server_request=False,
                    )

                # RECORD mode
                return handle_record_mode(
                    original_function_call=original_call,
                    record_mode_handler=lambda is_pre_app_start: instrumentation_self._handle_record_connection_pool_urlopen(
                        pool_self,
                        method,
                        url,
                        full_url,
                        is_pre_app_start,
                        original_urlopen,
                        body=body,
                        headers=headers,
                        retries=retries,
                        redirect=redirect,
                        assert_same_host=assert_same_host,
                        timeout=timeout,
                        pool_timeout=pool_timeout,
                        release_conn=release_conn,
                        chunked=chunked,
                        body_pos=body_pos,
                        preload_content=preload_content,
                        decode_content=decode_content,
                        **response_kw,
                    ),
                    span_kind=OTelSpanKind.CLIENT,
                )
            finally:
                calling_library_context.reset(context_token)

        module.HTTPConnectionPool.urlopen = patched_urlopen
        logger.info("urllib3.HTTPConnectionPool.urlopen instrumented")

    def _get_default_response(self, urllib3_module: Any, url: str) -> Any:
        """Return default response for background requests in REPLAY mode.

        Background requests (health checks, metrics, etc.) that happen outside
        of any trace context should return a default response instead of failing.
        """
        # Create a minimal HTTPResponse-like object
        from io import BytesIO

        # urllib3.HTTPResponse expects specific arguments
        response = urllib3_module.HTTPResponse(
            body=BytesIO(b""),
            headers={},
            status=200,
            preload_content=True,
        )
        logger.debug(f"[Urllib3Instrumentation] Returning default response for background request to {url}")
        return response

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
        span_name = f"{method.upper()} {parsed_url.path or '/'}"

        return SpanUtils.create_span(
            CreateSpanOptions(
                name=span_name,
                kind=OTelSpanKind.CLIENT,
                attributes={
                    TdSpanAttributes.NAME: span_name,
                    TdSpanAttributes.PACKAGE_NAME: parsed_url.scheme,
                    TdSpanAttributes.INSTRUMENTATION_NAME: "Urllib3Instrumentation",
                    TdSpanAttributes.SUBMODULE_NAME: method.upper(),
                    TdSpanAttributes.PACKAGE_TYPE: PackageType.HTTP.name,
                    TdSpanAttributes.IS_PRE_APP_START: is_pre_app_start,
                },
                is_pre_app_start=is_pre_app_start,
            )
        )

    def _handle_replay_urlopen(
        self,
        sdk: TuskDrift,
        urllib3_module: Any,
        method: str,
        url: str,
        body: Any = None,
        headers: dict | None = None,
        **kwargs,
    ) -> Any:
        """Handle urlopen in REPLAY mode.

        Creates a span, fetches mock response.
        Raises RuntimeError if no mock is found.
        """
        span_info = self._create_client_span(method, url, not sdk.app_ready)
        if not span_info:
            raise RuntimeError(f"Error creating span in replay mode for {method} {url}")

        try:
            with SpanUtils.with_span(span_info):
                mock_response = self._try_get_mock(
                    sdk,
                    urllib3_module,
                    method,
                    url,
                    span_info.trace_id,
                    span_info.span_id,
                    body=body,
                    headers=headers,
                )

                if mock_response is not None:
                    return mock_response

                # No mock found - raise error in REPLAY mode
                raise RuntimeError(f"No mock found for {method} {url} in REPLAY mode")
        finally:
            span_info.span.end()

    def _handle_record_urlopen(
        self,
        pool_self: Any,
        method: str,
        url: str,
        is_pre_app_start: bool,
        original_urlopen: Any,
        redirect: bool = True,
        **kw,
    ) -> Any:
        """Handle PoolManager.urlopen in RECORD mode.

        Creates a span, makes the real request, and records the response.
        """
        span_info = self._create_client_span(method, url, is_pre_app_start)
        if not span_info:
            # Span creation failed (trace blocked, etc.) - just make the request
            return original_urlopen(pool_self, method, url, redirect=redirect, **kw)

        try:
            with SpanUtils.with_span(span_info):
                # Check drop transforms before making the request
                headers = kw.get("headers") or {}
                if isinstance(headers, (list, tuple)):
                    headers = dict(headers)
                if self._transform_engine and self._transform_engine.should_drop_outbound_request(
                    method.upper(), url, headers
                ):
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

                error = None
                response = None

                try:
                    response = original_urlopen(pool_self, method, url, redirect=redirect, **kw)
                    return response
                except Exception as e:
                    error = e
                    raise
                finally:
                    self._finalize_span(
                        span_info.span,
                        method,
                        url,
                        response,
                        error,
                        body=kw.get("body"),
                        headers=kw.get("headers"),
                        fields=kw.get("fields"),
                    )
        finally:
            span_info.span.end()

    def _handle_record_connection_pool_urlopen(
        self,
        pool_self: Any,
        method: str,
        url: str,
        full_url: str,
        is_pre_app_start: bool,
        original_urlopen: Any,
        body: Any = None,
        headers: dict | None = None,
        **kwargs,
    ) -> Any:
        """Handle HTTPConnectionPool.urlopen in RECORD mode.

        Creates a span, makes the real request, and records the response.
        """
        span_info = self._create_client_span(method, full_url, is_pre_app_start)
        if not span_info:
            # Span creation failed (trace blocked, etc.) - just make the request
            return original_urlopen(pool_self, method, url, body=body, headers=headers, **kwargs)

        try:
            with SpanUtils.with_span(span_info):
                # Check drop transforms before making the request
                headers_dict = dict(headers) if headers else {}
                if self._transform_engine and self._transform_engine.should_drop_outbound_request(
                    method.upper(), full_url, headers_dict
                ):
                    span_info.span.set_attribute(
                        TdSpanAttributes.OUTPUT_VALUE,
                        json.dumps({"bodyProcessingError": "dropped"}),
                    )
                    span_info.span.set_status(Status(OTelStatusCode.ERROR, "Dropped by transform"))
                    raise RequestDroppedByTransform(
                        f"Outbound request to {full_url} was dropped by transform rule",
                        method.upper(),
                        full_url,
                    )

                error = None
                response = None

                try:
                    response = original_urlopen(pool_self, method, url, body=body, headers=headers, **kwargs)
                    return response
                except Exception as e:
                    error = e
                    raise
                finally:
                    self._finalize_span(
                        span_info.span,
                        method,
                        full_url,
                        response,
                        error,
                        body=body,
                        headers=headers,
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

        if isinstance(body_data, bytes):
            body_bytes = body_data
        elif isinstance(body_data, str):
            body_bytes = body_data.encode("utf-8")
        elif isinstance(body_data, dict):
            body_bytes = json.dumps(body_data).encode("utf-8")
        else:
            # Fallback: convert to string then encode
            body_bytes = str(body_data).encode("utf-8")

        base64_body = base64.b64encode(body_bytes).decode("ascii")

        return base64_body, len(body_bytes)

    def _get_response_body_safely(self, response: Any) -> bytes | None:
        """Get response body without consuming the stream for non-preloaded responses.

        urllib3's .data property will read from the underlying stream if the response
        was created with preload_content=False. This breaks applications that:
        - Use preload_content=False and call response.read() manually
        - Use response.stream() to iterate over chunks
        - Use any streaming pattern to process large responses

        This method checks whether it's safe to access the body without consuming
        the stream, and returns None if the body cannot be safely captured.

        IMPORTANT: We must NOT use hasattr(response, "data") because in Python,
        hasattr() calls getattr() internally, which would trigger the .data
        property getter and consume the stream!

        Args:
            response: urllib3 HTTPResponse object

        Returns:
            Response body as bytes, or None if body cannot be safely captured
        """
        # Check if this is a urllib3 HTTPResponse by checking for _body attribute
        # Note: We use getattr with a sentinel to avoid triggering any property getters
        _sentinel = object()
        body = getattr(response, "_body", _sentinel)

        if body is _sentinel:
            # Not a urllib3 HTTPResponse or doesn't have _body
            return b""

        # Check if body was already preloaded/cached
        if body is not None:
            # Body was already read and cached, safe to return
            # Ensure it's bytes (urllib3's _body should always be bytes when set)
            return body if isinstance(body, bytes) else b""

        # Check if the stream was already fully consumed (fp is None or closed)
        fp = getattr(response, "_fp", None)
        if fp is None:
            # Stream was consumed, body should be in _body (but it might be None)
            return b""

        # Check if the file pointer is closed
        if hasattr(fp, "closed") and fp.closed:
            return b""

        # At this point, the stream is still open and body hasn't been read
        # This means preload_content=False was used (otherwise the body would
        # have been read during __init__)
        #
        # We skip capturing the body to avoid breaking the application
        return None

    def _get_decoded_type_from_content_type(self, content_type: str | None) -> DecodedType | None:
        """Determine decoded type from Content-Type header. Extracts
        the main type (before semicolon) and returns the corresponding
        DecodedType enum value.

        Args:
            content_type: Content-Type header value

        Returns:
            DecodedType enum value or None
        """
        if not content_type:
            return None

        main_type = content_type.lower().split(";")[0].strip()

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

    def _get_content_type_header(self, headers: dict | None) -> str | None:
        """Get content-type header (case-insensitive lookup)."""
        if not headers:
            return None
        for key, value in headers.items():
            if key.lower() == "content-type":
                return value
        return None

    def _try_get_mock(
        self,
        sdk: TuskDrift,
        urllib3_module: Any,
        method: str,
        url: str,
        trace_id: str,
        span_id: str,
        body: Any = None,
        headers: dict | None = None,
        fields: dict | None = None,
    ) -> Any:
        """Try to get a mocked response from CLI.

        Returns:
            Mocked response object if found, None otherwise
        """
        try:
            parsed_url = urlparse(url)

            params = {}
            if parsed_url.query:
                params = {k: v[0] if len(v) == 1 else v for k, v in parse_qs(parsed_url.query).items()}

            body_base64 = None
            body_size = 0

            if body is not None:
                body_base64, body_size = self._encode_body_to_base64(body)
            elif fields is not None:
                encoded_fields = urlencode(fields)
                body_base64, body_size = self._encode_body_to_base64(encoded_fields)

            headers_dict = dict(headers) if headers else {}

            raw_input_value = {
                "method": method.upper(),
                "url": url,
                "protocol": parsed_url.scheme,
                "hostname": parsed_url.hostname,
                "port": parsed_url.port,
                "path": parsed_url.path or "/",
                "headers": headers_dict,
                "query": params,
            }

            # Add body fields only if body exists
            if body_base64 is not None:
                raw_input_value["body"] = body_base64
                raw_input_value["bodySize"] = body_size

            input_value = create_mock_input_value(raw_input_value)

            input_schema_merges = {
                "headers": SchemaMerge(match_importance=0.0),
            }
            if body_base64 is not None:
                request_content_type = self._get_content_type_header(headers_dict)
                input_schema_merges["body"] = SchemaMerge(
                    encoding=EncodingType.BASE64,
                    decoded_type=self._get_decoded_type_from_content_type(request_content_type),
                )

            from ...core.mock_utils import find_mock_response_sync

            mock_response_output = find_mock_response_sync(
                sdk=sdk,
                trace_id=trace_id,
                span_id=span_id,
                name=f"{method.upper()} {parsed_url.path or '/'}",
                package_name=parsed_url.scheme,
                package_type=PackageType.HTTP,
                instrumentation_name="Urllib3Instrumentation",
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
            return self._create_mock_response(urllib3_module, mock_response_output.response, url)

        except Exception as e:
            logger.error(f"Error getting mock for {method} {url}: {e}")
            return None

    def _create_mock_response(self, urllib3_module: Any, mock_data: dict[str, Any], url: str) -> Any:
        """Create a mocked urllib3.HTTPResponse object.

        Args:
            urllib3_module: The urllib3 module
            mock_data: Mock response data from CLI
            url: Request URL

        Returns:
            Mocked HTTPResponse object
        """
        from io import BytesIO

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

        response = urllib3_module.HTTPResponse(
            body=BytesIO(content),
            headers=headers,
            status=status_code,
            preload_content=True,
        )

        # Read the content to make it available via response.data
        response.read()

        logger.debug(f"Created mock urllib3 response: {status_code} for {url}")
        return response

    def _finalize_span(
        self,
        span: Span,
        method: str,
        url: str,
        response: Any,
        error: Exception | None,
        body: Any = None,
        headers: dict | None = None,
        fields: dict | None = None,
    ) -> None:
        """Finalize span with request/response data.

        Args:
            span: The OpenTelemetry span to finalize
            method: HTTP method
            url: Request URL
            response: Response object (if successful)
            error: Exception (if failed)
            body: Request body
            headers: Request headers
            fields: URL-encoded form fields
        """
        try:
            parsed_url = urlparse(url)

            # Build input value
            params = {}
            if parsed_url.query:
                params = {k: v[0] if len(v) == 1 else v for k, v in parse_qs(parsed_url.query).items()}

            headers_dict = dict(headers) if headers else {}

            body_base64 = None
            body_size = 0

            if body is not None:
                body_base64, body_size = self._encode_body_to_base64(body)
            elif fields is not None:
                encoded_fields = urlencode(fields)
                body_base64, body_size = self._encode_body_to_base64(encoded_fields)

            input_value = {
                "method": method.upper(),
                "url": url,
                "protocol": parsed_url.scheme,
                "hostname": parsed_url.hostname,
                "port": parsed_url.port,
                "path": parsed_url.path or "/",
                "headers": headers_dict,
                "query": params,
            }

            if body_base64 is not None:
                input_value["body"] = body_base64
                input_value["bodySize"] = body_size

            # Build output value
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
                response_headers = dict(response.headers) if hasattr(response, "headers") else {}
                response_body_size = 0

                try:
                    # Get response content safely without consuming the stream
                    # urllib3's .data property will read from the stream if not preloaded,
                    # which would break applications using preload_content=False or response.stream()
                    response_bytes = self._get_response_body_safely(response)
                    if response_bytes is not None:
                        response_body_base64, response_body_size = self._encode_body_to_base64(response_bytes)
                    else:
                        # Response body not captured (likely preload_content=False or streaming)
                        response_body_base64 = None
                        response_body_size = 0
                        logger.warning(
                            f"Response body not captured for {method} {url} - request used "
                            f"preload_content=False or streaming. Replay may return an empty body."
                        )
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

            # Apply transforms
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

            # Create schema merge hints
            request_content_type = self._get_content_type_header(headers_dict)
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

            # Set span attributes
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

            if status.code == StatusCode.ERROR:
                span.set_status(Status(OTelStatusCode.ERROR, status.message))
            else:
                span.set_status(Status(OTelStatusCode.OK))

        except Exception as e:
            logger.error(f"Error finalizing span for {method} {url}: {e}")
            span.set_status(Status(OTelStatusCode.ERROR, str(e)))
