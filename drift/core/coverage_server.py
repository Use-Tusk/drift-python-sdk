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

# Cache branch structure from baseline to ensure deterministic branch counts.
# Branch detection via _analyze() depends on observed arcs, which vary with
# thread timing. By caching from the baseline (which has the fullest data),
# per-test snapshots report consistent totals.
_branch_cache: dict[str, dict] | None = None


def start_coverage_collection() -> bool:
    """Initialize coverage.py collection if TUSK_COVERAGE is set.

    TUSK_COVERAGE is set by the CLI when coverage is enabled.
    This is the language-agnostic signal (Node uses NODE_V8_COVERAGE additionally).

    Returns True if coverage was started, False otherwise.
    """
    global _cov_instance, _source_root

    # TUSK_COVERAGE is the language-agnostic signal from the CLI
    if not os.environ.get("TUSK_COVERAGE"):
        return False

    # Coverage collection only makes sense in REPLAY mode.
    # If TUSK_DRIFT_MODE is not set we still proceed for backwards compatibility.
    mode = os.environ.get("TUSK_DRIFT_MODE", "").upper()
    if mode and mode != "REPLAY":
        logger.debug("Coverage collection skipped: not in REPLAY mode (mode=%s)", mode)
        return False

    try:
        import coverage as coverage_module
    except ImportError:
        logger.warning(
            "Coverage requested but 'coverage' package is not installed. Install it with: pip install coverage"
        )
        return False

    with _lock:
        # Guard against double initialization — stop existing instance first
        if _cov_instance is not None:
            try:
                _cov_instance.stop()
            except Exception:
                pass

        _source_root = os.path.realpath(os.getcwd())

        _cov_instance = coverage_module.Coverage(
            source=[_source_root],
            branch=True,
            omit=[
                "*/site-packages/*",
                "*/venv/*",
                "*/.venv/*",
                "*/tests/*",
                "*/test_*.py",
                "*/__pycache__/*",
            ],
        )
        _cov_instance.start()

    logger.info("Coverage collection started")
    return True


def stop_coverage_collection() -> None:
    """Stop coverage collection and clean up. Thread-safe."""
    global _cov_instance, _branch_cache
    with _lock:
        if _cov_instance is not None:
            try:
                _cov_instance.stop()
            except Exception:
                pass
            _cov_instance = None
        _branch_cache = None


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
    with _lock:
        if _cov_instance is None:
            raise RuntimeError("Coverage not initialized")
        _cov_instance.stop()
        coverage = {}

        try:
            global _branch_cache
            if baseline:
                # Baseline: compute fresh branch data and cache it for per-test reuse
                _branch_cache = {}
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
                        _branch_cache[filename] = branch_data
                        if lines_map:
                            coverage[filename] = {"lines": lines_map, **branch_data}
                    except Exception as e:
                        logger.debug(f"Failed to analyze {filename}: {e}")
                        continue
            else:
                data = _cov_instance.get_data()
                for filename in data.measured_files():
                    if not _is_user_file(filename):
                        continue
                    lines = data.lines(filename)
                    if lines:
                        # Use cached branch data from baseline for stable totals.
                        # Fall back to live _analyze() if no cache (e.g., no baseline taken).
                        if _branch_cache is not None and filename in _branch_cache:
                            branch_data = _get_per_test_branch_data(data, filename, _branch_cache[filename])
                        else:
                            branch_data = _get_branch_data(data, filename)
                        coverage[filename] = {
                            "lines": {str(line): 1 for line in lines},
                            **branch_data,
                        }
        finally:
            _cov_instance.erase()
            _cov_instance.start()

    return coverage


def _is_user_file(filename: str) -> bool:
    """Check if a file is a user source file (not third-party)."""
    if "site-packages" in filename or "lib/python" in filename:
        return False
    # Resolve symlinks for consistent path comparison
    resolved = os.path.realpath(filename)
    # Use trailing separator to avoid prefix collisions (/app matching /application)
    return not _source_root or resolved.startswith(_source_root + os.sep) or resolved == _source_root


def _get_branch_data(data, filename: str) -> dict:
    """Extract branch coverage data for a file.

    Uses coverage.py's arc tracking (from_line, to_line) to compute
    per-line branch coverage.
    """
    try:
        if not data.has_arcs():
            return {"totalBranches": 0, "coveredBranches": 0, "branches": {}}

        if _cov_instance is None:
            return {"totalBranches": 0, "coveredBranches": 0, "branches": {}}

        analysis = _cov_instance._analyze(filename)
        numbers = analysis.numbers

        total_branches = numbers.n_branches
        covered_branches = max(0, total_branches - numbers.n_missing_branches)

        missing_arcs = analysis.missing_branch_arcs()
        executed_arcs = set(data.arcs(filename) or [])

        # Group executed arcs by from_line (skip negative entry arcs)
        executed_by_line: dict[int, list[int]] = {}
        for from_line, to_line in executed_arcs:
            if from_line < 0:
                continue
            executed_by_line.setdefault(from_line, []).append(to_line)

        # A line is a branch point if:
        # - it appears in missing_arcs (at least one path wasn't taken), OR
        # - it has multiple executed arcs (multiple paths from same line)
        branch_point_lines = set(missing_arcs.keys())
        for from_line, to_lines in executed_by_line.items():
            if len(to_lines) > 1:
                branch_point_lines.add(from_line)

        branch_lines: dict[int, dict] = {}

        for from_line in branch_point_lines:
            executed_count = len(executed_by_line.get(from_line, []))
            missing_count = len(missing_arcs.get(from_line, []))
            branch_lines[from_line] = {
                "total": executed_count + missing_count,
                "covered": executed_count,
            }

        branches = {str(line): info for line, info in branch_lines.items()}

        return {
            "totalBranches": total_branches,
            "coveredBranches": covered_branches,
            "branches": branches,
        }
    except Exception:
        return {"totalBranches": 0, "coveredBranches": 0, "branches": {}}


def _get_per_test_branch_data(data, filename: str, cached: dict) -> dict:
    """Compute per-test branch coverage using cached branch structure from baseline.

    Uses the cached branch point set (from baseline) for stable totals,
    but computes covered counts from the current test's executed arcs.
    This avoids flaky branch totals caused by non-deterministic arc detection.
    """
    try:
        if not data.has_arcs():
            return {"totalBranches": 0, "coveredBranches": 0, "branches": {}}

        executed_arcs = set(data.arcs(filename) or [])

        # Group executed arcs by from_line (skip negative entry arcs)
        executed_by_line: dict[int, list[int]] = {}
        for from_line, to_line in executed_arcs:
            if from_line < 0:
                continue
            executed_by_line.setdefault(from_line, []).append(to_line)

        # Use cached branch points — only compute covered from current arcs
        cached_branches = cached.get("branches", {})
        branches: dict[str, dict] = {}
        total_covered = 0

        for line_str, info in cached_branches.items():
            total = info["total"]
            covered = min(len(executed_by_line.get(int(line_str), [])), total)
            branches[line_str] = {"total": total, "covered": covered}
            total_covered += covered

        return {
            "totalBranches": cached.get("totalBranches", 0),
            "coveredBranches": total_covered,
            "branches": branches,
        }
    except Exception:
        return {"totalBranches": 0, "coveredBranches": 0, "branches": {}}
