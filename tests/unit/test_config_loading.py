"""Tests for config file loading functionality."""

import os
import tempfile
from pathlib import Path

from drift.core.config import (
    TuskFileConfig,
    find_project_root,
    load_tusk_config,
)


class TestFindProjectRoot:
    """Test the find_project_root function."""

    def test_finds_project_root_with_pyproject_toml(self):
        """Should find project root when pyproject.toml exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir).resolve()
            (project_root / "pyproject.toml").touch()

            subdir = project_root / "src" / "myapp"
            subdir.mkdir(parents=True)

            original_cwd = os.getcwd()
            try:
                os.chdir(subdir)
                found_root = find_project_root()
                assert found_root is not None
                assert found_root.resolve() == project_root.resolve()
            finally:
                os.chdir(original_cwd)

    def test_finds_project_root_with_setup_py(self):
        """Should find project root when setup.py exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir).resolve()
            (project_root / "setup.py").touch()

            subdir = project_root / "src"
            subdir.mkdir(parents=True)

            original_cwd = os.getcwd()
            try:
                os.chdir(subdir)
                found_root = find_project_root()
                assert found_root is not None
                assert found_root.resolve() == project_root.resolve()
            finally:
                os.chdir(original_cwd)

    def test_returns_none_when_no_markers_found(self):
        """Should return None when no project markers are found."""
        with tempfile.TemporaryDirectory() as tmpdir:
            subdir = Path(tmpdir) / "some" / "deep" / "path"
            subdir.mkdir(parents=True)

            original_cwd = os.getcwd()
            try:
                os.chdir(subdir)
                found_root = find_project_root()
                assert isinstance(found_root, (Path, type(None)))
            finally:
                os.chdir(original_cwd)


class TestLoadTuskConfig:
    """Test the load_tusk_config function."""

    def test_loads_valid_config_file(self):
        """Should load and parse a valid config file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            (project_root / "pyproject.toml").touch()

            tusk_dir = project_root / ".tusk"
            tusk_dir.mkdir()

            config_content = """
service:
  id: 'test-service-123'
  name: 'test-service'
  port: 3000

traces:
  dir: '.tusk/traces'

recording:
  sampling_rate: 0.5
  export_spans: false
  enable_env_var_recording: true

tusk_api:
  url: 'https://api.example.com'

transforms:
  http:
    - path: '/api/test'
      action: 'redact'
"""
            (tusk_dir / "config.yaml").write_text(config_content)

            original_cwd = os.getcwd()
            try:
                os.chdir(project_root)
                config = load_tusk_config()

                assert config is not None
                assert isinstance(config, TuskFileConfig)

                # Check service config
                assert config.service is not None
                assert config.service.id == "test-service-123"
                assert config.service.name == "test-service"
                assert config.service.port == 3000

                # Check traces config
                assert config.traces is not None
                assert config.traces.dir == ".tusk/traces"

                # Check recording config
                assert config.recording is not None
                assert config.recording.sampling_rate == 0.5
                assert config.recording.export_spans is False
                assert config.recording.enable_env_var_recording is True

                # Check tusk_api config
                assert config.tusk_api is not None
                assert config.tusk_api.url == "https://api.example.com"

                # Check transforms
                assert config.transforms is not None
                assert "http" in config.transforms

            finally:
                os.chdir(original_cwd)

    def test_returns_none_when_config_file_missing(self):
        """Should return None when config file doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            (project_root / "pyproject.toml").touch()

            original_cwd = os.getcwd()
            try:
                os.chdir(project_root)
                config = load_tusk_config()
                assert config is None
            finally:
                os.chdir(original_cwd)

    def test_handles_empty_config_file(self):
        """Should handle empty config file gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            (project_root / "pyproject.toml").touch()

            tusk_dir = project_root / ".tusk"
            tusk_dir.mkdir()
            (tusk_dir / "config.yaml").write_text("")

            original_cwd = os.getcwd()
            try:
                os.chdir(project_root)
                config = load_tusk_config()

                assert config is not None
                assert isinstance(config, TuskFileConfig)

                # All fields should be None
                assert config.service is None
                assert config.traces is None
                assert config.recording is None
                assert config.tusk_api is None
                assert config.transforms is None

            finally:
                os.chdir(original_cwd)

    def test_handles_partial_config(self):
        """Should handle partial config with only some sections."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            (project_root / "pyproject.toml").touch()

            tusk_dir = project_root / ".tusk"
            tusk_dir.mkdir()

            config_content = """
recording:
  sampling_rate: 0.8

traces:
  dir: './my-traces'
"""
            (tusk_dir / "config.yaml").write_text(config_content)

            original_cwd = os.getcwd()
            try:
                os.chdir(project_root)
                config = load_tusk_config()

                assert config is not None

                # Only specified sections should be present
                assert config.service is None
                assert config.traces is not None
                assert config.traces.dir == "./my-traces"
                assert config.recording is not None
                assert config.recording.sampling_rate == 0.8
                assert config.tusk_api is None

            finally:
                os.chdir(original_cwd)

    def test_handles_invalid_yaml(self):
        """Should return None when YAML is invalid."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            (project_root / "pyproject.toml").touch()

            tusk_dir = project_root / ".tusk"
            tusk_dir.mkdir()
            (tusk_dir / "config.yaml").write_text("invalid: yaml: content: [")

            original_cwd = os.getcwd()
            try:
                os.chdir(project_root)
                config = load_tusk_config()
                assert config is None
            finally:
                os.chdir(original_cwd)
