"""Generic WSGI response body capture wrapper."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from _typeshed.wsgi import WSGIEnvironment

MAX_BODY_SIZE = 10000  # 10KB limit


class ResponseBodyCapture(Iterable[bytes]):
    """
    Wrapper for WSGI response iterable that captures the response body.

    Captures body chunks up to MAX_BODY_SIZE and calls a callback function
    when the response is complete. Uses a callback pattern to decouple
    WSGI layer from span creation logic.

    Args:
        response: The original WSGI response iterable
        environ: WSGI environ dictionary
        response_data: Dictionary to store response metadata (status, headers)
        on_complete: Callback function called when response is done.
                     Receives (environ, response_data) where response_data
                     includes 'body', 'body_size', and 'body_truncated'.
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
        self._body_truncated = False
        self._closed = False

    def __iter__(self) -> Iterator[bytes]:
        try:
            for chunk in self._response:
                # Capture chunk for body
                if chunk and not self._body_truncated:
                    if self._body_size >= MAX_BODY_SIZE:
                        self._body_truncated = True
                    else:
                        remaining = MAX_BODY_SIZE - self._body_size
                        if len(chunk) > remaining:
                            self._body_parts.append(chunk[:remaining])
                            self._body_size += remaining
                            self._body_truncated = True
                        else:
                            self._body_parts.append(chunk)
                            self._body_size += len(chunk)
                yield chunk
        finally:
            self._finalize()

    def close(self) -> None:
        """Called by WSGI server when response is done."""
        self._finalize()
        if hasattr(self._response, "close"):
            self._response.close()

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
            if self._body_truncated:
                self._response_data["body_truncated"] = True

        # Call completion callback
        self._on_complete(self._environ, self._response_data)
