"""Generic WSGI response body capture wrapper."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from _typeshed.wsgi import WSGIEnvironment


class ResponseBodyCapture(Iterable[bytes]):
    """
    Wrapper for WSGI response iterable that captures the response body.

    Captures all body chunks and calls a callback function when the response
    is complete. Uses a callback pattern to decouple WSGI layer from span
    creation logic.

    No truncation at capture time - span-level 1MB blocking at export handles
    oversized spans.

    Args:
        response: The original WSGI response iterable
        environ: WSGI environ dictionary
        response_data: Dictionary to store response metadata (status, headers)
        on_complete: Callback function called when response is done.
                     Receives (environ, response_data) where response_data
                     includes 'body' and 'body_size'.
    """

    def __init__(
        self,
        response: Iterable[bytes],
        environ: WSGIEnvironment,
        response_data: dict[str, Any],
        on_complete: Callable[[WSGIEnvironment, dict[str, Any]], None],
    ):
        self._response = response
        self._environ = environ
        self._response_data = response_data
        self._on_complete = on_complete
        self._body_parts: list[bytes] = []
        self._body_size = 0
        self._closed = False

    def __iter__(self) -> Iterator[bytes]:
        try:
            for chunk in self._response:
                # Capture chunk for body (no truncation)
                if chunk:
                    self._body_parts.append(chunk)
                    self._body_size += len(chunk)
                yield chunk
        finally:
            self._finalize()

    def close(self) -> None:
        """Called by WSGI server when response is done."""
        self._finalize()
        close_method = getattr(self._response, "close", None)
        if close_method is not None:
            close_method()

    def _finalize(self) -> None:
        """Capture the span with collected response body."""
        if self._closed:
            return
        self._closed = True

        # Add response body to response_data
        if self._body_parts:
            body = b"".join(self._body_parts)
            self._response_data["body"] = body
            self._response_data["body_size"] = len(body)

        # Call completion callback
        self._on_complete(self._environ, self._response_data)
