#!/usr/bin/env python3
"""Parse stack sampling benchmark logs and generate analysis artifacts."""

from __future__ import annotations

import argparse
import csv
import math
import re
import statistics
from collections import defaultdict
from pathlib import Path

BENCHMARK_LINE_RE = re.compile(
    r"(Benchmark_\S+)\s+\d+\s+\d+\s+ns/op\s+([\d.]+)\s+ops/s(\s+\(~\))?"
)
LOG_NAME_RE = re.compile(r"(?P<stack>.+)_rate-(?P<rate>[0-9.]+)_run-(?P<run>\d+)\.log$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build sampling-rate degradation datasets and plots from benchmark logs."
    )
    parser.add_argument("--logs-dir", required=True, help="Directory containing *_rate-*_run-*.log files")
    parser.add_argument("--output-dir", required=True, help="Directory to write CSV/plot artifacts")
    return parser.parse_args()


def percentile(values: list[float], p: float) -> float:
    if not values:
        raise ValueError("percentile() requires non-empty values")
    if len(values) == 1:
        return values[0]
    sorted_vals = sorted(values)
    rank = (len(sorted_vals) - 1) * (p / 100.0)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return sorted_vals[lo]
    frac = rank - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def parse_log_file(path: Path) -> list[dict[str, object]]:
    match = LOG_NAME_RE.match(path.name)
    if not match:
        return []

    stack = match.group("stack")
    rate = float(match.group("rate"))
    run = int(match.group("run"))

    baseline: dict[str, tuple[float, bool]] = {}
    sdk: dict[str, tuple[float, bool]] = {}
    section: str | None = None

    with path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()

            if "BASELINE (SDK DISABLED)" in line:
                section = "baseline"
                continue
            if "WITH SDK (TUSK_DRIFT_MODE=RECORD)" in line:
                section = "sdk"
                continue

            m = BENCHMARK_LINE_RE.match(line)
            if not m or section is None:
                continue

            benchmark_name = m.group(1)
            ops = float(m.group(2))
            reliable = m.group(3) is None
            entry = (ops, reliable)

            if section == "baseline":
                baseline[benchmark_name] = entry
            elif section == "sdk":
                sdk[benchmark_name] = entry

    rows: list[dict[str, object]] = []
    all_benchmarks = sorted(set(baseline.keys()) | set(sdk.keys()))
    for benchmark in all_benchmarks:
        base_entry = baseline.get(benchmark)
        sdk_entry = sdk.get(benchmark)

        base_ops = base_entry[0] if base_entry else None
        sdk_ops = sdk_entry[0] if sdk_entry else None
        reliable = bool(base_entry and sdk_entry and base_entry[1] and sdk_entry[1])

        degradation = None
        if base_ops is not None and sdk_ops is not None and base_ops > 0:
            # Positive means slower with SDK.
            degradation = ((base_ops - sdk_ops) / base_ops) * 100.0

        rows.append(
            {
                "stack": stack,
                "sampling_rate": rate,
                "run": run,
                "benchmark": benchmark,
                "baseline_ops": base_ops,
                "sdk_ops": sdk_ops,
                "degradation_pct": degradation,
                "reliable": reliable,
            }
        )

    return rows


def write_csv(path: Path, rows: list[dict[str, object]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def build_per_benchmark_summary(raw_rows: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, float, str], list[float]] = defaultdict(list)
    for row in raw_rows:
        degradation = row["degradation_pct"]
        if row["reliable"] and isinstance(degradation, float):
            key = (str(row["stack"]), float(row["sampling_rate"]), str(row["benchmark"]))
            grouped[key].append(degradation)

    summary_rows: list[dict[str, object]] = []
    for (stack, rate, benchmark), values in grouped.items():
        summary_rows.append(
            {
                "stack": stack,
                "sampling_rate": rate,
                "benchmark": benchmark,
                "samples": len(values),
                "median_degradation_pct": statistics.median(values),
                "p95_degradation_pct": percentile(values, 95.0),
                "min_degradation_pct": min(values),
                "max_degradation_pct": max(values),
            }
        )

    summary_rows.sort(key=lambda r: (str(r["stack"]), float(r["sampling_rate"]), str(r["benchmark"])))
    return summary_rows


def build_stack_summary(raw_rows: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, float], list[float]] = defaultdict(list)
    for row in raw_rows:
        degradation = row["degradation_pct"]
        if row["reliable"] and isinstance(degradation, float):
            key = (str(row["stack"]), float(row["sampling_rate"]))
            grouped[key].append(degradation)

    summary_rows: list[dict[str, object]] = []
    for (stack, rate), values in grouped.items():
        summary_rows.append(
            {
                "stack": stack,
                "sampling_rate": rate,
                "samples": len(values),
                "median_degradation_pct": statistics.median(values),
                "p95_degradation_pct": percentile(values, 95.0),
                "min_degradation_pct": min(values),
                "max_degradation_pct": max(values),
            }
        )

    summary_rows.sort(key=lambda r: (str(r["stack"]), float(r["sampling_rate"])))
    return summary_rows


def build_recommendations(stack_summary_rows: list[dict[str, object]]) -> str:
    tolerances = [1.0, 3.0, 5.0, 10.0]
    by_stack: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in stack_summary_rows:
        by_stack[str(row["stack"])].append(row)

    lines = [
        "# Sampling Recommendations",
        "",
        "Recommended rate uses the highest sampling rate where `p95_degradation_pct <= tolerance`.",
        "",
    ]

    for stack in sorted(by_stack.keys()):
        rows = sorted(by_stack[stack], key=lambda r: float(r["sampling_rate"]))
        lines.append(f"## {stack}")
        for tol in tolerances:
            eligible = [r for r in rows if float(r["p95_degradation_pct"]) <= tol]
            if eligible:
                best = max(eligible, key=lambda r: float(r["sampling_rate"]))
                lines.append(
                    f"- tolerance <= {tol:.0f}%: sampling_rate <= {best['sampling_rate']} "
                    f"(p95={float(best['p95_degradation_pct']):.2f}%, samples={best['samples']})"
                )
            else:
                lines.append(f"- tolerance <= {tol:.0f}%: no measured rate satisfies this bound")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def maybe_render_plot(stack_summary_rows: list[dict[str, object]], output_dir: Path) -> Path | None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed; skipping PNG plot generation")
        return None

    by_stack: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in stack_summary_rows:
        by_stack[str(row["stack"])].append(row)

    if not by_stack:
        return None

    fig, axes = plt.subplots(nrows=len(by_stack), ncols=1, figsize=(10, 4 * len(by_stack)), sharex=True)
    if len(by_stack) == 1:
        axes = [axes]

    for ax, stack in zip(axes, sorted(by_stack.keys())):
        rows = sorted(by_stack[stack], key=lambda r: float(r["sampling_rate"]))
        rates = [float(r["sampling_rate"]) for r in rows]
        med = [float(r["median_degradation_pct"]) for r in rows]
        p95 = [float(r["p95_degradation_pct"]) for r in rows]

        ax.plot(rates, med, marker="o", label="median degradation (%)")
        ax.plot(rates, p95, marker="x", linestyle="--", label="p95 degradation (%)")
        ax.axhline(1.0, color="gray", linestyle=":", linewidth=1)
        ax.axhline(3.0, color="gray", linestyle=":", linewidth=1)
        ax.axhline(5.0, color="gray", linestyle=":", linewidth=1)
        ax.set_title(stack)
        ax.set_ylabel("Degradation (%)")
        ax.grid(True, alpha=0.25)
        ax.legend(loc="best")

    axes[-1].set_xlabel("Sampling rate")
    fig.suptitle("Sampling Rate vs Performance Degradation (stack summary)")
    fig.tight_layout()

    plot_path = output_dir / "sampling-rate-vs-degradation.png"
    fig.savefig(plot_path, dpi=150)
    print(f"wrote plot: {plot_path}")
    return plot_path


def main() -> int:
    args = parse_args()
    logs_dir = Path(args.logs_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    log_files = sorted(logs_dir.glob("*_rate-*_run-*.log"))
    if not log_files:
        raise SystemExit(f"No log files found in {logs_dir}")

    raw_rows: list[dict[str, object]] = []
    for log_file in log_files:
        raw_rows.extend(parse_log_file(log_file))

    if not raw_rows:
        raise SystemExit("No benchmark rows parsed. Check logs for expected benchmark output.")

    raw_rows.sort(
        key=lambda r: (
            str(r["stack"]),
            float(r["sampling_rate"]),
            int(r["run"]),
            str(r["benchmark"]),
        )
    )

    per_benchmark_summary = build_per_benchmark_summary(raw_rows)
    stack_summary = build_stack_summary(raw_rows)

    raw_csv = output_dir / "sampling_benchmark_raw.csv"
    per_bench_csv = output_dir / "sampling_benchmark_per_benchmark_summary.csv"
    stack_csv = output_dir / "sampling_benchmark_stack_summary.csv"
    rec_md = output_dir / "sampling_recommendations.md"

    write_csv(
        raw_csv,
        raw_rows,
        [
            "stack",
            "sampling_rate",
            "run",
            "benchmark",
            "baseline_ops",
            "sdk_ops",
            "degradation_pct",
            "reliable",
        ],
    )
    write_csv(
        per_bench_csv,
        per_benchmark_summary,
        [
            "stack",
            "sampling_rate",
            "benchmark",
            "samples",
            "median_degradation_pct",
            "p95_degradation_pct",
            "min_degradation_pct",
            "max_degradation_pct",
        ],
    )
    write_csv(
        stack_csv,
        stack_summary,
        [
            "stack",
            "sampling_rate",
            "samples",
            "median_degradation_pct",
            "p95_degradation_pct",
            "min_degradation_pct",
            "max_degradation_pct",
        ],
    )

    rec_md.write_text(build_recommendations(stack_summary), encoding="utf-8")

    maybe_render_plot(stack_summary, output_dir)

    print(f"wrote csv: {raw_csv}")
    print(f"wrote csv: {per_bench_csv}")
    print(f"wrote csv: {stack_csv}")
    print(f"wrote markdown: {rec_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

