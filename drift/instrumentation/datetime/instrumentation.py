"""DateTime instrumentation for mocking time in replay mode.

Uses the `time-machine` library to freeze/travel time during replay.
This ensures all code (including third-party libraries) sees the mocked time.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Global traveller instance
_traveller = None
_current_trace_id: str | None = None

try:
    import time_machine

    HAS_TIME_MACHINE = True
except ImportError:
    HAS_TIME_MACHINE = False
    logger.debug("time-machine not installed, datetime mocking disabled")


def start_time_travel(timestamp: datetime | str | int | float, trace_id: str | None = None) -> bool:
    """Start time travel to the given timestamp.

    Args:
        timestamp: The timestamp to travel to. Can be:
            - datetime object
            - ISO format string (e.g., "2024-01-15T12:00:00Z")
            - Unix timestamp (int or float)
        trace_id: Optional trace ID to associate with this time travel session

    Returns:
        True if time travel started, False if time-machine not available
    """
    global _traveller, _current_trace_id

    if not HAS_TIME_MACHINE:
        logger.debug("time-machine not installed, skipping time travel")
        return False

    # Parse timestamp to datetime if needed
    if isinstance(timestamp, (int, float)):
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    elif isinstance(timestamp, str):
        ts = timestamp.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
    else:
        dt = timestamp

    # Stop existing travel if any
    stop_time_travel()

    # Start new time travel (tick=True means time moves forward from this point)
    _traveller = time_machine.travel(dt, tick=True)
    _traveller.start()
    _current_trace_id = trace_id

    logger.info(f"Time travel started: {dt.isoformat()}")
    return True


def stop_time_travel() -> None:
    """Stop time travel and return to real time."""
    global _traveller, _current_trace_id

    if _traveller:
        _traveller.stop()
        _traveller = None
        _current_trace_id = None
        logger.debug("Time travel stopped")
