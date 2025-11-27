"""Instrumentation for requests HTTP client library."""

from __future__ import annotations

import hashlib
import json
import logging
import time
import traceback
from typing import Any
from urllib.parse import urlparse

from ..base import InstrumentationBase
from ...core.communication.types import MockRequestInput
from ...core.data_normalization import create_mock_input_value
from ...core.drift_sdk import TuskDrift
from ...core.json_schema_helper import JsonSchemaHelper
from ...core.types import CleanSpanData, PackageType, SpanKind, SpanStatus, StatusCode

logger = logging.getLogger(__name__)


class RequestsInstrumentation(InstrumentationBase):
    """Instrumentation for the requests HTTP client library.

    Patches requests.Session.request to:
    - Intercept HTTP requests in REPLAY mode and return mocked responses
    - Capture request/response data as CLIENT spans in RECORD mode
    """

    def __init__(self) -> None:
        super().__init__(module_name="requests")

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

            # Extract trace info
            trace_id = self._generate_trace_id()
            span_id = self._generate_span_id()
            stack_trace = self._get_stack_trace()

            # REPLAY mode: Try to get mock
            if sdk.mode == "REPLAY":
                mock_response = self._try_get_mock(
                    sdk, method, url, trace_id, span_id, stack_trace, **kwargs
                )
                if mock_response is not None:
                    return mock_response

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
                        duration_ms,
                        response,
                        error,
                        kwargs,
                    )

        # Apply patch
        module.Session.request = patched_request
        logger.info("requests.Session.request instrumented")

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
            body = None
            body_size = 0

            # Handle request body
            data = kwargs.get("data")
            json_data = kwargs.get("json")

            if json_data is not None:
                body = json.dumps(json_data)
                body_size = len(body.encode("utf-8"))
            elif data is not None:
                if isinstance(data, (str, bytes)):
                    body = data if isinstance(data, str) else data.decode("utf-8", errors="replace")
                    body_size = len(body.encode("utf-8"))
                else:
                    body = str(data)
                    body_size = len(body.encode("utf-8"))

            raw_input_value = {
                "method": method.upper(),
                "url": url,
                "protocol": parsed_url.scheme,
                "hostname": parsed_url.hostname,
                "port": parsed_url.port,
                "path": parsed_url.path or "/",
                "headers": dict(headers),
                "query": params,
                "body": body,
                "bodySize": body_size,
            }

            input_value = create_mock_input_value(raw_input_value)

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
                kind=SpanKind.CLIENT,
                status=SpanStatus(code=StatusCode.UNSPECIFIED, message=""),
                timestamp=Timestamp(seconds=timestamp_seconds, nanos=timestamp_nanos),
                duration=Duration(seconds=0, nanos=0),
                is_root_span=False,
                is_pre_app_start=False,
            )

            # Request mock from CLI (synchronous for requests library)
            mock_request = MockRequestInput(
                test_id=trace_id,  # Use trace_id as test_id in REPLAY
                outbound_span=mock_span,
            )

            mock_response_output = sdk.request_mock_sync(mock_request)

            if not mock_response_output.found:
                logger.debug(f"No mock found for {method} {url}")
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

        # Set body
        body = mock_data.get("body", "")
        if isinstance(body, str):
            response._content = body.encode("utf-8")
        elif isinstance(body, bytes):
            response._content = body
        else:
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
        duration_ms: float,
        response: Any,
        error: Exception | None,
        request_kwargs: dict[str, Any],
    ) -> None:
        """Create and collect a CLIENT span for the HTTP request.

        Args:
            sdk: TuskDrift instance
            method: HTTP method
            url: Request URL
            trace_id: Trace ID
            span_id: Span ID
            duration_ms: Request duration in milliseconds
            response: Response object (if successful)
            error: Exception (if failed)
            request_kwargs: Original request kwargs
        """
        try:
            parsed_url = urlparse(url)

            # Build input value
            headers = request_kwargs.get("headers", {})
            params = request_kwargs.get("params", {})
            body = None

            data = request_kwargs.get("data")
            json_data = request_kwargs.get("json")

            if json_data is not None:
                body = json.dumps(json_data)
            elif data is not None:
                if isinstance(data, (str, bytes)):
                    body = data if isinstance(data, str) else data.decode("utf-8", errors="replace")
                else:
                    body = str(data)

            input_value = {
                "method": method.upper(),
                "url": url,
                "protocol": parsed_url.scheme,
                "hostname": parsed_url.hostname,
                "port": parsed_url.port,
                "path": parsed_url.path or "/",
                "headers": dict(headers),
                "query": params,
                "body": body,
            }

            # Build output value
            output_value = {}
            status = SpanStatus(code=StatusCode.OK, message="")

            if error:
                output_value = {
                    "errorName": type(error).__name__,
                    "errorMessage": str(error),
                }
                status = SpanStatus(code=StatusCode.ERROR, message=str(error))
            elif response:
                # Extract response data
                response_headers = dict(response.headers)
                response_body = None

                try:
                    # Try to get response text (handles encoding automatically)
                    response_body = response.text
                    # Truncate large responses
                    if len(response_body) > 10240:  # 10KB limit
                        response_body = response_body[:10240]
                except Exception:
                    response_body = None

                output_value = {
                    "statusCode": response.status_code,
                    "statusMessage": response.reason,
                    "headers": response_headers,
                    "body": response_body,
                }

                if response.status_code >= 400:
                    status = SpanStatus(
                        code=StatusCode.ERROR,
                        message=f"HTTP {response.status_code}",
                    )

            # Generate schemas and hashes
            input_result = JsonSchemaHelper.generate_schema_and_hash(input_value)
            output_result = JsonSchemaHelper.generate_schema_and_hash(output_value)

            # Convert timestamp and duration to proper objects
            from ...core.types import Timestamp, Duration

            timestamp_ms = time.time() * 1000
            timestamp_seconds = int(timestamp_ms // 1000)
            timestamp_nanos = int((timestamp_ms % 1000) * 1_000_000)

            duration_seconds = int(duration_ms // 1000)
            duration_nanos = int((duration_ms % 1000) * 1_000_000)

            # Create span
            span = CleanSpanData(
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=None,
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
                is_root_span=False,
                is_pre_app_start=not sdk.app_ready,
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
