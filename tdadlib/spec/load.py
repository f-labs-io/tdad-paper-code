from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict
import yaml

def load_spec(path: str | Path) -> dict[str, Any]:
    """Load a spec.yaml into a plain dict.

    We intentionally keep this lightweight: the YAML is a PRD-like contract, not executable code.
    """
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Spec must be a mapping at top level: {p}")
    return data
