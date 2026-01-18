"""Common benchmark runner logic."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from typing import Any

import requests

# Add parent to path for imports
sys.path.insert(0, str(__file__).rsplit("/", 3)[0])

from benchmarks.bench.resource_monitor import ResourceMonitor
from benchmarks.bench.result_utils import (
    create_benchmark_result,
    create_task_result,
    persist_result,
)
from benchmarks.server.test_server import TestServer

# Default benchmark options
DEFAULT_OPTIONS = {
    "time_per_task_ms": 10_000,  # Run each task for 10 seconds
    "warmup_iterations": 5,
    "enable_memory_tracking": True,
}


class BenchmarkTask:
    """A single benchmark task."""

    def __init__(
        self,
        name: str,
        func: Callable[[], Any],
    ):
        self.name = name
        self.func = func
        self.samples_ns: list[float] = []


def run_benchmarks(
    label: str = "benchmark",
    options: dict[str, Any] | None = None,
) -> None:
    """Run all benchmarks with the given label."""
    opts = {**DEFAULT_OPTIONS, **(options or {})}

    enable_memory_tracking = os.environ.get("BENCHMARK_ENABLE_MEMORY", "true").lower() != "false"

    # Start test server
    server = TestServer()
    server_info = server.start()
    server_url = server_info["url"]

    # Wait for server to be ready
    _wait_for_server(server_url)

    # Initialize resource monitor
    resource_monitor = ResourceMonitor(
        interval_ms=100,
        enable_memory_tracking=enable_memory_tracking,
    )

    # Define benchmark tasks
    tasks = _create_tasks(server_url, label)

    print(f"\n{'=' * 60}")
    print(f"Running benchmarks: {label}")
    print(f"{'=' * 60}\n")

    benchmark_start = time.time()
    resource_monitor.start()

    task_results = []
    for task in tasks:
        print(f"Running: {task.name}")
        resource_monitor.start_task(task.name)

        # Warmup
        for _ in range(opts["warmup_iterations"]):
            task.func()

        # Actual benchmark
        task_start = time.time()
        while (time.time() - task_start) * 1000 < opts["time_per_task_ms"]:
            iter_start = time.perf_counter_ns()
            task.func()
            iter_end = time.perf_counter_ns()
            task.samples_ns.append(iter_end - iter_start)

        resource_monitor.end_task()

        # Create result for this task
        task_result = create_task_result(task.name, task.samples_ns, resource_monitor)
        task_results.append(task_result)

        # Print summary
        if task_result.latency.mean:
            mean_ms = task_result.latency.mean / 1_000_000
            print(f"  Samples: {task_result.samples}, Mean: {mean_ms:.2f}ms")
            if task_result.throughput.mean:
                print(f"  Throughput: {task_result.throughput.mean:.1f} ops/s")
        print()

    resource_monitor.stop()
    benchmark_duration_ms = (time.time() - benchmark_start) * 1000

    # Create and save result
    result = create_benchmark_result(
        label=label,
        tasks=task_results,
        duration_ms=benchmark_duration_ms,
        options=opts,
    )

    output_path = persist_result(result)
    print(f"\n{'=' * 60}")
    print("Benchmark complete!")
    print(f"Results saved to: {output_path}")
    print(f"Total duration: {benchmark_duration_ms / 1000:.1f}s")
    print(f"{'=' * 60}\n")

    # Stop server
    server.stop()


def _wait_for_server(url: str, timeout: float = 10.0) -> None:
    """Wait for the server to be ready."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            response = requests.get(f"{url}/health", timeout=1)
            if response.status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"Server at {url} did not become ready in {timeout}s")


def _create_tasks(server_url: str, test_name: str) -> list[BenchmarkTask]:
    """Create benchmark tasks."""
    tasks = []

    # Use a session for connection pooling to avoid port exhaustion
    session = requests.Session()

    # Task 1: High CPU (compute hash)
    def cpu_bound():
        response = session.post(
            f"{server_url}/api/compute-hash",
            json={"data": "sensitive-data-to-hash", "iterations": 1000},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    tasks.append(
        BenchmarkTask(
            name=f"High CPU: POST /api/compute-hash ({test_name})",
            func=cpu_bound,
        )
    )

    # Task 2: High IO, Low CPU
    def io_bound():
        response = session.post(
            f"{server_url}/api/io-bound",
            json={"jobs": 5, "delayMs": 5},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    tasks.append(
        BenchmarkTask(
            name=f"High IO, Low CPU: POST /api/io-bound ({test_name})",
            func=io_bound,
        )
    )

    # Task 3: Transform endpoints (cycling through sensitive data endpoints)
    transform_endpoints = [
        {
            "path": "/api/auth/login",
            "body": {"email": "user@example.com", "password": "super-secret-password-123"},
        },
        {
            "path": "/api/users",
            "body": {
                "username": "testuser",
                "email": "test@example.com",
                "ssn": "123-45-6789",
                "creditCard": "4111-1111-1111-1111",
            },
        },
    ]
    endpoint_index = [0]  # Use list to allow mutation in closure

    def transform_task():
        endpoint = transform_endpoints[endpoint_index[0] % len(transform_endpoints)]
        endpoint_index[0] += 1
        response = session.post(
            f"{server_url}{endpoint['path']}",
            json=endpoint["body"],
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    tasks.append(
        BenchmarkTask(
            name=f"Transform endpoints ({test_name})",
            func=transform_task,
        )
    )

    return tasks


if __name__ == "__main__":
    # Quick test run
    run_benchmarks(label="test-run", options={"time_per_task_ms": 3000})
