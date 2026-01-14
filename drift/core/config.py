"""Configuration loading and schema for the Tusk Drift SDK."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ServiceStartConfig:
    """Configuration for service startup command."""

    command: str | None = None


@dataclass
class ReadinessCheckConfig:
    """Configuration for service readiness checks."""

    command: str | None = None
    timeout: str | None = None
    interval: str | None = None


@dataclass
class ServiceConfig:
    """Configuration for the service being instrumented."""

    id: str | None = None
    name: str | None = None
    port: int | None = None
    start: ServiceStartConfig | None = None
    readiness_check: ReadinessCheckConfig | None = None


@dataclass
class TracesConfig:
    """Configuration for trace storage."""

    dir: str | None = None


@dataclass
class TuskApiConfig:
    """Configuration for the Tusk API."""

    url: str | None = None


@dataclass
class TestExecutionConfig:
    """Configuration for test execution."""

    concurrency: int | None = None
    timeout: str | None = None


@dataclass
class ComparisonConfig:
    """Configuration for span comparison."""

    ignore_fields: list[str] = field(default_factory=list)


@dataclass
class RecordingConfig:
    """Configuration for recording behavior."""

    sampling_rate: float | None = None
    export_spans: bool | None = None
    enable_env_var_recording: bool | None = None
    enable_analytics: bool | None = None
    exclude_paths: list[str] = field(default_factory=list)


@dataclass
class TuskFileConfig:
    """
    Configuration loaded from .tusk/config.yaml file.

    This matches the Node SDK's TuskConfig interface exactly.
    """

    service: ServiceConfig | None = None
    traces: TracesConfig | None = None
    tusk_api: TuskApiConfig | None = None
    test_execution: TestExecutionConfig | None = None
    comparison: ComparisonConfig | None = None
    recording: RecordingConfig | None = None
    transforms: dict[str, Any] | None = None


@dataclass
class TuskConfig:
    """
    Runtime configuration for the TuskDrift SDK.

    This combines values from initialization parameters, environment variables,
    and the config file.
    """

    api_key: str | None = None
    env: str | None = None
    sampling_rate: float = 1.0
    transforms: dict[str, Any] | None = None


def _parse_service_config(data: dict[str, Any]) -> ServiceConfig:
    """Parse service configuration from raw dict."""
    start = None
    if "start" in data and data["start"]:
        start = ServiceStartConfig(command=data["start"].get("command"))

    readiness_check = None
    if "readiness_check" in data and data["readiness_check"]:
        rc = data["readiness_check"]
        readiness_check = ReadinessCheckConfig(
            command=rc.get("command"),
            timeout=rc.get("timeout"),
            interval=rc.get("interval"),
        )

    return ServiceConfig(
        id=data.get("id"),
        name=data.get("name"),
        port=data.get("port"),
        start=start,
        readiness_check=readiness_check,
    )


def _parse_recording_config(data: dict[str, Any]) -> RecordingConfig:
    """Parse recording configuration from raw dict."""
    # Validate sampling_rate type
    sampling_rate = data.get("sampling_rate")
    if sampling_rate is not None and not isinstance(sampling_rate, (int, float)):
        logger.warning(
            f"Invalid 'sampling_rate' in config: expected number, got {type(sampling_rate).__name__}. "
            "This value will be ignored."
        )
        sampling_rate = None

    return RecordingConfig(
        sampling_rate=sampling_rate,
        export_spans=data.get("export_spans"),
        enable_env_var_recording=data.get("enable_env_var_recording"),
        enable_analytics=data.get("enable_analytics"),
        exclude_paths=data.get("exclude_paths", []),
    )


def _parse_comparison_config(data: dict[str, Any]) -> ComparisonConfig:
    """Parse comparison configuration from raw dict."""
    return ComparisonConfig(
        ignore_fields=data.get("ignore_fields", []),
    )


def _parse_test_execution_config(data: dict[str, Any]) -> TestExecutionConfig:
    """Parse test execution configuration from raw dict."""
    return TestExecutionConfig(
        concurrency=data.get("concurrency"),
        timeout=data.get("timeout"),
    )


def _parse_file_config(data: dict[str, Any]) -> TuskFileConfig:
    """Parse the full config file into a TuskFileConfig object."""
    service = None
    if "service" in data and data["service"]:
        service = _parse_service_config(data["service"])

    traces = None
    if "traces" in data and data["traces"]:
        traces = TracesConfig(dir=data["traces"].get("dir"))

    tusk_api = None
    if "tusk_api" in data and data["tusk_api"]:
        tusk_api = TuskApiConfig(url=data["tusk_api"].get("url"))

    test_execution = None
    if "test_execution" in data and data["test_execution"]:
        test_execution = _parse_test_execution_config(data["test_execution"])

    comparison = None
    if "comparison" in data and data["comparison"]:
        comparison = _parse_comparison_config(data["comparison"])

    recording = None
    if "recording" in data and data["recording"]:
        recording = _parse_recording_config(data["recording"])

    transforms = data.get("transforms")

    return TuskFileConfig(
        service=service,
        traces=traces,
        tusk_api=tusk_api,
        test_execution=test_execution,
        comparison=comparison,
        recording=recording,
        transforms=transforms,
    )


def find_project_root() -> Path | None:
    """
    Find project root by traversing up from the current working directory.

    Looks for common Python project markers: pyproject.toml, setup.py, setup.cfg,
    or requirements.txt.

    Returns:
        Path to the project root, or None if not found.
    """
    current_dir = Path.cwd()

    # If we're running from within a virtual environment or site-packages,
    # try to find the actual project root
    current_str = str(current_dir)
    if "site-packages" in current_str:
        # Find the site-packages directory and go up to find the project
        parts = current_str.split("site-packages")
        if parts[0]:
            # Go up from the venv/lib directory
            potential_root = Path(parts[0]).parent.parent.parent
            if potential_root.exists():
                current_dir = potential_root

    # Project root markers (in order of preference)
    markers = ["pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"]

    # Traverse up to find a project marker
    search_dir = current_dir
    while search_dir != search_dir.parent:
        for marker in markers:
            marker_path = search_dir / marker
            if marker_path.exists():
                return search_dir
        search_dir = search_dir.parent

    # Check the root directory itself
    for marker in markers:
        if (search_dir / marker).exists():
            return search_dir

    return None


def load_tusk_config() -> TuskFileConfig | None:
    """
    Load the Tusk config from .tusk/config.yaml in the customer's project.

    Returns:
        TuskFileConfig object if the config file exists and is valid,
        None otherwise.
    """
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML not installed. Config file loading disabled.")
        return None

    try:
        project_root = find_project_root()
        if project_root is None:
            logger.debug("Could not find project root. Config loading skipped.")
            return None

        config_path = project_root / ".tusk" / "config.yaml"

        if not config_path.exists():
            logger.debug(f"No config file found at {config_path}")
            return None

        logger.debug(f"Loading config from {config_path}")

        with open(config_path, encoding="utf-8") as f:
            config_content = f.read()

        data = yaml.safe_load(config_content)

        if data is None:
            logger.debug("Config file is empty")
            return TuskFileConfig()

        if not isinstance(data, dict):
            logger.warning(f"Config file has invalid format (expected dict, got {type(data).__name__})")
            return None

        config = _parse_file_config(data)

        service_name = config.service.name if config.service else "unknown"
        logger.debug(f"Successfully loaded config for service: {service_name}")

        return config

    except yaml.YAMLError as e:
        logger.error(f"Error parsing config YAML: {e}")
        return None
    except Exception as e:
        logger.error(f"Error loading config: {e}")
        return None
