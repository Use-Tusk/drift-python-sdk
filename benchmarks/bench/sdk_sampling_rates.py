#!/usr/bin/env python3
"""Benchmark: SDK with different sampling rates."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# Set SDK to RECORD mode
os.environ["TUSK_DRIFT_MODE"] = "RECORD"

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Clean up benchmark traces directory
BENCHMARK_TRACE_DIR = Path(__file__).parent.parent / ".benchmark-traces-sampling"


def run_with_sampling_rate(sampling_rate: float) -> dict | None:
    """Run benchmark with a specific sampling rate."""
    # Clean traces directory
    if BENCHMARK_TRACE_DIR.exists():
        try:
            shutil.rmtree(BENCHMARK_TRACE_DIR)
        except Exception as e:
            print(f"Warning: Failed to clean benchmark trace directory: {e}")

    # We need to reset the SDK for each sampling rate test
    # This requires a fresh Python process, so we'll use subprocess
    import subprocess

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"""
import os
import sys
import shutil
from pathlib import Path

os.environ["TUSK_DRIFT_MODE"] = "RECORD"
sys.path.insert(0, "{os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))}")

BENCHMARK_TRACE_DIR = Path("{BENCHMARK_TRACE_DIR}")
if BENCHMARK_TRACE_DIR.exists():
    shutil.rmtree(BENCHMARK_TRACE_DIR)

from drift import TuskDrift
from drift.core.tracing.adapters.filesystem import FilesystemSpanAdapter

sdk = TuskDrift.initialize(
    api_key="benchmark-test-key",
    env="benchmark",
    sampling_rate={sampling_rate},
    log_level="warning",
)

if sdk.span_exporter:
    sdk.span_exporter.clear_adapters()
    adapter = FilesystemSpanAdapter(base_directory=BENCHMARK_TRACE_DIR)
    sdk.span_exporter.add_adapter(adapter)

sdk.mark_app_as_ready()

from benchmarks.bench.common import run_benchmarks
run_benchmarks(label="sdk-sampling-{int(sampling_rate * 100)}", options={{"time_per_task_ms": 5000, "warmup_iterations": 3}})

sdk.shutdown()
""",
        ],
        capture_output=True,
        text=True,
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    )

    if result.returncode != 0:
        print(f"Error running benchmark with sampling_rate={sampling_rate}:")
        print(result.stderr)
        return None

    # Print stdout (benchmark output)
    print(result.stdout)

    return {"sampling_rate": sampling_rate}


def main():
    """Run benchmarks with different sampling rates."""
    sampling_rates = [1.0, 0.5, 0.1, 0.01]

    print("=" * 60)
    print("Sampling Rate Impact Benchmarks")
    print("=" * 60)

    for rate in sampling_rates:
        print(f"\n{'=' * 60}")
        print(f"Testing sampling_rate = {rate} ({int(rate * 100)}%)")
        print("=" * 60)
        run_with_sampling_rate(rate)

    print("\n" + "=" * 60)
    print("All sampling rate benchmarks complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()
