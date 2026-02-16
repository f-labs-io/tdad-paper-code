from __future__ import annotations

from typing import Any

def assert_required_fields(obj: dict[str, Any], fields: list[str]) -> None:
    missing = [f for f in fields if f not in obj]
    assert not missing, f"Missing required JSON fields: {missing}. Got keys={list(obj.keys())}"

def assert_decision_allowed(obj: dict[str, Any], allowed: list[str]) -> None:
    d = obj.get("decision")
    assert d in allowed, f"decision '{d}' not in allowed set: {allowed}"
