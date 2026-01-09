"""Socket instrumentation for detecting unpatched dependencies in REPLAY mode.

This instrumentation monitors TCP socket operations to detect when unpatched
libraries make network calls during request handling. When detected, it logs
a warning and alerts the CLI so developers know which dependencies need
instrumentation.

Only active in REPLAY mode.
"""

from __future__ import annotations

import logging
import traceback
import weakref
from types import ModuleType
from typing import Any

from ...core.tracing.span_utils import SpanUtils
from ...core.types import TuskDriftMode
from ..base import InstrumentationBase

logger = logging.getLogger(__name__)

# Maximum number of logged span keys to cache before clearing
MAX_LOGGED_SPANS = 1000


class SocketInstrumentation(InstrumentationBase):
    """Instrumentation to detect unpatched dependencies via TCP socket monitoring.

    This instrumentation patches socket.socket methods (connect, send, sendall)
    to detect when TCP calls are made from within a SERVER span context without
    going through a patched library.

    Detection logic:
    - If spanKind == SERVER (inbound request context)
    - AND callingLibrary != "ProtobufCommunicator" (not SDK's own communication)
    - Then log warning and send alert to CLI
    """

    def __init__(self, mode: TuskDriftMode = TuskDriftMode.DISABLED, enabled: bool = True) -> None:
        """Initialize socket instrumentation.

        Args:
            mode: The SDK mode (RECORD, REPLAY, DISABLED)
            enabled: Whether instrumentation is enabled
        """
        self.mode = mode
        self._logged_spans: set[str] = set()
        # Track sockets that have called connect() - these are outbound sockets
        # HTTP response sockets (from accept()) never call connect(), so they won't be tracked
        # Using WeakSet so sockets can be garbage collected when no longer referenced
        self._outbound_sockets: weakref.WeakSet[Any] = weakref.WeakSet()

        # Only enable in REPLAY mode
        should_enable = enabled and mode == TuskDriftMode.REPLAY

        super().__init__(
            name="SocketInstrumentation",
            module_name="socket",
            supported_versions="*",
            enabled=should_enable,
        )

        if should_enable:
            logger.debug("[SocketInstrumentation] Initialized in REPLAY mode")

    def patch(self, module: ModuleType) -> None:
        """Patch socket.socket methods to detect unpatched dependencies.

        Args:
            module: The socket module to patch
        """
        if self.mode != TuskDriftMode.REPLAY:
            logger.debug("[SocketInstrumentation] Not in REPLAY mode, skipping patch")
            return

        socket_class = getattr(module, "socket", None)
        if socket_class is None:
            logger.warning("[SocketInstrumentation] socket.socket class not found")
            return

        # Store original methods exactly like the working test pattern
        original_connect = socket_class.connect
        original_send = socket_class.send
        original_sendall = socket_class.sendall

        instrumentation = self

        # Patch connect - always track and detect
        def patched_connect(self: Any, *args: Any, **kwargs: Any) -> Any:
            """Patched socket.connect method."""
            # Track this socket as an outbound socket
            instrumentation._outbound_sockets.add(self)
            instrumentation._handle_socket_call("connect", self)
            return original_connect(self, *args, **kwargs)

        # Patch send - only detect if socket is tracked (outbound)
        def patched_send(self: Any, *args: Any, **kwargs: Any) -> int:
            """Patched socket.send method."""
            # Only flag if this is an outbound socket (we saw connect() on it)
            # HTTP response sockets never call connect(), so they won't be in _outbound_sockets
            if self in instrumentation._outbound_sockets:
                instrumentation._handle_socket_call("send", self)
            return original_send(self, *args, **kwargs)

        # Patch sendall - only detect if socket is tracked (outbound)
        def patched_sendall(self: Any, *args: Any, **kwargs: Any) -> None:
            """Patched socket.sendall method."""
            # Only flag if this is an outbound socket (we saw connect() on it)
            # HTTP response sockets never call connect(), so they won't be in _outbound_sockets
            if self in instrumentation._outbound_sockets:
                instrumentation._handle_socket_call("sendall", self)
            return original_sendall(self, *args, **kwargs)

        # Apply patches
        socket_class.connect = patched_connect
        socket_class.send = patched_send
        socket_class.sendall = patched_sendall

        logger.debug("[SocketInstrumentation] Patched socket.socket methods")

    def _handle_socket_call(self, method_name: str, socket_self: Any) -> None:
        """Handle a socket call and detect unpatched dependencies.

        Args:
            method_name: Name of the socket method being called
            socket_self: The socket instance
        """
        from ...core.types import SpanKind, calling_library_context, span_kind_context

        # Get context values
        span_kind = span_kind_context.get()
        calling_library = calling_library_context.get()

        # Detect unpatched dependency:
        # - Must be in a SERVER span (inbound request context)
        # - Must NOT be from ProtobufCommunicator
        if span_kind == SpanKind.SERVER and calling_library != "ProtobufCommunicator":
            self._log_unpatched_dependency(method_name, socket_self)

    def _log_unpatched_dependency(self, method_name: str, socket_self: Any) -> None:
        """Log and alert about an unpatched dependency.

        Args:
            method_name: Name of the socket method that was called
            socket_self: The socket instance
        """
        from ...core.types import replay_trace_id_context

        # Get span ID for deduplication
        span_id = SpanUtils.get_current_span_id()
        span_key = f"{span_id}-{method_name}"

        # Deduplicate: only log once per span+method combination
        if span_key in self._logged_spans:
            return

        self._logged_spans.add(span_key)

        # Memory management: clear cache if too large
        if len(self._logged_spans) > MAX_LOGGED_SPANS:
            logger.debug("[SocketInstrumentation] Clearing logged spans cache")
            self._logged_spans.clear()

        # Get stack trace for debugging
        stack_trace = "".join(traceback.format_stack())

        # Get replay trace ID for alert
        trace_test_server_span_id = replay_trace_id_context.get() or ""

        # Log warning
        logger.warning(
            f"[SocketInstrumentation] TCP {method_name}() called from inbound request context, "
            f"likely unpatched dependency. spanId={span_id}, traceId={trace_test_server_span_id}"
        )
        logger.warning(f"[SocketInstrumentation] Stack trace:\n{stack_trace}")

        # Send alert to CLI (fire-and-forget, async)
        self._send_alert_async(stack_trace, trace_test_server_span_id)

    def _send_alert_async(self, stack_trace: str, trace_test_server_span_id: str) -> None:
        """Send unpatched dependency alert to CLI asynchronously.

        Args:
            stack_trace: The stack trace of the unpatched call
            trace_test_server_span_id: The replay trace ID
        """
        try:
            from ...core.drift_sdk import TuskDrift

            sdk = TuskDrift.get_instance()
            if sdk.communicator is None:
                return

            # Use asyncio to send alert without blocking
            import asyncio

            try:
                loop = asyncio.get_running_loop()
                # If there's a running loop, create a task
                loop.create_task(
                    sdk.communicator.send_unpatched_dependency_alert(
                        stack_trace=stack_trace,
                        trace_test_server_span_id=trace_test_server_span_id,
                    )
                )
            except RuntimeError:
                # No running loop - we're in sync context
                # Fire-and-forget using a daemon thread
                import threading

                def _send_in_thread() -> None:
                    try:
                        if sdk.communicator is not None:
                            asyncio.run(
                                sdk.communicator.send_unpatched_dependency_alert(
                                    stack_trace=stack_trace,
                                    trace_test_server_span_id=trace_test_server_span_id,
                                )
                            )
                    except Exception:
                        pass  # Fire-and-forget, ignore errors

                thread = threading.Thread(target=_send_in_thread, daemon=True)
                thread.start()

        except Exception as e:
            # Alerts are non-critical, don't fail on errors
            logger.debug(f"[SocketInstrumentation] Failed to send alert: {e}")
