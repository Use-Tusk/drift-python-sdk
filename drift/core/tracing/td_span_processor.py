"""Custom OpenTelemetry SpanProcessor for Drift SDK.

This processor converts OpenTelemetry spans to CleanSpanData and exports them
using the existing Drift export infrastructure.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.trace import Span

from ..trace_blocking_manager import TraceBlockingManager, should_block_span
from ..types import TD_INSTRUMENTATION_LIBRARY_NAME, TuskDriftMode, replay_trace_id_context
from ..types import SpanKind as TdSpanKind
from .otel_converter import otel_span_to_clean_span_data

if TYPE_CHECKING:
    from opentelemetry.context import Context

    from ..batch_processor import BatchSpanProcessor
    from .span_exporter import TdSpanExporter

logger = logging.getLogger(__name__)


def _should_skip_proto_validation(clean_span) -> bool:
    """Skip expensive betterproto validation when Rust precomputed payloads are present."""
    if os.getenv("TUSK_SKIP_PROTO_VALIDATION", "0").lower() in {"1", "true", "yes"}:
        return True

    use_rust = os.getenv("TUSK_USE_RUST_CORE", "0").lower() in {"1", "true", "yes"}
    if not use_rust:
        return False

    return (
        clean_span.proto_span_bytes is not None
        or (
        clean_span.input_value_proto_struct_bytes is not None
        and clean_span.output_value_proto_struct_bytes is not None
        )
    )


class TdSpanProcessor(SpanProcessor):
    """Custom SpanProcessor that converts OTel spans to CleanSpanData and exports.

    This processor implements OpenTelemetry's SpanProcessor interface and serves
    as the bridge between OTel's tracing system and Drift's export infrastructure.

    When a span ends:
    1. Convert to CleanSpanData using otel_converter
    2. Apply sampling logic
    3. Apply trace blocking logic
    4. Validate protobuf serialization
    5. Forward to batch processor for export
    """

    def __init__(
        self,
        exporter: TdSpanExporter,
        mode: TuskDriftMode,
        sampling_rate: float = 1.0,
        app_ready: bool = False,
        environment: str | None = None,
    ) -> None:
        """Initialize the TdSpanProcessor.

        Args:
            exporter: The TdSpanExporter to use for span export
            mode: SDK mode (RECORD, REPLAY, DISABLED)
            sampling_rate: Sampling rate (0.0-1.0)
            app_ready: Whether the application is ready
            environment: Environment name to include on spans
        """
        self._exporter = exporter
        self._mode = mode
        self._sampling_rate = sampling_rate
        self._app_ready = app_ready
        self._environment = environment

        # We'll import and create batch processor lazily to avoid circular imports
        self._batch_processor: BatchSpanProcessor | None = None
        self._started = False

    def start(self) -> None:
        """Start the processor and batch processor."""
        if self._started:
            return

        # Import here to avoid circular dependency
        from ..batch_processor import BatchSpanProcessor, BatchSpanProcessorConfig

        self._batch_processor = BatchSpanProcessor(
            exporter=self._exporter,
            config=BatchSpanProcessorConfig(),
        )
        self._batch_processor.start()
        self._started = True
        logger.debug("TdSpanProcessor started")

    def on_start(
        self,
        span: Span,
        parent_context: Context | None = None,
    ) -> None:
        """Called when a span starts.

        We don't need to do anything on start - all processing happens on end.
        """
        pass

    def on_end(self, span: ReadableSpan) -> None:
        """Called when a span ends.

        This is where we convert the OTel span to CleanSpanData and export it.

        Args:
            span: The ReadableSpan that just ended
        """
        logger.debug(f"[TdSpanProcessor] on_end called for span: {span.name}")

        if not self._started:
            logger.warning("TdSpanProcessor.on_end called but processor not started")
            return

        if self._mode == TuskDriftMode.DISABLED:
            logger.debug(f"[TdSpanProcessor] Skipping span '{span.name}' - mode is DISABLED")
            return

        # Only process spans created by this SDK
        instrumentation_scope = getattr(span, "instrumentation_scope", None)
        if instrumentation_scope is None or instrumentation_scope.name != TD_INSTRUMENTATION_LIBRARY_NAME:
            logger.debug(
                f"[TdSpanProcessor] Skipping span '{span.name}' - not from Drift SDK "
                f"(scope: {instrumentation_scope.name if instrumentation_scope else 'None'})"
            )
            return

        try:
            # Convert OTel span to CleanSpanData
            logger.debug(f"[TdSpanProcessor] Converting span '{span.name}' to CleanSpanData")
            clean_span = otel_span_to_clean_span_data(span, self._environment)
            logger.debug(
                f"[TdSpanProcessor] Converted span: {clean_span.name} (package: {clean_span.package_name}, kind: {clean_span.kind})"
            )

            # Apply trace blocking logic
            trace_blocking_manager = TraceBlockingManager.get_instance()
            if trace_blocking_manager.is_trace_blocked(clean_span.trace_id):
                logger.debug(f"Skipping span '{clean_span.name}' - trace {clean_span.trace_id} is blocked")
                return

            # Check if this span should block the trace (size limit or server error)
            if should_block_span(clean_span):
                return

            # Validate protobuf serialization
            if not _should_skip_proto_validation(clean_span):
                try:
                    clean_span.to_proto()
                except Exception as e:
                    logger.error(f"Failed to serialize span to protobuf: {e}")
                    return

            # Handle REPLAY mode inbound spans
            if self._mode == TuskDriftMode.REPLAY and clean_span.kind == TdSpanKind.SERVER:
                from ..drift_sdk import TuskDrift

                # Set the trace ID to the replay trace ID, CLI will use this to match the trace test with the result
                replay_trace_id = replay_trace_id_context.get()
                if replay_trace_id:
                    clean_span.trace_id = replay_trace_id
                else:
                    logger.error("No replay trace ID found, cannot send inbound span for replay")
                    return

                sdk = TuskDrift.get_instance()
                # Check for running loop BEFORE creating the coroutine to avoid
                # "coroutine was never awaited" warnings. If we call the async
                # function first and then fail to schedule it, the coroutine
                # object is created but never executed.
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None

                if loop is not None:
                    loop.create_task(sdk.send_inbound_span_for_replay(clean_span))
                else:
                    # No running loop - run synchronously
                    try:
                        asyncio.run(sdk.send_inbound_span_for_replay(clean_span))
                    except RuntimeError:
                        logger.error("No running loop, cannot send inbound span for replay")
                        pass

            # Export span via batch processor (RECORD mode)
            if self._mode == TuskDriftMode.RECORD:
                logger.debug(f"[TdSpanProcessor] Exporting span '{clean_span.name}' in RECORD mode")
                if self._batch_processor:
                    self._batch_processor.add_span(clean_span)
                    logger.debug(f"[TdSpanProcessor] Span '{clean_span.name}' added to batch processor")
                else:
                    logger.warning("Batch processor not initialized, cannot export span")

        except Exception as e:
            logger.error(f"Error in TdSpanProcessor.on_end: {e}", exc_info=True)

    def shutdown(self) -> None:
        """Shutdown the processor.

        This stops the batch processor and ensures all pending spans are exported.
        """
        if not self._started:
            return

        logger.debug("Shutting down TdSpanProcessor")

        if self._batch_processor:
            self._batch_processor.stop(timeout=30.0)

        self._started = False
        logger.debug("TdSpanProcessor shutdown complete")

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Force flush all pending spans.

        Args:
            timeout_millis: Maximum time to wait for flush in milliseconds

        Returns:
            True if flush succeeded, False otherwise
        """
        if not self._started or not self._batch_processor:
            return True

        try:
            self._batch_processor._force_flush()
            return True
        except Exception as e:
            logger.error(f"Error during force_flush: {e}")
            return False

    def update_app_ready(self, app_ready: bool) -> None:
        """Update the app_ready flag.

        This is called when the application marks itself as ready.

        Args:
            app_ready: Whether the application is ready
        """
        self._app_ready = app_ready
        logger.debug(f"TdSpanProcessor app_ready updated to {app_ready}")

    def update_sampling_rate(self, sampling_rate: float) -> None:
        """Update the sampling rate.

        Args:
            sampling_rate: New sampling rate (0.0-1.0)
        """
        self._sampling_rate = sampling_rate
        logger.debug(f"TdSpanProcessor sampling_rate updated to {sampling_rate}")
