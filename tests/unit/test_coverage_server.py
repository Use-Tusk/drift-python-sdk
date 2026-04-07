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
