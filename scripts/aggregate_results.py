#!/usr/bin/env python3
"""
Aggregate pipeline results across multiple runs per spec (RPR - Repeated Pipeline Runs).

Computes mean, std dev, min, max for key metrics across runs.

Usage:
    python scripts/aggregate_results.py [--spec SPEC] [--version VERSION]

Examples:
    python scripts/aggregate_results.py                    # All specs
    python scripts/aggregate_results.py --spec supportops  # Just supportops
    python scripts/aggregate_results.py --spec supportops --version v1  # Just v1
"""

import json
import argparse
from pathlib import Path
from collections import defaultdict
import statistics


def load_results(results_dir: Path) -> list[dict]:
    """Load all results from results/ folder."""
    results = []

    # Load from all_runs.json if it exists
    all_runs_file = results_dir / "all_runs.json"
    if all_runs_file.exists():
        with open(all_runs_file) as f:
            results.extend(json.load(f))

    # Also load any individual result files
    for f in results_dir.glob("*.json"):
        if f.name != "all_runs.json" and f.name != "aggregated.json":
            try:
                with open(f) as fp:
                    data = json.load(fp)
                    # Individual files are single objects, not arrays
                    if isinstance(data, dict):
                        # Check if already in results (by run_id)
                        if not any(r.get("run_id") == data.get("run_id") for r in results):
                            results.append(data)
            except json.JSONDecodeError:
                print(f"Warning: Could not parse {f}")

    return results


def aggregate_by_spec_version(results: list[dict]) -> dict:
    """Group results by spec and version, compute statistics."""
    grouped = defaultdict(list)

    for r in results:
        spec = r.get("spec", "unknown")
        version = r.get("version", "v1")
        key = f"{spec}/{version}"
        grouped[key].append(r)

    aggregated = {}
    for key, runs in grouped.items():
        metrics_to_aggregate = [
            "vpr_percent", "hpr_percent", "mutation_score", "surs_percent",
            "compiler_iterations", "seed_vpr_passed", "seed_vpr_total",
            "seed_hpr_passed", "seed_hpr_total"
        ]

        agg = {
            "spec": runs[0].get("spec"),
            "version": runs[0].get("version"),
            "num_runs": len(runs),
            "run_ids": [r.get("run_id") for r in runs],
            "metrics": {}
        }

        for metric in metrics_to_aggregate:
            values = []
            for r in runs:
                m = r.get("metrics", {})
                if metric in m and m[metric] is not None:
                    values.append(m[metric])

            if values:
                agg["metrics"][metric] = {
                    "mean": round(statistics.mean(values), 2),
                    "std": round(statistics.stdev(values), 2) if len(values) > 1 else 0,
                    "min": round(min(values), 2),
                    "max": round(max(values), 2),
                    "n": len(values),
                    "values": values
                }

        # Also aggregate timing and costs
        timing_metrics = ["testsmith_seconds", "compiler_seconds", "evaluate_seconds",
                         "mutation_seconds", "total_seconds"]
        agg["timing"] = {}
        for metric in timing_metrics:
            values = [r.get("timing", {}).get(metric, 0) for r in runs if r.get("timing", {}).get(metric)]
            if values:
                agg["timing"][metric] = {
                    "mean": round(statistics.mean(values), 1),
                    "std": round(statistics.stdev(values), 1) if len(values) > 1 else 0,
                    "min": min(values),
                    "max": max(values)
                }

        cost_values = [r.get("costs", {}).get("total_cost_usd", 0) for r in runs if r.get("costs", {}).get("total_cost_usd")]
        if cost_values:
            agg["costs"] = {
                "total_cost_usd": {
                    "mean": round(statistics.mean(cost_values), 2),
                    "std": round(statistics.stdev(cost_values), 2) if len(cost_values) > 1 else 0,
                    "min": round(min(cost_values), 2),
                    "max": round(max(cost_values), 2)
                }
            }

        aggregated[key] = agg

    return aggregated


def print_summary(aggregated: dict, spec_filter: str = None, version_filter: str = None):
    """Print a summary table of aggregated results."""
    print("\n" + "=" * 80)
    print("AGGREGATED PIPELINE RESULTS (RPR - Repeated Pipeline Runs)")
    print("=" * 80)

    for key, agg in sorted(aggregated.items()):
        spec = agg["spec"]
        version = agg["version"]

        # Apply filters
        if spec_filter and spec != spec_filter:
            continue
        if version_filter and version != version_filter:
            continue

        print(f"\n{spec}/{version} ({agg['num_runs']} runs)")
        print("-" * 40)

        m = agg.get("metrics", {})

        # VPR
        if "vpr_percent" in m:
            v = m["vpr_percent"]
            print(f"  VPR:      {v['mean']:.1f}% ± {v['std']:.1f}% (range: {v['min']:.1f}-{v['max']:.1f}%)")

        # HPR
        if "hpr_percent" in m:
            v = m["hpr_percent"]
            print(f"  HPR:      {v['mean']:.1f}% ± {v['std']:.1f}% (range: {v['min']:.1f}-{v['max']:.1f}%)")

        # Mutation score
        if "mutation_score" in m:
            v = m["mutation_score"]
            print(f"  Mutation: {v['mean']:.1f}% ± {v['std']:.1f}% (range: {v['min']:.1f}-{v['max']:.1f}%)")

        # SURS (v2 only)
        if "surs_percent" in m and m["surs_percent"]["mean"] > 0:
            v = m["surs_percent"]
            print(f"  SURS:     {v['mean']:.1f}% ± {v['std']:.1f}% (range: {v['min']:.1f}-{v['max']:.1f}%)")

        # Seed baseline (if available)
        if "seed_vpr_total" in m and m["seed_vpr_total"]["mean"] > 0:
            seed_total = m["seed_vpr_total"]["mean"]
            seed_passed = m.get("seed_vpr_passed", {}).get("mean", 0)
            print(f"  Seed VPR: {seed_passed:.0f}/{seed_total:.0f} (0% baseline)")

        # Compiler iterations
        if "compiler_iterations" in m:
            v = m["compiler_iterations"]
            print(f"  Iterations: {v['mean']:.1f} ± {v['std']:.1f} (range: {v['min']:.0f}-{v['max']:.0f})")

        # Timing
        t = agg.get("timing", {})
        if "total_seconds" in t:
            v = t["total_seconds"]
            print(f"  Time:     {v['mean']/60:.1f}min ± {v['std']/60:.1f}min")

        # Cost
        c = agg.get("costs", {})
        if "total_cost_usd" in c:
            v = c["total_cost_usd"]
            print(f"  Cost:     ${v['mean']:.2f} ± ${v['std']:.2f}")

    print("\n" + "=" * 80)


def main():
    parser = argparse.ArgumentParser(description="Aggregate pipeline results")
    parser.add_argument("--spec", help="Filter by spec name")
    parser.add_argument("--version", help="Filter by version (v1, v2)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--output", help="Save aggregated results to file")
    args = parser.parse_args()

    # Find results directory
    script_dir = Path(__file__).parent
    results_dir = script_dir.parent / "results"

    if not results_dir.exists():
        print(f"Error: Results directory not found: {results_dir}")
        return 1

    # Load and aggregate
    results = load_results(results_dir)
    print(f"Loaded {len(results)} runs from {results_dir}")

    aggregated = aggregate_by_spec_version(results)

    if args.json:
        print(json.dumps(aggregated, indent=2))
    else:
        print_summary(aggregated, args.spec, args.version)

    # Save to file if requested
    if args.output:
        output_path = Path(args.output)
        with open(output_path, 'w') as f:
            json.dump(aggregated, f, indent=2)
        print(f"\nSaved aggregated results to: {output_path}")

    # Always save to results/aggregated.json
    agg_file = results_dir / "aggregated.json"
    with open(agg_file, 'w') as f:
        json.dump(aggregated, f, indent=2)
    print(f"Updated: {agg_file}")

    return 0


if __name__ == "__main__":
    exit(main())
