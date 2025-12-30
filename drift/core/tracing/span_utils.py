"""Centralized span management utilities for Drift SDK.

This module provides utilities for creating and managing OpenTelemetry spans
with Drift-specific attributes, following patterns from the Node.js SDK.
"""

from __future__ import annotations

import json
import logging
import traceback
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import SpanKind as OTelSpanKind
from opentelemetry.trace import Status, StatusCode

from .td_attributes import TdSpanAttributes

if TYPE_CHECKING:
    from opentelemetry.context import Context
    from opentelemetry.trace import Span

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class SpanInfo:
    """Span information container.

    Matches the SpanInfo interface from Node.js SDK.
    Contains all necessary information about an active span.
    """

    trace_id: str
    span_id: str
    span: Span
    context: Context
    is_pre_app_start: bool


@dataclass
class CreateSpanOptions:
    """Options for creating a span.

    Matches CreateSpanOptions from Node.js SDK.
    """

    name: str
    kind: OTelSpanKind
    attributes: dict[str, Any] | None = None
    parent_context: Context | None = None
    is_pre_app_start: bool = False


@dataclass
class SpanExecutorOptions:
    """Options for createAndExecuteSpan.

    Matches SpanExecutorOptions from Node.js SDK.
    """

    name: str
    kind: OTelSpanKind
    package_type: str | None = None
    package_name: str = ""
    instrumentation_name: str = ""
    submodule: str = ""
    input_value: dict[str, Any] | None = None
    output_value: dict[str, Any] | None = None
    is_pre_app_start: bool = False
    input_schema_merges: dict | None = None
    metadata: dict | None = None
    stop_recording_child_spans: bool = False


@dataclass
class AddSpanAttributesOptions:
    """Options for adding span attributes.

    Matches AddSpanAttributesOptions from Node.js SDK.
    """

    name: str | None = None
    package_name: str | None = None
    instrumentation_name: str | None = None
    package_type: str | None = None
    submodule: str | None = None
    is_pre_app_start: bool | None = None
    input_value: dict | None = None
    output_value: dict | None = None
    input_schema_merges: dict | None = None
    output_schema_merges: dict | None = None
    metadata: dict | None = None
    transform_metadata: dict | None = None


def format_trace_id(trace_id: int) -> str:
    """Format OpenTelemetry trace ID (int) to hex string."""
    return format(trace_id, "032x")


def format_span_id(span_id: int) -> str:
    """Format OpenTelemetry span ID (int) to hex string."""
    return format(span_id, "016x")


class SpanUtils:
    """Centralized span management utilities.

    Provides static methods for creating and managing OpenTelemetry spans
    with Drift-specific attributes. Matches the structure of SpanUtils.ts
    from the Node.js SDK.
    """

    @staticmethod
    def create_span(options: CreateSpanOptions) -> SpanInfo | None:
        """Create a new span and return span info.

        Matches createSpan() from Node.js SDK.

        Args:
            options: Options for span creation

        Returns:
            SpanInfo containing trace ID, span ID, span, context, and flags.
            Returns None if span creation fails.
        """
        try:
            # Import here to avoid circular dependency
            from ..drift_sdk import TuskDrift

            sdk = TuskDrift.get_instance()
            tracer = sdk.get_tracer()

            # Get parent context
            parent_context = options.parent_context or otel_context.get_current()

            # Check if we should block span creation for this trace
            # (This matches the trace blocking check in Node.js SDK)
            active_span = trace.get_span(parent_context)
            if active_span and active_span.is_recording():
                from ..trace_blocking_manager import TraceBlockingManager

                parent_trace_id = format_trace_id(active_span.get_span_context().trace_id)
                trace_blocking_manager = TraceBlockingManager.get_instance()

                if trace_blocking_manager.is_trace_blocked(parent_trace_id):
                    logger.debug(
                        f"[SpanUtils] Skipping span creation for '{options.name}' - trace {parent_trace_id} is blocked"
                    )
                    return None

            # Create span
            span = tracer.start_span(
                name=options.name,
                context=parent_context,
                kind=options.kind,
                attributes=options.attributes or {},
            )

            # Get span context info
            span_context = span.get_span_context()
            trace_id = format_trace_id(span_context.trace_id)
            span_id = format_span_id(span_context.span_id)

            # Create new context with span active
            new_context = trace.set_span(parent_context, span)

            # Store is_pre_app_start in context (matches Node.js SDK pattern)
            # We'll use span attributes for this instead of context variables
            if options.is_pre_app_start:
                span.set_attribute(TdSpanAttributes.IS_PRE_APP_START, True)

            return SpanInfo(
                trace_id=trace_id,
                span_id=span_id,
                span=span,
                context=new_context,
                is_pre_app_start=options.is_pre_app_start,
            )

        except Exception as e:
            logger.error(f"SpanUtils error creating span: {e}")
            return None

    @staticmethod
    @contextmanager
    def with_span(span_info: SpanInfo):
        """Execute within a span context.

        Matches withSpan() from Node.js SDK.

        Args:
            span_info: The span info containing context

        Yields:
            None
        """
        token = otel_context.attach(span_info.context)
        try:
            yield
        finally:
            otel_context.detach(token)

    @staticmethod
    def create_and_execute_span(
        mode: str,
        original_function_call: Callable[[], T],
        options: SpanExecutorOptions,
        fn: Callable[[SpanInfo], T],
    ) -> T:
        """Execute a function within a properly configured span.

        Matches createAndExecuteSpan() from Node.js SDK.

        If there is an error creating the span:
        - In record mode, the original function will be called
        - In replay mode, an error will be thrown

        Args:
            mode: The mode of the TuskDrift instance
            original_function_call: The function to call if the span is not created
            options: Span configuration options
            fn: Function to execute within the span

        Returns:
            The result of the function execution
        """
        # Check if we should stop recording child spans
        # (Matches Node.js SDK pattern with STOP_RECORDING_CHILD_SPANS_CONTEXT_KEY)
        active_span = trace.get_current_span()
        if active_span and active_span.is_recording():
            # Check if parent has stop_recording flag in attributes
            stop_recording = not active_span.get_span_context().trace_flags.sampled
            if stop_recording:
                logger.debug(
                    f"[SpanUtils] Stopping recording of child spans for "
                    f"packageName: {options.package_name}, "
                    f"instrumentationName: {options.instrumentation_name}"
                )
                return original_function_call()

        span_info: SpanInfo | None = None

        try:
            # Build attributes from options (matches Node.js SDK pattern)
            attributes = {
                TdSpanAttributes.NAME: options.name,
                TdSpanAttributes.PACKAGE_NAME: options.package_name,
                TdSpanAttributes.SUBMODULE_NAME: options.submodule,
                TdSpanAttributes.INSTRUMENTATION_NAME: options.instrumentation_name,
                TdSpanAttributes.IS_PRE_APP_START: options.is_pre_app_start,
            }

            if options.package_type:
                attributes[TdSpanAttributes.PACKAGE_TYPE] = options.package_type

            if options.input_value:
                attributes[TdSpanAttributes.INPUT_VALUE] = json.dumps(options.input_value)

            if options.output_value:
                attributes[TdSpanAttributes.OUTPUT_VALUE] = json.dumps(options.output_value)

            if options.input_schema_merges:
                attributes[TdSpanAttributes.INPUT_SCHEMA_MERGES] = json.dumps(options.input_schema_merges)

            if options.metadata:
                attributes[TdSpanAttributes.METADATA] = json.dumps(options.metadata)

            # Create span
            span_info = SpanUtils.create_span(
                CreateSpanOptions(
                    name=options.name,
                    kind=options.kind,
                    is_pre_app_start=options.is_pre_app_start,
                    attributes=attributes,
                )
            )

        except Exception as e:
            logger.error(f"SpanExecutor error creating span: {e}")
            span_info = None

        if not span_info:
            if mode == "REPLAY":
                # Safe to throw error since we're in replay mode
                raise RuntimeError("Error creating span in replay mode")
            else:
                # Call the original function, don't want SDK errors to propagate to the user
                return original_function_call()

        # Execute function within span context (matches Node.js SDK pattern)
        with SpanUtils.with_span(span_info):
            return fn(span_info)

    @staticmethod
    def get_current_span_info() -> SpanInfo | None:
        """Get the current active span info.

        Matches getCurrentSpanInfo() from Node.js SDK.

        Returns:
            SpanInfo if there's an active span, None otherwise
        """
        try:
            active_span = trace.get_current_span()
            if not active_span or not active_span.is_recording():
                return None

            span_context = active_span.get_span_context()
            trace_id = format_trace_id(span_context.trace_id)
            span_id = format_span_id(span_context.span_id)

            # Check if span has is_pre_app_start attribute
            is_pre_app_start = False
            # Note: We can't easily read attributes from active span
            # So we'll default to False for now

            return SpanInfo(
                trace_id=trace_id,
                span_id=span_id,
                span=active_span,
                context=otel_context.get_current(),
                is_pre_app_start=is_pre_app_start,
            )

        except Exception as e:
            logger.error(f"SpanUtils error getting current span info: {e}")
            return None

    @staticmethod
    def add_span_attributes(span: Span, options: AddSpanAttributesOptions) -> None:
        """Add attributes to a span.

        Matches addSpanAttributes() from Node.js SDK.

        Args:
            span: The span to add attributes to
            options: Attributes to add
        """
        try:
            attributes: dict[str, Any] = {}

            if options.name is not None:
                attributes[TdSpanAttributes.NAME] = options.name

            if options.package_name is not None:
                attributes[TdSpanAttributes.PACKAGE_NAME] = options.package_name

            if options.instrumentation_name is not None:
                attributes[TdSpanAttributes.INSTRUMENTATION_NAME] = options.instrumentation_name

            if options.package_type is not None:
                attributes[TdSpanAttributes.PACKAGE_TYPE] = options.package_type

            if options.submodule is not None:
                attributes[TdSpanAttributes.SUBMODULE_NAME] = options.submodule

            if options.is_pre_app_start is not None:
                attributes[TdSpanAttributes.IS_PRE_APP_START] = options.is_pre_app_start

            if options.input_value is not None:
                attributes[TdSpanAttributes.INPUT_VALUE] = json.dumps(options.input_value)

            if options.output_value is not None:
                attributes[TdSpanAttributes.OUTPUT_VALUE] = json.dumps(options.output_value)

            if options.input_schema_merges is not None:
                attributes[TdSpanAttributes.INPUT_SCHEMA_MERGES] = json.dumps(options.input_schema_merges)

            if options.output_schema_merges is not None:
                attributes[TdSpanAttributes.OUTPUT_SCHEMA_MERGES] = json.dumps(options.output_schema_merges)

            if options.metadata is not None:
                attributes[TdSpanAttributes.METADATA] = json.dumps(options.metadata)

            if options.transform_metadata is not None:
                attributes[TdSpanAttributes.TRANSFORM_METADATA] = json.dumps(options.transform_metadata)

            span.set_attributes(attributes)

        except Exception as e:
            logger.error(f"SpanUtils error adding span attributes: {e}")

    @staticmethod
    def set_status(span: Span, status: Status) -> None:
        """Set span status.

        Matches setStatus() from Node.js SDK.

        Args:
            span: The span to set status on
            status: The status to set
        """
        try:
            span.set_status(status)
        except Exception as e:
            logger.error(f"SpanUtils error setting span status: {e}")

    @staticmethod
    def end_span(
        span: Span,
        status: dict[str, Any] | None = None,
    ) -> None:
        """Set span status and end the span.

        Matches endSpan() from Node.js SDK.

        Spans are only exported once span.end() is called.

        Args:
            span: The span to end
            status: Optional status dict with 'code' and optional 'message'
        """
        try:
            if status:
                code = status.get("code")
                message = status.get("message", "")
                if code == StatusCode.OK:
                    span.set_status(Status(StatusCode.OK, message))
                elif code == StatusCode.ERROR:
                    span.set_status(Status(StatusCode.ERROR, message))

            span.end()

        except Exception as e:
            logger.error(f"SpanUtils error ending span: {e}")

    @staticmethod
    def get_current_trace_id() -> str | None:
        """Extract trace ID from current context.

        Matches getCurrentTraceId() from Node.js SDK.

        Returns:
            Trace ID as hex string, or None if no active span
        """
        try:
            span_info = SpanUtils.get_current_span_info()
            return span_info.trace_id if span_info else None
        except Exception as e:
            logger.error(f"SpanUtils error getting current trace id: {e}")
            return None

    @staticmethod
    def get_current_span_id() -> str | None:
        """Extract span ID from current context.

        Matches getCurrentSpanId() from Node.js SDK.

        Returns:
            Span ID as hex string, or None if no active span
        """
        try:
            span_info = SpanUtils.get_current_span_info()
            return span_info.span_id if span_info else None
        except Exception as e:
            logger.error(f"SpanUtils error getting current span id: {e}")
            return None

    @staticmethod
    def get_trace_info() -> str:
        """Get trace and span IDs as a combined string for logging.

        Matches getTraceInfo() from Node.js SDK.

        Returns:
            String in format "trace=<id> span=<id>" or "no-trace"
        """
        try:
            trace_id = SpanUtils.get_current_trace_id()
            span_id = SpanUtils.get_current_span_id()

            if trace_id and span_id:
                return f"trace={trace_id} span={span_id}"

            return "no-trace"

        except Exception as e:
            logger.error(f"SpanUtils error getting trace info: {e}")
            return "no-trace"

    @staticmethod
    def capture_stack_trace(max_frames: int = 10, filter_drift: bool = True) -> str:
        """Capture current stack trace.

        Args:
            max_frames: Maximum number of stack frames to include
            filter_drift: Whether to filter out Drift SDK frames

        Returns:
            Stack trace as string
        """
        try:
            stack = traceback.format_stack()

            if filter_drift:
                stack = [line for line in stack if "instrumentation" not in line and "drift" not in line.lower()]

            # Return last N frames
            return "".join(stack[-max_frames:])

        except Exception as e:
            logger.error(f"Error capturing stack trace: {e}")
            return ""
