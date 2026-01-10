"""Instrumentation for httpx HTTP client library."""

from __future__ import annotations

import base64
import inspect
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
)
from ..base import InstrumentationBase
from ..http import HttpSpanData, HttpTransformEngine

logger = logging.getLogger(__name__)

# Schema merge hints for headers (low match importance)
HEADER_SCHEMA_MERGES = {
    "headers": SchemaMerge(match_importance=0.0),
}


class HttpxInstrumentation(InstrumentationBase):
    """Instrumentation for the httpx HTTP client library.

    Patches both sync and async clients at the send() level:
    - httpx.Client.send (sync)
    - httpx.AsyncClient.send (async)

    We patch send() instead of request() because both request() and stream()
    call send() internally. This ensures we capture all HTTP calls including
    streaming requests (client.stream()) and direct send() calls.

    Supports:
    - Intercept HTTP requests in REPLAY mode and return mocked responses
    - Capture request/response data as CLIENT spans in RECORD mode
    - Streaming responses via client.stream() and AsyncClient.stream()
    """

    def __init__(self, enabled: bool = True, transforms: dict[str, Any] | None = None) -> None:
        self._transform_engine = HttpTransformEngine(self._resolve_http_transforms(transforms))
        super().__init__(
            name="HttpxInstrumentation",
            module_name="httpx",
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
        """Patch the httpx module."""
        # Patch sync client
        if hasattr(module, "Client"):
            self._patch_sync_client(module)
        else:
            logger.warning("httpx.Client not found, skipping sync instrumentation")

        # Patch async client
        if hasattr(module, "AsyncClient"):
            self._patch_async_client(module)
        else:
            logger.warning("httpx.AsyncClient not found, skipping async instrumentation")

    def _get_default_response(self, httpx_module: Any, url: str) -> Any:
        """Return default response for background requests in REPLAY mode.

        Background requests (health checks, metrics, etc.) that happen outside
        of any trace context should return a default response instead of failing.
        """
        request = httpx_module.Request("GET", url)
        response = httpx_module.Response(
            status_code=200,
            content=b"",
            request=request,
        )
        logger.debug(f"[HttpxInstrumentation] Returning default response for background request to {url}")
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
        span_name = f"{method} {parsed_url.path or '/'}"

        return SpanUtils.create_span(
            CreateSpanOptions(
                name=span_name,
                kind=OTelSpanKind.CLIENT,
                attributes={
                    TdSpanAttributes.NAME: span_name,
                    TdSpanAttributes.PACKAGE_NAME: parsed_url.scheme,
                    TdSpanAttributes.INSTRUMENTATION_NAME: "HttpxInstrumentation",
                    TdSpanAttributes.SUBMODULE_NAME: method,
                    TdSpanAttributes.PACKAGE_TYPE: PackageType.HTTP.name,
                    TdSpanAttributes.IS_PRE_APP_START: is_pre_app_start,
                },
                is_pre_app_start=is_pre_app_start,
            )
        )

    def _fire_request_hooks(self, client: Any, request: Any) -> None:
        """Fire request event hooks if present on client.

        In REPLAY mode, we need to manually fire hooks since original_send()
        is not called and hooks normally run inside _send_handling_redirects().

        Args:
            client: httpx Client instance (may be None)
            request: httpx.Request object to pass to hooks
        """
        if client is None:
            return

        event_hooks = getattr(client, "_event_hooks", None)
        if not event_hooks:
            return

        request_hooks = event_hooks.get("request", [])
        for hook in request_hooks:
            try:
                hook(request)
            except Exception as e:
                logger.warning(f"Error firing request hook in REPLAY mode: {e}")

    def _fire_response_hooks(self, client: Any, response: Any) -> None:
        """Fire response event hooks if present on client.

        In REPLAY mode, we need to manually fire hooks since original_send()
        is not called and hooks normally run inside _send_handling_redirects().

        Args:
            client: httpx Client instance (may be None)
            response: httpx.Response object to pass to hooks
        """
        if client is None:
            return

        event_hooks = getattr(client, "_event_hooks", None)
        if not event_hooks:
            return

        response_hooks = event_hooks.get("response", [])
        for hook in response_hooks:
            try:
                hook(response)
            except Exception as e:
                logger.warning(f"Error firing response hook in REPLAY mode: {e}")

    async def _fire_request_hooks_async(self, client: Any, request: Any) -> None:
        """Fire request event hooks if present on client (async version).

        In REPLAY mode with AsyncClient, hooks may be async functions that need
        to be awaited. This method checks if the hook returns a coroutine and
        awaits it if necessary.

        Args:
            client: httpx AsyncClient instance (may be None)
            request: httpx.Request object to pass to hooks
        """
        if client is None:
            return

        event_hooks = getattr(client, "_event_hooks", None)
        if not event_hooks:
            return

        request_hooks = event_hooks.get("request", [])
        for hook in request_hooks:
            try:
                result = hook(request)
                if inspect.iscoroutine(result):
                    await result
            except Exception as e:
                logger.warning(f"Error firing request hook in REPLAY mode: {e}")

    async def _fire_response_hooks_async(self, client: Any, response: Any) -> None:
        """Fire response event hooks if present on client (async version).

        In REPLAY mode with AsyncClient, hooks may be async functions that need
        to be awaited. This method checks if the hook returns a coroutine and
        awaits it if necessary.

        Args:
            client: httpx AsyncClient instance (may be None)
            response: httpx.Response object to pass to hooks
        """
        if client is None:
            return

        event_hooks = getattr(client, "_event_hooks", None)
        if not event_hooks:
            return

        response_hooks = event_hooks.get("response", [])
        for hook in response_hooks:
            try:
                result = hook(response)
                if inspect.iscoroutine(result):
                    await result
            except Exception as e:
                logger.warning(f"Error firing response hook in REPLAY mode: {e}")

    def _patch_sync_client(self, module: Any) -> None:
        """Patch httpx.Client.send for sync HTTP calls.

        We patch send() instead of request() because both request() and stream()
        call send() internally. This ensures we capture all HTTP calls including
        streaming requests.
        """
        original_send = module.Client.send
        instrumentation_self = self

        def patched_send(
            client_self,
            request,  # httpx.Request object
            *,
            stream: bool = False,
            auth=None,
            follow_redirects=None,
        ):
            """Patched Client.send method."""
            url_str = str(request.url)
            sdk = TuskDrift.get_instance()

            # Pass through if SDK is disabled
            if sdk.mode == TuskDriftMode.DISABLED:
                return original_send(client_self, request, stream=stream, auth=auth, follow_redirects=follow_redirects)

            def original_call():
                return original_send(client_self, request, stream=stream, auth=auth, follow_redirects=follow_redirects)

            # REPLAY mode: Use handle_replay_mode for proper background request handling
            if sdk.mode == TuskDriftMode.REPLAY:
                return handle_replay_mode(
                    replay_mode_handler=lambda: instrumentation_self._handle_replay_send_sync(
                        sdk, module, request, auth=auth, client=client_self
                    ),
                    no_op_request_handler=lambda: instrumentation_self._get_default_response(module, url_str),
                    is_server_request=False,
                )

            # RECORD mode: Use handle_record_mode for proper is_pre_app_start handling
            return handle_record_mode(
                original_function_call=original_call,
                record_mode_handler=lambda is_pre_app_start: instrumentation_self._handle_record_send_sync(
                    client_self,
                    request,
                    stream,
                    is_pre_app_start,
                    original_send,
                    auth=auth,
                    follow_redirects=follow_redirects,
                ),
                span_kind=OTelSpanKind.CLIENT,
            )

        # Apply patch
        module.Client.send = patched_send
        logger.info("httpx.Client.send instrumented")

    def _handle_replay_send_sync(
        self,
        sdk: TuskDrift,
        httpx_module: Any,
        request: Any,  # httpx.Request object
        auth: Any = None,  # Auth parameter (unused - auth headers stripped for matching)
        client: Any = None,  # Client instance
    ) -> Any:
        """Handle send in REPLAY mode (sync).

        Creates a span, fetches mock response.
        Raises RuntimeError if no mock is found.

        Note: Auth is NOT applied in REPLAY mode. Instead, we strip auth-related
        headers (Authorization, Cookie) from the input value in both RECORD and
        REPLAY modes to ensure consistent matching. This handles multi-step auth
        flows like DigestAuth where we can't simulate the full challenge-response.
        """
        # Fire request hooks to modify request before mock lookup
        # This matches RECORD behavior where hooks run before the request is sent
        self._fire_request_hooks(client, request)

        method = request.method
        url = str(request.url)

        span_info = self._create_client_span(method, url, not sdk.app_ready)
        if not span_info:
            raise RuntimeError(f"Error creating span in replay mode for {method} {url}")

        try:
            with SpanUtils.with_span(span_info):
                # Use IDs from SpanInfo (already formatted)
                mock_response = self._try_get_mock_from_request_sync(
                    sdk,
                    httpx_module,
                    request,
                    span_info.trace_id,
                    span_info.span_id,
                )

                if mock_response is not None:
                    # Fire response hooks on mock response
                    # This matches RECORD behavior where hooks run after response
                    self._fire_response_hooks(client, mock_response)
                    return mock_response

                # No mock found - raise error in REPLAY mode
                raise RuntimeError(f"No mock found for {method} {url} in REPLAY mode")
        finally:
            span_info.span.end()

    def _handle_record_send_sync(
        self,
        client_self: Any,
        request: Any,  # httpx.Request object
        stream: bool,
        is_pre_app_start: bool,
        original_send: Any,
        **kwargs,
    ) -> Any:
        """Handle send in RECORD mode (sync).

        Creates a span, makes the real request, and records the response.
        For streaming responses, reads the body to capture it for recording.
        """
        method = request.method
        url = str(request.url)

        span_info = self._create_client_span(method, url, is_pre_app_start)
        if not span_info:
            # Span creation failed (trace blocked, etc.) - just make the request
            return original_send(client_self, request, stream=stream, **kwargs)

        try:
            with SpanUtils.with_span(span_info):
                # Check drop transforms BEFORE making the request
                headers = dict(request.headers)
                if self._transform_engine and self._transform_engine.should_drop_outbound_request(method, url, headers):
                    # Request should be dropped - mark span and raise exception
                    span_info.span.set_attribute(
                        TdSpanAttributes.OUTPUT_VALUE,
                        json.dumps({"bodyProcessingError": "dropped"}),
                    )
                    span_info.span.set_status(Status(OTelStatusCode.ERROR, "Dropped by transform"))
                    raise RequestDroppedByTransform(
                        f"Outbound request to {url} was dropped by transform rule",
                        method,
                        url,
                    )

                # Pre-capture request body before send - file-like objects (BytesIO, etc.)
                # get consumed when httpx sends the request, so we must capture first
                pre_captured_body = self._get_request_body_safely(request)

                # Make the real request
                error = None
                response = None

                try:
                    response = original_send(client_self, request, stream=stream, **kwargs)

                    # For streaming responses, we need to read the body to capture it
                    # This is necessary for recording the response data
                    if stream and response is not None:
                        response.read()

                    return response
                except Exception as e:
                    error = e
                    raise
                finally:
                    # Finalize span with request/response data
                    self._finalize_span_from_request(
                        span_info.span,
                        request,
                        response,
                        error,
                        pre_captured_body,
                    )
        finally:
            span_info.span.end()

    def _patch_async_client(self, module: Any) -> None:
        """Patch httpx.AsyncClient.send for async HTTP calls.

        We patch send() instead of request() because both request() and stream()
        call send() internally. This ensures we capture all HTTP calls including
        streaming requests.
        """
        original_send = module.AsyncClient.send
        instrumentation_self = self

        async def patched_send(
            client_self,
            request,  # httpx.Request object
            *,
            stream: bool = False,
            auth=None,
            follow_redirects=None,
        ):
            """Patched AsyncClient.send method."""
            url_str = str(request.url)
            sdk = TuskDrift.get_instance()

            # Pass through if SDK is disabled
            if sdk.mode == TuskDriftMode.DISABLED:
                return await original_send(
                    client_self, request, stream=stream, auth=auth, follow_redirects=follow_redirects
                )

            async def original_call():
                return await original_send(
                    client_self, request, stream=stream, auth=auth, follow_redirects=follow_redirects
                )

            # REPLAY mode: Use handle_replay_mode for proper background request handling
            # handle_replay_mode returns coroutine which we await
            if sdk.mode == TuskDriftMode.REPLAY:
                return await handle_replay_mode(
                    replay_mode_handler=lambda: instrumentation_self._handle_replay_send_async(
                        sdk, module, request, auth=auth, client=client_self
                    ),
                    no_op_request_handler=lambda: instrumentation_self._get_default_response(module, url_str),
                    is_server_request=False,
                )

            # RECORD mode: Use handle_record_mode for proper is_pre_app_start handling
            # handle_record_mode returns coroutine which we await
            return await handle_record_mode(
                original_function_call=original_call,
                record_mode_handler=lambda is_pre_app_start: instrumentation_self._handle_record_send_async(
                    client_self,
                    request,
                    stream,
                    is_pre_app_start,
                    original_send,
                    auth=auth,
                    follow_redirects=follow_redirects,
                ),
                span_kind=OTelSpanKind.CLIENT,
            )

        # Apply patch
        module.AsyncClient.send = patched_send
        logger.info("httpx.AsyncClient.send instrumented")

    async def _handle_replay_send_async(
        self,
        sdk: TuskDrift,
        httpx_module: Any,
        request: Any,  # httpx.Request object
        auth: Any = None,  # Auth parameter (unused - auth headers stripped for matching)
        client: Any = None,  # Client instance
    ) -> Any:
        """Handle send in REPLAY mode (async).

        Creates a span, fetches mock response. Uses async hook methods to properly
        await async event hooks that may be registered with AsyncClient.

        Note: Auth is NOT applied in REPLAY mode. Instead, we strip auth-related
        headers (Authorization, Cookie) from the input value in both RECORD and
        REPLAY modes to ensure consistent matching. This handles multi-step auth
        flows like DigestAuth where we can't simulate the full challenge-response.
        """
        # Fire request hooks to modify request before mock lookup
        # This matches RECORD behavior where hooks run before the request is sent
        # Use async version to properly await async hooks
        await self._fire_request_hooks_async(client, request)

        method = request.method
        url = str(request.url)

        span_info = self._create_client_span(method, url, not sdk.app_ready)
        if not span_info:
            raise RuntimeError(f"Error creating span in replay mode for {method} {url}")

        try:
            with SpanUtils.with_span(span_info):
                # Use IDs from SpanInfo (already formatted)
                mock_response = self._try_get_mock_from_request_sync(
                    sdk,
                    httpx_module,
                    request,
                    span_info.trace_id,
                    span_info.span_id,
                )

                if mock_response is not None:
                    # Fire response hooks on mock response
                    # This matches RECORD behavior where hooks run after response
                    # Use async version to properly await async hooks
                    await self._fire_response_hooks_async(client, mock_response)
                    return mock_response

                # No mock found - raise error in REPLAY mode
                raise RuntimeError(f"No mock found for {method} {url} in REPLAY mode")
        finally:
            span_info.span.end()

    async def _handle_record_send_async(
        self,
        client_self: Any,
        request: Any,  # httpx.Request object
        stream: bool,
        is_pre_app_start: bool,
        original_send: Any,
        **kwargs,
    ) -> Any:
        """Handle send in RECORD mode (async).

        Creates a span, makes the real request, and records the response.
        For streaming responses, reads the body to capture it for recording.
        """
        method = request.method
        url = str(request.url)

        span_info = self._create_client_span(method, url, is_pre_app_start)
        if not span_info:
            # Span creation failed (trace blocked, etc.) - just make the request
            return await original_send(client_self, request, stream=stream, **kwargs)

        try:
            with SpanUtils.with_span(span_info):
                # Check drop transforms BEFORE making the request
                headers = dict(request.headers)
                if self._transform_engine and self._transform_engine.should_drop_outbound_request(method, url, headers):
                    # Request should be dropped - mark span and raise exception
                    span_info.span.set_attribute(
                        TdSpanAttributes.OUTPUT_VALUE,
                        json.dumps({"bodyProcessingError": "dropped"}),
                    )
                    span_info.span.set_status(Status(OTelStatusCode.ERROR, "Dropped by transform"))
                    raise RequestDroppedByTransform(
                        f"Outbound request to {url} was dropped by transform rule",
                        method,
                        url,
                    )

                # Pre-capture request body before send - file-like objects (BytesIO, etc.)
                # get consumed when httpx sends the request, so we must capture first
                pre_captured_body = self._get_request_body_safely(request)

                # Make the real request
                error = None
                response = None

                try:
                    response = await original_send(client_self, request, stream=stream, **kwargs)

                    # For streaming responses, we need to read the body to capture it
                    # This is necessary for recording the response data
                    if stream and response is not None:
                        await response.aread()

                    return response
                except Exception as e:
                    error = e
                    raise
                finally:
                    # Finalize span with request/response data
                    await self._finalize_span_from_request_async(
                        span_info.span,
                        request,
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

    def _get_request_body_safely(self, request: Any) -> bytes | None:
        """Safely get request body content.

        For multipart/streaming requests, the body may not be available
        until request.read() is called. This method handles the edge cases.

        Args:
            request: httpx.Request object

        Returns:
            bytes content or None if unavailable
        """
        try:
            # Check if content is already available (non-streaming requests)
            if hasattr(request, "_content") and request._content is not None:
                return request.content

            # For streaming/multipart requests, read the content
            # This is safe because:
            # - In REPLAY mode: we're not sending the actual request anyway
            # - In RECORD mode finalize: the request has already been sent
            request.read()
            return request.content
        except Exception:
            # Content not available - could be streaming or error
            return None

    def _try_get_mock_from_request_sync(
        self,
        sdk: TuskDrift,
        httpx_module: Any,
        request: Any,  # httpx.Request object
        trace_id: str,
        span_id: str,
    ) -> Any:
        """Try to get a mocked response from CLI using Request object (sync version).

        This method extracts request data from the httpx.Request object rather than
        from kwargs. Used by the send() patch which receives a pre-built Request.

        Returns:
            Mocked response object if found, None otherwise
        """
        try:
            method = request.method
            url = str(request.url)
            parsed_url = urlparse(url)

            # Extract headers from Request
            headers = dict(request.headers)
            # Strip auth-related headers to ensure consistent matching between
            # RECORD and REPLAY modes. This matches the behavior in
            # _finalize_span_from_request() which strips Authorization and Cookie.
            headers.pop("authorization", None)
            headers.pop("Authorization", None)
            headers.pop("cookie", None)
            headers.pop("Cookie", None)

            # Extract query params from URL
            # httpx.URL has a .params attribute that returns QueryParams
            params = {}
            if hasattr(request.url, "params"):
                params = dict(request.url.params)

            # Get body from request - handle streaming/multipart bodies
            body_base64 = None
            body_size = 0
            request_body = self._get_request_body_safely(request)
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
                instrumentation_name="HttpxInstrumentation",
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
            return self._create_mock_response(httpx_module, mock_response_output.response, method, url)

        except Exception as e:
            logger.error(f"Error getting mock for request {request.method} {request.url}: {e}")
            return None

    def _create_mock_response(self, httpx_module: Any, mock_data: dict[str, Any], method: str, url: str) -> Any:
        """Create a mocked httpx.Response object.

        Args:
            httpx_module: The httpx module
            mock_data: Mock response data from CLI
            method: HTTP method
            url: Request URL

        Returns:
            Mocked Response object
        """
        # Get status code and headers
        status_code = mock_data.get("statusCode", 200)
        headers = dict(mock_data.get("headers", {}))

        # Remove content-encoding and transfer-encoding headers since the body
        # was already decompressed when recorded (httpx auto-decompresses)
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

        # Build history list if redirects were recorded
        history = []
        history_count = int(mock_data.get("historyCount", 0))
        if history_count > 0:
            # Create minimal placeholder Response objects for history
            # These represent the intermediate redirect responses
            for i in range(history_count):
                redirect_request = httpx_module.Request(method.upper(), url if i == 0 else f"redirect_{i}")
                redirect_response = httpx_module.Response(
                    status_code=302,  # Standard redirect status
                    content=b"",
                    request=redirect_request,
                )
                history.append(redirect_response)

        # Create httpx.Response
        # httpx.Response requires a request object - use final URL so response.url is correct
        request = httpx_module.Request(method.upper(), final_url)
        response = httpx_module.Response(
            status_code=status_code,
            headers=headers,
            content=content,
            request=request,
            history=history,
        )

        logger.debug(f"Created mock httpx response: {status_code} for {final_url}")
        return response

    def _finalize_span_from_request(
        self,
        span: Span,
        request: Any,  # httpx.Request object
        response: Any,
        error: Exception | None,
        pre_captured_body: bytes | None = None,
    ) -> None:
        """Finalize span with request/response data, extracting info from Request object.

        This method is used by the send() patch which receives a pre-built Request object
        rather than separate method/url/kwargs.

        Args:
            span: The OpenTelemetry span to finalize
            request: The httpx.Request object
            response: Response object (if successful)
            error: Exception (if failed)
            pre_captured_body: Pre-captured request body bytes (for file-like objects
                that get consumed during send)
        """
        try:
            method = request.method
            url = str(request.url)
            parsed_url = urlparse(url)

            # ===== BUILD INPUT VALUE =====
            headers = dict(request.headers)
            # Strip auth-related headers to ensure consistent matching between
            # RECORD and REPLAY modes. During RECORD, httpx may apply auth internally
            # (especially for multi-step auth like DigestAuth), modifying the request
            # in-place. DigestAuth also adds cookies from the 401 response to the
            # retry request. By excluding these headers from the input value, we
            # ensure mock matching works regardless of auth state.
            headers.pop("authorization", None)
            headers.pop("Authorization", None)
            headers.pop("cookie", None)
            headers.pop("Cookie", None)

            # Extract query params from URL
            params = {}
            if hasattr(request.url, "params"):
                params = dict(request.url.params)

            # Get request body - use pre-captured if available (for file-like objects
            # that get consumed during send), otherwise try to read from request
            body_base64 = None
            body_size = 0
            request_body = (
                pre_captured_body if pre_captured_body is not None else self._get_request_body_safely(request)
            )
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
                response_headers = dict(response.headers)
                response_body_size = 0

                try:
                    # Get response content (httpx Response has .content property)
                    response_bytes = response.content
                    response_body_base64, response_body_size = self._encode_body_to_base64(response_bytes)
                except Exception:
                    response_body_base64 = None
                    response_body_size = 0

                output_value = {
                    "statusCode": response.status_code,
                    "statusMessage": response.reason_phrase or "",
                    "headers": response_headers,
                }

                # Add body fields only if body exists
                if response_body_base64 is not None:
                    output_value["body"] = response_body_base64
                    output_value["bodySize"] = response_body_size

                # Capture redirect information for proper replay
                final_url = str(response.url)
                if final_url != url:  # Only store if redirects occurred
                    output_value["finalUrl"] = final_url

                if response.history:
                    output_value["historyCount"] = len(response.history)

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
            logger.error(f"Error finalizing span for {request.method} {request.url}: {e}")
            span.set_status(Status(OTelStatusCode.ERROR, str(e)))

    async def _finalize_span_from_request_async(
        self,
        span: Span,
        request: Any,  # httpx.Request object
        response: Any,
        error: Exception | None,
        pre_captured_body: bytes | None = None,
    ) -> None:
        """Finalize span with request/response data (async version).

        Delegates to sync version since no async operations are needed.
        """
        self._finalize_span_from_request(span, request, response, error, pre_captured_body)
