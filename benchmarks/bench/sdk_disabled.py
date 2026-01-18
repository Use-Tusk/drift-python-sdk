#!/usr/bin/env python3
"""Benchmark: SDK Disabled (baseline)."""

import os
import sys

# Ensure SDK is disabled
os.environ["TUSK_DRIFT_MODE"] = "DISABLED"

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from benchmarks.bench.common import run_benchmarks

if __name__ == "__main__":
    run_benchmarks(label="sdk-disabled")
