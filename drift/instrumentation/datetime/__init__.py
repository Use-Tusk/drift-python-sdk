"""DateTime instrumentation for mocking time in replay mode."""

from .instrumentation import (
    HAS_TIME_MACHINE,
    start_time_travel,
    stop_time_travel,
)

__all__ = [
    "start_time_travel",
    "stop_time_travel",
    "HAS_TIME_MACHINE",
]
