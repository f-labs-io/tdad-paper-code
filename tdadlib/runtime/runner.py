from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from tdadlib.harness.trace import ToolTrace


class AgentRunnerError(RuntimeError):
    """Raised when the agent runtime cannot be executed (missing deps, auth, etc.)."""
    pass


@dataclass
class SessionCost:
    """Cost and token usage for an agent conversation session."""
    total_cost_usd: float
    usage_tokens: dict[str, int]  # {input, cache_creation, cache_read, output}
    session_id: str | None
    num_turns: int

    @property
    def total_input_tokens(self) -> int:
        """Total input tokens including all cache types."""
        return (
            self.usage_tokens.get("input", 0) +
            self.usage_tokens.get("cache_creation", 0) +
            self.usage_tokens.get("cache_read", 0)
        )

    @property
    def total_tokens(self) -> int:
        """Total tokens (input + output)."""
        return self.total_input_tokens + self.usage_tokens.get("output", 0)


@dataclass
class TurnResult:
    user_input: str
    assistant_text: str
    assistant_json: dict[str, Any]
    trace: ToolTrace
    role: str = "assistant"  # All TurnResults represent assistant responses

    @property
    def assistant_message(self) -> str:
        """Get the user-facing message from the response."""
        return self.assistant_json.get("user_message", self.assistant_text)


def _to_mcp_tool_result(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    """Convert an arbitrary JSON-serializable payload into an MCP tool result object.

    Claude Agent SDK MCP tool handlers must return a dict with:
      - content: list[{type: 'text', text: '...'}]
      - optional is_error: bool
    """
    txt = json.dumps(payload, ensure_ascii=False)
    out: dict[str, Any] = {
        "content": [{"type": "text", "text": txt}],
    }
    if is_error:
        out["is_error"] = True
    return out


def _normalize_allowed_tools(allowed_tools: List[str], *, server_alias: str) -> List[str]:
    """Allow callers to pass either raw tool names or fully-qualified MCP names."""
    normalized: List[str] = []
    for name in allowed_tools:
        if name.startswith("mcp__"):
            normalized.append(name)
        else:
            normalized.append(f"mcp__{server_alias}__{name}")
    return normalized


async def run_agent_conversation(
    *,
    system_prompt: str,
    user_turns: List[str],
    tool_impls: Dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]],
    allowed_tools: List[str],
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    tool_schemas: Dict[str, Any] | None = None,
    tool_descriptions: Dict[str, str] | None = None,
    trace: ToolTrace | None = None,
    permission_mode: str | None = "bypassPermissions",
    server_alias: str = "supportops",
) -> tuple[List[TurnResult], SessionCost]:
    """Run the built agent on a scripted multi-turn conversation (product-agent runtime).

    This function is the *runtime harness* for a "prompt-built" agent:
    - It uses Claude Agent SDK (Python) to run a continuous conversation session.
    - It exposes our deterministic fixture tools as an in-process MCP server.
    - It restricts tool use with an allowlist (only MCP tools allowed by config).
    - Agent MUST call the `respond` tool to return structured output (no JSON parsing).

    Returns:
        Tuple of (results, session_cost):
        - results: List[TurnResult] - conversation turn results
        - session_cost: SessionCost - total cost and token usage for the session

    Notes:
    - `temperature` and `max_tokens` are accepted for future wiring; as of current
      Claude Agent SDK API, these are not top-level options fields. We keep them
      here for compatibility with agent_config.yaml and future CLI pass-through.

    Requirements:
    - `claude-agent-sdk` installed (pip install claude-agent-sdk)
    - Anthropic credentials configured for Claude Code / Agent SDK
    """
    try:
        from claude_agent_sdk import (
            ClaudeSDKClient,
            ClaudeAgentOptions,
            tool as sdk_tool,
            create_sdk_mcp_server,
            AssistantMessage,
            TextBlock,
            ResultMessage,
        )
    except Exception as e:  # pragma: no cover
        raise AgentRunnerError(
            "claude-agent-sdk is required to run the agent. " 
            "Install with: pip install claude-agent-sdk"
        ) from e

    tool_schemas = tool_schemas or {}
    tool_descriptions = tool_descriptions or {}

    # Create trace first since handlers need to reference it
    session_trace = trace or ToolTrace()

    # Wrap our domain tool functions into SDK MCP tool handlers.
    sdk_tools = []
    for tool_name, impl in tool_impls.items():
        desc = tool_descriptions.get(tool_name, f"Tool: {tool_name}")
        schema = tool_schemas.get(tool_name) or {"type": "object"}

        # IMPORTANT: bind loop variables via defaults
        # CRITICAL: We record to session_trace here because MCP tools may run in a separate
        # context where closures in the impl don't share our trace object
        async def _handler(args: dict[str, Any], _impl=impl, _name=tool_name, _trace=session_trace) -> dict[str, Any]:
            try:
                payload = await _impl(args)
                # Record to our trace (in main process) since impl's trace may be different
                _trace.record(_name, args, result=payload)
                return _to_mcp_tool_result(payload, is_error=False)
            except Exception as ex:  # pragma: no cover
                _trace.record(_name, args, error=str(ex))
                # If an exception escapes, we still return an error block so the agent can react.
                return _to_mcp_tool_result({"error": str(ex)}, is_error=True)

        sdk_tools.append(sdk_tool(tool_name, desc, schema)(_handler))

    mcp_server = create_sdk_mcp_server(
        name=server_alias,
        version="1.0.0",
        tools=sdk_tools,
    )

    normalized_allowed = _normalize_allowed_tools(allowed_tools, server_alias=server_alias)

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        mcp_servers={server_alias: mcp_server},
        allowed_tools=normalized_allowed,
        disallowed_tools=[],
        permission_mode=permission_mode,
        model=model,
        # We intentionally do NOT attempt to pass temperature/max_tokens unless the SDK
        # supports it explicitly (avoids brittle CLI flag guessing).
    )

    results: List[TurnResult] = []

    # Track cost/token usage across all turns
    # Cost: take last value (SDK reports cumulative)
    # Tokens: sum across turns
    session_cost_usd = 0.0
    session_usage: dict[str, int] = {"input": 0, "cache_creation": 0, "cache_read": 0, "output": 0}
    session_id: str | None = None

    async with ClaudeSDKClient(options=options) as client:
        for turn in user_turns:
            await client.query(turn)

            # Collect assistant text blocks for *this* query until we hit the ResultMessage.
            text_parts: List[str] = []
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            text_parts.append(block.text)
                if isinstance(msg, ResultMessage):
                    # Capture cost/token data from ResultMessage
                    if hasattr(msg, 'total_cost_usd') and msg.total_cost_usd:
                        session_cost_usd = msg.total_cost_usd  # Take last (cumulative)
                    if hasattr(msg, 'usage') and msg.usage:
                        sdk_usage = msg.usage
                        session_usage["input"] += sdk_usage.get('input_tokens', 0)
                        session_usage["cache_creation"] += sdk_usage.get('cache_creation_input_tokens', 0)
                        session_usage["cache_read"] += sdk_usage.get('cache_read_input_tokens', 0)
                        session_usage["output"] += sdk_usage.get('output_tokens', 0)
                    if hasattr(msg, 'session_id') and msg.session_id:
                        session_id = msg.session_id
                    # End of the response for this query.
                    break

            assistant_text = "".join(text_parts).strip()

            # Extract response from respond tool call - this is the ONLY way to get structured output
            # No fallback to JSON parsing - agent MUST call the respond tool
            assistant_json = session_trace.get_response()

            results.append(
                TurnResult(
                    user_input=turn,
                    assistant_text=assistant_text,
                    assistant_json=assistant_json,
                    trace=session_trace,
                )
            )

    cost = SessionCost(
        total_cost_usd=session_cost_usd,
        usage_tokens=session_usage,
        session_id=session_id,
        num_turns=len(user_turns),
    )

    return results, cost
