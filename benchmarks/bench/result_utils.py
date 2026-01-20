"""Result utilities for benchmarks - serialization and comparison."""

from __future__ import annotations

import json
import os
import platform
import statistics
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .resource_monitor import ResourceMonitor

RESULTS_DIR = Path(__file__).parent.parent / "results"


@dataclass
class MetricSummary:
    """Summary statistics for a metric."""

    unit: str  # "ns" or "ops/s"
    mean: float | None = None
    median: float | None = None
    min: float | None = None
    max: float | None = None
    std_dev: float | None = None
    p99: float | None = None
    samples: int = 0


@dataclass
class HistogramBucket:
    """A histogram bucket."""

    min_ns: float
    max_ns: float
    count: int


@dataclass
class TaskBenchmarkResult:
    """Benchmark result for a single task."""

    name: str
    samples: int
    latency: MetricSummary
    throughput: MetricSummary
    resource: dict[str, Any] | None = None
    histogram: list[HistogramBucket] | None = None


@dataclass
class SystemInfo:
    """System information."""

    python_version: str
    platform_name: str
    arch: str
    cpu_count: int
    total_memory: int
    load_average: list[float]


@dataclass
class BenchmarkRunResult:
    """Complete benchmark run result."""

    id: str
    label: str
    timestamp: str
    duration_ms: float
    options: dict[str, Any]
    system: SystemInfo
    tasks: list[TaskBenchmarkResult] = field(default_factory=list)


def get_system_info() -> SystemInfo:
    """Collect system information."""
    import psutil  # ty:ignore[unresolved-import]

    return SystemInfo(
        python_version=platform.python_version(),
        platform_name=platform.system().lower(),
        arch=platform.machine(),
        cpu_count=os.cpu_count() or 1,
        total_memory=psutil.virtual_memory().total,
        load_average=list(os.getloadavg()) if hasattr(os, "getloadavg") else [0.0, 0.0, 0.0],
    )


def build_latency_summary(samples_ns: list[float]) -> MetricSummary:
    """Build latency summary from samples (in nanoseconds)."""
    if not samples_ns:
        return MetricSummary(unit="ns", samples=0)

    sorted_samples = sorted(samples_ns)
    n = len(sorted_samples)

    # Calculate p99
    p99_idx = int(n * 0.99)
    p99 = sorted_samples[min(p99_idx, n - 1)]

    return MetricSummary(
        unit="ns",
        mean=statistics.mean(samples_ns),
        median=statistics.median(samples_ns),
        min=min(samples_ns),
        max=max(samples_ns),
        std_dev=statistics.stdev(samples_ns) if len(samples_ns) > 1 else 0.0,
        p99=p99,
        samples=n,
    )


def build_throughput_summary(samples_ns: list[float]) -> MetricSummary:
    """Build throughput summary (ops/s) from latency samples (ns)."""
    if not samples_ns:
        return MetricSummary(unit="ops/s", samples=0)

    # Convert ns latencies to ops/s
    ops_per_sec = [1_000_000_000 / ns if ns > 0 else 0 for ns in samples_ns]

    return MetricSummary(
        unit="ops/s",
        mean=statistics.mean(ops_per_sec),
        median=statistics.median(ops_per_sec),
        min=min(ops_per_sec),
        max=max(ops_per_sec),
        std_dev=statistics.stdev(ops_per_sec) if len(ops_per_sec) > 1 else 0.0,
        samples=len(ops_per_sec),
    )


def build_histogram(samples_ns: list[float], bucket_count: int = 20) -> list[HistogramBucket]:
    """Build a histogram from latency samples."""
    if not samples_ns:
        return []

    sorted_samples = sorted(samples_ns)
    min_val = sorted_samples[0]
    max_val = sorted_samples[-1]

    if min_val == max_val:
        return [HistogramBucket(min_ns=min_val, max_ns=max_val, count=len(samples_ns))]

    bucket_size = (max_val - min_val) / bucket_count
    counts = [0] * bucket_count

    for sample in samples_ns:
        idx = int((sample - min_val) / bucket_size)
        idx = min(idx, bucket_count - 1)
        counts[idx] += 1

    buckets = []
    for i, count in enumerate(counts):
        bucket_min = min_val + i * bucket_size
        bucket_max = max_val if i == bucket_count - 1 else min_val + (i + 1) * bucket_size
        buckets.append(HistogramBucket(min_ns=bucket_min, max_ns=bucket_max, count=count))

    return buckets


def create_task_result(
    name: str,
    samples_ns: list[float],
    resource_monitor: ResourceMonitor,
) -> TaskBenchmarkResult:
    """Create a task benchmark result from raw samples."""
    return TaskBenchmarkResult(
        name=name,
        samples=len(samples_ns),
        latency=build_latency_summary(samples_ns),
        throughput=build_throughput_summary(samples_ns),
        resource=resource_monitor.to_dict(name),
        histogram=build_histogram(samples_ns),
    )


def create_benchmark_result(
    label: str,
    tasks: list[TaskBenchmarkResult],
    duration_ms: float,
    options: dict[str, Any],
) -> BenchmarkRunResult:
    """Create a complete benchmark run result."""
    return BenchmarkRunResult(
        id=str(uuid.uuid4()),
        label=label,
        timestamp=datetime.now(timezone.utc).isoformat(),
        duration_ms=duration_ms,
        options=options,
        system=get_system_info(),
        tasks=tasks,
    )


def metric_to_dict(metric: MetricSummary) -> dict[str, Any]:
    """Convert MetricSummary to dict."""
    return {
        "unit": metric.unit,
        "mean": metric.mean,
        "median": metric.median,
        "min": metric.min,
        "max": metric.max,
        "standardDeviation": metric.std_dev,
        "p99": metric.p99,
        "samples": metric.samples,
    }


def task_to_dict(task: TaskBenchmarkResult) -> dict[str, Any]:
    """Convert TaskBenchmarkResult to dict."""
    result: dict[str, Any] = {
        "name": task.name,
        "samples": task.samples,
        "latency": metric_to_dict(task.latency),
        "throughput": metric_to_dict(task.throughput),
    }
    if task.resource:
        result["resource"] = task.resource
    if task.histogram:
        result["histogram"] = [{"minNs": b.min_ns, "maxNs": b.max_ns, "count": b.count} for b in task.histogram]
    return result


def result_to_dict(result: BenchmarkRunResult) -> dict[str, Any]:
    """Convert BenchmarkRunResult to dict."""
    return {
        "id": result.id,
        "label": result.label,
        "timestamp": result.timestamp,
        "durationMs": result.duration_ms,
        "options": result.options,
        "system": {
            "pythonVersion": result.system.python_version,
            "platform": result.system.platform_name,
            "arch": result.system.arch,
            "cpuCount": result.system.cpu_count,
            "totalMemory": result.system.total_memory,
            "loadAverage": result.system.load_average,
        },
        "tasks": [task_to_dict(task) for task in result.tasks],
    }


def persist_result(result: BenchmarkRunResult) -> Path:
    """Save benchmark result to JSON file."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    sanitized_label = "".join(c if c.isalnum() or c in "-_" else "-" for c in result.label)
    filename = f"{sanitized_label}.json"
    output_path = RESULTS_DIR / filename

    with open(output_path, "w") as f:
        json.dump(result_to_dict(result), f, indent=2)

    return output_path


def load_result(label: str) -> BenchmarkRunResult | None:
    """Load a benchmark result from JSON file."""
    sanitized_label = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)
    filename = f"{sanitized_label}.json"
    filepath = RESULTS_DIR / filename

    if not filepath.exists():
        return None

    with open(filepath) as f:
        data = json.load(f)

    # Reconstruct the result object
    system = SystemInfo(
        python_version=data["system"]["pythonVersion"],
        platform_name=data["system"]["platform"],
        arch=data["system"]["arch"],
        cpu_count=data["system"]["cpuCount"],
        total_memory=data["system"]["totalMemory"],
        load_average=data["system"]["loadAverage"],
    )

    tasks = []
    for task_data in data["tasks"]:
        latency = MetricSummary(
            unit=task_data["latency"]["unit"],
            mean=task_data["latency"].get("mean"),
            median=task_data["latency"].get("median"),
            min=task_data["latency"].get("min"),
            max=task_data["latency"].get("max"),
            std_dev=task_data["latency"].get("standardDeviation"),
            p99=task_data["latency"].get("p99"),
            samples=task_data["latency"].get("samples", 0),
        )
        throughput = MetricSummary(
            unit=task_data["throughput"]["unit"],
            mean=task_data["throughput"].get("mean"),
            median=task_data["throughput"].get("median"),
            min=task_data["throughput"].get("min"),
            max=task_data["throughput"].get("max"),
            std_dev=task_data["throughput"].get("standardDeviation"),
            samples=task_data["throughput"].get("samples", 0),
        )

        histogram = None
        if "histogram" in task_data and task_data["histogram"]:
            histogram = [
                HistogramBucket(
                    min_ns=b["minNs"],
                    max_ns=b["maxNs"],
                    count=b["count"],
                )
                for b in task_data["histogram"]
            ]

        tasks.append(
            TaskBenchmarkResult(
                name=task_data["name"],
                samples=task_data["samples"],
                latency=latency,
                throughput=throughput,
                resource=task_data.get("resource"),
                histogram=histogram,
            )
        )

    return BenchmarkRunResult(
        id=data["id"],
        label=data["label"],
        timestamp=data["timestamp"],
        duration_ms=data["durationMs"],
        options=data["options"],
        system=system,
        tasks=tasks,
    )
