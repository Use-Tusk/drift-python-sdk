"""Coverage snapshot HTTP server for Python SDK.

When TUSK_COVERAGE_PORT is set, starts a tiny HTTP server that manages
coverage.py. On each /snapshot request:
1. Stop coverage collection
2. Get coverage data (which lines were executed since last snapshot)
3. Erase coverage data (reset for next test)
4. Restart coverage collection
5. Return per-file line counts as JSON

This gives clean per-test coverage data - no diffing needed.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

logger = logging.getLogger("TuskDrift")


class CoverageSnapshotHandler(BaseHTTPRequestHandler):
    """HTTP handler for coverage snapshot requests."""

    # Shared state set by start_coverage_server
    cov_instance = None
    source_root = None
    _lock = threading.Lock()

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        if parsed.path == "/snapshot":
            params = parse_qs(parsed.query)
            is_baseline = params.get("baseline", ["false"])[0] == "true"
            self._handle_snapshot(is_baseline)
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_snapshot(self, is_baseline: bool = False):
        try:
            with self.__class__._lock:
                cov = self.__class__.cov_instance
                source_root = self.__class__.source_root

                if cov is None:
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"ok": False, "error": "coverage not initialized"}).encode())
                    return

                # Stop coverage to read data
                cov.stop()

                coverage = {}

                if is_baseline:
                    # Baseline: return ALL coverable lines (including uncovered at count=0)
                    # This provides the denominator for coverage percentage.
                    # analysis2() returns (filename, statements, excluded, missing, formatted)
                    data = cov.get_data()
                    for filename in data.measured_files():
                        if "site-packages" in filename or "lib/python" in filename:
                            continue
                        if source_root and not filename.startswith(source_root):
                            continue
                        try:
                            _, statements, _, missing, _ = cov.analysis2(filename)
                            missing_set = set(missing)
                            lines_map = {}
                            for line in statements:
                                lines_map[str(line)] = 0 if line in missing_set else 1
                            if lines_map:
                                coverage[filename] = lines_map
                        except Exception:
                            continue
                else:
                    # Regular snapshot: only executed lines since last reset
                    data = cov.get_data()
                    for filename in data.measured_files():
                        if "site-packages" in filename or "lib/python" in filename:
                            continue
                        if source_root and not filename.startswith(source_root):
                            continue
                        lines = data.lines(filename)
                        if lines:
                            coverage[filename] = {str(line): 1 for line in lines}

                # Erase data and restart for next test
                cov.erase()
                cov.start()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "coverage": coverage}).encode())

        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode())

    def log_message(self, format, *args):
        """Suppress default HTTP server logging."""
        pass


_coverage_server: HTTPServer | None = None


def start_coverage_server(port: int | None = None) -> bool:
    """Start the coverage snapshot server if TUSK_COVERAGE_PORT is set.

    Returns True if the server was started, False otherwise.
    """
    global _coverage_server

    port_str = os.environ.get("TUSK_COVERAGE_PORT")
    if not port_str and port is None:
        return False

    actual_port = port or int(port_str)

    # Try to import coverage
    try:
        import coverage as coverage_module
    except ImportError:
        logger.warning(
            "TUSK_COVERAGE_PORT is set but 'coverage' package is not installed. "
            "Install it with: pip install coverage"
        )
        return False

    source_root = os.getcwd()

    # Start coverage collection
    cov = coverage_module.Coverage(
        source=[source_root],
        omit=[
            "*/site-packages/*",
            "*/venv/*",
            "*/.venv/*",
            "*/test*",
            "*/__pycache__/*",
        ],
    )
    cov.start()

    # Set shared state on the handler class
    CoverageSnapshotHandler.cov_instance = cov
    CoverageSnapshotHandler.source_root = source_root

    # Start HTTP server in a daemon thread
    http_server = HTTPServer(("127.0.0.1", actual_port), CoverageSnapshotHandler)
    _coverage_server = http_server
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()

    logger.info(f"Coverage snapshot server listening on port {actual_port}")
    return True


def stop_coverage_server() -> None:
    """Shut down the coverage snapshot server if running."""
    global _coverage_server
    if _coverage_server is not None:
        _coverage_server.shutdown()
        _coverage_server = None
