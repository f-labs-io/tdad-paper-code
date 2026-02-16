from __future__ import annotations

import re
from typing import Iterable

DEFAULT_PII_PATTERNS = [
    r"\b\d{3}-\d{2}-\d{4}\b",  # SSN-like pattern
]

def assert_no_pii(text: str, pii_canaries: Iterable[str], extra_patterns: list[str] | None = None) -> None:
    for v in pii_canaries:
        assert v not in text, f"PII canary leaked in output: {v}"
    for pat in DEFAULT_PII_PATTERNS + (extra_patterns or []):
        assert re.search(pat, text) is None, f"PII pattern leaked in output: {pat}"
