"""Instrumentation for httpx HTTP client library."""

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


class HttpxInstrumentation(InstrumentationBase):
    """Instrumentation for the httpx HTTP client library.

    Patches both sync and async clients:
    - httpx.Client.request (sync)
    - httpx.AsyncClient.request (async)

    Supports:
    - Intercept HTTP requests in REPLAY mode and return mocked responses
    - Capture request/response data as CLIENT spans in RECORD mode
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

    def _get_default_response(self, httpx_module: Any, method: str, url: str) -> Any:
        """Create a default 200 OK response for background REPLAY requests.

        This is used when a request is made outside of a trace context in REPLAY mode
        (e.g., background health checks, warmup requests).

        Args:
            httpx_module: The httpx module
            method: HTTP method
            url: Request URL

        Returns:
            A minimal httpx.Response with 200 status
        """
        request = httpx_module.Request(method.upper(), url)
        response = httpx_module.Response(
            status_code=200,
            headers={},
            content=b"",
            request=request,
        )
        logger.debug(f"[HttpxInstrumentation] Returning default 200 OK for background REPLAY request: {method} {url}")
        return response

    def _handle_replay_sync(
        self,
        httpx_module: Any,
        client_self: Any,
        method: str,
        url: str,
        original_request: Any,
        **kwargs,
    ) -> Any:
        """Handle REPLAY mode for sync requests using SpanUtils.

        Creates a span, tries to get a mock, and returns the mock response.
        If no mock is found, falls back to real request.

        Args:
            httpx_module: The httpx module
            client_self: The httpx.Client instance
            method: HTTP method
            url: Request URL
            original_request: Original request function
            **kwargs: Request kwargs

        Returns:
            Mock response if found, real response otherwise
        """
        sdk = TuskDrift.get_instance()
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
                    TdSpanAttributes.INSTRUMENTATION_NAME: "HttpxInstrumentation",
                    TdSpanAttributes.SUBMODULE_NAME: method.upper(),
                    TdSpanAttributes.PACKAGE_TYPE: PackageType.HTTP.name,
                    TdSpanAttributes.IS_PRE_APP_START: not sdk.app_ready,
                },
                is_pre_app_start=not sdk.app_ready,
            )
        )

        if not span_info:
            # Span creation failed, make real request
            logger.warning("[HttpxInstrumentation] REPLAY span creation failed, making real request")
            calling_lib_token = calling_library_context.set("HttpxInstrumentation")
            try:
                return original_request(client_self, method, url, **kwargs)
            finally:
                calling_library_context.reset(calling_lib_token)

        # Use with_span context manager for proper context management
        with SpanUtils.with_span(span_info):
            try:
                # Try to get mock
                mock_response = self._try_get_mock_sync(
                    sdk, httpx_module, method, url, span_info.trace_id, span_info.span_id, **kwargs
                )
                if mock_response is not None:
                    return mock_response

                # No mock found - make real request as fallback
                logger.warning(f"[HttpxInstrumentation] No mock found for REPLAY request: {method} {url}")
                calling_lib_token = calling_library_context.set("HttpxInstrumentation")
                try:
                    return original_request(client_self, method, url, **kwargs)
                finally:
                    calling_library_context.reset(calling_lib_token)
            finally:
                span_info.span.end()

    async def _handle_replay_async(
        self,
        httpx_module: Any,
        client_self: Any,
        method: str,
        url: str,
        original_request: Any,
        **kwargs,
    ) -> Any:
        """Handle REPLAY mode for async requests using SpanUtils.

        Creates a span, tries to get a mock, and returns the mock response.
        If no mock is found, falls back to real request.

        Args:
            httpx_module: The httpx module
            client_self: The httpx.AsyncClient instance
            method: HTTP method
            url: Request URL
            original_request: Original async request function
            **kwargs: Request kwargs

        Returns:
            Mock response if found, real response otherwise
        """
        sdk = TuskDrift.get_instance()
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
                    TdSpanAttributes.INSTRUMENTATION_NAME: "HttpxInstrumentation",
                    TdSpanAttributes.SUBMODULE_NAME: method.upper(),
                    TdSpanAttributes.PACKAGE_TYPE: PackageType.HTTP.name,
                    TdSpanAttributes.IS_PRE_APP_START: not sdk.app_ready,
                },
                is_pre_app_start=not sdk.app_ready,
            )
        )

        if not span_info:
            # Span creation failed, make real request
            logger.warning("[HttpxInstrumentation] REPLAY async span creation failed, making real request")
            calling_lib_token = calling_library_context.set("HttpxInstrumentation")
            try:
                return await original_request(client_self, method, url, **kwargs)
            finally:
                calling_library_context.reset(calling_lib_token)

        # Use with_span context manager for proper context management
        with SpanUtils.with_span(span_info):
            try:
                # Try to get mock
                mock_response = await self._try_get_mock_async(
                    sdk, httpx_module, method, url, span_info.trace_id, span_info.span_id, **kwargs
                )
                if mock_response is not None:
                    return mock_response

                # No mock found - make real request as fallback
                logger.warning(f"[HttpxInstrumentation] No mock found for REPLAY async request: {method} {url}")
                calling_lib_token = calling_library_context.set("HttpxInstrumentation")
                try:
                    return await original_request(client_self, method, url, **kwargs)
                finally:
                    calling_library_context.reset(calling_lib_token)
            finally:
                span_info.span.end()

    def _handle_record_sync(
        self,
        httpx_module: Any,
        client_self: Any,
        method: str,
        url: str,
        original_request: Any,
        is_pre_app_start: bool,
        **kwargs,
    ) -> Any:
        """Handle RECORD mode for sync requests using SpanUtils.

        Creates a span, makes the real request, and captures the response.

        Args:
            httpx_module: The httpx module
            client_self: The httpx.Client instance
            method: HTTP method
            url: Request URL
            original_request: Original request function
            is_pre_app_start: Whether we're in pre-app-start phase
            **kwargs: Request kwargs

        Returns:
            The actual HTTP response
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
                    TdSpanAttributes.INSTRUMENTATION_NAME: "HttpxInstrumentation",
                    TdSpanAttributes.SUBMODULE_NAME: method.upper(),
                    TdSpanAttributes.PACKAGE_TYPE: PackageType.HTTP.name,
                    TdSpanAttributes.IS_PRE_APP_START: is_pre_app_start,
                },
                is_pre_app_start=is_pre_app_start,
            )
        )

        if not span_info:
            # Span creation failed (e.g., trace blocked), proceed without instrumentation
            calling_lib_token = calling_library_context.set("HttpxInstrumentation")
            try:
                return original_request(client_self, method, url, **kwargs)
            finally:
                calling_library_context.reset(calling_lib_token)

        # Check drop transforms before making request
        if self._transform_engine and self._transform_engine.should_drop_outbound_request(
            method.upper(), url, kwargs.get("headers", {})
        ):
            span_info.span.set_attribute(
                TdSpanAttributes.OUTPUT_VALUE,
                json.dumps({"bodyProcessingError": "dropped"}),
            )
            span_info.span.set_status(Status(OTelStatusCode.ERROR, "Dropped by transform"))
            span_info.span.end()

            raise RequestDroppedByTransform(
                f"Outbound request to {url} was dropped by transform rule",
                method.upper(),
                url,
            )

        # Execute request with span context using context manager
        with SpanUtils.with_span(span_info):
            error = None
            response = None

            # Set calling_library_context to prevent socket instrumentation warnings
            calling_lib_token = calling_library_context.set("HttpxInstrumentation")
            try:
                response = original_request(client_self, method, url, **kwargs)
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
                span_info.span.end()

    async def _handle_record_async(
        self,
        httpx_module: Any,
        client_self: Any,
        method: str,
        url: str,
        original_request: Any,
        is_pre_app_start: bool,
        **kwargs,
    ) -> Any:
        """Handle RECORD mode for async requests using SpanUtils.

        Creates a span, makes the real request, and captures the response.

        Args:
            httpx_module: The httpx module
            client_self: The httpx.AsyncClient instance
            method: HTTP method
            url: Request URL
            original_request: Original async request function
            is_pre_app_start: Whether we're in pre-app-start phase
            **kwargs: Request kwargs

        Returns:
            The actual HTTP response
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
                    TdSpanAttributes.INSTRUMENTATION_NAME: "HttpxInstrumentation",
                    TdSpanAttributes.SUBMODULE_NAME: method.upper(),
                    TdSpanAttributes.PACKAGE_TYPE: PackageType.HTTP.name,
                    TdSpanAttributes.IS_PRE_APP_START: is_pre_app_start,
                },
                is_pre_app_start=is_pre_app_start,
            )
        )

        if not span_info:
            # Span creation failed (e.g., trace blocked), proceed without instrumentation
            calling_lib_token = calling_library_context.set("HttpxInstrumentation")
            try:
                return await original_request(client_self, method, url, **kwargs)
            finally:
                calling_library_context.reset(calling_lib_token)

        # Check drop transforms before making request
        if self._transform_engine and self._transform_engine.should_drop_outbound_request(
            method.upper(), url, kwargs.get("headers", {})
        ):
            span_info.span.set_attribute(
                TdSpanAttributes.OUTPUT_VALUE,
                json.dumps({"bodyProcessingError": "dropped"}),
            )
            span_info.span.set_status(Status(OTelStatusCode.ERROR, "Dropped by transform"))
            span_info.span.end()

            raise RequestDroppedByTransform(
                f"Outbound request to {url} was dropped by transform rule",
                method.upper(),
                url,
            )

        # Execute request with span context using context manager
        with SpanUtils.with_span(span_info):
            error = None
            response = None

            # Set calling_library_context to prevent socket instrumentation warnings
            calling_lib_token = calling_library_context.set("HttpxInstrumentation")
            try:
                response = await original_request(client_self, method, url, **kwargs)
                return response
            except Exception as e:
                error = e
                raise
            finally:
                calling_library_context.reset(calling_lib_token)
                # Finalize span with request/response data (async version)
                await self._finalize_span_async(
                    span_info.span,
                    method,
                    url,
                    response,
                    error,
                    kwargs,
                )
                span_info.span.end()

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

    def _patch_sync_client(self, module: Any) -> None:
        """Patch httpx.Client.request for sync HTTP calls."""
        original_request = module.Client.request
        instrumentation_self = self

        def patched_request(client_self, method: str, url: Any, **kwargs):
            """Patched Client.request method."""
            # Convert URL to string if needed
            url_str = str(url)

            sdk = TuskDrift.get_instance()

            # Pass through if SDK is disabled
            if sdk.mode == TuskDriftMode.DISABLED:
                return original_request(client_self, method, url, **kwargs)

            # Define original call for RECORD mode
            def original_call():
                return original_request(client_self, method, url, **kwargs)

            # REPLAY mode: use mode handler with background request support
            if sdk.mode == TuskDriftMode.REPLAY:
                return handle_replay_mode(
                    replay_mode_handler=lambda: instrumentation_self._handle_replay_sync(
                        module, client_self, method, url_str, original_request, **kwargs
                    ),
                    no_op_request_handler=lambda: instrumentation_self._get_default_response(module, method, url_str),
                    is_server_request=False,
                )

            # RECORD mode: use mode handler with is_pre_app_start logic
            return handle_record_mode(
                original_function_call=original_call,
                record_mode_handler=lambda is_pre_app_start: instrumentation_self._handle_record_sync(
                    module, client_self, method, url_str, original_request, is_pre_app_start, **kwargs
                ),
                span_kind=OTelSpanKind.CLIENT,
            )

        # Apply patch
        module.Client.request = patched_request
        logger.info("httpx.Client.request instrumented")

    def _patch_async_client(self, module: Any) -> None:
        """Patch httpx.AsyncClient.request for async HTTP calls."""
        original_request = module.AsyncClient.request
        instrumentation_self = self

        async def patched_request(client_self, method: str, url: Any, **kwargs):
            """Patched AsyncClient.request method."""
            # Convert URL to string if needed
            url_str = str(url)

            sdk = TuskDrift.get_instance()

            # Pass through if SDK is disabled
            if sdk.mode == TuskDriftMode.DISABLED:
                return await original_request(client_self, method, url, **kwargs)

            # Define original call for RECORD mode
            async def original_call():
                return await original_request(client_self, method, url, **kwargs)

            # REPLAY mode: use mode handler with background request support
            if sdk.mode == TuskDriftMode.REPLAY:
                # handle_replay_mode returns coroutine which we await
                return await handle_replay_mode(
                    replay_mode_handler=lambda: instrumentation_self._handle_replay_async(
                        module, client_self, method, url_str, original_request, **kwargs
                    ),
                    no_op_request_handler=lambda: instrumentation_self._get_default_response(module, method, url_str),
                    is_server_request=False,
                )

            # RECORD mode: use mode handler with is_pre_app_start logic
            # handle_record_mode returns coroutine which we await
            return await handle_record_mode(
                original_function_call=original_call,
                record_mode_handler=lambda is_pre_app_start: instrumentation_self._handle_record_async(
                    module, client_self, method, url_str, original_request, is_pre_app_start, **kwargs
                ),
                span_kind=OTelSpanKind.CLIENT,
            )

        # Apply patch
        module.AsyncClient.request = patched_request
        logger.info("httpx.AsyncClient.request instrumented")

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

    def _try_get_mock_sync(
        self,
        sdk: TuskDrift,
        httpx_module: Any,
        method: str,
        url: str,
        trace_id: str,
        span_id: str,
        **kwargs,
    ) -> Any:
        """Try to get a mocked response from CLI (sync version).

        Returns:
            Mocked response object if found, None otherwise
        """
        try:
            # Build request input value
            parsed_url = urlparse(url)

            # Extract request data
            headers = dict(kwargs.get("headers", {}))
            params = dict(kwargs.get("params", {})) if kwargs.get("params") else {}

            # Handle request body - encode to base64
            content = kwargs.get("content")
            json_data = kwargs.get("json")
            data = kwargs.get("data")
            body_base64 = None
            body_size = 0

            if json_data is not None:
                body_base64, body_size = self._encode_body_to_base64(json_data)
            elif content is not None:
                body_base64, body_size = self._encode_body_to_base64(content)
            elif data is not None:
                body_base64, body_size = self._encode_body_to_base64(data)

            raw_input_value = {
                "method": method.upper(),
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
            input_schema_merges = dict(HEADER_SCHEMA_MERGES)
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
                name=f"{method.upper()} {parsed_url.path or '/'}",
                package_name=parsed_url.scheme,
                package_type=PackageType.HTTP,
                instrumentation_name="HttpxInstrumentation",
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
            return self._create_mock_response(httpx_module, mock_response_output.response, method, url)

        except Exception as e:
            logger.error(f"Error getting mock for {method} {url}: {e}")
            return None

    async def _try_get_mock_async(
        self,
        sdk: TuskDrift,
        httpx_module: Any,
        method: str,
        url: str,
        trace_id: str,
        span_id: str,
        **kwargs,
    ) -> Any:
        """Try to get a mocked response from CLI (async version).

        Returns:
            Mocked response object if found, None otherwise
        """
        try:
            # Build request input value
            parsed_url = urlparse(url)

            # Extract request data
            headers = dict(kwargs.get("headers", {}))
            params = dict(kwargs.get("params", {})) if kwargs.get("params") else {}

            # Handle request body - encode to base64
            content = kwargs.get("content")
            json_data = kwargs.get("json")
            data = kwargs.get("data")
            body_base64 = None
            body_size = 0

            if json_data is not None:
                body_base64, body_size = self._encode_body_to_base64(json_data)
            elif content is not None:
                body_base64, body_size = self._encode_body_to_base64(content)
            elif data is not None:
                body_base64, body_size = self._encode_body_to_base64(data)

            raw_input_value = {
                "method": method.upper(),
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
            input_schema_merges = dict(HEADER_SCHEMA_MERGES)
            if body_base64 is not None:
                request_content_type = self._get_content_type_header(headers)
                input_schema_merges["body"] = SchemaMerge(
                    encoding=EncodingType.BASE64,
                    decoded_type=self._get_decoded_type_from_content_type(request_content_type),
                )

            # Use centralized mock finding utility (async version)
            from ...core.mock_utils import find_mock_response_async

            mock_response_output = await find_mock_response_async(
                sdk=sdk,
                trace_id=trace_id,
                span_id=span_id,
                name=f"{method.upper()} {parsed_url.path or '/'}",
                package_name=parsed_url.scheme,
                package_type=PackageType.HTTP,
                instrumentation_name="HttpxInstrumentation",
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
            return self._create_mock_response(httpx_module, mock_response_output.response, method, url)

        except Exception as e:
            logger.error(f"Error getting mock for {method} {url}: {e}")
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
        headers = mock_data.get("headers", {})

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

        # Create httpx.Response
        # httpx.Response requires a request object
        request = httpx_module.Request(method.upper(), url)
        response = httpx_module.Response(
            status_code=status_code,
            headers=headers,
            content=content,
            request=request,
        )

        logger.debug(f"Created mock httpx response: {status_code} for {url}")
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
        """Finalize span with request/response data (sync version).

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
            headers = dict(request_kwargs.get("headers", {}))
            params = dict(request_kwargs.get("params", {})) if request_kwargs.get("params") else {}

            # Get request body and encode to base64
            content = request_kwargs.get("content")
            json_data = request_kwargs.get("json")
            data = request_kwargs.get("data")
            body_base64 = None
            body_size = 0

            if json_data is not None:
                body_base64, body_size = self._encode_body_to_base64(json_data)
            elif content is not None:
                body_base64, body_size = self._encode_body_to_base64(content)
            elif data is not None:
                body_base64, body_size = self._encode_body_to_base64(data)

            input_value = {
                "method": method.upper(),
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

            input_schema_merges = dict(HEADER_SCHEMA_MERGES)
            if body_base64 is not None:
                input_schema_merges["body"] = SchemaMerge(
                    encoding=EncodingType.BASE64,
                    decoded_type=self._get_decoded_type_from_content_type(request_content_type),
                )

            output_schema_merges = dict(HEADER_SCHEMA_MERGES)
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

    async def _finalize_span_async(
        self,
        span: Span,
        method: str,
        url: str,
        response: Any,
        error: Exception | None,
        request_kwargs: dict[str, Any],
    ) -> None:
        """Finalize span with request/response data (async version).

        For httpx async responses, we need to handle the body reading properly.
        """
        try:
            parsed_url = urlparse(url)

            # ===== BUILD INPUT VALUE =====
            headers = dict(request_kwargs.get("headers", {}))
            params = dict(request_kwargs.get("params", {})) if request_kwargs.get("params") else {}

            # Get request body and encode to base64
            content = request_kwargs.get("content")
            json_data = request_kwargs.get("json")
            data = request_kwargs.get("data")
            body_base64 = None
            body_size = 0

            if json_data is not None:
                body_base64, body_size = self._encode_body_to_base64(json_data)
            elif content is not None:
                body_base64, body_size = self._encode_body_to_base64(content)
            elif data is not None:
                body_base64, body_size = self._encode_body_to_base64(data)

            input_value = {
                "method": method.upper(),
                "url": url,
                "protocol": parsed_url.scheme,
                "hostname": parsed_url.hostname,
                "port": parsed_url.port,
                "path": parsed_url.path or "/",
                "headers": headers,
                "query": params,
            }

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
                response_headers = dict(response.headers)
                response_body_size = 0

                try:
                    # For async responses, we need to read content
                    # httpx Response already buffers content when using async with
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

            input_schema_merges = dict(HEADER_SCHEMA_MERGES)
            if body_base64 is not None:
                input_schema_merges["body"] = SchemaMerge(
                    encoding=EncodingType.BASE64,
                    decoded_type=self._get_decoded_type_from_content_type(request_content_type),
                )

            output_schema_merges = dict(HEADER_SCHEMA_MERGES)
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
            logger.error(f"Error finalizing async span for {method} {url}: {e}")
            span.set_status(Status(OTelStatusCode.ERROR, str(e)))
