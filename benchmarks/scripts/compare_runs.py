#!/usr/bin/env python3
"""Compare benchmark results from different runs.

Usage:
    python compare_runs.py results1.json results2.json
    python compare_runs.py --download RUN_ID1 RUN_ID2  # Download from GitHub Actions
"""

import argparse
import json


def load_results(path: str) -> dict:
    """Load benchmark results from JSON file."""
    with open(path) as f:
        return json.load(f)


def compare_realistic_workload(old: dict, new: dict) -> list[dict]:
    """Compare realistic workload results."""
    comparisons = []

    old_comp = old.get("comparison_100", old.get("realistic_workload", {}).get("comparison_100", {}))
    new_comp = new.get("comparison_100", new.get("realistic_workload", {}).get("comparison_100", {}))

    for endpoint in ["typical_read", "typical_write", "realistic_mixed"]:
        if endpoint in old_comp and endpoint in new_comp:
            old_overhead = old_comp[endpoint]["mean_overhead_ms"]
            new_overhead = new_comp[endpoint]["mean_overhead_ms"]
            delta = new_overhead - old_overhead
            delta_pct = (delta / old_overhead * 100) if old_overhead != 0 else 0

            comparisons.append(
                {
                    "endpoint": endpoint,
                    "old_overhead_ms": round(old_overhead, 2),
                    "new_overhead_ms": round(new_overhead, 2),
                    "delta_ms": round(delta, 2),
                    "delta_pct": round(delta_pct, 1),
                    "regression": delta > 0.5,  # Flag if overhead increased by more than 0.5ms
                }
            )

    return comparisons


def compare_fixed_qps(old: dict, new: dict) -> list[dict]:
    """Compare fixed QPS results."""
    comparisons = []

    old_baseline = old.get("baseline", old.get("fixed_qps_latency", {}).get("baseline", {}))
    new_baseline = new.get("baseline", new.get("fixed_qps_latency", {}).get("baseline", {}))

    old_sdk = old.get("sdk_100", old.get("fixed_qps_latency", {}).get("sdk_100", {}))
    new_sdk = new.get("sdk_100", new.get("fixed_qps_latency", {}).get("sdk_100", {}))

    for qps in ["25", "50", "75"]:
        if qps in old_sdk and qps in new_sdk:
            old_overhead = old_sdk[qps]["mean_ms"] - old_baseline.get(qps, {}).get("mean_ms", 0)
            new_overhead = new_sdk[qps]["mean_ms"] - new_baseline.get(qps, {}).get("mean_ms", 0)
            delta = new_overhead - old_overhead

            comparisons.append(
                {
                    "qps": int(qps),
                    "old_overhead_ms": round(old_overhead, 2),
                    "new_overhead_ms": round(new_overhead, 2),
                    "delta_ms": round(delta, 2),
                    "regression": delta > 0.5,
                }
            )

    return comparisons


def print_comparison(old_path: str, new_path: str, old: dict, new: dict) -> None:
    """Print comparison results."""
    print("=" * 70)
    print("Benchmark Comparison")
    print("=" * 70)
    print()
    print(f"Old: {old_path}")
    print(f"New: {new_path}")
    print()

    # Metadata comparison if available
    old_meta = old.get("metadata", {})
    new_meta = new.get("metadata", {})

    if old_meta or new_meta:
        print("### Metadata")
        print(f"  Old run: {old_meta.get('run_id', 'N/A')} @ {old_meta.get('timestamp', 'N/A')}")
        print(f"  New run: {new_meta.get('run_id', 'N/A')} @ {new_meta.get('timestamp', 'N/A')}")
        print()

    # Realistic workload comparison
    print("### Realistic Workload (100% sampling)")
    print()
    print("| Endpoint | Old Overhead | New Overhead | Delta | Status |")
    print("|----------|--------------|--------------|-------|--------|")

    realistic_comps = compare_realistic_workload(old, new)
    has_regression = False

    for comp in realistic_comps:
        status = "⚠️ REGRESSION" if comp["regression"] else "✅ OK"
        if comp["regression"]:
            has_regression = True
        print(
            f"| {comp['endpoint']} | {comp['old_overhead_ms']}ms | {comp['new_overhead_ms']}ms | {comp['delta_ms']:+.2f}ms | {status} |"
        )

    print()

    # Fixed QPS comparison
    print("### Fixed QPS Latency (100% sampling)")
    print()
    print("| QPS | Old Overhead | New Overhead | Delta | Status |")
    print("|-----|--------------|--------------|-------|--------|")

    qps_comps = compare_fixed_qps(old, new)

    for comp in qps_comps:
        status = "⚠️ REGRESSION" if comp["regression"] else "✅ OK"
        if comp["regression"]:
            has_regression = True
        print(
            f"| {comp['qps']} | {comp['old_overhead_ms']}ms | {comp['new_overhead_ms']}ms | {comp['delta_ms']:+.2f}ms | {status} |"
        )

    print()

    if has_regression:
        print("⚠️  REGRESSION DETECTED: Some metrics show increased overhead")
    else:
        print("✅ No regressions detected")

    # Output as JSON for programmatic use
    output = {
        "realistic_workload": realistic_comps,
        "fixed_qps": qps_comps,
        "has_regression": has_regression,
    }

    print()
    print("### JSON Output")
    print(json.dumps(output, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Compare benchmark results")
    parser.add_argument("old", help="Path to old results JSON")
    parser.add_argument("new", help="Path to new results JSON")
    parser.add_argument("--json", action="store_true", help="Output only JSON")

    args = parser.parse_args()

    old = load_results(args.old)
    new = load_results(args.new)

    if args.json:
        realistic_comps = compare_realistic_workload(old, new)
        qps_comps = compare_fixed_qps(old, new)
        has_regression = any(c["regression"] for c in realistic_comps + qps_comps)

        output = {
            "realistic_workload": realistic_comps,
            "fixed_qps": qps_comps,
            "has_regression": has_regression,
        }
        print(json.dumps(output, indent=2))
    else:
        print_comparison(args.old, args.new, old, new)


if __name__ == "__main__":
    main()
