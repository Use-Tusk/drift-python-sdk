#!/usr/bin/env python3
"""Compare benchmark results and generate summary."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from benchmarks.bench.result_utils import (
    BenchmarkRunResult,
    TaskBenchmarkResult,
    load_result,
)

# Task base names (without the label suffix)
CPU_HEAVY_TASK_BASE = "High CPU: POST /api/compute-hash"
IO_HEAVY_TASK_BASE = "High IO, Low CPU: POST /api/io-bound"
TRANSFORM_TASK_BASE = "Transform endpoints"


def find_task_by_base_name(tasks: list[TaskBenchmarkResult], base_name: str) -> TaskBenchmarkResult | None:
    """Find a task by its base name (ignoring the label suffix)."""
    for task in tasks:
        if task.name.startswith(base_name + " ("):
            return task
    return None


def percentage_change(baseline: float | None, variant: float | None) -> float | None:
    """Calculate percentage change from baseline to variant."""
    if baseline is None or variant is None or baseline == 0:
        return None
    return ((variant - baseline) / baseline) * 100


def format_percentage(value: float | None) -> str:
    """Format a percentage value."""
    if value is None:
        return "n/a"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.1f}%"


def format_latency(ns: float | None) -> str:
    """Format latency in appropriate units."""
    if ns is None:
        return "n/a"
    if ns < 1_000:
        return f"{ns:.0f} ns"
    if ns < 1_000_000:
        return f"{ns / 1_000:.2f} μs"
    if ns < 1_000_000_000:
        return f"{ns / 1_000_000:.2f} ms"
    return f"{ns / 1_000_000_000:.2f} s"


def format_throughput(value: float | None) -> str:
    """Format throughput in ops/s."""
    if value is None:
        return "n/a"
    return f"{value:,.0f}"


def format_bytes(bytes_val: float | None) -> str:
    """Format bytes in MB."""
    if bytes_val is None:
        return "n/a"
    mb = bytes_val / (1024 * 1024)
    return f"{mb:.2f} MB"


def format_cpu_percent(value: float | None) -> str:
    """Format CPU percentage."""
    if value is None:
        return "n/a"
    return f"{value:.1f}%"


def compute_impact(
    label: str,
    baseline: TaskBenchmarkResult | None,
    variant: TaskBenchmarkResult | None,
) -> dict | None:
    """Compute impact metrics between baseline and variant."""
    if not baseline or not variant:
        return None

    baseline_throughput = baseline.throughput.mean
    variant_throughput = variant.throughput.mean
    baseline_p99 = baseline.latency.p99
    variant_p99 = variant.latency.p99

    # CPU stats
    baseline_cpu_user = baseline.resource.get("cpu", {}).get("userPercent") if baseline.resource else None
    variant_cpu_user = variant.resource.get("cpu", {}).get("userPercent") if variant.resource else None
    baseline_cpu_total = baseline.resource.get("cpu", {}).get("totalPercent") if baseline.resource else None
    variant_cpu_total = variant.resource.get("cpu", {}).get("totalPercent") if variant.resource else None

    return {
        "label": label,
        "throughput_delta_pct": percentage_change(baseline_throughput, variant_throughput),
        "tail_latency_delta_pct": percentage_change(baseline_p99, variant_p99),
        "baseline_throughput": baseline_throughput,
        "variant_throughput": variant_throughput,
        "baseline_p99": baseline_p99,
        "variant_p99": variant_p99,
        "cpu_user_delta_pct": percentage_change(baseline_cpu_user, variant_cpu_user),
        "baseline_cpu_total": baseline_cpu_total,
        "variant_cpu_total": variant_cpu_total,
    }


def compute_memory_overhead(
    baseline: BenchmarkRunResult,
    variant: BenchmarkRunResult,
) -> dict:
    """Compute memory overhead between baseline and variant."""
    rss_deltas = []
    rss_max_delta = None

    for baseline_task in baseline.tasks:
        # Find matching task in variant
        base_name = baseline_task.name.rsplit(" (", 1)[0]
        variant_task = find_task_by_base_name(variant.tasks, base_name)

        if not variant_task:
            continue

        baseline_mem = baseline_task.resource.get("memory", {}) if baseline_task.resource else {}
        variant_mem = variant_task.resource.get("memory", {}) if variant_task.resource else {}

        baseline_rss = baseline_mem.get("rss", {})
        variant_rss = variant_mem.get("rss", {})

        if baseline_rss.get("avg") is not None and variant_rss.get("avg") is not None:
            delta = variant_rss["avg"] - baseline_rss["avg"]
            rss_deltas.append(delta)

        if baseline_rss.get("max") is not None and variant_rss.get("max") is not None:
            delta = variant_rss["max"] - baseline_rss["max"]
            if rss_max_delta is None or delta > rss_max_delta:
                rss_max_delta = delta

    return {
        "avg_rss_delta": sum(rss_deltas) / len(rss_deltas) if rss_deltas else None,
        "max_rss_delta": rss_max_delta,
        "samples": len(rss_deltas),
    }


def main():
    """Main comparison function."""
    print("\nLoading benchmark results...")

    baseline = load_result("sdk-disabled")
    active = load_result("sdk-active")
    transforms = load_result("sdk-active-with-transforms")

    if not baseline:
        print("ERROR: sdk-disabled results not found. Run benchmarks first.")
        return
    if not active:
        print("ERROR: sdk-active results not found. Run benchmarks first.")
        return

    print(f"\nBaseline: {baseline.label} ({baseline.timestamp})")
    print(f"Active: {active.label} ({active.timestamp})")
    if transforms:
        print(f"Transforms: {transforms.label} ({transforms.timestamp})")

    # Compute impacts
    cpu_impact = compute_impact(
        CPU_HEAVY_TASK_BASE,
        find_task_by_base_name(baseline.tasks, CPU_HEAVY_TASK_BASE),
        find_task_by_base_name(active.tasks, CPU_HEAVY_TASK_BASE),
    )

    io_impact = compute_impact(
        IO_HEAVY_TASK_BASE,
        find_task_by_base_name(baseline.tasks, IO_HEAVY_TASK_BASE),
        find_task_by_base_name(active.tasks, IO_HEAVY_TASK_BASE),
    )

    transform_impact = None
    if transforms:
        transform_impact = compute_impact(
            TRANSFORM_TASK_BASE,
            find_task_by_base_name(active.tasks, TRANSFORM_TASK_BASE),
            find_task_by_base_name(transforms.tasks, TRANSFORM_TASK_BASE),
        )

    # Print performance impact table
    print("\n## Benchmark Impact Summary\n")
    print("| Workload | Throughput Δ | Tail Latency (p99) Δ | CPU User Δ |")
    print("|----------|-------------|----------------------|------------|")

    if cpu_impact:
        print(
            f"| **CPU-bound** | {format_percentage(cpu_impact['throughput_delta_pct'])} | "
            f"{format_percentage(cpu_impact['tail_latency_delta_pct'])} | "
            f"{format_percentage(cpu_impact['cpu_user_delta_pct'])} |"
        )
    else:
        print("| **CPU-bound** | N/A | N/A | N/A |")

    if io_impact:
        print(
            f"| **IO-bound** | {format_percentage(io_impact['throughput_delta_pct'])} | "
            f"{format_percentage(io_impact['tail_latency_delta_pct'])} | "
            f"{format_percentage(io_impact['cpu_user_delta_pct'])} |"
        )
    else:
        print("| **IO-bound** | N/A | N/A | N/A |")

    if transform_impact:
        print(
            f"| **Transform endpoints** | {format_percentage(transform_impact['throughput_delta_pct'])} | "
            f"{format_percentage(transform_impact['tail_latency_delta_pct'])} | "
            f"{format_percentage(transform_impact['cpu_user_delta_pct'])} |"
        )
    else:
        print("| **Transform endpoints** | N/A | N/A | N/A |")

    # Print absolute values table
    print("\n## Absolute Values\n")
    print("| Workload | Baseline Throughput | Active Throughput | Baseline p99 | Active p99 |")
    print("|----------|--------------------|--------------------|--------------|------------|")

    if cpu_impact:
        print(
            f"| **CPU-bound** | {format_throughput(cpu_impact['baseline_throughput'])} ops/s | "
            f"{format_throughput(cpu_impact['variant_throughput'])} ops/s | "
            f"{format_latency(cpu_impact['baseline_p99'])} | "
            f"{format_latency(cpu_impact['variant_p99'])} |"
        )

    if io_impact:
        print(
            f"| **IO-bound** | {format_throughput(io_impact['baseline_throughput'])} ops/s | "
            f"{format_throughput(io_impact['variant_throughput'])} ops/s | "
            f"{format_latency(io_impact['baseline_p99'])} | "
            f"{format_latency(io_impact['variant_p99'])} |"
        )

    # Memory overhead
    print("\n## Memory Overhead vs Baseline\n")
    print("| Configuration | Avg RSS Δ | Max RSS Δ |")
    print("|---------------|-----------|-----------|")

    active_memory = compute_memory_overhead(baseline, active)
    if active_memory["samples"] > 0:
        print(
            f"| **SDK Active** | {format_bytes(active_memory['avg_rss_delta'])} | "
            f"{format_bytes(active_memory['max_rss_delta'])} |"
        )
    else:
        print("| **SDK Active** | N/A | N/A |")

    if transforms:
        transform_memory = compute_memory_overhead(baseline, transforms)
        if transform_memory["samples"] > 0:
            print(
                f"| **SDK Active w/ Transforms** | {format_bytes(transform_memory['avg_rss_delta'])} | "
                f"{format_bytes(transform_memory['max_rss_delta'])} |"
            )
        else:
            print("| **SDK Active w/ Transforms** | N/A | N/A |")

    print("\n✓ Summary complete.\n")


if __name__ == "__main__":
    main()
