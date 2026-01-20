#!/usr/bin/env python3
"""Benchmark: Fixed QPS Latency Test.

This benchmark measures latency at fixed request rates (QPS) to show
how the SDK affects response times under controlled load.

Unlike throughput tests that blast requests as fast as possible, this
sends requests at a steady rate to measure realistic latency impact.
"""

import json
import os
import subprocess
import sys
from pathlib import Path


def run_fixed_qps_benchmark(
    mode: str, target_qps: int, duration_seconds: int = 10, sampling_rate: float = 1.0
) -> dict | None:
    """Run benchmark at a fixed QPS in a subprocess."""

    script = f'''
import os
import sys
import time
import shutil
import statistics
import threading
from pathlib import Path

os.environ["TUSK_DRIFT_MODE"] = "{mode}"
sys.path.insert(0, "{os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))}")

import requests

TRACE_DIR = Path("{Path(__file__).parent.parent / ".benchmark-traces-qps"}")
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
time.sleep(0.5)

session = requests.Session()

# Warmup
for _ in range(20):
    session.post(f"{{server_url}}/api/realistic", json={{"userId": "warmup", "query": "test"}})

# Fixed QPS parameters
target_qps = {target_qps}
duration_seconds = {duration_seconds}
interval = 1.0 / target_qps  # Time between requests

latencies = []
start_time = time.perf_counter()
request_count = 0
target_requests = target_qps * duration_seconds

while request_count < target_requests:
    # Calculate when this request should start
    expected_start = start_time + (request_count * interval)
    now = time.perf_counter()

    # Wait if we're ahead of schedule
    if now < expected_start:
        time.sleep(expected_start - now)

    # Make request and measure latency
    req_start = time.perf_counter_ns()
    response = session.post(
        f"{{server_url}}/api/realistic",
        json={{"userId": f"user-{{request_count}}", "query": "search query", "email": "test@example.com"}},
    )
    response.json()
    latencies.append(time.perf_counter_ns() - req_start)
    request_count += 1

elapsed = time.perf_counter() - start_time
actual_qps = request_count / elapsed

# Calculate statistics
sorted_latencies = sorted(latencies)
results = {{
    "target_qps": target_qps,
    "actual_qps": round(actual_qps, 1),
    "total_requests": request_count,
    "duration_seconds": round(elapsed, 2),
    "mean_ms": statistics.mean(latencies) / 1_000_000,
    "p50_ms": sorted_latencies[len(sorted_latencies) // 2] / 1_000_000,
    "p90_ms": sorted_latencies[int(len(sorted_latencies) * 0.90)] / 1_000_000,
    "p99_ms": sorted_latencies[int(len(sorted_latencies) * 0.99)] / 1_000_000,
    "min_ms": min(latencies) / 1_000_000,
    "max_ms": max(latencies) / 1_000_000,
}}

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
        timeout=180,
    )

    if result.returncode != 0:
        print(f"Error running benchmark ({mode}, {target_qps} QPS):")
        print(result.stderr[-1000:])
        return None

    for line in result.stdout.split("\n"):
        if line.startswith("RESULTS_JSON:"):
            return json.loads(line.replace("RESULTS_JSON:", ""))

    print("Could not find results in output")
    return None


def main():
    print("=" * 70)
    print("Fixed QPS Latency Benchmark")
    print("=" * 70)
    print()
    print("This test sends requests at a fixed rate to measure latency impact.")
    print("Endpoint: POST /api/realistic (~10-15ms baseline)")
    print()

    # Test at multiple QPS levels (duration configurable via BENCHMARK_QPS_DURATION env var)
    qps_levels = [25, 50, 75]  # Requests per second
    duration = int(os.environ.get("BENCHMARK_QPS_DURATION", "10"))  # seconds per test

    results = {"baseline": {}, "sdk_100": {}, "sdk_10": {}}

    for qps in qps_levels:
        print(f"Testing at {qps} QPS...")

        # Baseline
        print("  Running baseline (SDK disabled)...")
        baseline = run_fixed_qps_benchmark("DISABLED", qps, duration)
        if baseline:
            results["baseline"][qps] = baseline
            print(f"    Mean: {baseline['mean_ms']:.1f}ms, p99: {baseline['p99_ms']:.1f}ms")

        # SDK 100%
        print("  Running SDK (100% sampling)...")
        sdk_100 = run_fixed_qps_benchmark("RECORD", qps, duration, sampling_rate=1.0)
        if sdk_100:
            results["sdk_100"][qps] = sdk_100
            print(f"    Mean: {sdk_100['mean_ms']:.1f}ms, p99: {sdk_100['p99_ms']:.1f}ms")

        # SDK 10%
        print("  Running SDK (10% sampling)...")
        sdk_10 = run_fixed_qps_benchmark("RECORD", qps, duration, sampling_rate=0.1)
        if sdk_10:
            results["sdk_10"][qps] = sdk_10
            print(f"    Mean: {sdk_10['mean_ms']:.1f}ms, p99: {sdk_10['p99_ms']:.1f}ms")

        print()

    # Print summary table
    print("=" * 70)
    print("Results Summary: Latency at Fixed QPS")
    print("=" * 70)
    print()

    print("### Mean Latency (ms)")
    print()
    print("| QPS | Baseline | SDK (100%) | Overhead | SDK (10%) | Overhead |")
    print("|-----|----------|------------|----------|-----------|----------|")

    for qps in qps_levels:
        b = results["baseline"].get(qps, {})
        s100 = results["sdk_100"].get(qps, {})
        s10 = results["sdk_10"].get(qps, {})

        if b and s100 and s10:
            b_mean = b["mean_ms"]
            s100_mean = s100["mean_ms"]
            s10_mean = s10["mean_ms"]

            overhead_100 = s100_mean - b_mean
            overhead_10 = s10_mean - b_mean
            pct_100 = (overhead_100 / b_mean) * 100 if b_mean > 0 else 0
            pct_10 = (overhead_10 / b_mean) * 100 if b_mean > 0 else 0

            print(
                f"| {qps} | {b_mean:.1f}ms | {s100_mean:.1f}ms | +{overhead_100:.1f}ms ({pct_100:+.0f}%) | {s10_mean:.1f}ms | +{overhead_10:.1f}ms ({pct_10:+.0f}%) |"
            )

    print()
    print("### P99 Latency (ms)")
    print()
    print("| QPS | Baseline | SDK (100%) | Overhead | SDK (10%) | Overhead |")
    print("|-----|----------|------------|----------|-----------|----------|")

    for qps in qps_levels:
        b = results["baseline"].get(qps, {})
        s100 = results["sdk_100"].get(qps, {})
        s10 = results["sdk_10"].get(qps, {})

        if b and s100 and s10:
            b_p99 = b["p99_ms"]
            s100_p99 = s100["p99_ms"]
            s10_p99 = s10["p99_ms"]

            overhead_100 = s100_p99 - b_p99
            overhead_10 = s10_p99 - b_p99
            pct_100 = (overhead_100 / b_p99) * 100 if b_p99 > 0 else 0
            pct_10 = (overhead_10 / b_p99) * 100 if b_p99 > 0 else 0

            print(
                f"| {qps} | {b_p99:.1f}ms | {s100_p99:.1f}ms | +{overhead_100:.1f}ms ({pct_100:+.0f}%) | {s10_p99:.1f}ms | +{overhead_10:.1f}ms ({pct_10:+.0f}%) |"
            )

    print()

    # Save results
    results_dir = Path(__file__).parent.parent / "results"
    results_dir.mkdir(exist_ok=True)

    with open(results_dir / "fixed-qps-latency.json", "w") as f:
        json.dump(results, f, indent=2)

    print(f"Results saved to {results_dir / 'fixed-qps-latency.json'}")


if __name__ == "__main__":
    main()
