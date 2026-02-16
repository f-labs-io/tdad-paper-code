"""
Conftest for ExpenseGuard hidden tests.

Hidden tests are NOT used during compilation (PromptSmith doesn't see them).
They measure generalization via the HPR (Hidden Pass Rate) metric.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from tdadlib.spec.lint import lint_spec
from tdadlib.runtime.prompt_loader import load_prompt_and_config, load_tool_description_overrides
from tdadlib.runtime.runner import run_agent_conversation, TurnResult, SessionCost
from tdadlib.runtime.cost_tracker import write_test_cost
from tdadlib.harness.trace import ToolTrace
from tdadlib.harness.fixtures.expenseguard_tools import ExpenseGuardFixture, build_tools

REPO_ROOT = Path(__file__).resolve().parents[3]

# Version overrides via environment variables (for v1→v2 evolution testing)
SPEC_VERSION = os.environ.get("TDAD_SPEC_VERSION", "v1")
ARTIFACT_SUFFIX = os.environ.get("TDAD_ARTIFACT_SUFFIX", "")

SPEC_PATH = REPO_ROOT / f"specs/core/expenseguard/{SPEC_VERSION}/spec.yaml"
AGENT_DIR = REPO_ROOT / f"agent_artifacts/core/expenseguard{ARTIFACT_SUFFIX}"


@pytest.fixture(scope="session")
def spec() -> dict[str, Any]:
    return lint_spec(SPEC_PATH)


@pytest.fixture()
def agent_prompt_cfg() -> tuple[str, dict]:
    return load_prompt_and_config(AGENT_DIR)


def _tool_meta_from_spec(spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Extract tool schemas/descriptions for nicer tool use."""
    schemas: dict[str, Any] = {}
    descs: dict[str, str] = {}
    for t in spec.get("tools", []) or []:
        name = t.get("name")
        if not name:
            continue
        if t.get("input_schema"):
            schemas[name] = t["input_schema"]
        if t.get("description"):
            descs[name] = t["description"]
    return schemas, descs


def run_expenseguard_case(
    *,
    system_prompt: str,
    cfg: dict,
    spec: dict[str, Any],
    user_turns: List[str] | List[Dict[str, str]],
    fx: ExpenseGuardFixture,
) -> Tuple[List[TurnResult], ToolTrace, List[str], SessionCost]:
    """Run an ExpenseGuard test case."""
    trace = ToolTrace()
    tools, pii_canaries = build_tools(trace, fx)

    tool_schemas, tool_desc = _tool_meta_from_spec(spec)

    # Merge tool description overrides (allows PromptSmith to optimize tool descriptions)
    tool_desc_overrides = load_tool_description_overrides(AGENT_DIR)
    tool_desc.update(tool_desc_overrides)

    model = cfg.get("model", "claude-sonnet-4-5-20250929")
    temperature = float(cfg.get("temperature", 0.2))
    max_tokens = int(cfg.get("max_tokens", 800))
    allowed_tools = cfg.get("allowed_tools", list(tools.keys()))

    # Normalize user_turns to list of strings
    normalized_turns: List[str] = []
    for turn in user_turns:
        if isinstance(turn, dict):
            normalized_turns.append(turn.get("content", str(turn)))
        else:
            normalized_turns.append(str(turn))

    results, cost = asyncio.run(
        run_agent_conversation(
            system_prompt=system_prompt,
            user_turns=normalized_turns,
            tool_impls=tools,
            allowed_tools=allowed_tools,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            tool_schemas=tool_schemas,
            tool_descriptions=tool_desc,
            trace=trace,
            server_alias="expenseguard",
        )
    )
    return results, trace, pii_canaries, cost


@pytest.fixture()
def runner(agent_prompt_cfg, spec, request):
    """Fixture that provides a runner function for ExpenseGuard tests."""
    system_prompt, cfg = agent_prompt_cfg

    def _run(
        user_turns: List[str] | List[Dict[str, str]],
        fx_or_config: ExpenseGuardFixture | Dict[str, Any],
    ) -> Tuple[List[TurnResult], ToolTrace, List[str], SessionCost]:
        if isinstance(fx_or_config, ExpenseGuardFixture):
            fx = fx_or_config
        elif isinstance(fx_or_config, dict):
            fx = ExpenseGuardFixture(**fx_or_config) if fx_or_config else ExpenseGuardFixture()
        else:
            fx = ExpenseGuardFixture()

        results, trace, pii, cost = run_expenseguard_case(
            system_prompt=system_prompt,
            cfg=cfg,
            spec=spec,
            user_turns=user_turns,
            fx=fx,
        )

        # Write cost to temp file for aggregation after pytest
        test_name = request.node.nodeid if hasattr(request, 'node') else None
        write_test_cost(cost, test_name)

        return results, trace, pii, cost

    return _run


@pytest.fixture()
def expenseguard_runner(runner):
    """Alias for runner fixture."""
    return runner
