from __future__ import annotations

from pathlib import Path
from typing import Any

from tdadlib.spec.load import load_spec

class SpecLintError(Exception):
    pass

def lint_spec(path: str | Path) -> dict[str, Any]:
    """Basic spec linting.

    This is NOT a full verifier; it enforces a few structural invariants so tests
    can rely on the spec being sane.
    """
    spec = load_spec(path)

    required_top = ["spec_id", "title", "version", "tools", "policies", "response_contract", "decision_tree", "tests"]
    missing = [k for k in required_top if k not in spec]
    if missing:
        raise SpecLintError(f"Missing required top-level keys: {missing}")

    # Ensure tool names are unique
    tools = spec.get("tools", [])
    names = [t.get("name") for t in tools]
    if len(names) != len(set(names)):
        raise SpecLintError("Duplicate tool names detected")

    # Ensure decision_enum exists
    rc = spec.get("response_contract", {})
    if "decision_enum" not in rc:
        raise SpecLintError("response_contract.decision_enum is required")

    return spec
