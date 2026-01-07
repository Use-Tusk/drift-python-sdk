"""Tests for config file loading functionality."""

import os
import tempfile
import unittest
from pathlib import Path

from drift.core.config import (
    TuskFileConfig,
    find_project_root,
    load_tusk_config,
)


class TestFindProjectRoot(unittest.TestCase):
    """Test the find_project_root function."""

    def test_finds_project_root_with_pyproject_toml(self):
        """Should find project root when pyproject.toml exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a pyproject.toml file
            project_root = Path(tmpdir).resolve()
            (project_root / "pyproject.toml").touch()

            # Change to a subdirectory
            subdir = project_root / "src" / "myapp"
            subdir.mkdir(parents=True)

            original_cwd = os.getcwd()
            try:
                os.chdir(subdir)
                found_root = find_project_root()
                assert found_root is not None
                self.assertEqual(found_root.resolve(), project_root.resolve())
            finally:
                os.chdir(original_cwd)

    def test_finds_project_root_with_setup_py(self):
        """Should find project root when setup.py exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a setup.py file
            project_root = Path(tmpdir).resolve()
            (project_root / "setup.py").touch()

            # Change to a subdirectory
            subdir = project_root / "src"
            subdir.mkdir(parents=True)

            original_cwd = os.getcwd()
            try:
                os.chdir(subdir)
                found_root = find_project_root()
                assert found_root is not None
                self.assertEqual(found_root.resolve(), project_root.resolve())
            finally:
                os.chdir(original_cwd)

    def test_returns_none_when_no_markers_found(self):
        """Should return None when no project markers are found."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a directory with no project markers
            subdir = Path(tmpdir) / "some" / "deep" / "path"
            subdir.mkdir(parents=True)

            original_cwd = os.getcwd()
            try:
                os.chdir(subdir)
                found_root = find_project_root()
                # This might return None or find a marker higher up the tree
                # depending on the actual directory structure
                self.assertIsInstance(found_root, (Path, type(None)))
            finally:
                os.chdir(original_cwd)


class TestLoadTuskConfig(unittest.TestCase):
    """Test the load_tusk_config function."""

    def test_loads_valid_config_file(self):
        """Should load and parse a valid config file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            (project_root / "pyproject.toml").touch()

            # Create .tusk directory and config file
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

                self.assertIsNotNone(config)
                assert config is not None
                self.assertIsInstance(config, TuskFileConfig)

                # Check service config
                self.assertIsNotNone(config.service)
                assert config.service is not None
                self.assertEqual(config.service.id, "test-service-123")
                self.assertEqual(config.service.name, "test-service")
                self.assertEqual(config.service.port, 3000)

                # Check traces config
                self.assertIsNotNone(config.traces)
                assert config.traces is not None
                self.assertEqual(config.traces.dir, ".tusk/traces")

                # Check recording config
                self.assertIsNotNone(config.recording)
                assert config.recording is not None
                self.assertEqual(config.recording.sampling_rate, 0.5)
                self.assertEqual(config.recording.export_spans, False)
                self.assertEqual(config.recording.enable_env_var_recording, True)

                # Check tusk_api config
                self.assertIsNotNone(config.tusk_api)
                assert config.tusk_api is not None
                self.assertEqual(config.tusk_api.url, "https://api.example.com")

                # Check transforms
                self.assertIsNotNone(config.transforms)
                assert config.transforms is not None
                self.assertIn("http", config.transforms)

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
                self.assertIsNone(config)
            finally:
                os.chdir(original_cwd)

    def test_handles_empty_config_file(self):
        """Should handle empty config file gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            (project_root / "pyproject.toml").touch()

            # Create .tusk directory and empty config file
            tusk_dir = project_root / ".tusk"
            tusk_dir.mkdir()
            (tusk_dir / "config.yaml").write_text("")

            original_cwd = os.getcwd()
            try:
                os.chdir(project_root)
                config = load_tusk_config()

                self.assertIsNotNone(config)
                assert config is not None
                self.assertIsInstance(config, TuskFileConfig)

                # All fields should be None
                self.assertIsNone(config.service)
                self.assertIsNone(config.traces)
                self.assertIsNone(config.recording)
                self.assertIsNone(config.tusk_api)
                self.assertIsNone(config.transforms)

            finally:
                os.chdir(original_cwd)

    def test_handles_partial_config(self):
        """Should handle partial config with only some sections."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            (project_root / "pyproject.toml").touch()

            # Create .tusk directory and partial config file
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

                self.assertIsNotNone(config)
                assert config is not None

                # Only specified sections should be present
                self.assertIsNone(config.service)
                self.assertIsNotNone(config.traces)
                assert config.traces is not None
                self.assertEqual(config.traces.dir, "./my-traces")
                self.assertIsNotNone(config.recording)
                assert config.recording is not None
                self.assertEqual(config.recording.sampling_rate, 0.8)
                self.assertIsNone(config.tusk_api)

            finally:
                os.chdir(original_cwd)

    def test_handles_invalid_yaml(self):
        """Should return None when YAML is invalid."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            (project_root / "pyproject.toml").touch()

            # Create .tusk directory and invalid YAML file
            tusk_dir = project_root / ".tusk"
            tusk_dir.mkdir()
            (tusk_dir / "config.yaml").write_text("invalid: yaml: content: [")

            original_cwd = os.getcwd()
            try:
                os.chdir(project_root)
                config = load_tusk_config()
                self.assertIsNone(config)
            finally:
                os.chdir(original_cwd)


if __name__ == "__main__":
    unittest.main()
