"""Flask test server utilities for integration tests."""

from __future__ import annotations

import socket
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from flask import Flask


def find_free_port() -> int:
    """Find a free port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
    return port


class FlaskTestServer:
    """
    A test server wrapper for Flask applications.

    Runs Flask in a background thread for integration testing.
    Automatically finds a free port and provides clean startup/shutdown.

    Usage:
        with FlaskTestServer(app=my_app) as server:
            response = requests.get(f"{server.base_url}/health")
    """

    def __init__(
        self,
        app: Flask | None = None,
        host: str = "127.0.0.1",
        port: int | None = None,
    ) -> None:
        self.host = host
        self.port = port or find_free_port()
        self._thread: threading.Thread | None = None
        self._app: Flask | None = app
        self._server: Any = None

    @property
    def app(self) -> Flask:
        if self._app is None:
            from flask import Flask

            self._app = Flask(__name__)
        return self._app

    @app.setter
    def app(self, value: Flask) -> None:
        self._app = value

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self, wait_seconds: float = 0.5) -> None:
        from werkzeug.serving import make_server

        self._server = make_server(self.host, self.port, self.app, threaded=True)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        time.sleep(wait_seconds)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def __enter__(self) -> FlaskTestServer:
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()
