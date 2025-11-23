"""FastAPI test server utilities for integration tests."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI


def find_free_port() -> int:
    """Find a free port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
    return port


class FastAPITestServer:
    """
    A test server wrapper for FastAPI applications.

    Runs FastAPI with uvicorn in a background thread for integration testing.
    Automatically finds a free port and provides clean startup/shutdown.

    Usage:
        with FastAPITestServer(app=my_app) as server:
            response = requests.get(f"{server.base_url}/health")
    """

    def __init__(
        self,
        app: "FastAPI" | None = None,
        host: str = "127.0.0.1",
        port: int | None = None,
    ) -> None:
        self.host = host
        self.port = port or find_free_port()
        self._thread: threading.Thread | None = None
        self._app: FastAPI | None = app
        self._server: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def app(self) -> "FastAPI":
        if self._app is None:
            from fastapi import FastAPI
            self._app = FastAPI()
        return self._app

    @app.setter
    def app(self, value: "FastAPI") -> None:
        self._app = value

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _run_server(self) -> None:
        import uvicorn

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_level="warning",
            loop="asyncio",
        )
        self._server = uvicorn.Server(config)
        self._loop.run_until_complete(self._server.serve())

    def start(self, wait_seconds: float = 1.0) -> None:
        self._thread = threading.Thread(target=self._run_server, daemon=True)
        self._thread.start()
        time.sleep(wait_seconds)

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        self._server = None
        self._loop = None

    def __enter__(self) -> "FastAPITestServer":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()
