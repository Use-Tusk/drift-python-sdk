"""Tests for WSGI handler request lifecycle behavior."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

from drift.instrumentation.wsgi.handler import _create_and_handle_request


class StreamingResponse(Iterable[bytes]):
    def __init__(self, observed: list[tuple[str, bool]]) -> None:
        self._observed = observed
        self._yielded = False

    def __iter__(self) -> Iterator[bytes]:
        from drift.core.no_recording import is_recording_suppressed

        self._observed.append(("iter", is_recording_suppressed()))
        return self

    def __next__(self) -> bytes:
        from drift.core.no_recording import is_recording_suppressed

        self._observed.append(("next", is_recording_suppressed()))
        if self._yielded:
            raise StopIteration
        self._yielded = True
        return b"chunk"

    def close(self) -> None:
        from drift.core.no_recording import is_recording_suppressed

        self._observed.append(("close", is_recording_suppressed()))


def test_skipped_wsgi_request_keeps_suppression_during_streaming_iteration_and_close(mocker) -> None:
    observed: list[tuple[str, bool]] = []
    response = StreamingResponse(observed)

    mocker.patch(
        "drift.instrumentation.wsgi.handler.should_record_inbound_http_request",
        return_value=(False, "not_sampled"),
    )

    def original_wsgi_app(_app: Any, _environ: dict[str, Any], _start_response: Any) -> Iterable[bytes]:
        from drift.core.no_recording import is_recording_suppressed

        observed.append(("call", is_recording_suppressed()))
        return response

    def app(_environ: dict[str, Any], _start_response: Any) -> Iterable[bytes]:
        return response

    wrapped_response = _create_and_handle_request(
        app=app,
        environ={
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/stream",
            "QUERY_STRING": "",
        },
        start_response=lambda status, headers, exc_info=None: None,
        original_wsgi_app=original_wsgi_app,
        framework_name="wsgi",
        instrumentation_name="WsgiInstrumentation",
        transform_engine=None,
        sdk=object(),
        is_pre_app_start=False,
        replay_token=None,
    )

    assert list(wrapped_response) == [b"chunk"]
    close_method = getattr(wrapped_response, "close", None)
    assert close_method is not None
    close_method()

    assert observed == [
        ("call", True),
        ("iter", True),
        ("next", True),
        ("next", True),
        ("close", True),
    ]
