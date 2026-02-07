"""
Shared test utilities for e2e tests.

This module provides common functions used across all instrumentation e2e tests,
including request counting for validation and optional benchmark mode.
"""

import os
import time

import requests

BASE_URL = "http://localhost:8000"
_request_count = 0

BENCHMARK_MODE = os.environ.get("BENCHMARKS", "") != ""
BENCHMARK_DURATION = int(os.environ.get("BENCHMARK_DURATION", "10"))
BENCHMARK_WARMUP = int(os.environ.get("BENCHMARK_WARMUP", "3"))

# Minimum iterations required for a result to be considered reliable
_MIN_RELIABLE_ITERATIONS = 30


def make_request(method, endpoint, **kwargs):
    """Make HTTP request, log result, and track count."""
    if BENCHMARK_MODE:
        return _benchmark_request(method, endpoint, **kwargs)

    global _request_count
    _request_count += 1

    url = f"{BASE_URL}{endpoint}"
    print(f"→ {method} {endpoint}")
    kwargs.setdefault("timeout", 30)
    response = requests.request(method, url, **kwargs)
    print(f"  Status: {response.status_code}")
    time.sleep(0.5)
    return response


def _benchmark_request(method, endpoint, **kwargs):
    """Run a timed benchmark loop for a single request and print Go-style stats."""
    url = f"{BASE_URL}{endpoint}"
    name = f"Benchmark_{method}_{endpoint}"
    # Use shorter timeout for benchmarks to fail fast on slow external endpoints
    kwargs.setdefault("timeout", 10)
    session = requests.Session()

    # Check endpoint is reachable first
    try:
        resp = session.request(method, url, **kwargs)
        if resp.status_code >= 500:
            print(f"{name:<40} SKIPPED (endpoint error: {resp.status_code})")
            return resp
    except Exception as exc:
        print(f"{name:<40} SKIPPED ({type(exc).__name__}: {exc})")
        return None

    # Warmup: stabilize caches, TCP connections, server-side JIT, etc.
    # Errors during warmup are tolerated — some endpoints hit external APIs that may flake.
    warmup_deadline = time.perf_counter_ns() + (BENCHMARK_WARMUP * 1_000_000_000)
    warmup_errors = 0
    while time.perf_counter_ns() < warmup_deadline:
        try:
            session.request(method, url, **kwargs)
        except Exception:
            warmup_errors += 1
            if warmup_errors > 5:
                print(f"{name:<40} SKIPPED (too many warmup errors)")
                return None

    # Timed benchmark loop
    # Errors during the loop abort the benchmark for this endpoint.
    last_resp = None
    start = time.perf_counter_ns()
    deadline = start + (BENCHMARK_DURATION * 1_000_000_000)
    iterations = 0
    errors = 0
    while time.perf_counter_ns() < deadline:
        try:
            last_resp = session.request(method, url, **kwargs)
            iterations += 1
        except Exception:
            errors += 1
            if errors > 3:
                # Too many errors — endpoint is flaky (likely external API issues)
                elapsed_ns = time.perf_counter_ns() - start
                if iterations > 0:
                    ns_per_op = elapsed_ns // iterations
                    ops_per_sec = iterations / (elapsed_ns / 1_000_000_000)
                    print(
                        f"{name:<40} {iterations:>8} {ns_per_op:>15} ns/op {ops_per_sec:>12.2f} ops/s (partial, {errors} errors)"
                    )
                else:
                    print(f"{name:<40} FAILED ({errors} errors, 0 successful iterations)")
                return last_resp

    elapsed_ns = time.perf_counter_ns() - start
    ns_per_op = elapsed_ns // iterations
    ops_per_sec = iterations / (elapsed_ns / 1_000_000_000)

    flag = "" if iterations >= _MIN_RELIABLE_ITERATIONS else " (~)"
    if errors > 0:
        flag = f" ({errors} errors)"
    print(f"{name:<40} {iterations:>8} {ns_per_op:>15} ns/op {ops_per_sec:>12.2f} ops/s{flag}")
    return last_resp


def get_request_count():
    """Return the current request count."""
    return _request_count


def print_request_summary():
    """Print the total request count in a parseable format."""
    if BENCHMARK_MODE:
        return
    print(f"\nTOTAL_REQUESTS_SENT:{_request_count}")
