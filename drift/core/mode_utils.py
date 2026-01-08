"""Mode utilities for handling RECORD and REPLAY mode logic.

This module provides utilities that abstract common mode-handling patterns,
matching the Node SDK's modeUtils.ts. These utilities help instrumentations
decide how to handle requests based on the SDK mode and app state.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar

from opentelemetry.trace import SpanKind as OTelSpanKind

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Type aliases for handler functions
OriginalFunctionCall = Callable[[], T]
RecordModeHandler = Callable[[bool], T]  # (is_pre_app_start: bool) -> T
ReplayModeHandler = Callable[[], T]
NoOpRequestHandler = Callable[[], T]


def handle_record_mode(
    original_function_call: OriginalFunctionCall[T],
    record_mode_handler: RecordModeHandler[T],
    span_kind: OTelSpanKind,
) -> T:
    """Handle RECORD mode logic for instrumentations.

    This utility abstracts the common record mode pattern of checking for
    current span context and deciding whether to execute record mode logic
    or just call the original function.

    Decision logic:
    - If app NOT ready -> call record_mode_handler(is_pre_app_start=True)
    - If no span context AND not SERVER span, OR span was pre-app-start -> call original_function_call() (skip)
    - Otherwise -> call record_mode_handler(is_pre_app_start=False)

    Args:
        original_function_call: Function that calls the original function when no span context exists
        record_mode_handler: Function that handles record mode logic; receives is_pre_app_start flag
        span_kind: The kind of span being created (determines if this is a server request)

    Returns:
        Result from either original_function_call or record_mode_handler
    """
    from .drift_sdk import TuskDrift
    from .tracing.span_utils import SpanUtils

    try:
        sdk = TuskDrift.get_instance()
        is_app_ready = sdk.is_app_ready()
        current_span_info = SpanUtils.get_current_span_info()
    except Exception as e:
        logger.error(f"ModeUtils error checking app readiness or getting current span info: {e}")
        return original_function_call()

    if not is_app_ready:
        # App not ready - record with is_pre_app_start=True
        return record_mode_handler(True)

    # App is ready - check span context
    is_server_span = span_kind == OTelSpanKind.SERVER

    if (not current_span_info and not is_server_span) or (current_span_info and current_span_info.is_pre_app_start):
        # No span context and not a server request, OR within a pre-app-start span
        # Skip recording - call original function
        return original_function_call()

    # App ready with valid span context - record with is_pre_app_start=False
    return record_mode_handler(False)


def handle_replay_mode(
    replay_mode_handler: ReplayModeHandler[T],
    no_op_request_handler: NoOpRequestHandler[T],
    is_server_request: bool,
) -> T:
    """Handle REPLAY mode logic for instrumentations.

    This utility abstracts the common replay mode pattern of checking if
    the request is a background request.

    Decision logic:
    - If background request (app ready + no parent span + not server request) -> call no_op_request_handler()
    - Otherwise -> call replay_mode_handler()

    Background requests are requests that happen after app startup but outside
    of any trace context (health checks, background jobs, etc.). In REPLAY mode,
    these should return dummy responses instead of querying for mocks.

    Args:
        replay_mode_handler: Function that handles normal replay mode logic (fetches mocks)
        no_op_request_handler: Function that returns a dummy/no-op response for background requests
        is_server_request: True if this is a SERVER span (inbound HTTP request)

    Returns:
        Result from either no_op_request_handler or replay_mode_handler
    """
    from .drift_sdk import TuskDrift
    from .tracing.span_utils import SpanUtils

    sdk = TuskDrift.get_instance()
    is_app_ready = sdk.is_app_ready()
    current_span_info = SpanUtils.get_current_span_info()

    # Background request: App is ready + not within a trace (no parent span) + not a server request
    if is_app_ready and not current_span_info and not is_server_request:
        logger.debug("[ModeUtils] Handling no-op request")
        return no_op_request_handler()

    return replay_mode_handler()


def is_background_request(is_server_request: bool = False) -> bool:
    """Check if the current request is a background request.

    A background request is one that:
    - Happens after app is ready (not pre-app-start)
    - Has no parent span context (not within an existing trace)
    - Is not a server request (not an incoming HTTP request that starts a new trace)

    Background requests should typically be handled with no-op/dummy responses
    in REPLAY mode since they were never recorded.

    Args:
        is_server_request: True if this is a SERVER span type

    Returns:
        True if this is a background request, False otherwise
    """
    from .drift_sdk import TuskDrift
    from .tracing.span_utils import SpanUtils

    sdk = TuskDrift.get_instance()
    is_app_ready = sdk.is_app_ready()
    current_span_info = SpanUtils.get_current_span_info()

    return is_app_ready and not current_span_info and not is_server_request
