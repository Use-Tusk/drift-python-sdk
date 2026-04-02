"""Coverage collection for Python SDK.

Manages coverage.py for collecting per-test code coverage data.
Coverage data is accessed via take_coverage_snapshot() which is called
from the protobuf communicator handler.

Flow:
1. start_coverage_collection() initializes coverage.py at SDK startup
2. Between tests, CLI sends CoverageSnapshotRequest via protobuf
3. Communicator calls take_coverage_snapshot(baseline)
4. For baseline: returns ALL coverable lines (including uncovered at count=0)
5. For per-test: returns only executed lines since last snapshot, then resets
"""

from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger("TuskDrift")

# Shared state for coverage collection
_cov_instance = None
_source_root: str | None = None
_lock = threading.Lock()


def start_coverage_collection() -> bool:
    """Initialize coverage.py collection if NODE_V8_COVERAGE is set.

    NODE_V8_COVERAGE is set by the CLI when coverage is enabled.
    Python doesn't use V8 but we use the same env var as the signal.

    Returns True if coverage was started, False otherwise.
    """
    global _cov_instance, _source_root

    if not os.environ.get("NODE_V8_COVERAGE"):
        return False

    try:
        import coverage as coverage_module
    except ImportError:
        logger.warning(
            "Coverage requested but 'coverage' package is not installed. "
            "Install it with: pip install coverage"
        )
        return False

    _source_root = os.getcwd()

    _cov_instance = coverage_module.Coverage(
        source=[_source_root],
        branch=True,
        omit=[
            "*/site-packages/*",
            "*/venv/*",
            "*/.venv/*",
            "*/test*",
            "*/__pycache__/*",
        ],
    )
    _cov_instance.start()

    logger.info("Coverage collection started")
    return True


def stop_coverage_collection() -> None:
    """Stop coverage collection and clean up. Thread-safe."""
    global _cov_instance
    with _lock:
        if _cov_instance is not None:
            try:
                _cov_instance.stop()
            except Exception:
                pass
            _cov_instance = None


def take_coverage_snapshot(baseline: bool = False) -> dict:
    """Take a coverage snapshot.

    Called from the protobuf communicator handler between tests.

    Args:
        baseline: If True, returns ALL coverable lines (including uncovered at count=0)
                  for computing the total coverage denominator.
                  If False, returns only lines executed since the last snapshot.

    Returns:
        dict of { filePath: { "lines": {...}, "totalBranches": N, "coveredBranches": N, "branches": {...} } }
    """
    if _cov_instance is None:
        raise RuntimeError("Coverage not initialized")

    with _lock:
        _cov_instance.stop()
        coverage = {}

        if baseline:
            data = _cov_instance.get_data()
            for filename in data.measured_files():
                if not _is_user_file(filename):
                    continue
                try:
                    _, statements, _, missing, _ = _cov_instance.analysis2(filename)
                    missing_set = set(missing)
                    lines_map = {}
                    for line in statements:
                        lines_map[str(line)] = 0 if line in missing_set else 1
                    branch_data = _get_branch_data(data, filename)
                    if lines_map:
                        coverage[filename] = {"lines": lines_map, **branch_data}
                except Exception:
                    continue
        else:
            data = _cov_instance.get_data()
            for filename in data.measured_files():
                if not _is_user_file(filename):
                    continue
                lines = data.lines(filename)
                if lines:
                    branch_data = _get_branch_data(data, filename)
                    coverage[filename] = {
                        "lines": {str(line): 1 for line in lines},
                        **branch_data,
                    }

        _cov_instance.erase()
        _cov_instance.start()

    return coverage


def _is_user_file(filename: str) -> bool:
    """Check if a file is a user source file (not third-party)."""
    if "site-packages" in filename or "lib/python" in filename:
        return False
    if _source_root and not filename.startswith(_source_root):
        return False
    return True


def _get_branch_data(data, filename: str) -> dict:
    """Extract branch coverage data for a file.

    Uses coverage.py's arc tracking (from_line, to_line) to compute
    per-line branch coverage.
    """
    try:
        if not data.has_arcs():
            return {"totalBranches": 0, "coveredBranches": 0, "branches": {}}

        analysis = _cov_instance._analyze(filename)
        numbers = analysis.numbers

        total_branches = numbers.n_branches
        covered_branches = max(0, total_branches - numbers.n_missing_branches)

        missing_arcs = analysis.missing_branch_arcs()
        executed_arcs = set(data.arcs(filename) or [])

        branch_lines: dict[int, dict] = {}

        for from_line, to_line in executed_arcs:
            if from_line < 0:
                continue
            if from_line not in branch_lines:
                branch_lines[from_line] = {"total": 0, "covered": 0}
            branch_lines[from_line]["total"] += 1
            branch_lines[from_line]["covered"] += 1

        for from_line, to_lines in missing_arcs.items():
            if from_line not in branch_lines:
                branch_lines[from_line] = {"total": 0, "covered": 0}
            branch_lines[from_line]["total"] += len(to_lines)

        branches = {str(line): info for line, info in branch_lines.items()}

        return {
            "totalBranches": total_branches,
            "coveredBranches": covered_branches,
            "branches": branches,
        }
    except Exception:
        return {"totalBranches": 0, "coveredBranches": 0, "branches": {}}
