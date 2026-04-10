"""Context helpers for suppressing child span creation."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_recording_suppressed: ContextVar[bool] = ContextVar("td_recording_suppressed", default=False)


def is_recording_suppressed() -> bool:
    return _recording_suppressed.get()


@contextmanager
def suppress_recording() -> Iterator[None]:
    token = _recording_suppressed.set(True)
    try:
        yield
    finally:
        _recording_suppressed.reset(token)
