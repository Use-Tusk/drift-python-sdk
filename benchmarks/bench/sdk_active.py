#!/usr/bin/env python3
"""Benchmark: SDK Active (RECORD mode)."""

import os
import shutil
import sys
from pathlib import Path

# Set SDK to RECORD mode
os.environ["TUSK_DRIFT_MODE"] = "RECORD"

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Clean up benchmark traces directory
BENCHMARK_TRACE_DIR = Path(__file__).parent.parent / ".benchmark-traces"
if BENCHMARK_TRACE_DIR.exists():
    try:
        shutil.rmtree(BENCHMARK_TRACE_DIR)
    except Exception as e:
        print(f"Warning: Failed to clean benchmark trace directory: {e}")

# Initialize SDK BEFORE importing common (which imports requests)
from drift import TuskDrift
from drift.core.tracing.adapters.filesystem import FilesystemSpanAdapter

# Initialize the SDK
sdk = TuskDrift.initialize(
    api_key="benchmark-test-key",
    env="benchmark",
    log_level="warn",  # Reduce log noise during benchmarks
)

# Configure filesystem adapter for traces
if sdk.span_exporter:
    sdk.span_exporter.clear_adapters()
    adapter = FilesystemSpanAdapter(base_directory=BENCHMARK_TRACE_DIR)
    sdk.span_exporter.add_adapter(adapter)

# Mark app as ready
sdk.mark_app_as_ready()

from benchmarks.bench.common import run_benchmarks

if __name__ == "__main__":
    try:
        run_benchmarks(label="sdk-active")
    finally:
        sdk.shutdown()
