"""
Cost tracking utilities for test execution.

Writes per-test cost data to temp files that can be aggregated after pytest finishes.
This approach works with pytest-xdist parallel execution.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tdadlib.runtime.runner import SessionCost

# Default directory for cost files (can be overridden via env var)
DEFAULT_COST_DIR = "/tmp/tdad_test_costs"


def get_cost_dir() -> Path:
    """Get the directory for cost files from env var or default."""
    return Path(os.environ.get("TDAD_COST_DIR", DEFAULT_COST_DIR))


def write_test_cost(cost: "SessionCost", test_name: str | None = None) -> None:
    """Write cost data for a single test to a temp file.

    Args:
        cost: SessionCost object with usage data
        test_name: Optional test name for the filename (uses UUID if not provided)
    """
    cost_dir = get_cost_dir()
    cost_dir.mkdir(parents=True, exist_ok=True)

    # Generate unique filename
    unique_id = uuid.uuid4().hex[:8]
    safe_name = test_name.replace("/", "_").replace("::", "_") if test_name else "test"
    filename = f"{safe_name}_{unique_id}.json"

    cost_data = {
        "test_name": test_name or "unknown",
        "total_cost_usd": cost.total_cost_usd,
        "usage_tokens": cost.usage_tokens,
        "num_turns": cost.num_turns,
    }

    cost_file = cost_dir / filename
    cost_file.write_text(json.dumps(cost_data), encoding="utf-8")


def aggregate_test_costs() -> dict:
    """Aggregate all cost files in the cost directory.

    Returns:
        Dict with aggregated cost data:
        {
            "num_tests": int,
            "total_cost_usd": float,
            "input_tokens": int,
            "cache_creation_tokens": int,
            "cache_read_tokens": int,
            "output_tokens": int,
        }
    """
    cost_dir = get_cost_dir()

    result = {
        "num_tests": 0,
        "total_cost_usd": 0.0,
        "input_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "output_tokens": 0,
    }

    if not cost_dir.exists():
        return result

    for cost_file in cost_dir.glob("*.json"):
        try:
            data = json.loads(cost_file.read_text(encoding="utf-8"))
            result["num_tests"] += 1
            # Take max cost (SDK reports cumulative, so last/max is total)
            result["total_cost_usd"] = max(result["total_cost_usd"], data.get("total_cost_usd", 0))
            # Sum tokens
            usage = data.get("usage_tokens", {})
            result["input_tokens"] += usage.get("input", 0)
            result["cache_creation_tokens"] += usage.get("cache_creation", 0)
            result["cache_read_tokens"] += usage.get("cache_read", 0)
            result["output_tokens"] += usage.get("output", 0)
        except (json.JSONDecodeError, KeyError):
            pass  # Skip invalid files

    return result


def clear_cost_files() -> int:
    """Remove all cost files from the cost directory.

    Returns:
        Number of files removed.
    """
    cost_dir = get_cost_dir()
    count = 0

    if cost_dir.exists():
        for cost_file in cost_dir.glob("*.json"):
            cost_file.unlink()
            count += 1

    return count


def print_cost_summary() -> None:
    """Print aggregated cost summary in machine-readable format."""
    costs = aggregate_test_costs()

    if costs["num_tests"] == 0:
        return

    print(f"TEST_COST_SUMMARY: tests={costs['num_tests']} "
          f"input_tokens={costs['input_tokens']} "
          f"cache_creation={costs['cache_creation_tokens']} "
          f"cache_read={costs['cache_read_tokens']} "
          f"output_tokens={costs['output_tokens']} "
          f"total_cost_usd={costs['total_cost_usd']:.6f}",
          flush=True)
