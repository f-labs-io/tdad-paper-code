"""
MutationSmith predicates - Evaluate expect_violation conditions.

This module implements a simple predicate language for checking whether
a mutation activated (caused the expected violation).

Supported predicates:
- trace.called: list of tool names that must appear
- trace.not_called: list of tool names that must not appear
- trace.order_violation.must_not_happen: [A, B] meaning "A must NOT occur before B"

- text.contains_any: list of substrings (any must match)
- text.not_contains_any: list of substrings (none may appear)

- json.field_equals: {field: <path>, value: <x>}
- json.field_in: {field: <path>, allowed: [...]}
"""
from __future__ import annotations

from typing import Any

from tdadlib.harness.trace import ToolTrace


def _check_trace_called(trace: ToolTrace, tool_names: list[str]) -> bool:
    """Check that all specified tools were called."""
    called = set(trace.names())
    required = set(tool_names)
    return required.issubset(called)


def _check_trace_not_called(trace: ToolTrace, tool_names: list[str]) -> bool:
    """Check that none of the specified tools were called."""
    called = set(trace.names())
    forbidden = set(tool_names)
    return len(called.intersection(forbidden)) == 0


def _check_trace_order_violation(trace: ToolTrace, must_not_happen: list[str]) -> bool:
    """Check for order violation: A must NOT occur before B.

    must_not_happen = [A, B] means: if A and B both appear, A must NOT come before B.
    Returns True if the violation occurred (A came before B).
    """
    if len(must_not_happen) != 2:
        return False

    a, b = must_not_happen
    names = trace.names()

    # Find first occurrence of each
    a_idx = None
    b_idx = None
    for i, name in enumerate(names):
        if name == a and a_idx is None:
            a_idx = i
        if name == b and b_idx is None:
            b_idx = i

    # Violation occurs if both exist and A came before B
    if a_idx is not None and b_idx is not None:
        return a_idx < b_idx

    return False


def _check_text_contains_any(text: str, substrings: list[str]) -> bool:
    """Check if text contains any of the substrings."""
    return any(sub in text for sub in substrings)


def _check_text_not_contains_any(text: str, substrings: list[str]) -> bool:
    """Check that text does not contain any of the substrings."""
    return not any(sub in text for sub in substrings)


def _get_json_field(data: dict[str, Any], path: str) -> Any:
    """Get a value from a dict using dot-notation path."""
    parts = path.split('.')
    current = data
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _check_json_field_equals(data: dict[str, Any], field: str, value: Any) -> bool:
    """Check if a JSON field equals a specific value."""
    actual = _get_json_field(data, field)
    return actual == value


def _check_json_field_in(data: dict[str, Any], field: str, allowed: list[Any]) -> bool:
    """Check if a JSON field is in a list of allowed values."""
    actual = _get_json_field(data, field)
    return actual in allowed


def evaluate_violation(
    expect_violation: dict[str, Any],
    trace: ToolTrace,
    assistant_text: str,
    assistant_json: dict[str, Any] | None,
) -> tuple[bool, str]:
    """Evaluate whether the expected violation occurred.

    Args:
        expect_violation: The expect_violation spec from mutations.yaml
        trace: Tool call trace from the agent run
        assistant_text: Raw text output from the agent
        assistant_json: Parsed JSON response (from respond tool or endcap)

    Returns:
        Tuple of (violation_occurred: bool, reason: str)
        - violation_occurred=True means the mutation activated (behavior changed)
        - reason explains what triggered or why it didn't trigger
    """
    reasons: list[str] = []
    all_passed = True
    predicates_checked = 0  # Track how many predicates we actually evaluated

    # Supported predicate keys
    SUPPORTED_TRACE = {"called", "not_called", "order_violation"}
    SUPPORTED_TEXT = {"contains_any", "not_contains_any"}
    SUPPORTED_JSON = {"field_equals", "field_in"}

    # Check trace predicates
    trace_spec = expect_violation.get("trace", {})

    # Warn about unsupported trace predicates
    unknown_trace = set(trace_spec.keys()) - SUPPORTED_TRACE
    if unknown_trace:
        reasons.append(f"WARNING: unsupported trace predicates ignored: {list(unknown_trace)}")

    if "called" in trace_spec:
        predicates_checked += 1
        required = trace_spec["called"]
        if _check_trace_called(trace, required):
            reasons.append(f"trace.called: {required}")
        else:
            all_passed = False
            called = trace.names()
            missing = set(required) - set(called)
            reasons.append(f"trace.called FAILED: missing {list(missing)}")

    if "not_called" in trace_spec:
        predicates_checked += 1
        forbidden = trace_spec["not_called"]
        if _check_trace_not_called(trace, forbidden):
            reasons.append(f"trace.not_called: {forbidden}")
        else:
            all_passed = False
            called = set(trace.names())
            found = called.intersection(set(forbidden))
            reasons.append(f"trace.not_called FAILED: found {list(found)}")

    if "order_violation" in trace_spec:
        predicates_checked += 1
        order_spec = trace_spec["order_violation"]
        if "must_not_happen" in order_spec:
            pair = order_spec["must_not_happen"]
            if _check_trace_order_violation(trace, pair):
                reasons.append(f"trace.order_violation: {pair[0]} before {pair[1]}")
            else:
                all_passed = False
                reasons.append(f"trace.order_violation FAILED: {pair[0]} not before {pair[1]}")

    # Check text predicates
    text_spec = expect_violation.get("text", {})

    # Warn about unsupported text predicates
    unknown_text = set(text_spec.keys()) - SUPPORTED_TEXT
    if unknown_text:
        reasons.append(f"WARNING: unsupported text predicates ignored: {list(unknown_text)}")

    if "contains_any" in text_spec:
        predicates_checked += 1
        substrings = text_spec["contains_any"]
        if _check_text_contains_any(assistant_text, substrings):
            found = [s for s in substrings if s in assistant_text]
            reasons.append(f"text.contains_any: found {found}")
        else:
            all_passed = False
            reasons.append(f"text.contains_any FAILED: none of {substrings} found")

    if "not_contains_any" in text_spec:
        predicates_checked += 1
        substrings = text_spec["not_contains_any"]
        if _check_text_not_contains_any(assistant_text, substrings):
            reasons.append("text.not_contains_any: none found")
        else:
            all_passed = False
            found = [s for s in substrings if s in assistant_text]
            reasons.append(f"text.not_contains_any FAILED: found {found}")

    # Check JSON predicates
    json_spec = expect_violation.get("json", {})
    json_data = assistant_json or {}

    # Warn about unsupported json predicates
    unknown_json = set(json_spec.keys()) - SUPPORTED_JSON
    if unknown_json:
        reasons.append(f"WARNING: unsupported json predicates ignored: {list(unknown_json)}")

    if "field_equals" in json_spec:
        predicates_checked += 1
        spec = json_spec["field_equals"]
        field = spec.get("field", "")
        value = spec.get("value")
        if _check_json_field_equals(json_data, field, value):
            reasons.append(f"json.field_equals: {field}={value}")
        else:
            all_passed = False
            actual = _get_json_field(json_data, field)
            reasons.append(f"json.field_equals FAILED: {field}={actual}, expected {value}")

    if "field_in" in json_spec:
        predicates_checked += 1
        spec = json_spec["field_in"]
        field = spec.get("field", "")
        allowed = spec.get("allowed", [])
        if _check_json_field_in(json_data, field, allowed):
            actual = _get_json_field(json_data, field)
            reasons.append(f"json.field_in: {field}={actual} in {allowed}")
        else:
            all_passed = False
            actual = _get_json_field(json_data, field)
            reasons.append(f"json.field_in FAILED: {field}={actual} not in {allowed}")

    # If no supported predicates were checked, fail the activation
    # This prevents mutations with only unsupported predicates from silently "passing"
    if predicates_checked == 0:
        all_passed = False
        reasons.append("ERROR: no supported predicates found - mutation cannot activate")

    reason_text = "; ".join(reasons) if reasons else "no predicates specified"
    return (all_passed, reason_text)
