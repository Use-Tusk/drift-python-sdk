"""Instrumentation for gRPC client library (grpcio)."""

from __future__ import annotations

import json
import logging
from enum import Enum
from typing import Any

from opentelemetry.trace import Span, Status
from opentelemetry.trace import SpanKind as OTelSpanKind
from opentelemetry.trace import StatusCode as OTelStatusCode

from ...core.data_normalization import remove_none_values
from ...core.drift_sdk import TuskDrift
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
from .utils import (
    deserialize_grpc_payload,
    parse_grpc_path,
    serialize_grpc_metadata,
    serialize_grpc_payload,
)

logger = logging.getLogger(__name__)

GRPC_MODULE_NAME = "grpc"


class ReplayResponseType(Enum):
    """How to format the mock response in replay mode."""

    DIRECT = "direct"  # Return mock directly
    WITH_CALL = "with_call"  # Return (mock, MockGrpcCall())
    ITERATOR = "iterator"  # Return iter(mock)
    FUTURE = "future"  # Return MockGrpcFuture(mock)


class GrpcInstrumentation(InstrumentationBase):
    """Instrumentation for the grpcio gRPC client library.

    Patches grpc.Channel methods to:
    - Intercept gRPC requests in REPLAY mode and return mocked responses
    - Capture request/response data as CLIENT spans in RECORD mode

    This instrumentation focuses on client-side gRPC calls (unary and server streaming).
    Server-side instrumentation is not yet implemented.
    """

    def __init__(self, enabled: bool = True) -> None:
        super().__init__(
            name="GrpcInstrumentation",
            module_name="grpc",
            supported_versions="*",
            enabled=enabled,
        )

    def patch(self, module: Any) -> None:
        """Patch the grpc module.

        Patches the Channel class to intercept all RPC calls:
        - unary_unary: Single request, single response
        - unary_stream: Single request, streaming response (server streaming)
        - stream_unary: Streaming request, single response (client streaming)
        - stream_stream: Streaming request, streaming response (bidirectional)
        """
        # CRITICAL: Patch the concrete _channel.Channel class, not just the abstract class.
        # The abstract grpc.Channel defines the interface, but grpc._channel.Channel
        # provides the implementation. Python's MRO resolves methods from the concrete
        # class first, so patching the abstract class has no effect.
        try:
            from grpc import _channel

            if hasattr(_channel, "Channel"):
                self._patch_channel_class(_channel.Channel)
                logger.debug("Patched grpc._channel.Channel (concrete implementation)")
        except ImportError:
            logger.warning("Could not import grpc._channel - falling back to abstract class patching")
            # Fallback to abstract class (less reliable but better than nothing)
            if hasattr(module, "Channel"):
                self._patch_channel_class(module.Channel)

        # Also patch insecure_channel and secure_channel factory functions
        # to return instrumented channels
        self._patch_channel_factories(module)

        logger.info("grpc module instrumented")

    def _patch_channel_factories(self, module: Any) -> None:
        """Patch channel factory functions to instrument created channels."""
        instrumentation_self = self

        # Patch insecure_channel
        if hasattr(module, "insecure_channel"):
            original_insecure_channel = module.insecure_channel

            def patched_insecure_channel(*args, **kwargs):
                channel = original_insecure_channel(*args, **kwargs)
                return instrumentation_self._wrap_channel(channel)

            module.insecure_channel = patched_insecure_channel
            logger.debug("Patched grpc.insecure_channel")

        # Patch secure_channel
        if hasattr(module, "secure_channel"):
            original_secure_channel = module.secure_channel

            def patched_secure_channel(*args, **kwargs):
                channel = original_secure_channel(*args, **kwargs)
                return instrumentation_self._wrap_channel(channel)

            module.secure_channel = patched_secure_channel
            logger.debug("Patched grpc.secure_channel")

    def _patch_channel_class(self, channel_class: Any) -> None:
        """Patch the Channel class methods for all RPC patterns."""
        instrumentation_self = self

        # Store original methods
        original_unary_unary = channel_class.unary_unary
        original_unary_stream = channel_class.unary_stream
        original_stream_unary = channel_class.stream_unary
        original_stream_stream = channel_class.stream_stream

        def patched_unary_unary(channel_self: Any, method: str, *args: Any, **kwargs: Any) -> Any:
            """Patched unary_unary that returns instrumented callable."""
            original_callable = original_unary_unary(channel_self, method, *args, **kwargs)
            return instrumentation_self._wrap_unary_unary_callable(original_callable, method)

        def patched_unary_stream(channel_self: Any, method: str, *args: Any, **kwargs: Any) -> Any:
            """Patched unary_stream that returns instrumented callable."""
            original_callable = original_unary_stream(channel_self, method, *args, **kwargs)
            return instrumentation_self._wrap_unary_stream_callable(original_callable, method)

        def patched_stream_unary(channel_self: Any, method: str, *args: Any, **kwargs: Any) -> Any:
            """Patched stream_unary that returns instrumented callable."""
            original_callable = original_stream_unary(channel_self, method, *args, **kwargs)
            return instrumentation_self._wrap_stream_unary_callable(original_callable, method)

        def patched_stream_stream(channel_self: Any, method: str, *args: Any, **kwargs: Any) -> Any:
            """Patched stream_stream that returns instrumented callable."""
            original_callable = original_stream_stream(channel_self, method, *args, **kwargs)
            return instrumentation_self._wrap_stream_stream_callable(original_callable, method)

        channel_class.unary_unary = patched_unary_unary
        channel_class.unary_stream = patched_unary_stream
        channel_class.stream_unary = patched_stream_unary
        channel_class.stream_stream = patched_stream_stream
        logger.debug("Patched grpc.Channel methods (unary_unary, unary_stream, stream_unary, stream_stream)")

    def _wrap_channel(self, channel: Any) -> Any:
        """Wrap an existing channel with instrumented methods.

        This is used for channels created before patching.
        """
        # The channel is already using the patched Channel class methods
        # due to how we patch at the class level
        return channel

    def _wrap_unary_unary_callable(self, original_callable: Any, method: str) -> Any:
        """Wrap a unary-unary callable with instrumentation."""
        instrumentation_self = self

        class InstrumentedUnaryUnaryCallable:
            """Wrapper for unary-unary RPC callable."""

            def __init__(self, original: Any, grpc_method: str):
                self._original = original
                self._method = grpc_method

            def __call__(
                self,
                request: Any,
                timeout: float | None = None,
                metadata: Any = None,
                credentials: Any = None,
                wait_for_ready: bool | None = None,
                compression: Any = None,
            ) -> Any:
                """Make the unary-unary RPC call."""
                return instrumentation_self._handle_unary_unary_call(
                    self._original,
                    self._method,
                    request,
                    timeout=timeout,
                    metadata=metadata,
                    credentials=credentials,
                    wait_for_ready=wait_for_ready,
                    compression=compression,
                )

            def with_call(
                self,
                request: Any,
                timeout: float | None = None,
                metadata: Any = None,
                credentials: Any = None,
                wait_for_ready: bool | None = None,
                compression: Any = None,
            ) -> tuple[Any, Any]:
                """Make the unary-unary RPC call and return (response, call)."""
                return instrumentation_self._handle_unary_unary_with_call(
                    self._original,
                    self._method,
                    request,
                    timeout=timeout,
                    metadata=metadata,
                    credentials=credentials,
                    wait_for_ready=wait_for_ready,
                    compression=compression,
                )

            def future(
                self,
                request: Any,
                timeout: float | None = None,
                metadata: Any = None,
                credentials: Any = None,
                wait_for_ready: bool | None = None,
                compression: Any = None,
            ) -> Any:
                """Make async unary-unary RPC call returning a future."""
                return instrumentation_self._handle_unary_unary_future(
                    self._original,
                    self._method,
                    request,
                    timeout=timeout,
                    metadata=metadata,
                    credentials=credentials,
                    wait_for_ready=wait_for_ready,
                    compression=compression,
                )

        return InstrumentedUnaryUnaryCallable(original_callable, method)

    def _wrap_unary_stream_callable(self, original_callable: Any, method: str) -> Any:
        """Wrap a unary-stream callable with instrumentation."""
        instrumentation_self = self

        class InstrumentedUnaryStreamCallable:
            """Wrapper for unary-stream RPC callable."""

            def __init__(self, original: Any, grpc_method: str):
                self._original = original
                self._method = grpc_method

            def __call__(
                self,
                request: Any,
                timeout: float | None = None,
                metadata: Any = None,
                credentials: Any = None,
                wait_for_ready: bool | None = None,
                compression: Any = None,
            ) -> Any:
                """Make the unary-stream RPC call."""
                return instrumentation_self._handle_unary_stream_call(
                    self._original,
                    self._method,
                    request,
                    timeout=timeout,
                    metadata=metadata,
                    credentials=credentials,
                    wait_for_ready=wait_for_ready,
                    compression=compression,
                )

        return InstrumentedUnaryStreamCallable(original_callable, method)

    def _wrap_stream_unary_callable(self, original_callable: Any, method: str) -> Any:
        """Wrap a stream-unary callable with instrumentation."""
        instrumentation_self = self

        class InstrumentedStreamUnaryCallable:
            """Wrapper for stream-unary RPC callable (client streaming)."""

            def __init__(self, original: Any, grpc_method: str):
                self._original = original
                self._method = grpc_method

            def __call__(
                self,
                request_iterator: Any,
                timeout: float | None = None,
                metadata: Any = None,
                credentials: Any = None,
                wait_for_ready: bool | None = None,
                compression: Any = None,
            ) -> Any:
                """Make the stream-unary RPC call."""
                return instrumentation_self._handle_stream_unary_call(
                    self._original,
                    self._method,
                    request_iterator,
                    timeout=timeout,
                    metadata=metadata,
                    credentials=credentials,
                    wait_for_ready=wait_for_ready,
                    compression=compression,
                )

            def with_call(
                self,
                request_iterator: Any,
                timeout: float | None = None,
                metadata: Any = None,
                credentials: Any = None,
                wait_for_ready: bool | None = None,
                compression: Any = None,
            ) -> tuple[Any, Any]:
                """Make the stream-unary RPC call and return (response, call)."""
                return instrumentation_self._handle_stream_unary_with_call(
                    self._original,
                    self._method,
                    request_iterator,
                    timeout=timeout,
                    metadata=metadata,
                    credentials=credentials,
                    wait_for_ready=wait_for_ready,
                    compression=compression,
                )

            def future(
                self,
                request_iterator: Any,
                timeout: float | None = None,
                metadata: Any = None,
                credentials: Any = None,
                wait_for_ready: bool | None = None,
                compression: Any = None,
            ) -> Any:
                """Make async stream-unary RPC call returning a future."""
                return instrumentation_self._handle_stream_unary_future(
                    self._original,
                    self._method,
                    request_iterator,
                    timeout=timeout,
                    metadata=metadata,
                    credentials=credentials,
                    wait_for_ready=wait_for_ready,
                    compression=compression,
                )

        return InstrumentedStreamUnaryCallable(original_callable, method)

    def _wrap_stream_stream_callable(self, original_callable: Any, method: str) -> Any:
        """Wrap a stream-stream callable with instrumentation."""
        instrumentation_self = self

        class InstrumentedStreamStreamCallable:
            """Wrapper for stream-stream RPC callable (bidirectional streaming)."""

            def __init__(self, original: Any, grpc_method: str):
                self._original = original
                self._method = grpc_method

            def __call__(
                self,
                request_iterator: Any,
                timeout: float | None = None,
                metadata: Any = None,
                credentials: Any = None,
                wait_for_ready: bool | None = None,
                compression: Any = None,
            ) -> Any:
                """Make the stream-stream RPC call."""
                return instrumentation_self._handle_stream_stream_call(
                    self._original,
                    self._method,
                    request_iterator,
                    timeout=timeout,
                    metadata=metadata,
                    credentials=credentials,
                    wait_for_ready=wait_for_ready,
                    compression=compression,
                )

        return InstrumentedStreamStreamCallable(original_callable, method)

    def _build_input_value(self, method: str, request: Any, metadata: Any) -> dict[str, Any]:
        """Build the input value for a gRPC request."""
        grpc_method, service = parse_grpc_path(method)
        readable_body, buffer_map, jsonable_string_map = serialize_grpc_payload(request)
        readable_metadata = serialize_grpc_metadata(metadata)

        input_value = {
            "method": grpc_method,
            "service": service,
            "body": readable_body,
            "metadata": readable_metadata,
            "inputMeta": {
                "bufferMap": buffer_map,
                "jsonableStringMap": jsonable_string_map,
            },
        }

        return input_value

    def _handle_unary_unary_call(
        self,
        original_callable: Any,
        method: str,
        request: Any,
        **kwargs,
    ) -> Any:
        """Handle a unary-unary RPC call."""
        sdk = TuskDrift.get_instance()

        if sdk.mode == TuskDriftMode.DISABLED:
            return original_callable(request, **kwargs)

        # Set calling_library_context to suppress socket instrumentation warnings
        context_token = calling_library_context.set("grpc")
        try:

            def original_call():
                return original_callable(request, **kwargs)

            metadata = kwargs.get("metadata")
            input_value = self._build_input_value(method, request, metadata)

            if sdk.mode == TuskDriftMode.REPLAY:
                return handle_replay_mode(
                    replay_mode_handler=lambda: self._handle_replay_unary_unary(sdk, method, input_value, request),
                    no_op_request_handler=lambda: self._get_default_response(),
                    is_server_request=False,
                )

            return handle_record_mode(
                original_function_call=original_call,
                record_mode_handler=lambda is_pre_app_start: self._handle_record_unary_unary(
                    original_callable, method, request, input_value, is_pre_app_start, **kwargs
                ),
                span_kind=OTelSpanKind.CLIENT,
            )
        finally:
            calling_library_context.reset(context_token)

    def _handle_unary_unary_with_call(
        self,
        original_callable: Any,
        method: str,
        request: Any,
        **kwargs,
    ) -> tuple[Any, Any]:
        """Handle a unary-unary RPC call with call object returned."""
        sdk = TuskDrift.get_instance()

        if sdk.mode == TuskDriftMode.DISABLED:
            return original_callable.with_call(request, **kwargs)

        context_token = calling_library_context.set("grpc")
        try:

            def original_call():
                return original_callable.with_call(request, **kwargs)

            metadata = kwargs.get("metadata")
            input_value = self._build_input_value(method, request, metadata)

            if sdk.mode == TuskDriftMode.REPLAY:
                return handle_replay_mode(
                    replay_mode_handler=lambda: self._handle_replay_unary_unary_with_call(
                        sdk, method, input_value, request
                    ),
                    no_op_request_handler=lambda: (self._get_default_response(), None),
                    is_server_request=False,
                )

            return handle_record_mode(
                original_function_call=original_call,
                record_mode_handler=lambda is_pre_app_start: self._handle_record_unary_unary_with_call(
                    original_callable, method, request, input_value, is_pre_app_start, **kwargs
                ),
                span_kind=OTelSpanKind.CLIENT,
            )
        finally:
            calling_library_context.reset(context_token)

    def _handle_unary_unary_future(
        self,
        original_callable: Any,
        method: str,
        request: Any,
        **kwargs,
    ) -> Any:
        """Handle an async unary-unary RPC call (future).

        Wraps the returned Future to intercept result() calls for recording/replay.
        """
        sdk = TuskDrift.get_instance()

        if sdk.mode == TuskDriftMode.DISABLED:
            return original_callable.future(request, **kwargs)

        context_token = calling_library_context.set("grpc")
        try:
            metadata = kwargs.get("metadata")
            input_value = self._build_input_value(method, request, metadata)

            if sdk.mode == TuskDriftMode.REPLAY:
                return handle_replay_mode(
                    replay_mode_handler=lambda: self._handle_replay_unary_unary_future(
                        sdk, method, input_value, request
                    ),
                    no_op_request_handler=lambda: MockGrpcFuture(None),
                    is_server_request=False,
                )

            def original_call():
                return original_callable.future(request, **kwargs)

            return handle_record_mode(
                original_function_call=original_call,
                record_mode_handler=lambda is_pre_app_start: self._handle_record_unary_unary_future(
                    original_callable, method, request, input_value, is_pre_app_start, **kwargs
                ),
                span_kind=OTelSpanKind.CLIENT,
            )
        finally:
            calling_library_context.reset(context_token)

    def _handle_unary_stream_call(
        self,
        original_callable: Any,
        method: str,
        request: Any,
        **kwargs,
    ) -> Any:
        """Handle a unary-stream RPC call."""
        sdk = TuskDrift.get_instance()

        if sdk.mode == TuskDriftMode.DISABLED:
            return original_callable(request, **kwargs)

        context_token = calling_library_context.set("grpc")
        try:

            def original_call():
                return original_callable(request, **kwargs)

            metadata = kwargs.get("metadata")
            input_value = self._build_input_value(method, request, metadata)

            if sdk.mode == TuskDriftMode.REPLAY:
                return handle_replay_mode(
                    replay_mode_handler=lambda: self._handle_replay_unary_stream(sdk, method, input_value, request),
                    no_op_request_handler=lambda: iter([]),
                    is_server_request=False,
                )

            return handle_record_mode(
                original_function_call=original_call,
                record_mode_handler=lambda is_pre_app_start: self._handle_record_unary_stream(
                    original_callable, method, request, input_value, is_pre_app_start, **kwargs
                ),
                span_kind=OTelSpanKind.CLIENT,
            )
        finally:
            calling_library_context.reset(context_token)

    def _get_default_response(self) -> None:
        """Return default response for background requests in REPLAY mode."""
        logger.debug("[GrpcInstrumentation] Returning default response for background request")
        return None

    def _handle_record_unary_unary(
        self,
        original_callable: Any,
        method: str,
        request: Any,
        input_value: dict[str, Any],
        is_pre_app_start: bool,
        **kwargs,
    ) -> Any:
        """Handle unary-unary call in RECORD mode."""
        span_info = self._create_client_span("grpc.client.unary", is_pre_app_start)

        if not span_info:
            return original_callable(request, **kwargs)

        error = None
        response = None
        response_metadata = None
        trailing_metadata = None

        try:
            with SpanUtils.with_span(span_info):
                try:
                    response, call = original_callable.with_call(request, **kwargs)
                    response_metadata = call.initial_metadata()
                    trailing_metadata = call.trailing_metadata()
                    return response
                except Exception as e:
                    error = e
                    raise
                finally:
                    self._finalize_unary_span(
                        span_info.span,
                        input_value,
                        response,
                        error,
                        response_metadata,
                        trailing_metadata,
                    )
        finally:
            span_info.span.end()

    def _handle_record_unary_unary_with_call(
        self,
        original_callable: Any,
        method: str,
        request: Any,
        input_value: dict[str, Any],
        is_pre_app_start: bool,
        **kwargs,
    ) -> tuple[Any, Any]:
        """Handle unary-unary with_call in RECORD mode."""
        span_info = self._create_client_span("grpc.client.unary", is_pre_app_start)

        if not span_info:
            return original_callable.with_call(request, **kwargs)

        error = None
        response = None
        call = None
        response_metadata = None
        trailing_metadata = None

        try:
            with SpanUtils.with_span(span_info):
                try:
                    response, call = original_callable.with_call(request, **kwargs)
                    response_metadata = call.initial_metadata()
                    trailing_metadata = call.trailing_metadata()
                    return response, call
                except Exception as e:
                    error = e
                    raise
                finally:
                    self._finalize_unary_span(
                        span_info.span,
                        input_value,
                        response,
                        error,
                        response_metadata,
                        trailing_metadata,
                    )
        finally:
            span_info.span.end()

    def _handle_record_unary_stream(
        self,
        original_callable: Any,
        method: str,
        request: Any,
        input_value: dict[str, Any],
        is_pre_app_start: bool,
        **kwargs,
    ) -> Any:
        """Handle unary-stream call in RECORD mode."""
        span_info = self._create_client_span("grpc.client.server_stream", is_pre_app_start)

        if not span_info:
            return original_callable(request, **kwargs)

        # For streaming, we need to wrap the iterator to capture all responses
        instrumentation_self = self

        class RecordingStreamIterator:
            """Iterator that records streaming responses."""

            def __init__(self, original_iterator: Any, span_info_ref: Any, input_val: dict):
                self._original = original_iterator
                self._span_info = span_info_ref
                self._input_value = input_val
                self._responses: list[dict] = []
                self._error: Exception | None = None
                self._finished = False

            def __iter__(self):
                return self

            def __next__(self):
                try:
                    response = next(self._original)
                    # Serialize the response
                    readable_body, buffer_map, jsonable_string_map = serialize_grpc_payload(response)
                    self._responses.append(
                        {
                            "body": readable_body,
                            "bufferMap": buffer_map,
                            "jsonableStringMap": jsonable_string_map,
                        }
                    )
                    return response
                except StopIteration:
                    self._finish()
                    raise
                except Exception as e:
                    self._error = e
                    self._finish()
                    raise

            def _finish(self):
                if self._finished:
                    return
                self._finished = True

                try:
                    instrumentation_self._finalize_stream_span(
                        self._span_info.span,
                        self._input_value,
                        self._responses,
                        self._error,
                        self._original,
                    )
                finally:
                    self._span_info.span.end()

        # Get the original iterator and wrap it
        try:
            with SpanUtils.with_span(span_info):
                original_iterator = original_callable(request, **kwargs)
                return RecordingStreamIterator(original_iterator, span_info, input_value)
        except Exception as e:
            # If we fail to even start the stream, finalize and re-raise
            self._finalize_stream_span(span_info.span, input_value, [], e, None)
            span_info.span.end()
            raise

    def _handle_record_unary_unary_future(
        self,
        original_callable: Any,
        method: str,
        request: Any,
        input_value: dict[str, Any],
        is_pre_app_start: bool,
        **kwargs,
    ) -> Any:
        """Handle unary-unary future call in RECORD mode."""
        span_info = self._create_client_span("grpc.client.unary", is_pre_app_start)

        if not span_info:
            return original_callable.future(request, **kwargs)

        # Create a wrapper future that records the result when accessed
        instrumentation_self = self

        class RecordingFuture:
            """Future wrapper that records the result when accessed."""

            def __init__(self, original_future: Any, span_info_ref: Any, input_val: dict):
                self._original = original_future
                self._span_info = span_info_ref
                self._input_value = input_val
                self._recorded = False

            def result(self, timeout: float | None = None) -> Any:
                """Get the result and record it."""
                error = None
                response = None
                response_metadata = None
                trailing_metadata = None

                try:
                    response = self._original.result(timeout=timeout)
                    # Try to get metadata
                    if hasattr(self._original, "initial_metadata"):
                        response_metadata = self._original.initial_metadata()
                    if hasattr(self._original, "trailing_metadata"):
                        trailing_metadata = self._original.trailing_metadata()
                    return response
                except Exception as e:
                    error = e
                    raise
                finally:
                    if not self._recorded:
                        self._recorded = True
                        instrumentation_self._finalize_unary_span(
                            self._span_info.span,
                            self._input_value,
                            response,
                            error,
                            response_metadata,
                            trailing_metadata,
                        )
                        self._span_info.span.end()

            def exception(self, timeout: float | None = None) -> Any:
                """Get the exception if any."""
                return self._original.exception(timeout=timeout)

            def traceback(self, timeout: float | None = None) -> Any:
                """Get the traceback if any."""
                return self._original.traceback(timeout=timeout)

            def add_done_callback(self, fn: Any) -> None:
                """Add a callback to be called when the future completes."""
                self._original.add_done_callback(fn)

            def cancelled(self) -> bool:
                """Return True if the future was cancelled."""
                return self._original.cancelled()

            def running(self) -> bool:
                """Return True if the future is currently running."""
                return self._original.running()

            def done(self) -> bool:
                """Return True if the future is done."""
                return self._original.done()

            def cancel(self) -> bool:
                """Attempt to cancel the future."""
                return self._original.cancel()

            # gRPC-specific methods
            def initial_metadata(self) -> Any:
                """Get initial metadata."""
                if hasattr(self._original, "initial_metadata"):
                    return self._original.initial_metadata()
                return []

            def trailing_metadata(self) -> Any:
                """Get trailing metadata."""
                if hasattr(self._original, "trailing_metadata"):
                    return self._original.trailing_metadata()
                return []

            def code(self) -> Any:
                """Get status code."""
                if hasattr(self._original, "code"):
                    return self._original.code()
                return None

            def details(self) -> str:
                """Get status details."""
                if hasattr(self._original, "details"):
                    return self._original.details()
                return ""

        # Get the original future and wrap it
        try:
            with SpanUtils.with_span(span_info):
                original_future = original_callable.future(request, **kwargs)
                return RecordingFuture(original_future, span_info, input_value)
        except Exception as e:
            # If we fail to even create the future, finalize and re-raise
            self._finalize_unary_span(span_info.span, input_value, None, e, None, None)
            span_info.span.end()
            raise

    def _handle_stream_unary_call(
        self,
        original_callable: Any,
        method: str,
        request_iterator: Any,
        **kwargs,
    ) -> Any:
        """Handle a stream-unary RPC call (client streaming)."""
        sdk = TuskDrift.get_instance()

        if sdk.mode == TuskDriftMode.DISABLED:
            return original_callable(request_iterator, **kwargs)

        context_token = calling_library_context.set("grpc")
        try:
            # For client streaming, we need to consume the iterator to capture all requests
            # This changes the behavior slightly - the iterator is consumed upfront
            requests_list = list(request_iterator)
            metadata = kwargs.get("metadata")
            input_value = self._build_stream_input_value(method, requests_list, metadata)

            def original_call():
                return original_callable(iter(requests_list), **kwargs)

            if sdk.mode == TuskDriftMode.REPLAY:
                return handle_replay_mode(
                    replay_mode_handler=lambda: self._handle_replay_stream_unary(sdk, method, input_value),
                    no_op_request_handler=lambda: self._get_default_response(),
                    is_server_request=False,
                )

            return handle_record_mode(
                original_function_call=original_call,
                record_mode_handler=lambda is_pre_app_start: self._handle_record_stream_unary(
                    original_callable, method, requests_list, input_value, is_pre_app_start, **kwargs
                ),
                span_kind=OTelSpanKind.CLIENT,
            )
        finally:
            calling_library_context.reset(context_token)

    def _handle_stream_unary_with_call(
        self,
        original_callable: Any,
        method: str,
        request_iterator: Any,
        **kwargs,
    ) -> tuple[Any, Any]:
        """Handle a stream-unary RPC call with call object returned."""
        sdk = TuskDrift.get_instance()

        if sdk.mode == TuskDriftMode.DISABLED:
            return original_callable.with_call(request_iterator, **kwargs)

        context_token = calling_library_context.set("grpc")
        try:
            requests_list = list(request_iterator)
            metadata = kwargs.get("metadata")
            input_value = self._build_stream_input_value(method, requests_list, metadata)

            def original_call():
                return original_callable.with_call(iter(requests_list), **kwargs)

            if sdk.mode == TuskDriftMode.REPLAY:
                return handle_replay_mode(
                    replay_mode_handler=lambda: self._handle_replay_stream_unary_with_call(sdk, method, input_value),
                    no_op_request_handler=lambda: (self._get_default_response(), None),
                    is_server_request=False,
                )

            return handle_record_mode(
                original_function_call=original_call,
                record_mode_handler=lambda is_pre_app_start: self._handle_record_stream_unary_with_call(
                    original_callable, method, requests_list, input_value, is_pre_app_start, **kwargs
                ),
                span_kind=OTelSpanKind.CLIENT,
            )
        finally:
            calling_library_context.reset(context_token)

    def _handle_stream_unary_future(
        self,
        original_callable: Any,
        method: str,
        request_iterator: Any,
        **kwargs,
    ) -> Any:
        """Handle an async stream-unary RPC call (future)."""
        sdk = TuskDrift.get_instance()

        if sdk.mode == TuskDriftMode.DISABLED:
            return original_callable.future(request_iterator, **kwargs)

        context_token = calling_library_context.set("grpc")
        try:
            requests_list = list(request_iterator)
            metadata = kwargs.get("metadata")
            input_value = self._build_stream_input_value(method, requests_list, metadata)

            if sdk.mode == TuskDriftMode.REPLAY:
                return handle_replay_mode(
                    replay_mode_handler=lambda: self._handle_replay_stream_unary_future(sdk, method, input_value),
                    no_op_request_handler=lambda: MockGrpcFuture(None),
                    is_server_request=False,
                )

            def original_call():
                return original_callable.future(iter(requests_list), **kwargs)

            return handle_record_mode(
                original_function_call=original_call,
                record_mode_handler=lambda is_pre_app_start: self._handle_record_stream_unary_future(
                    original_callable, method, requests_list, input_value, is_pre_app_start, **kwargs
                ),
                span_kind=OTelSpanKind.CLIENT,
            )
        finally:
            calling_library_context.reset(context_token)

    def _handle_stream_stream_call(
        self,
        original_callable: Any,
        method: str,
        request_iterator: Any,
        **kwargs,
    ) -> Any:
        """Handle a stream-stream RPC call (bidirectional streaming)."""
        sdk = TuskDrift.get_instance()

        if sdk.mode == TuskDriftMode.DISABLED:
            return original_callable(request_iterator, **kwargs)

        context_token = calling_library_context.set("grpc")
        try:
            # For bidirectional streaming, we need to capture both request and response streams
            requests_list = list(request_iterator)
            metadata = kwargs.get("metadata")
            input_value = self._build_stream_input_value(method, requests_list, metadata)

            def original_call():
                return original_callable(iter(requests_list), **kwargs)

            if sdk.mode == TuskDriftMode.REPLAY:
                return handle_replay_mode(
                    replay_mode_handler=lambda: self._handle_replay_stream_stream(sdk, method, input_value),
                    no_op_request_handler=lambda: iter([]),
                    is_server_request=False,
                )

            return handle_record_mode(
                original_function_call=original_call,
                record_mode_handler=lambda is_pre_app_start: self._handle_record_stream_stream(
                    original_callable, method, requests_list, input_value, is_pre_app_start, **kwargs
                ),
                span_kind=OTelSpanKind.CLIENT,
            )
        finally:
            calling_library_context.reset(context_token)

    def _build_stream_input_value(self, method: str, requests: list[Any], metadata: Any) -> dict[str, Any]:
        """Build the input value for a streaming gRPC request."""
        grpc_method, service = parse_grpc_path(method)
        readable_metadata = serialize_grpc_metadata(metadata)

        # Serialize all requests in the stream
        serialized_requests = []
        combined_buffer_map: dict[str, dict[str, str]] = {}
        combined_jsonable_string_map: dict[str, str] = {}

        for i, request in enumerate(requests):
            readable_body, buffer_map, jsonable_string_map = serialize_grpc_payload(request)
            serialized_requests.append(
                {
                    "body": readable_body,
                    "bufferMap": buffer_map,
                    "jsonableStringMap": jsonable_string_map,
                }
            )
            # Prefix keys to avoid collisions
            for key, value in buffer_map.items():
                combined_buffer_map[f"{i}_{key}"] = value
            for key, value in jsonable_string_map.items():
                combined_jsonable_string_map[f"{i}_{key}"] = value

        input_value = {
            "method": grpc_method,
            "service": service,
            "body": serialized_requests,  # List of request bodies
            "metadata": readable_metadata,
            "inputMeta": {
                "bufferMap": combined_buffer_map,
                "jsonableStringMap": combined_jsonable_string_map,
            },
        }

        return input_value

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _create_client_span(self, span_name: str, is_pre_app_start: bool) -> Any:
        """Create a gRPC CLIENT span with standard attributes.

        Args:
            span_name: Name for the span (e.g., "grpc.client.unary")
            is_pre_app_start: Whether this is before app startup

        Returns:
            SpanInfo object or None if span creation fails
        """
        return SpanUtils.create_span(
            CreateSpanOptions(
                name=span_name,
                kind=OTelSpanKind.CLIENT,
                attributes={
                    TdSpanAttributes.NAME: span_name,
                    TdSpanAttributes.PACKAGE_NAME: GRPC_MODULE_NAME,
                    TdSpanAttributes.INSTRUMENTATION_NAME: "GrpcInstrumentation",
                    TdSpanAttributes.SUBMODULE_NAME: "client",
                    TdSpanAttributes.PACKAGE_TYPE: PackageType.GRPC.name,
                    TdSpanAttributes.IS_PRE_APP_START: is_pre_app_start,
                },
                is_pre_app_start=is_pre_app_start,
            )
        )

    def _handle_replay_generic(
        self,
        sdk: TuskDrift,
        method: str,
        input_value: dict[str, Any],
        span_name: str,
        is_stream: bool,
        response_type: ReplayResponseType,
    ) -> Any:
        """Generic replay handler for all gRPC call types.

        Args:
            sdk: TuskDrift instance
            method: gRPC method path
            input_value: Serialized input for mock matching
            span_name: Name for the span
            is_stream: Whether this is a streaming response
            response_type: How to format the mock response

        Returns:
            Mock response formatted according to response_type
        """
        span_info = self._create_client_span(span_name, not sdk.app_ready)

        if not span_info:
            raise RuntimeError(f"Error creating span in replay mode for gRPC {method}")

        try:
            with SpanUtils.with_span(span_info):
                mock_response = self._try_get_mock(
                    sdk,
                    method,
                    span_info.trace_id,
                    span_info.span_id,
                    input_value,
                    is_stream=is_stream,
                )

                if mock_response is None:
                    raise RuntimeError(f"No mock found for gRPC {method} in REPLAY mode")

                # Format response based on type
                if response_type == ReplayResponseType.DIRECT:
                    return mock_response
                elif response_type == ReplayResponseType.WITH_CALL:
                    return mock_response, MockGrpcCall()
                elif response_type == ReplayResponseType.ITERATOR:
                    return iter(mock_response)
                elif response_type == ReplayResponseType.FUTURE:
                    return MockGrpcFuture(mock_response)
                else:
                    return mock_response
        finally:
            span_info.span.end()

    # =========================================================================
    # Record Mode Handlers
    # =========================================================================

    def _handle_record_stream_unary(
        self,
        original_callable: Any,
        method: str,
        requests: list[Any],
        input_value: dict[str, Any],
        is_pre_app_start: bool,
        **kwargs,
    ) -> Any:
        """Handle stream-unary call in RECORD mode."""
        span_info = self._create_client_span("grpc.client.client_stream", is_pre_app_start)

        if not span_info:
            return original_callable(iter(requests), **kwargs)

        error = None
        response = None
        response_metadata = None
        trailing_metadata = None

        try:
            with SpanUtils.with_span(span_info):
                try:
                    response, call = original_callable.with_call(iter(requests), **kwargs)
                    response_metadata = call.initial_metadata()
                    trailing_metadata = call.trailing_metadata()
                    return response
                except Exception as e:
                    error = e
                    raise
                finally:
                    self._finalize_unary_span(
                        span_info.span,
                        input_value,
                        response,
                        error,
                        response_metadata,
                        trailing_metadata,
                    )
        finally:
            span_info.span.end()

    def _handle_record_stream_unary_with_call(
        self,
        original_callable: Any,
        method: str,
        requests: list[Any],
        input_value: dict[str, Any],
        is_pre_app_start: bool,
        **kwargs,
    ) -> tuple[Any, Any]:
        """Handle stream-unary with_call in RECORD mode."""
        span_info = self._create_client_span("grpc.client.client_stream", is_pre_app_start)

        if not span_info:
            return original_callable.with_call(iter(requests), **kwargs)

        error = None
        response = None
        call = None
        response_metadata = None
        trailing_metadata = None

        try:
            with SpanUtils.with_span(span_info):
                try:
                    response, call = original_callable.with_call(iter(requests), **kwargs)
                    response_metadata = call.initial_metadata()
                    trailing_metadata = call.trailing_metadata()
                    return response, call
                except Exception as e:
                    error = e
                    raise
                finally:
                    self._finalize_unary_span(
                        span_info.span,
                        input_value,
                        response,
                        error,
                        response_metadata,
                        trailing_metadata,
                    )
        finally:
            span_info.span.end()

    def _handle_record_stream_unary_future(
        self,
        original_callable: Any,
        method: str,
        requests: list[Any],
        input_value: dict[str, Any],
        is_pre_app_start: bool,
        **kwargs,
    ) -> Any:
        """Handle stream-unary future call in RECORD mode."""
        span_info = self._create_client_span("grpc.client.client_stream", is_pre_app_start)

        if not span_info:
            return original_callable.future(iter(requests), **kwargs)

        instrumentation_self = self

        class RecordingFuture:
            """Future wrapper that records the result when accessed."""

            def __init__(self, original_future: Any, span_info_ref: Any, input_val: dict):
                self._original = original_future
                self._span_info = span_info_ref
                self._input_value = input_val
                self._recorded = False

            def result(self, timeout: float | None = None) -> Any:
                error = None
                response = None
                response_metadata = None
                trailing_metadata = None

                try:
                    response = self._original.result(timeout=timeout)
                    if hasattr(self._original, "initial_metadata"):
                        response_metadata = self._original.initial_metadata()
                    if hasattr(self._original, "trailing_metadata"):
                        trailing_metadata = self._original.trailing_metadata()
                    return response
                except Exception as e:
                    error = e
                    raise
                finally:
                    if not self._recorded:
                        self._recorded = True
                        instrumentation_self._finalize_unary_span(
                            self._span_info.span,
                            self._input_value,
                            response,
                            error,
                            response_metadata,
                            trailing_metadata,
                        )
                        self._span_info.span.end()

            def exception(self, timeout: float | None = None) -> Any:
                return self._original.exception(timeout=timeout)

            def traceback(self, timeout: float | None = None) -> Any:
                return self._original.traceback(timeout=timeout)

            def add_done_callback(self, fn: Any) -> None:
                self._original.add_done_callback(fn)

            def cancelled(self) -> bool:
                return self._original.cancelled()

            def running(self) -> bool:
                return self._original.running()

            def done(self) -> bool:
                return self._original.done()

            def cancel(self) -> bool:
                return self._original.cancel()

            def initial_metadata(self) -> Any:
                if hasattr(self._original, "initial_metadata"):
                    return self._original.initial_metadata()
                return []

            def trailing_metadata(self) -> Any:
                if hasattr(self._original, "trailing_metadata"):
                    return self._original.trailing_metadata()
                return []

            def code(self) -> Any:
                if hasattr(self._original, "code"):
                    return self._original.code()
                return None

            def details(self) -> str:
                if hasattr(self._original, "details"):
                    return self._original.details()
                return ""

        try:
            with SpanUtils.with_span(span_info):
                original_future = original_callable.future(iter(requests), **kwargs)
                return RecordingFuture(original_future, span_info, input_value)
        except Exception as e:
            self._finalize_unary_span(span_info.span, input_value, None, e, None, None)
            span_info.span.end()
            raise

    def _handle_record_stream_stream(
        self,
        original_callable: Any,
        method: str,
        requests: list[Any],
        input_value: dict[str, Any],
        is_pre_app_start: bool,
        **kwargs,
    ) -> Any:
        """Handle stream-stream call in RECORD mode."""
        span_info = self._create_client_span("grpc.client.bidi_stream", is_pre_app_start)

        if not span_info:
            return original_callable(iter(requests), **kwargs)

        instrumentation_self = self

        class RecordingStreamIterator:
            """Iterator that records streaming responses for bidirectional streaming."""

            def __init__(self, original_iterator: Any, span_info_ref: Any, input_val: dict):
                self._original = original_iterator
                self._span_info = span_info_ref
                self._input_value = input_val
                self._responses: list[dict] = []
                self._error: Exception | None = None
                self._finished = False

            def __iter__(self):
                return self

            def __next__(self):
                try:
                    response = next(self._original)
                    readable_body, buffer_map, jsonable_string_map = serialize_grpc_payload(response)
                    self._responses.append(
                        {
                            "body": readable_body,
                            "bufferMap": buffer_map,
                            "jsonableStringMap": jsonable_string_map,
                        }
                    )
                    return response
                except StopIteration:
                    self._finish()
                    raise
                except Exception as e:
                    self._error = e
                    self._finish()
                    raise

            def _finish(self):
                if self._finished:
                    return
                self._finished = True

                try:
                    instrumentation_self._finalize_stream_span(
                        self._span_info.span,
                        self._input_value,
                        self._responses,
                        self._error,
                        self._original,
                    )
                finally:
                    self._span_info.span.end()

        try:
            with SpanUtils.with_span(span_info):
                original_iterator = original_callable(iter(requests), **kwargs)
                return RecordingStreamIterator(original_iterator, span_info, input_value)
        except Exception as e:
            self._finalize_stream_span(span_info.span, input_value, [], e, None)
            span_info.span.end()
            raise

    # =========================================================================
    # Replay Mode Handlers (using generic handler)
    # =========================================================================

    def _handle_replay_unary_unary(self, sdk: TuskDrift, method: str, input_value: dict[str, Any], request: Any) -> Any:
        """Handle unary-unary call in REPLAY mode."""
        return self._handle_replay_generic(
            sdk,
            method,
            input_value,
            "grpc.client.unary",
            is_stream=False,
            response_type=ReplayResponseType.DIRECT,
        )

    def _handle_replay_unary_unary_with_call(
        self, sdk: TuskDrift, method: str, input_value: dict[str, Any], request: Any
    ) -> tuple[Any, Any]:
        """Handle unary-unary with_call in REPLAY mode."""
        return self._handle_replay_generic(
            sdk,
            method,
            input_value,
            "grpc.client.unary",
            is_stream=False,
            response_type=ReplayResponseType.WITH_CALL,
        )

    def _handle_replay_unary_stream(
        self, sdk: TuskDrift, method: str, input_value: dict[str, Any], request: Any
    ) -> Any:
        """Handle unary-stream call in REPLAY mode."""
        return self._handle_replay_generic(
            sdk,
            method,
            input_value,
            "grpc.client.server_stream",
            is_stream=True,
            response_type=ReplayResponseType.ITERATOR,
        )

    def _handle_replay_unary_unary_future(
        self, sdk: TuskDrift, method: str, input_value: dict[str, Any], request: Any
    ) -> Any:
        """Handle unary-unary future call in REPLAY mode."""
        return self._handle_replay_generic(
            sdk,
            method,
            input_value,
            "grpc.client.unary",
            is_stream=False,
            response_type=ReplayResponseType.FUTURE,
        )

    def _handle_replay_stream_unary(self, sdk: TuskDrift, method: str, input_value: dict[str, Any]) -> Any:
        """Handle stream-unary call in REPLAY mode."""
        return self._handle_replay_generic(
            sdk,
            method,
            input_value,
            "grpc.client.client_stream",
            is_stream=False,
            response_type=ReplayResponseType.DIRECT,
        )

    def _handle_replay_stream_unary_with_call(
        self, sdk: TuskDrift, method: str, input_value: dict[str, Any]
    ) -> tuple[Any, Any]:
        """Handle stream-unary with_call in REPLAY mode."""
        return self._handle_replay_generic(
            sdk,
            method,
            input_value,
            "grpc.client.client_stream",
            is_stream=False,
            response_type=ReplayResponseType.WITH_CALL,
        )

    def _handle_replay_stream_unary_future(self, sdk: TuskDrift, method: str, input_value: dict[str, Any]) -> Any:
        """Handle stream-unary future call in REPLAY mode."""
        return self._handle_replay_generic(
            sdk,
            method,
            input_value,
            "grpc.client.client_stream",
            is_stream=False,
            response_type=ReplayResponseType.FUTURE,
        )

    def _handle_replay_stream_stream(self, sdk: TuskDrift, method: str, input_value: dict[str, Any]) -> Any:
        """Handle stream-stream call in REPLAY mode."""
        return self._handle_replay_generic(
            sdk,
            method,
            input_value,
            "grpc.client.bidi_stream",
            is_stream=True,
            response_type=ReplayResponseType.ITERATOR,
        )

    def _try_get_mock(
        self,
        sdk: TuskDrift,
        method: str,
        trace_id: str,
        span_id: str,
        input_value: dict[str, Any],
        is_stream: bool = False,
    ) -> Any:
        """Try to get a mocked response from CLI."""
        try:
            grpc_method, service = parse_grpc_path(method)
            span_name = "grpc.client.server_stream" if is_stream else "grpc.client.unary"

            # Use centralized mock finding utility
            from ...core.mock_utils import find_mock_response_sync

            mock_response_output = find_mock_response_sync(
                sdk=sdk,
                trace_id=trace_id,
                span_id=span_id,
                name=span_name,
                package_name=GRPC_MODULE_NAME,
                package_type=PackageType.GRPC,
                instrumentation_name="GrpcInstrumentation",
                submodule_name="client",
                input_value=input_value,
                kind=SpanKind.CLIENT,
                is_pre_app_start=not sdk.app_ready,
            )

            if not mock_response_output or not mock_response_output.found:
                logger.debug(f"No mock found for gRPC {method} (trace_id={trace_id})")
                return None

            if mock_response_output.response is None:
                logger.debug(f"Mock found but response data is None for gRPC {method}")
                return None

            return self._create_mock_response(mock_response_output.response, is_stream)

        except Exception as e:
            logger.error(f"Error getting mock for gRPC {method}: {e}")
            return None

    def _create_mock_response(self, mock_data: dict[str, Any], is_stream: bool) -> Any:
        """Create a mocked gRPC response.

        Args:
            mock_data: Mock response data from CLI
            is_stream: Whether this is a streaming response

        Returns:
            Mocked response object or list of responses for streaming
        """
        # Check if it's an error response
        if "error" in mock_data:
            import grpc

            error_info = mock_data["error"]
            status_info = mock_data.get("status", {})
            status_code = status_info.get("code", grpc.StatusCode.UNKNOWN.value[0])

            # Map numeric code to StatusCode
            try:
                grpc_status = grpc.StatusCode(status_code)
            except ValueError:
                grpc_status = grpc.StatusCode.UNKNOWN

            raise grpc.RpcError(grpc_status, error_info.get("message", "Unknown error"))

        # Get the response body
        body = mock_data.get("body")
        buffer_map = mock_data.get("bufferMap", {})
        jsonable_string_map = mock_data.get("jsonableStringMap", {})

        if is_stream:
            # For streams, body should be a list of responses
            if isinstance(body, list):
                responses = []
                for item in body:
                    item_body = item.get("body") if isinstance(item, dict) else item
                    item_buffer_map = item.get("bufferMap", {}) if isinstance(item, dict) else {}
                    item_string_map = item.get("jsonableStringMap", {}) if isinstance(item, dict) else {}
                    restored = deserialize_grpc_payload(item_body, item_buffer_map, item_string_map)
                    # Convert dict to object with attribute access (like protobuf messages)
                    responses.append(self._dict_to_object(restored))
                return responses
            return []

        # For unary, restore the body
        restored_body = deserialize_grpc_payload(body, buffer_map, jsonable_string_map)
        # Convert dict to object with attribute access (like protobuf messages)
        return self._dict_to_object(restored_body)

    def _dict_to_object(self, data: Any) -> Any:
        """Convert a dict to an object with attribute access.

        This allows mock responses to be accessed like protobuf messages:
        response.message instead of response["message"]

        Args:
            data: Dictionary or other value to convert

        Returns:
            MockProtoMessage object or original value if not a dict
        """
        if data is None:
            return None
        if isinstance(data, dict):
            return MockProtoMessage(data)
        if isinstance(data, list):
            return [self._dict_to_object(item) for item in data]
        return data

    def _finalize_unary_span(
        self,
        span: Span,
        input_value: dict[str, Any],
        response: Any,
        error: Exception | None,
        response_metadata: Any,
        trailing_metadata: Any,
    ) -> None:
        """Finalize span with request/response data for unary call."""
        try:
            # Build output value
            output_value: dict[str, Any] = {}
            status = SpanStatus(code=StatusCode.OK, message="")

            if error:
                error_output: dict[str, Any] = {
                    "error": {
                        "message": str(error),
                        "name": type(error).__name__,
                    }
                }

                # Try to get gRPC status from error
                # Use getattr to safely access gRPC-specific error attributes
                code_fn = getattr(error, "code", None)
                if code_fn is not None and callable(code_fn):
                    try:
                        code = code_fn()
                        details_fn = getattr(error, "details", None)
                        trailing_fn = getattr(error, "trailing_metadata", None)
                        error_output["status"] = {
                            "code": code.value[0] if hasattr(code, "value") else int(code),
                            "details": str(details_fn()) if details_fn and callable(details_fn) else str(error),
                            "metadata": serialize_grpc_metadata(
                                trailing_fn() if trailing_fn and callable(trailing_fn) else None
                            ),
                        }
                    except Exception:
                        error_output["status"] = {"code": 2, "details": str(error), "metadata": {}}
                else:
                    error_output["status"] = {"code": 2, "details": str(error), "metadata": {}}

                if response_metadata:
                    error_output["metadata"] = serialize_grpc_metadata(response_metadata)

                output_value = error_output
                status = SpanStatus(code=StatusCode.ERROR, message=str(error))
            elif response is not None:
                # Serialize response
                readable_body, buffer_map, jsonable_string_map = serialize_grpc_payload(response)

                output_value = {
                    "body": readable_body,
                    "metadata": serialize_grpc_metadata(response_metadata),
                    "status": {
                        "code": 0,  # OK
                        "details": "",
                        "metadata": serialize_grpc_metadata(trailing_metadata),
                    },
                    "bufferMap": buffer_map,
                    "jsonableStringMap": jsonable_string_map,
                }

            # Set span attributes
            normalized_input = remove_none_values(input_value)
            normalized_output = remove_none_values(output_value)
            span.set_attribute(TdSpanAttributes.INPUT_VALUE, json.dumps(normalized_input))
            span.set_attribute(TdSpanAttributes.OUTPUT_VALUE, json.dumps(normalized_output))

            # Set status
            if status.code == StatusCode.ERROR:
                span.set_status(Status(OTelStatusCode.ERROR, status.message))
            else:
                span.set_status(Status(OTelStatusCode.OK))

        except Exception as e:
            logger.error(f"Error finalizing gRPC span: {e}")
            span.set_status(Status(OTelStatusCode.ERROR, str(e)))

    def _finalize_stream_span(
        self,
        span: Span,
        input_value: dict[str, Any],
        responses: list[dict],
        error: Exception | None,
        original_iterator: Any,
    ) -> None:
        """Finalize span with request/response data for streaming call."""
        try:
            # Build output value
            output_value: dict[str, Any] = {}
            status = SpanStatus(code=StatusCode.OK, message="")

            if error:
                error_output: dict[str, Any] = {
                    "error": {
                        "message": str(error),
                        "name": type(error).__name__,
                    }
                }

                # Try to get gRPC status from error
                # Use getattr to safely access gRPC-specific error attributes
                code_fn = getattr(error, "code", None)
                if code_fn is not None and callable(code_fn):
                    try:
                        code = code_fn()
                        details_fn = getattr(error, "details", None)
                        error_output["status"] = {
                            "code": code.value[0] if hasattr(code, "value") else int(code),
                            "details": str(details_fn()) if details_fn and callable(details_fn) else str(error),
                            "metadata": {},
                        }
                    except Exception:
                        error_output["status"] = {"code": 2, "details": str(error), "metadata": {}}
                else:
                    error_output["status"] = {"code": 2, "details": str(error), "metadata": {}}

                output_value = error_output
                status = SpanStatus(code=StatusCode.ERROR, message=str(error))
            else:
                # Get metadata from iterator if available
                response_metadata = {}
                trailing_metadata = {}
                if original_iterator:
                    try:
                        if hasattr(original_iterator, "initial_metadata"):
                            response_metadata = serialize_grpc_metadata(original_iterator.initial_metadata())
                        if hasattr(original_iterator, "trailing_metadata"):
                            trailing_metadata = serialize_grpc_metadata(original_iterator.trailing_metadata())
                    except Exception:
                        pass

                output_value = {
                    "body": responses,
                    "metadata": response_metadata,
                    "status": {
                        "code": 0,  # OK
                        "details": "",
                        "metadata": trailing_metadata,
                    },
                    "bufferMap": {},
                    "jsonableStringMap": {},
                }

            # Set span attributes
            normalized_input = remove_none_values(input_value)
            normalized_output = remove_none_values(output_value)
            span.set_attribute(TdSpanAttributes.INPUT_VALUE, json.dumps(normalized_input))
            span.set_attribute(TdSpanAttributes.OUTPUT_VALUE, json.dumps(normalized_output))

            # Set status
            if status.code == StatusCode.ERROR:
                span.set_status(Status(OTelStatusCode.ERROR, status.message))
            else:
                span.set_status(Status(OTelStatusCode.OK))

        except Exception as e:
            logger.error(f"Error finalizing gRPC stream span: {e}")
            span.set_status(Status(OTelStatusCode.ERROR, str(e)))


class MockGrpcCall:
    """Mock gRPC call object for replay mode."""

    def __init__(
        self,
        initial_metadata: list[tuple[str, str | bytes]] | None = None,
        trailing_metadata: list[tuple[str, str | bytes]] | None = None,
    ):
        self._initial_metadata = initial_metadata or []
        self._trailing_metadata = trailing_metadata or []

    def initial_metadata(self) -> list[tuple[str, str | bytes]]:
        return self._initial_metadata

    def trailing_metadata(self) -> list[tuple[str, str | bytes]]:
        return self._trailing_metadata

    def code(self) -> Any:
        import grpc

        return grpc.StatusCode.OK

    def details(self) -> str:
        return ""


class MockGrpcFuture:
    """Mock gRPC future object for replay mode.

    Implements the Future interface to return pre-recorded responses.
    """

    def __init__(
        self,
        result_value: Any,
        initial_metadata: list[tuple[str, str | bytes]] | None = None,
        trailing_metadata: list[tuple[str, str | bytes]] | None = None,
    ):
        self._result_value = result_value
        self._initial_metadata = initial_metadata or []
        self._trailing_metadata = trailing_metadata or []

    def result(self, timeout: float | None = None) -> Any:
        """Return the pre-recorded result."""
        return self._result_value

    def exception(self, timeout: float | None = None) -> None:
        """Return None (no exception for successful mocks)."""
        return None

    def traceback(self, timeout: float | None = None) -> None:
        """Return None (no traceback for successful mocks)."""
        return None

    def add_done_callback(self, fn: Any) -> None:
        """Call the callback immediately (future is already done)."""
        fn(self)

    def cancelled(self) -> bool:
        """Return False (mock futures are never cancelled)."""
        return False

    def running(self) -> bool:
        """Return False (mock futures are already done)."""
        return False

    def done(self) -> bool:
        """Return True (mock futures are already done)."""
        return True

    def cancel(self) -> bool:
        """Return False (cannot cancel a completed future)."""
        return False

    # gRPC-specific methods
    def initial_metadata(self) -> list[tuple[str, str | bytes]]:
        """Return initial metadata."""
        return self._initial_metadata

    def trailing_metadata(self) -> list[tuple[str, str | bytes]]:
        """Return trailing metadata."""
        return self._trailing_metadata

    def code(self) -> Any:
        """Return OK status code."""
        import grpc

        return grpc.StatusCode.OK

    def details(self) -> str:
        """Return empty details."""
        return ""


class MockProtoMessage:
    """Mock protobuf message that allows attribute access to dict values.

    This wrapper makes mock responses behave like protobuf messages,
    supporting both attribute access (response.message) and dict access
    (response["message"]).

    Also handles type coercion for int64 fields that were serialized as strings
    by protobuf's MessageToDict (which converts int64 to strings for JS compat).
    """

    def __init__(self, data: dict[str, Any]):
        # Store data with a private name to avoid conflicts
        object.__setattr__(self, "_data", data)
        # Recursively convert nested dicts and handle type coercion
        for key, value in data.items():
            if isinstance(value, dict):
                data[key] = MockProtoMessage(value)
            elif isinstance(value, list):
                data[key] = [
                    MockProtoMessage(item) if isinstance(item, dict) else self._coerce_type(item) for item in value
                ]
            else:
                data[key] = self._coerce_type(value)

    @staticmethod
    def _coerce_type(value: Any) -> Any:
        """Convert numeric strings back to numbers (int64 → string → int).

        Protobuf's MessageToDict converts int64 to strings to preserve
        precision for JavaScript. We reverse this during replay.
        """
        if isinstance(value, str):
            # Try to convert to int if it looks like a number
            if value.lstrip("-").isdigit():
                try:
                    return int(value)
                except (ValueError, OverflowError):
                    pass
            # Try to convert to float for scientific notation
            elif value.replace(".", "", 1).replace("-", "", 1).replace("e", "", 1).replace("+", "", 1).isdigit():
                try:
                    return float(value)
                except ValueError:
                    pass
        return value

    def __getattr__(self, name: str) -> Any:
        data = object.__getattribute__(self, "_data")
        if name in data:
            return data[name]
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_data":
            object.__setattr__(self, name, value)
        else:
            self._data[name] = value

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __repr__(self) -> str:
        return f"MockProtoMessage({self._data})"

    def __str__(self) -> str:
        return str(self._data)

    def keys(self):
        """Return dict keys."""
        return self._data.keys()

    def values(self):
        """Return dict values."""
        return self._data.values()

    def items(self):
        """Return dict items."""
        return self._data.items()

    def get(self, key: str, default: Any = None) -> Any:
        """Get value with default."""
        return self._data.get(key, default)
