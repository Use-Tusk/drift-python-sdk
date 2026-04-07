"""Tests for coverage_server.py - Coverage collection management."""

from __future__ import annotations

import os

import pytest

from drift.core import coverage_server
from drift.core.coverage_server import (
    _is_user_file,
    start_coverage_collection,
    stop_coverage_collection,
    take_coverage_snapshot,
)


@pytest.fixture(autouse=True)
def _reset_coverage_state():
    """Reset module-level globals between tests."""
    yield
    stop_coverage_collection()
    # Also make sure _source_root is cleared
    coverage_server._source_root = None


class TestStartCoverageCollection:
    """Tests for start_coverage_collection function."""

    def test_returns_false_when_tusk_coverage_not_set(self, monkeypatch):
        """Should return False when TUSK_COVERAGE env var is not set."""
        monkeypatch.delenv("TUSK_COVERAGE", raising=False)
        monkeypatch.delenv("TUSK_DRIFT_MODE", raising=False)

        result = start_coverage_collection()

        assert result is False

    def test_returns_false_when_mode_is_record(self, monkeypatch):
        """Should return False when TUSK_DRIFT_MODE=RECORD even if TUSK_COVERAGE=true."""
        monkeypatch.setenv("TUSK_COVERAGE", "true")
        monkeypatch.setenv("TUSK_DRIFT_MODE", "RECORD")

        result = start_coverage_collection()

        assert result is False

    def test_returns_true_when_mode_is_replay(self, monkeypatch, mocker):
        """Should return True when TUSK_COVERAGE=true and mode is REPLAY."""
        monkeypatch.setenv("TUSK_COVERAGE", "true")
        monkeypatch.setenv("TUSK_DRIFT_MODE", "REPLAY")

        mock_cov_instance = mocker.MagicMock()
        mock_coverage_module = mocker.MagicMock()
        mock_coverage_module.Coverage.return_value = mock_cov_instance
        mocker.patch.dict("sys.modules", {"coverage": mock_coverage_module})

        result = start_coverage_collection()

        assert result is True
        mock_cov_instance.start.assert_called_once()

    def test_returns_true_when_mode_not_set(self, monkeypatch, mocker):
        """Should return True when TUSK_COVERAGE=true and TUSK_DRIFT_MODE is not set (backwards compat)."""
        monkeypatch.setenv("TUSK_COVERAGE", "true")
        monkeypatch.delenv("TUSK_DRIFT_MODE", raising=False)

        mock_cov_instance = mocker.MagicMock()
        mock_coverage_module = mocker.MagicMock()
        mock_coverage_module.Coverage.return_value = mock_cov_instance
        mocker.patch.dict("sys.modules", {"coverage": mock_coverage_module})

        result = start_coverage_collection()

        assert result is True
        mock_cov_instance.start.assert_called_once()


class TestIsUserFile:
    """Tests for _is_user_file function."""

    def test_returns_false_for_site_packages(self):
        """Should return False for paths containing site-packages."""
        assert _is_user_file("/usr/lib/python3.11/site-packages/requests/api.py") is False

    def test_returns_false_for_venv_paths(self):
        """Should return False for paths containing lib/python (venv pattern)."""
        assert _is_user_file("/app/venv/lib/python3.11/somepkg/mod.py") is False

    def test_returns_true_for_source_root_file(self, monkeypatch):
        """Should return True for files within the source root."""
        source_root = os.path.realpath("/tmp/myproject")
        monkeypatch.setattr(coverage_server, "_source_root", source_root)

        assert _is_user_file(os.path.join(source_root, "app", "main.py")) is True


class TestTakeCoverageSnapshot:
    """Tests for take_coverage_snapshot function."""

    def test_raises_runtime_error_when_not_initialized(self):
        """Should raise RuntimeError when coverage is not initialized."""
        with pytest.raises(RuntimeError, match="Coverage not initialized"):
            take_coverage_snapshot()


class TestStopCoverageCollection:
    """Tests for stop_coverage_collection function."""

    def test_cleans_up_state(self, monkeypatch, mocker):
        """Should stop coverage instance and set it to None."""
        mock_cov = mocker.MagicMock()
        monkeypatch.setattr(coverage_server, "_cov_instance", mock_cov)

        stop_coverage_collection()

        mock_cov.stop.assert_called_once()
        assert coverage_server._cov_instance is None


class TestTakeCoverageSnapshotBaseline:
    """Tests for take_coverage_snapshot in baseline mode."""

    def test_baseline_returns_all_coverable_lines_including_uncovered(self, monkeypatch, mocker):
        """Should return all coverable lines including uncovered (count=0) in baseline mode."""
        mock_cov = mocker.MagicMock()
        mock_data = mocker.MagicMock()

        # Mock measured files
        mock_data.measured_files.return_value = ["/app/main.py"]
        mock_data.has_arcs.return_value = False

        # analysis2 returns: (filename, statements, excluded, missing, missing_formatted)
        # statements=[1,2,3,4], missing=[2,4] means lines 1,3 covered, lines 2,4 uncovered
        mock_cov.analysis2.return_value = (None, [1, 2, 3, 4], [], [2, 4], None)
        mock_cov.get_data.return_value = mock_data

        monkeypatch.setattr(coverage_server, "_cov_instance", mock_cov)
        monkeypatch.setattr(coverage_server, "_source_root", os.path.realpath("/app"))

        result = take_coverage_snapshot(baseline=True)

        assert "/app/main.py" in result
        lines = result["/app/main.py"]["lines"]
        assert lines["1"] == 1  # covered
        assert lines["2"] == 0  # uncovered
        assert lines["3"] == 1  # covered
        assert lines["4"] == 0  # uncovered
        mock_cov.stop.assert_called()
        mock_cov.erase.assert_called()
        mock_cov.start.assert_called()

    def test_baseline_caches_branch_data_for_per_test_use(self, monkeypatch, mocker):
        """Should cache branch structure in baseline mode for deterministic per-test counts."""
        mock_cov = mocker.MagicMock()
        mock_data = mocker.MagicMock()

        mock_data.measured_files.return_value = ["/app/branchy.py"]
        mock_data.has_arcs.return_value = True
        mock_data.arcs.return_value = [(1, 2), (1, 3)]

        mock_cov.analysis2.return_value = (None, [1, 2, 3], [], [], None)
        mock_cov.get_data.return_value = mock_data

        # Mock _analyze for branch detection
        mock_analysis = mocker.MagicMock()
        mock_numbers = mocker.MagicMock()
        mock_numbers.n_branches = 2
        mock_numbers.n_missing_branches = 0
        mock_analysis.numbers = mock_numbers
        mock_analysis.missing_branch_arcs.return_value = {}
        mock_cov._analyze.return_value = mock_analysis

        monkeypatch.setattr(coverage_server, "_cov_instance", mock_cov)
        monkeypatch.setattr(coverage_server, "_source_root", os.path.realpath("/app"))

        take_coverage_snapshot(baseline=True)

        # Verify branch cache was populated
        assert coverage_server._branch_cache is not None
        assert "/app/branchy.py" in coverage_server._branch_cache

    def test_baseline_handles_analysis_exceptions_gracefully(self, monkeypatch, mocker):
        """Should continue processing other files if analysis2 raises exception."""
        mock_cov = mocker.MagicMock()
        mock_data = mocker.MagicMock()

        mock_data.measured_files.return_value = ["/app/broken.py", "/app/ok.py"]
        mock_data.has_arcs.return_value = False

        def analysis_side_effect(filename):
            if "broken" in filename:
                raise Exception("Analysis failed")
            return (None, [1, 2], [], [], None)

        mock_cov.analysis2.side_effect = analysis_side_effect
        mock_cov.get_data.return_value = mock_data

        monkeypatch.setattr(coverage_server, "_cov_instance", mock_cov)
        monkeypatch.setattr(coverage_server, "_source_root", os.path.realpath("/app"))

        result = take_coverage_snapshot(baseline=True)

        # Should skip broken.py but process ok.py
        assert "/app/broken.py" not in result
        assert "/app/ok.py" in result


class TestTakeCoverageSnapshotPerTest:
    """Tests for take_coverage_snapshot in per-test mode."""

    def test_per_test_returns_only_executed_lines(self, monkeypatch, mocker):
        """Should return only executed lines since last snapshot in per-test mode."""
        mock_cov = mocker.MagicMock()
        mock_data = mocker.MagicMock()

        mock_data.measured_files.return_value = ["/app/main.py"]
        mock_data.has_arcs.return_value = False
        mock_data.lines.return_value = [5, 6, 7]  # Only these lines executed

        mock_cov.get_data.return_value = mock_data

        monkeypatch.setattr(coverage_server, "_cov_instance", mock_cov)
        monkeypatch.setattr(coverage_server, "_source_root", os.path.realpath("/app"))

        result = take_coverage_snapshot(baseline=False)

        assert "/app/main.py" in result
        lines = result["/app/main.py"]["lines"]
        assert lines == {"5": 1, "6": 1, "7": 1}

    def test_per_test_uses_cached_branch_data_when_available(self, monkeypatch, mocker):
        """Should use cached branch structure from baseline for stable totals."""
        mock_cov = mocker.MagicMock()
        mock_data = mocker.MagicMock()

        mock_data.measured_files.return_value = ["/app/branchy.py"]
        mock_data.has_arcs.return_value = True
        mock_data.lines.return_value = [1, 2]
        mock_data.arcs.return_value = [(1, 2)]  # Only one branch taken this test

        mock_cov.get_data.return_value = mock_data

        # Populate cache with baseline data
        cached_branch_data = {
            "totalBranches": 2,
            "coveredBranches": 2,
            "branches": {"1": {"total": 2, "covered": 2}},
        }

        monkeypatch.setattr(coverage_server, "_cov_instance", mock_cov)
        monkeypatch.setattr(coverage_server, "_source_root", os.path.realpath("/app"))
        monkeypatch.setattr(coverage_server, "_branch_cache", {"/app/branchy.py": cached_branch_data})

        result = take_coverage_snapshot(baseline=False)

        # Should use cached totalBranches=2, but compute covered from current arcs
        assert result["/app/branchy.py"]["totalBranches"] == 2
        assert result["/app/branchy.py"]["coveredBranches"] == 1  # Only 1 arc executed this test

    def test_per_test_falls_back_to_live_analyze_when_no_cache(self, monkeypatch, mocker):
        """Should fall back to _get_branch_data if no baseline cache exists."""
        mock_cov = mocker.MagicMock()
        mock_data = mocker.MagicMock()

        mock_data.measured_files.return_value = ["/app/main.py"]
        mock_data.has_arcs.return_value = True
        mock_data.lines.return_value = [1, 2]
        mock_data.arcs.return_value = [(1, 2)]

        mock_cov.get_data.return_value = mock_data

        mock_analysis = mocker.MagicMock()
        mock_numbers = mocker.MagicMock()
        mock_numbers.n_branches = 1
        mock_numbers.n_missing_branches = 0
        mock_analysis.numbers = mock_numbers
        mock_analysis.missing_branch_arcs.return_value = {}
        mock_cov._analyze.return_value = mock_analysis

        monkeypatch.setattr(coverage_server, "_cov_instance", mock_cov)
        monkeypatch.setattr(coverage_server, "_source_root", os.path.realpath("/app"))
        monkeypatch.setattr(coverage_server, "_branch_cache", None)  # No cache

        result = take_coverage_snapshot(baseline=False)

        # Should call _analyze since no cache
        mock_cov._analyze.assert_called_with("/app/main.py")
        assert "/app/main.py" in result

    def test_per_test_skips_files_with_no_executed_lines(self, monkeypatch, mocker):
        """Should skip files with no executed lines in per-test mode."""
        mock_cov = mocker.MagicMock()
        mock_data = mocker.MagicMock()

        mock_data.measured_files.return_value = ["/app/notrun.py", "/app/run.py"]
        mock_data.has_arcs.return_value = False

        def lines_side_effect(filename):
            if "notrun" in filename:
                return []  # No lines executed
            return [1, 2]

        mock_data.lines.side_effect = lines_side_effect
        mock_cov.get_data.return_value = mock_data

        monkeypatch.setattr(coverage_server, "_cov_instance", mock_cov)
        monkeypatch.setattr(coverage_server, "_source_root", os.path.realpath("/app"))

        result = take_coverage_snapshot(baseline=False)

        assert "/app/notrun.py" not in result
        assert "/app/run.py" in result


class TestGetBranchData:
    """Tests for _get_branch_data function."""

    def test_returns_empty_when_no_arcs(self, monkeypatch, mocker):
        """Should return zero branches when data has no arcs (branch coverage disabled)."""
        from drift.core.coverage_server import _get_branch_data

        mock_data = mocker.MagicMock()
        mock_data.has_arcs.return_value = False

        result = _get_branch_data(mock_data, "/app/main.py")

        assert result == {"totalBranches": 0, "coveredBranches": 0, "branches": {}}

    def test_returns_empty_when_cov_instance_is_none(self, monkeypatch, mocker):
        """Should return empty branch data when _cov_instance is None."""
        from drift.core.coverage_server import _get_branch_data

        mock_data = mocker.MagicMock()
        mock_data.has_arcs.return_value = True

        monkeypatch.setattr(coverage_server, "_cov_instance", None)

        result = _get_branch_data(mock_data, "/app/main.py")

        assert result == {"totalBranches": 0, "coveredBranches": 0, "branches": {}}

    def test_computes_branch_coverage_from_arcs(self, monkeypatch, mocker):
        """Should compute per-line branch coverage from arc data."""
        from drift.core.coverage_server import _get_branch_data

        mock_cov = mocker.MagicMock()
        mock_data = mocker.MagicMock()

        mock_data.has_arcs.return_value = True
        # Line 5 has two paths: 5->6 (executed) and 5->8 (missing)
        mock_data.arcs.return_value = [(5, 6)]

        mock_analysis = mocker.MagicMock()
        mock_numbers = mocker.MagicMock()
        mock_numbers.n_branches = 2
        mock_numbers.n_missing_branches = 1
        mock_analysis.numbers = mock_numbers
        mock_analysis.missing_branch_arcs.return_value = {5: [(5, 8)]}
        mock_cov._analyze.return_value = mock_analysis

        monkeypatch.setattr(coverage_server, "_cov_instance", mock_cov)

        result = _get_branch_data(mock_data, "/app/main.py")

        assert result["totalBranches"] == 2
        assert result["coveredBranches"] == 1
        assert "5" in result["branches"]
        assert result["branches"]["5"]["total"] == 2
        assert result["branches"]["5"]["covered"] == 1

    def test_skips_negative_entry_arcs(self, monkeypatch, mocker):
        """Should skip negative entry arcs (function entry points) when grouping."""
        from drift.core.coverage_server import _get_branch_data

        mock_cov = mocker.MagicMock()
        mock_data = mocker.MagicMock()

        mock_data.has_arcs.return_value = True
        # Negative arcs are function entry points, should be ignored
        mock_data.arcs.return_value = [(-1, 1), (1, 2), (1, 3)]

        mock_analysis = mocker.MagicMock()
        mock_numbers = mocker.MagicMock()
        mock_numbers.n_branches = 2
        mock_numbers.n_missing_branches = 0
        mock_analysis.numbers = mock_numbers
        mock_analysis.missing_branch_arcs.return_value = {}
        mock_cov._analyze.return_value = mock_analysis

        monkeypatch.setattr(coverage_server, "_cov_instance", mock_cov)

        result = _get_branch_data(mock_data, "/app/main.py")

        # Line 1 should have 2 branches (to 2 and 3), -1 should be skipped
        assert "1" in result["branches"]
        assert result["branches"]["1"]["covered"] == 2
        assert "-1" not in result["branches"]

    def test_handles_exceptions_gracefully(self, monkeypatch, mocker):
        """Should return empty branch data on exceptions."""
        from drift.core.coverage_server import _get_branch_data

        mock_cov = mocker.MagicMock()
        mock_data = mocker.MagicMock()

        mock_data.has_arcs.return_value = True
        mock_cov._analyze.side_effect = Exception("Analysis error")

        monkeypatch.setattr(coverage_server, "_cov_instance", mock_cov)

        result = _get_branch_data(mock_data, "/app/main.py")

        assert result == {"totalBranches": 0, "coveredBranches": 0, "branches": {}}


class TestGetPerTestBranchData:
    """Tests for _get_per_test_branch_data function."""

    def test_returns_empty_when_no_arcs(self, mocker):
        """Should return zero branches when data has no arcs."""
        from drift.core.coverage_server import _get_per_test_branch_data

        mock_data = mocker.MagicMock()
        mock_data.has_arcs.return_value = False

        cached = {"totalBranches": 2, "branches": {}}
        result = _get_per_test_branch_data(mock_data, "/app/main.py", cached)

        assert result == {"totalBranches": 0, "coveredBranches": 0, "branches": {}}

    def test_uses_cached_totals_with_current_covered_counts(self, mocker):
        """Should use cached totals but compute covered from current test's arcs."""
        from drift.core.coverage_server import _get_per_test_branch_data

        mock_data = mocker.MagicMock()
        mock_data.has_arcs.return_value = True
        # Current test only executed one path from line 5
        mock_data.arcs.return_value = [(5, 6)]

        cached = {
            "totalBranches": 2,
            "branches": {
                "5": {"total": 2, "covered": 2},  # Baseline saw both branches
            },
        }

        result = _get_per_test_branch_data(mock_data, "/app/main.py", cached)

        # Should use cached total but compute covered from current arcs
        assert result["totalBranches"] == 2
        assert result["coveredBranches"] == 1  # Only 1 arc in current test
        assert result["branches"]["5"]["total"] == 2
        assert result["branches"]["5"]["covered"] == 1

    def test_caps_covered_at_cached_total(self, mocker):
        """Should cap covered count at cached total (prevents count overflow)."""
        from drift.core.coverage_server import _get_per_test_branch_data

        mock_data = mocker.MagicMock()
        mock_data.has_arcs.return_value = True
        # More arcs than cached total (shouldn't happen but defensive)
        mock_data.arcs.return_value = [(5, 6), (5, 7), (5, 8)]

        cached = {
            "totalBranches": 2,
            "branches": {
                "5": {"total": 2, "covered": 1},
            },
        }

        result = _get_per_test_branch_data(mock_data, "/app/main.py", cached)

        # Should cap at cached total of 2, not report 3
        assert result["branches"]["5"]["covered"] == 2

    def test_handles_exceptions_gracefully(self, mocker):
        """Should return empty branch data on exceptions."""
        from drift.core.coverage_server import _get_per_test_branch_data

        mock_data = mocker.MagicMock()
        mock_data.has_arcs.side_effect = Exception("Arc error")

        cached = {"totalBranches": 2, "branches": {}}
        result = _get_per_test_branch_data(mock_data, "/app/main.py", cached)

        assert result == {"totalBranches": 0, "coveredBranches": 0, "branches": {}}


class TestIsUserFileEdgeCases:
    """Tests for _is_user_file edge cases."""

    def test_handles_source_root_exactly(self, monkeypatch):
        """Should return True for the source root itself (not just children)."""
        source_root = os.path.realpath("/app")
        monkeypatch.setattr(coverage_server, "_source_root", source_root)

        result = _is_user_file(source_root)

        assert result is True

    def test_avoids_prefix_collision_with_trailing_separator(self, monkeypatch):
        """Should use trailing separator to prevent /app matching /application."""
        source_root = os.path.realpath("/app")
        monkeypatch.setattr(coverage_server, "_source_root", source_root)

        # /application should NOT match /app due to trailing separator check
        result = _is_user_file("/application/file.py")

        assert result is False

    def test_returns_true_when_source_root_is_none(self):
        """Should return True when _source_root is None (before initialization)."""
        # When _source_root is None, the function returns True
        # (see line 177: "not _source_root or ...")
        result = _is_user_file("/any/path/file.py")

        assert result is True
