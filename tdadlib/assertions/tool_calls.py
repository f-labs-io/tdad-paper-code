from __future__ import annotations

from tdadlib.harness.trace import ToolTrace

def assert_called(trace: ToolTrace, name: str) -> None:
    assert any(c.name == name for c in trace.calls), f"Expected tool {name} to be called. Got: {trace.names()}"

def assert_not_called(trace: ToolTrace, name: str) -> None:
    assert all(c.name != name for c in trace.calls), f"Expected tool {name} NOT to be called. Got: {trace.names()}"

def assert_call_order(trace: ToolTrace, *args) -> None:
    """Assert expected tools appear in order (as a subsequence).

    Accepts either:
        assert_call_order(trace, ["tool1", "tool2"])  # list
        assert_call_order(trace, "tool1", "tool2")    # varargs
    """
    # Handle both list and varargs styles
    if len(args) == 1 and isinstance(args[0], list):
        expected_in_order = args[0]
    else:
        expected_in_order = list(args)

    got = trace.names()
    idx = 0
    for e in expected_in_order:
        try:
            j = got.index(e, idx)
        except ValueError:
            raise AssertionError(f"Expected tool '{e}' in order {expected_in_order}. Got calls={got}")
        idx = j + 1

def assert_calls_exactly(trace: ToolTrace, expected: list[str]) -> None:
    got = trace.names()
    assert got == expected, f"Expected exact calls {expected}, got {got}"
