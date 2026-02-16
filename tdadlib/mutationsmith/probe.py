"""
MutationSmith probe runner - Execute activation probes against mutated prompts.

This module runs the agent with a mutated prompt and checks whether the
mutation activated (caused the expected behavioral change).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tdadlib.harness.trace import ToolTrace
from tdadlib.mutationsmith.predicates import evaluate_violation
from tdadlib.runtime.runner import run_agent_conversation, TurnResult
from tdadlib.spec.load import load_spec


@dataclass
class ProbeResult:
    """Result of running an activation probe."""
    activated: bool
    reason: str
    trace: ToolTrace
    assistant_text: str
    assistant_json: dict[str, Any] | None


def _get_fixture_builder(spec_name: str):
    """Get the fixture builder and class for a spec name.

    Returns (build_tools, FixtureClass) tuple.
    """
    spec_name = spec_name.lower()

    if spec_name == "supportops":
        from tdadlib.harness.fixtures.supportops_tools import build_tools, SupportOpsFixture
        return build_tools, SupportOpsFixture

    elif spec_name == "datainsights":
        from tdadlib.harness.fixtures.datainsights_tools import build_tools, DataInsightsFixture
        return build_tools, DataInsightsFixture

    elif spec_name == "incidentrunbook":
        from tdadlib.harness.fixtures.incidentrunbook_tools import build_tools, IncidentRunbookFixture
        return build_tools, IncidentRunbookFixture

    elif spec_name == "expenseguard":
        from tdadlib.harness.fixtures.expenseguard_tools import build_tools, ExpenseGuardFixture
        return build_tools, ExpenseGuardFixture

    else:
        raise ValueError(f"Unknown spec name: {spec_name}")


def _build_fixture_from_case(FixtureClass: type, case_name: str) -> Any:
    """Build a fixture instance from a case name.

    Case names map to specific fixture configurations that trigger
    the probe scenario.

    This is extensible - add more case mappings as needed.
    """
    # Default fixture
    fx = FixtureClass()

    # Case-specific overrides based on spec
    case_lower = case_name.lower()

    # SupportOps cases
    if hasattr(fx, 'verified'):
        if "without_auth" in case_lower:
            fx.verified = True  # Will pass if auth is skipped
        elif "ineligible_plan" in case_lower:
            fx.can_cancel = False

    # DataInsights cases
    if hasattr(fx, 'sql_error'):
        if "sql_error" in case_lower:
            fx.sql_error = "Query execution failed: timeout"
    if hasattr(fx, 'sql_row_count'):
        if "empty_results" in case_lower:
            fx.sql_row_count = 0

    # IncidentRunbook cases
    if hasattr(fx, 'severity'):
        if "high_severity" in case_lower or "sev1" in case_lower:
            fx.severity = "SEV1"
        elif "sev2" in case_lower:
            fx.severity = "SEV2"
        elif "sev3" in case_lower or "minor" in case_lower or "low_severity" in case_lower:
            fx.severity = "SEV3"

    # ExpenseGuard cases
    if hasattr(fx, 'expense_amount'):
        if "over_limit" in case_lower:
            fx.expense_amount = 10000.00  # Over typical threshold

    return fx


def _get_tool_info_from_spec(spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    """Extract tool schemas, descriptions, and allowed tools from spec."""
    schemas = {}
    descriptions = {}
    allowed = []

    for tool in spec.get("tools", []):
        name = tool.get("name")
        if name:
            allowed.append(name)
            if tool.get("input_schema"):
                schemas[name] = tool["input_schema"]
            if tool.get("description"):
                descriptions[name] = tool["description"]

    return schemas, descriptions, allowed


async def _run_probe_async(
    system_prompt: str,
    probe_spec: dict[str, Any],
    spec_name: str,
    spec_path: Path | None = None,
    tool_description_overrides: dict[str, str] | None = None,
    model: str | None = None,
    verbose: bool = True,
) -> ProbeResult:
    """Run an activation probe against a prompt.

    Args:
        system_prompt: The (mutated) system prompt to test
        probe_spec: The activation_probe from mutations.yaml
        spec_name: Name of the spec (supportops, datainsights, etc.)
        spec_path: Optional path to spec.yaml for tool metadata
        tool_description_overrides: Optional mutated tool descriptions
        model: Model override
        verbose: Print progress

    Returns:
        ProbeResult with activation status and details
    """
    # Get fixture builder for this spec
    build_tools, FixtureClass = _get_fixture_builder(spec_name)

    # Build fixture from case name
    case_name = probe_spec.get("case", "default")
    fixture = _build_fixture_from_case(FixtureClass, case_name)

    # Create trace and build tools
    trace = ToolTrace()
    tool_impls, pii_canaries = build_tools(trace, fixture)

    # Get user turns from probe spec
    user_turns = probe_spec.get("user_turns", [])
    if not user_turns:
        return ProbeResult(
            activated=False,
            reason="No user_turns specified in probe",
            trace=trace,
            assistant_text="",
            assistant_json=None,
        )

    # Load spec for tool metadata if available
    tool_schemas = {}
    tool_descriptions = {}
    allowed_tools = list(tool_impls.keys())

    if spec_path and spec_path.exists():
        spec = load_spec(spec_path)
        tool_schemas, tool_descriptions, allowed_tools = _get_tool_info_from_spec(spec)

    # Apply tool description overrides (from mutated artifacts)
    if tool_description_overrides:
        tool_descriptions.update(tool_description_overrides)

    # Run the agent conversation
    if verbose:
        print(f"  Running probe: {case_name} ({len(user_turns)} turns)...", flush=True)

    try:
        results, _cost = await run_agent_conversation(
            system_prompt=system_prompt,
            user_turns=user_turns,
            tool_impls=tool_impls,
            allowed_tools=allowed_tools,
            tool_schemas=tool_schemas,
            tool_descriptions=tool_descriptions,
            trace=trace,
            model=model,
            permission_mode="bypassPermissions",
            server_alias=spec_name.lower(),
        )
    except Exception as e:
        return ProbeResult(
            activated=False,
            reason=f"Agent execution error: {e}",
            trace=trace,
            assistant_text="",
            assistant_json=None,
        )

    if not results:
        return ProbeResult(
            activated=False,
            reason="No results from agent conversation",
            trace=trace,
            assistant_text="",
            assistant_json=None,
        )

    # Get the last turn's output
    last = results[-1]
    assistant_text = last.assistant_text
    assistant_json = last.assistant_json

    # Evaluate the expect_violation predicates
    expect_violation = probe_spec.get("expect_violation", {})
    activated, reason = evaluate_violation(
        expect_violation=expect_violation,
        trace=trace,
        assistant_text=assistant_text,
        assistant_json=assistant_json,
    )

    if verbose:
        status = "ACTIVATED" if activated else "NOT ACTIVATED"
        print(f"  Probe result: {status}", flush=True)
        print(f"  Reason: {reason}", flush=True)

    return ProbeResult(
        activated=activated,
        reason=reason,
        trace=trace,
        assistant_text=assistant_text,
        assistant_json=assistant_json,
    )


def run_activation_probe(
    prompt_path: Path,
    probe_spec: dict[str, Any],
    spec_name: str,
    repo_root: Path,
    spec_version: str = "v1",
    tool_description_overrides: dict[str, str] | None = None,
    model: str | None = None,
    verbose: bool = True,
) -> bool:
    """Run an activation probe to verify a mutation changes behavior.

    Args:
        prompt_path: Path to the mutated system prompt file
        probe_spec: The activation_probe from mutations.yaml
        spec_name: Name of the spec (supportops, datainsights, etc.)
        repo_root: Repository root path
        spec_version: Spec version (v1, v2)
        tool_description_overrides: Optional mutated tool descriptions
        model: Model override
        verbose: Print progress

    Returns:
        True if the mutation activated (behavior changed as expected)
    """
    # Load the mutated prompt
    with open(prompt_path) as f:
        system_prompt = f.read()

    # Find the spec file
    spec_path = repo_root / "specs" / "core" / spec_name / spec_version / "spec.yaml"

    result = asyncio.run(
        _run_probe_async(
            system_prompt=system_prompt,
            probe_spec=probe_spec,
            spec_name=spec_name,
            spec_path=spec_path if spec_path.exists() else None,
            tool_description_overrides=tool_description_overrides,
            model=model,
            verbose=verbose,
        )
    )

    return result.activated


def run_activation_probe_with_prompt(
    system_prompt: str,
    probe_spec: dict[str, Any],
    spec_name: str,
    spec_path: Path | None = None,
    tool_description_overrides: dict[str, str] | None = None,
    model: str | None = None,
    verbose: bool = True,
) -> ProbeResult:
    """Run an activation probe with a prompt string directly.

    This variant is useful when you already have the prompt in memory.

    Args:
        system_prompt: The mutated system prompt content
        probe_spec: The activation_probe from mutations.yaml
        spec_name: Name of the spec
        spec_path: Optional path to spec.yaml
        tool_description_overrides: Optional mutated tool descriptions
        model: Model override
        verbose: Print progress

    Returns:
        Full ProbeResult with details
    """
    return asyncio.run(
        _run_probe_async(
            system_prompt=system_prompt,
            probe_spec=probe_spec,
            spec_name=spec_name,
            spec_path=spec_path,
            tool_description_overrides=tool_description_overrides,
            model=model,
            verbose=verbose,
        )
    )
