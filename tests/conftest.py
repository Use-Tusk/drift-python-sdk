"""Pytest configuration and fixtures for Drift Python SDK tests."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from drift.core.metrics import MetricsCollector
    from drift.core.tracing.adapters import InMemorySpanAdapter


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def original_cwd() -> Generator[str, None, None]:
    """Save and restore the current working directory."""
    cwd = os.getcwd()
    yield cwd
    os.chdir(cwd)


@pytest.fixture
def in_memory_adapter() -> InMemorySpanAdapter:
    """Create a fresh InMemorySpanAdapter for testing."""
    from drift.core.tracing.adapters import InMemorySpanAdapter

    return InMemorySpanAdapter()


@pytest.fixture
def metrics_collector() -> MetricsCollector:
    """Create a fresh MetricsCollector for testing."""
    from drift.core.metrics import MetricsCollector

    return MetricsCollector()
