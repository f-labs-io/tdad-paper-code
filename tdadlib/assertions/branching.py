from __future__ import annotations

from tdadlib.harness.trace import ToolTrace

def assert_auth_gate(trace: ToolTrace, action_tool: str, auth_tool: str = "verify_identity") -> None:
    calls = trace.names()
    if action_tool in calls:
        assert auth_tool in calls and calls.index(auth_tool) < calls.index(action_tool), (
            f"Auth gate violated: {auth_tool} must occur before {action_tool}. Calls={calls}"
        )

def assert_no_action_without_confirmation(trace: ToolTrace, action_tool: str, confirmation_seen: bool) -> None:
    if not confirmation_seen:
        assert action_tool not in trace.names(), f"{action_tool} called without user confirmation"
