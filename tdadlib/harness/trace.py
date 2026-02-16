from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

@dataclass
class ToolCall:
    name: str
    args: Dict[str, Any]
    result: Dict[str, Any] | None = None
    error: str | None = None

    def get(self, key: str, default: Any = None) -> Any:
        """Dict-like get method for backward compatibility."""
        if key == "type":
            return "tool_call"
        return getattr(self, key, default)

class ToolTrace:
    def __init__(self) -> None:
        self.calls: List[ToolCall] = []

    def record(self, name: str, args: Dict[str, Any], result: Dict[str, Any] | None = None, error: str | None = None) -> None:
        self.calls.append(ToolCall(name=name, args=args, result=result, error=error))

    def names(self) -> List[str]:
        return [c.name for c in self.calls]

    def __iter__(self):
        """Make ToolTrace iterable over its calls."""
        return iter(self.calls)

    def __len__(self):
        """Return number of tool calls."""
        return len(self.calls)

    def __getitem__(self, key):
        """Support subscripting (trace[0], trace[-1], trace[1:3])."""
        return self.calls[key]

    def get_respond_call(self) -> ToolCall | None:
        """Get the respond tool call if present (extracts structured response)."""
        for call in reversed(self.calls):  # Last respond call wins
            if call.name == "respond":
                return call
        return None

    def get_response(self) -> Dict[str, Any]:
        """Extract the structured response from the respond tool call.

        Returns the args passed to respond(), which contains:
        - node_id, decision, tool_actions, evidence, user_message

        Raises ValueError if no respond call was made.
        """
        call = self.get_respond_call()
        if call is None:
            raise ValueError("No respond tool call found in trace")
        return call.args
