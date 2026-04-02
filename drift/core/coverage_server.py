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
                    # plus branch coverage data.
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

                            # Branch data from coverage.py
                            branch_data = _get_branch_data(cov, data, filename)

                            if lines_map:
                                coverage[filename] = {
                                    "lines": lines_map,
                                    **branch_data,
                                }
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
                            branch_data = _get_branch_data(cov, data, filename)
                            coverage[filename] = {
                                "lines": {str(line): 1 for line in lines},
                                **branch_data,
                            }

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


def _get_branch_data(cov, data, filename: str) -> dict:
    """Extract branch coverage data for a file.

    Returns dict with totalBranches, coveredBranches, and per-line branch detail.
    Uses coverage.py's analysis API which tracks branches as arcs (from_line, to_line).
    """
    try:
        if not data.has_arcs():
            return {"totalBranches": 0, "coveredBranches": 0, "branches": {}}

        # Use internal _analyze for full branch analysis
        analysis = cov._analyze(filename)
        numbers = analysis.numbers

        total_branches = numbers.n_branches
        covered_branches = total_branches - numbers.n_missing_branches

        # Get per-line branch detail from missing_branch_arcs
        missing_arcs = analysis.missing_branch_arcs()
        executed_arcs = set(data.arcs(filename) or [])

        # Build per-line branch info
        # Collect all branch source lines from both executed and missing
        branch_lines: dict[int, dict] = {}  # from_line -> {total, covered}

        # Count executed arcs by source line
        for from_line, to_line in executed_arcs:
            if from_line < 0:  # negative = entry/exit arcs, skip
                continue
            if from_line not in branch_lines:
                branch_lines[from_line] = {"total": 0, "covered": 0}
            branch_lines[from_line]["total"] += 1
            branch_lines[from_line]["covered"] += 1

        # Count missing arcs by source line
        for from_line, to_lines in missing_arcs.items():
            if from_line not in branch_lines:
                branch_lines[from_line] = {"total": 0, "covered": 0}
            branch_lines[from_line]["total"] += len(to_lines)

        # Convert to string keys
        branches = {str(line): info for line, info in branch_lines.items()}

        return {
            "totalBranches": total_branches,
            "coveredBranches": covered_branches,
            "branches": branches,
        }
    except Exception:
        return {"totalBranches": 0, "coveredBranches": 0, "branches": {}}


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
        branch=True,  # Enable branch coverage tracking
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
