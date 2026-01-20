#!/usr/bin/env python3
"""Benchmark: Realistic workload comparison.

This benchmark tests SDK overhead on realistic API workloads:
1. Typical read endpoint (~5-10ms baseline)
2. Typical write endpoint (~15-25ms baseline)
3. Realistic mixed workload (~10-20ms baseline)

These simulate production APIs that do actual work (I/O, validation, processing).
"""

import json
import os
import subprocess
import sys
from pathlib import Path


def run_benchmark_subprocess(mode: str, sampling_rate: float = 1.0) -> dict | None:
    """Run benchmark in a subprocess to ensure clean SDK state."""

    script = f'''
import os
import sys
import time
import shutil
import statistics
from pathlib import Path

# Configure SDK mode
os.environ["TUSK_DRIFT_MODE"] = "{mode}"
sys.path.insert(0, "{os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))}")

import requests

TRACE_DIR = Path("{Path(__file__).parent.parent / ".benchmark-traces-realistic"}")
if TRACE_DIR.exists():
    shutil.rmtree(TRACE_DIR)

if "{mode}" == "RECORD":
    from drift import TuskDrift
    from drift.core.tracing.adapters.filesystem import FilesystemSpanAdapter

    sdk = TuskDrift.initialize(
        api_key="benchmark-test-key",
        env="benchmark",
        sampling_rate={sampling_rate},
        log_level="warn",
    )
    if sdk.span_exporter:
        sdk.span_exporter.clear_adapters()
        adapter = FilesystemSpanAdapter(base_directory=TRACE_DIR)
        sdk.span_exporter.add_adapter(adapter)
    sdk.mark_app_as_ready()

from benchmarks.server.test_server import TestServer

# Start server
server = TestServer()
server_info = server.start()
server_url = server_info["url"]

# Wait for server
time.sleep(0.5)
session = requests.Session()

# Warmup
for _ in range(10):
    session.get(f"{{server_url}}/api/typical-read")
    session.post(f"{{server_url}}/api/typical-write", json={{"name": "test"}})
    session.post(f"{{server_url}}/api/realistic", json={{"userId": "u1", "query": "test"}})

# Benchmark parameters (configurable via BENCHMARK_ITERATIONS env var)
try:
    iterations = int(os.environ.get("BENCHMARK_ITERATIONS", "200"))
except ValueError:
    iterations = 200  # Fall back to default on invalid input
results = {{}}

# Test 1: Typical Read (~5-10ms baseline)
latencies = []
for _ in range(iterations):
    start = time.perf_counter_ns()
    response = session.get(f"{{server_url}}/api/typical-read")
    response.json()
    latencies.append(time.perf_counter_ns() - start)

results["typical_read"] = {{
    "mean_ms": statistics.mean(latencies) / 1_000_000,
    "p50_ms": statistics.median(latencies) / 1_000_000,
    "p99_ms": sorted(latencies)[int(len(latencies) * 0.99)] / 1_000_000,
    "samples": len(latencies),
}}

# Test 2: Typical Write (~15-25ms baseline)
latencies = []
for i in range(iterations):
    start = time.perf_counter_ns()
    response = session.post(f"{{server_url}}/api/typical-write", json={{"name": f"item-{{i}}"}})
    response.json()
    latencies.append(time.perf_counter_ns() - start)

results["typical_write"] = {{
    "mean_ms": statistics.mean(latencies) / 1_000_000,
    "p50_ms": statistics.median(latencies) / 1_000_000,
    "p99_ms": sorted(latencies)[int(len(latencies) * 0.99)] / 1_000_000,
    "samples": len(latencies),
}}

# Test 3: Realistic Mixed (~10-20ms baseline)
latencies = []
for i in range(iterations):
    start = time.perf_counter_ns()
    response = session.post(
        f"{{server_url}}/api/realistic",
        json={{"userId": f"user-{{i}}", "query": "search query", "email": "test@example.com"}},
    )
    response.json()
    latencies.append(time.perf_counter_ns() - start)

results["realistic_mixed"] = {{
    "mean_ms": statistics.mean(latencies) / 1_000_000,
    "p50_ms": statistics.median(latencies) / 1_000_000,
    "p99_ms": sorted(latencies)[int(len(latencies) * 0.99)] / 1_000_000,
    "samples": len(latencies),
}}

# Cleanup
session.close()
server.stop()

if "{mode}" == "RECORD":
    sdk.shutdown()

import json
print("RESULTS_JSON:" + json.dumps(results))
'''

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        timeout=120,
    )

    if result.returncode != 0:
        print(f"Error running benchmark ({mode}):")
        print(result.stderr)
        return None

    # Extract results from output
    for line in result.stdout.split("\n"):
        if line.startswith("RESULTS_JSON:"):
            return json.loads(line.replace("RESULTS_JSON:", ""))

    print(f"Could not find results in output for {mode}")
    print(result.stdout[-500:])
    return None


def format_comparison(baseline: dict, sdk_on: dict) -> dict:
    """Calculate overhead percentages."""
    comparison = {}
    for key in baseline:
        b_mean = baseline[key]["mean_ms"]
        s_mean = sdk_on[key]["mean_ms"]
        b_p99 = baseline[key]["p99_ms"]
        s_p99 = sdk_on[key]["p99_ms"]

        comparison[key] = {
            "baseline_mean_ms": b_mean,
            "sdk_mean_ms": s_mean,
            "mean_overhead_ms": s_mean - b_mean,
            "mean_overhead_pct": ((s_mean - b_mean) / b_mean) * 100 if b_mean > 0 else 0,
            "baseline_p99_ms": b_p99,
            "sdk_p99_ms": s_p99,
            "p99_overhead_ms": s_p99 - b_p99,
            "p99_overhead_pct": ((s_p99 - b_p99) / b_p99) * 100 if b_p99 > 0 else 0,
        }
    return comparison


def main():
    print("=" * 60)
    print("Realistic Workload Benchmark")
    print("=" * 60)
    print()

    # Run baseline (SDK disabled)
    print("Running baseline (SDK disabled)...")
    baseline = run_benchmark_subprocess("DISABLED")
    if not baseline:
        print("Failed to run baseline benchmark")
        return

    print(
        f"  Typical Read: {baseline['typical_read']['mean_ms']:.2f}ms mean, {baseline['typical_read']['p99_ms']:.2f}ms p99"
    )
    print(
        f"  Typical Write: {baseline['typical_write']['mean_ms']:.2f}ms mean, {baseline['typical_write']['p99_ms']:.2f}ms p99"
    )
    print(
        f"  Realistic Mixed: {baseline['realistic_mixed']['mean_ms']:.2f}ms mean, {baseline['realistic_mixed']['p99_ms']:.2f}ms p99"
    )
    print()

    # Run with SDK (100% sampling)
    print("Running with SDK (100% sampling)...")
    sdk_100 = run_benchmark_subprocess("RECORD", sampling_rate=1.0)
    if not sdk_100:
        print("Failed to run SDK benchmark")
        return

    print(
        f"  Typical Read: {sdk_100['typical_read']['mean_ms']:.2f}ms mean, {sdk_100['typical_read']['p99_ms']:.2f}ms p99"
    )
    print(
        f"  Typical Write: {sdk_100['typical_write']['mean_ms']:.2f}ms mean, {sdk_100['typical_write']['p99_ms']:.2f}ms p99"
    )
    print(
        f"  Realistic Mixed: {sdk_100['realistic_mixed']['mean_ms']:.2f}ms mean, {sdk_100['realistic_mixed']['p99_ms']:.2f}ms p99"
    )
    print()

    # Run with SDK (10% sampling)
    print("Running with SDK (10% sampling)...")
    sdk_10 = run_benchmark_subprocess("RECORD", sampling_rate=0.1)
    if not sdk_10:
        print("Failed to run SDK 10% benchmark")
        return

    print(
        f"  Typical Read: {sdk_10['typical_read']['mean_ms']:.2f}ms mean, {sdk_10['typical_read']['p99_ms']:.2f}ms p99"
    )
    print(
        f"  Typical Write: {sdk_10['typical_write']['mean_ms']:.2f}ms mean, {sdk_10['typical_write']['p99_ms']:.2f}ms p99"
    )
    print(
        f"  Realistic Mixed: {sdk_10['realistic_mixed']['mean_ms']:.2f}ms mean, {sdk_10['realistic_mixed']['p99_ms']:.2f}ms p99"
    )
    print()

    # Calculate and display comparisons
    comparison_100 = format_comparison(baseline, sdk_100)
    comparison_10 = format_comparison(baseline, sdk_10)

    print("=" * 60)
    print("Results Summary")
    print("=" * 60)
    print()

    print("## SDK Overhead on Realistic Workloads")
    print()
    print("| Endpoint | Baseline | SDK (100%) | Overhead | SDK (10%) | Overhead |")
    print("|----------|----------|------------|----------|-----------|----------|")

    for key, label in [
        ("typical_read", "Read API (~5ms)"),
        ("typical_write", "Write API (~15ms)"),
        ("realistic_mixed", "Mixed API (~10ms)"),
    ]:
        c100 = comparison_100[key]
        c10 = comparison_10[key]
        print(
            f"| {label} | {c100['baseline_mean_ms']:.1f}ms | {c100['sdk_mean_ms']:.1f}ms | +{c100['mean_overhead_ms']:.1f}ms ({c100['mean_overhead_pct']:+.1f}%) | {c10['sdk_mean_ms']:.1f}ms | +{c10['mean_overhead_ms']:.1f}ms ({c10['mean_overhead_pct']:+.1f}%) |"
        )

    print()
    print("## P99 Latency Impact")
    print()
    print("| Endpoint | Baseline p99 | SDK (100%) p99 | SDK (10%) p99 |")
    print("|----------|--------------|----------------|---------------|")

    for key, label in [("typical_read", "Read API"), ("typical_write", "Write API"), ("realistic_mixed", "Mixed API")]:
        c100 = comparison_100[key]
        c10 = comparison_10[key]
        print(
            f"| {label} | {c100['baseline_p99_ms']:.1f}ms | {c100['sdk_p99_ms']:.1f}ms (+{c100['p99_overhead_pct']:.1f}%) | {c10['sdk_p99_ms']:.1f}ms (+{c10['p99_overhead_pct']:.1f}%) |"
        )

    print()

    # Save results
    results_dir = Path(__file__).parent.parent / "results"
    results_dir.mkdir(exist_ok=True)

    results = {
        "baseline": baseline,
        "sdk_100_percent": sdk_100,
        "sdk_10_percent": sdk_10,
        "comparison_100": comparison_100,
        "comparison_10": comparison_10,
    }

    with open(results_dir / "realistic-workload.json", "w") as f:
        json.dump(results, f, indent=2)

    print(f"Results saved to {results_dir / 'realistic-workload.json'}")


if __name__ == "__main__":
    main()
