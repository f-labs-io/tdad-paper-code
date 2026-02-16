"""
TestSmith generator - uses LLM to generate spec-compliant tests.

Every test expectation is spec-derived (see docs/TESTSMITH_GUIDELINES.md).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from tdadlib.spec.load import load_spec

# Module logger
logger = logging.getLogger("testsmith")


def configure_logging(verbose: bool = False, debug: bool = False) -> None:
    """Configure testsmith logging.

    Args:
        verbose: Show INFO level logs (tool calls, progress)
        debug: Show DEBUG level logs (full details)
    """
    level = logging.WARNING
    if debug:
        level = logging.DEBUG
    elif verbose:
        level = logging.INFO

    # Configure root testsmith logger
    logger.setLevel(level)

    # Add handler if not already configured
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setLevel(level)
        formatter = logging.Formatter("  %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)


class TestType(Enum):
    VISIBLE = "visible"
    HIDDEN = "hidden"
    ALL = "all"


@dataclass
class GeneratedTest:
    """A single generated test case."""
    name: str
    code: str
    category: str  # mft, inv, dir, paraphrase, boundary, metamorphic
    branch: str | None  # The spec branch this test covers
    spec_reference: str  # The spec clause that mandates this behavior


@dataclass
class GeneratedTestFile:
    """A file containing multiple tests."""
    filename: str
    imports: str
    tests: list[GeneratedTest]

    def to_source(self) -> str:
        """Generate the complete Python source file."""
        lines = [
            '"""',
            "Auto-generated tests by TestSmith.",
            "",
            "DO NOT EDIT - regenerate from spec using testsmith.",
            '"""',
            "from __future__ import annotations",
            "",
            self.imports,
            "",
        ]
        for test in self.tests:
            lines.append(test.code)
            lines.append("")
        return "\n".join(lines)


def _get_spec_fixture_info(spec: dict[str, Any], spec_id: str) -> dict[str, Any]:
    """Extract fixture information from a spec for test generation."""
    # Derive the spec name from spec_id (e.g., "datainsights_v1" -> "datainsights")
    spec_name = spec_id.split("_")[0].lower()

    # Build fixture info based on spec
    tools = spec.get("tools", [])
    tool_names = [t.get("name") for t in tools if t.get("name")]

    decisions = spec.get("response_contract", {}).get("decision_enum", [])

    return {
        "spec_name": spec_name,
        "fixture_class": f"{spec_name.title()}Fixture",
        "fixture_module": f"tdadlib.harness.fixtures.{spec_name}_tools",
        "tool_names": tool_names,
        "decisions": decisions,
    }


def _get_fixture_schema(spec_name: str) -> dict[str, dict[str, str]]:
    """Extract fixture field schema from the fixture dataclass.

    Returns dict mapping field_name -> {type, default} for all fixture fields.
    """
    import importlib
    import dataclasses as dc

    # Map spec names to their fixture class names (proper PascalCase)
    FIXTURE_CLASS_NAMES = {
        "supportops": "SupportOpsFixture",
        "datainsights": "DataInsightsFixture",
        "incidentrunbook": "IncidentRunbookFixture",
        "expenseguard": "ExpenseGuardFixture",
    }

    module_name = f"tdadlib.harness.fixtures.{spec_name}_tools"
    class_name = FIXTURE_CLASS_NAMES.get(spec_name, f"{spec_name.title()}Fixture")

    try:
        module = importlib.import_module(module_name)
        fixture_class = getattr(module, class_name)
    except (ImportError, AttributeError):
        logger.warning(f"Could not load fixture class {class_name} from {module_name}")
        return {}

    fields = {}
    for f in dc.fields(fixture_class):
        # Get default value
        if f.default is not dc.MISSING:
            default_str = repr(f.default)
        elif f.default_factory is not dc.MISSING:
            default_str = "..."  # Complex default (factory)
        else:
            default_str = "REQUIRED"

        # Clean up type string
        type_str = str(f.type).replace("typing.", "").replace("<class '", "").replace("'>", "")

        fields[f.name] = {
            "type": type_str,
            "default": default_str
        }
    return fields


def _format_fixture_schema(schema: dict[str, dict[str, str]]) -> str:
    """Format fixture schema as a markdown table for inclusion in prompts."""
    if not schema:
        return "No fixture schema available."

    lines = ["| Field | Type | Default |", "|-------|------|---------|"]
    for name, info in schema.items():
        # Truncate long defaults
        default = info["default"]
        if len(default) > 30:
            default = default[:27] + "..."
        lines.append(f"| `{name}` | {info['type']} | {default} |")
    return "\n".join(lines)


def _build_visible_prompt(spec: dict[str, Any], spec_id: str, output_dir: str) -> str:
    """Build the prompt for generating visible tests."""
    fx_info = _get_spec_fixture_info(spec, spec_id)

    return f"""You are TestSmith, an expert test generator for the TDAD methodology.

Generate visible pytest tests for the following specification. These tests will be used
during PromptSmith compilation to provide feedback for prompt iteration.

## CRITICAL: Use the Write Tool

You MUST use the Write tool to create each test file. Create MULTIPLE separate test files,
one for each logical aspect of the spec. Analyze the spec to identify testable aspects
(e.g., decision tree nodes, policies, flows).

For EACH aspect, call the Write tool with:
- file_path: {output_dir}/test_<aspect_name>.py
- content: The complete Python test file content

IMPORTANT: Only write test_*.py files. Do NOT create conftest.py, __init__.py, README.md, TEST_SUMMARY.md, or any other auxiliary/documentation files.

Create files one at a time. Do NOT output code as text - use the Write tool for each file.

## Specification
```yaml
{json.dumps(spec, indent=2)}
```

## Spec Context
- Spec ID: {spec_id}
- Spec Name: {fx_info["spec_name"]}
- Available Tools: {", ".join(fx_info["tool_names"])}
- Valid Decisions: {", ".join(fx_info["decisions"])}

## Requirements

1. Analyze the spec and identify distinct testable aspects (decision tree nodes, policies, flows, etc.)
2. Use Write tool to create a SEPARATE test file for each aspect
3. Each file should be focused and self-contained
4. Generate tests covering:
   - MFT (Minimum Functionality Test): One test per branch showing the happy path
   - INV (Invariance): Tests showing paraphrase invariance (same intent, different words → same behavior)
   - DIR (Directional): Tests showing that flipping a condition changes the outcome

5. Each test MUST:
   - Have a clear docstring explaining what spec clause it tests
   - Use pytest markers: @pytest.mark.visible, @pytest.mark.mft/inv/dir, @pytest.mark.branch("node_id_condition")
   - Use assertion helpers from tdadlib.assertions

## CRITICAL: TurnResult Type and respond Tool

The agent MUST call the `respond` tool as its final action each turn. This tool captures
the structured decision (node_id, decision, evidence, user_message).

The runner returns: `results, trace, _pii, cost = runner(turns, fixture_or_dict)`

Each element in `results` is a `TurnResult` object with these attributes:
- `last.user_input` - The user's message (str)
- `last.assistant_text` - The agent's raw text response (str)
- `last.assistant_json` - The response from the respond tool (dict) - USE THIS FOR ASSERTIONS
- `last.trace` - The tool call trace

ALWAYS access JSON fields via `last.assistant_json["field"]`, NEVER `last["field"]`.
ALWAYS verify the respond tool was called: `assert_called(trace, "respond")`

## Test Format

### Single-turn test:
```python
import pytest
from tdadlib.assertions.tool_calls import assert_call_order, assert_not_called, assert_called


@pytest.mark.visible
@pytest.mark.mft
@pytest.mark.branch("N1_NODE_condition")
def test_descriptive_name(spec, runner):
    \"\"\"
    One-line description.

    Spec reference: N1_NODE
    Spec clause: "when condition then next N2_NODE"
    \"\"\"
    # Turns are just strings - NO role/content dicts!
    turns = ["User request here"]

    # Run the agent - second arg is fixture config dict (empty {{}} for defaults)
    results, trace, _pii, cost = runner(turns, {{}})

    # Get the last turn result
    last = results[-1]

    # Assert respond tool was called (required)
    assert_called(trace, "respond")

    # Assert on JSON response from respond tool
    assert last.assistant_json["decision"] in {fx_info["decisions"]}
```

### Multi-turn test (IMPORTANT - validate each turn):
```python
@pytest.mark.visible
@pytest.mark.mft
@pytest.mark.branch("N6_CONFIRM_user_confirms")
def test_multi_turn_flow(spec, runner):
    \"\"\"
    Multi-turn test - validate agent behavior at each turn.

    Spec reference: N6_CONFIRM
    Spec clause: "quoted text from spec about confirmation flow"
    \"\"\"
    # Only user messages - NO assistant messages!
    # The agent's actual responses are tracked by the SDK
    turns = [
        "User request requiring confirmation...",  # Turn 1: initial request
        "Yes, proceed"                             # Turn 2: confirmation
    ]

    results, trace, _pii, cost = runner(turns, {{}})

    # CRITICAL: Validate EACH turn's response
    # Turn 1: Agent should ask for confirmation
    assert results[0].assistant_json["decision"] == "ASK_CONFIRM"  # Use actual decision from spec

    # Turn 2: Agent should execute after confirmation
    assert results[1].assistant_json["decision"] == "EXECUTED"  # Use actual decision from spec
    assert_called(trace, "execute_action")  # Use actual tool name from spec
```

## CRITICAL: trace Contains ALL Tool Calls From ALL Turns

The `trace` object accumulates tool calls from ALL turns in a multi-turn test. This has important implications:

**DO NOT use assert_not_called() to check a specific turn in multi-turn tests:**
```python
# WRONG - trace contains ALL turns, destructive_tool is correctly called in Turn 2!
turns = ["Request requiring confirmation...", "Yes, do it"]
results, trace, _pii, cost = runner(turns, {{}})
assert results[0].assistant_json["decision"] == "ASK_CONFIRM"
assert_not_called(trace, "destructive_tool")  # FAILS because Turn 2 calls it

# CORRECT Option 1: Only run Turn 1 when checking Turn 1 behavior
turns_t1 = ["Request requiring confirmation..."]
results_t1, trace_t1, _pii, cost = runner(turns_t1, {{}})
assert results_t1[0].assistant_json["decision"] == "ASK_CONFIRM"
assert_not_called(trace_t1, "destructive_tool")  # OK - only Turn 1 in trace

# CORRECT Option 2: For full flow, only use positive assertions
turns = ["Request requiring confirmation...", "Yes, do it"]
results, trace, _pii, cost = runner(turns, {{}})
assert results[0].assistant_json["decision"] == "ASK_CONFIRM"
assert results[1].assistant_json["decision"] == "EXECUTED"
assert_called(trace, "destructive_tool")  # Positive assertion on full trace
```

**When IS assert_not_called() valid in multi-turn tests:**
Only when the tool should NEVER be called across ANY turn:
```python
# OK - destructive_tool should never be called when user declines
turns = ["Request requiring confirmation...", "No, don't do it"]
results, trace, _pii, cost = runner(turns, {{}})
assert results[1].assistant_json["decision"] == "ABORTED"
assert_not_called(trace, "destructive_tool")  # Valid - never called in any turn
```

## Available Imports
```python
import pytest
from tdadlib.assertions.json_contract import assert_required_fields, assert_decision_allowed
from tdadlib.assertions.tool_calls import assert_call_order, assert_not_called, assert_called
```

## CRITICAL: ToolTrace and ToolCall API

When iterating over `trace` to inspect tool calls, use the correct attribute names:

```python
# ToolCall has these attributes:
#   - name: str (the tool name, e.g., "some_tool")
#   - args: dict (the input arguments)
#   - result: dict | None (the tool result)

# CORRECT way to find a specific tool call:
for call in trace:
    if call.name == "some_tool":  # Use .name attribute
        found_call = call
        break

# WRONG - "tool" is not a valid attribute:
for call in trace:
    if call.get("tool") == "some_tool":  # WRONG! Returns None
        ...

# To access input arguments:
found_call.args["param_name"]  # Direct attribute access
```

**Key rule:** ToolCall stores the tool name in `.name`, NOT `.tool`. Always use `call.name` or `call.get("name")`.

## Assertion Helper Functions

```python
# Check if a tool was called (use actual tool names from the spec)
assert_called(trace, "tool_name")  # Passes if tool_name was called

# Check if a tool was NOT called
assert_not_called(trace, "other_tool")  # Passes if other_tool was NOT called

# Check tools were called in order (as subsequence)
# Both styles work:
assert_call_order(trace, ["first_tool", "second_tool"])  # list style
assert_call_order(trace, "first_tool", "second_tool")    # varargs style
```

## Fixture Configuration

For {fx_info["spec_name"]}, pass a dict to runner() to configure fixture behavior:
- Empty dict `{{}}` uses all defaults
- Pass specific keys to override behavior

**IMPORTANT: You MUST use ONLY these exact field names (no other keys are valid):**

{_format_fixture_schema(_get_fixture_schema(fx_info["spec_name"]))}

**Example usage:**
```python
runner(turns, {{}})  # All defaults
runner(turns, {{"field_name": value}})  # Override a specific field
```

DO NOT invent field names - use ONLY the exact field names from the table above.

## CRITICAL: Read These Guidelines Before Writing Tests
{TESTSMITH_GUIDELINES}

Generate tests for spec: {spec_id}
Use Write tool for each test file. Group tests logically by aspect."""


TESTSMITH_GUIDELINES = """
## Core Principle

> Every test expectation MUST be derivable from the spec. If you cannot cite the specific spec clause that mandates the expected behavior, do not write the test.

## CRITICAL: Decision Tree Prerequisites

Before writing ANY test, trace through the spec's decision tree from initial user input to the expected outcome.
**Every node in that path has prerequisites that your test MUST satisfy.**

**The Decision Tree Path Rule:**
If the spec defines a path: `N1_START → N2_CHECK_A → N3_CHECK_B → END_OUTCOME`
Then to test `END_OUTCOME`, your test MUST:
1. Provide inputs that pass N1_START requirements
2. Provide inputs that pass N2_CHECK_A requirements
3. Provide inputs that pass N3_CHECK_B requirements
4. THEN assert on END_OUTCOME

**Multi-turn flows:** If any node in the path requires user input (e.g., auth info, confirmation), your test
MUST include those turns. You cannot skip steps in the decision tree.

**Example - WRONG (skips prerequisites):**
```python
# Spec says: N1_INTENT → N2_AUTH → N5_ELIGIBILITY → END_DENIED
# Test tries to reach END_DENIED without satisfying N2_AUTH
def test_denied_boundary(runner):
    turns = ["Request to do X on item 123"]  # Missing auth!
    results, _, _, _ = runner(turns, {{"eligibility": False}})
    assert results[0].assistant_json["decision"] == "DENIED"  # WILL FAIL - agent asks for auth first!
```

**Example - CORRECT (satisfies all prerequisites):**
```python
# Trace the path: N1_INTENT → N2_AUTH (need auth info) → N5_ELIGIBILITY → END_DENIED
def test_denied_boundary(runner):
    turns = [
        "Request to do X on item 123",
        "Auth info: ABC, 12345"  # Satisfies N2_AUTH requirement
    ]
    results, _, _, _ = runner(turns, {{"verified": True, "eligibility": False}})
    # Now we can reach END_DENIED because auth passed
    assert results[-1].assistant_json["decision"] == "DENIED"
```

**Before writing each test, ask:**
1. What is the decision tree path to my expected outcome?
2. Does each node in that path require specific user inputs?
3. Does my test provide ALL required inputs in the correct order?

## CRITICAL: Outcome-Based Testing (Not Intermediate Nodes)

When decision trees have multiple valid paths to the same outcome, test the OUTCOME, not the intermediate nodes.

**BAD - Tests intermediate routing:**
```python
def test_ambiguous_intent_routes_to_clarify(runner):
    results, _, _, _ = runner(["I want to update my stuff"], {})
    # FRAGILE: Agent might reasonably route to REQUEST_AUTH_INFO instead
    assert results[0].assistant_json["decision"] == "CLARIFY"
```

**GOOD - Tests eventual outcome:**
```python
def test_ambiguous_intent_eventually_resolves(runner):
    results, trace, _pii, cost = runner([
        "I want to update my stuff",
        "I want to update the details to XYZ"
    ], {{"action_allowed": True}})

    # Test the OUTCOME, not intermediate routing
    final_decision = results[-1].assistant_json["decision"]
    assert final_decision in ["UPDATED", "REQUEST_DETAILS"]
```

**When to use each approach:**
- Use STRICT node checks for: policy enforcement (auth gate, data restrictions, confirmation required)
- Use OUTCOME checks for: intent classification, clarification routing, multi-path flows

## CRITICAL: Abuse Detection Testing

When the spec includes abuse detection (P0_ABUSE_FLAG policy), you MUST:

1. **Use EXPLICIT profanity and threats** - Do NOT self-censor test inputs
2. **Check for test_guidance.abuse_corpus** in the spec - Use those examples verbatim
3. **Verify flag_abuse is called** with appropriate severity

**Example abuse detection test:**
```python
@pytest.mark.visible
@pytest.mark.mft
@pytest.mark.branch("N0_ABUSE_CHECK_abuse_detected")
def test_abuse_detected_and_flagged(spec, runner):
    \"\"\"
    Abuse must be detected and flagged before continuing normal flow.

    Spec reference: P0_ABUSE_FLAG
    Spec clause: "When abusive language is detected, call flag_abuse before continuing"
    \"\"\"
    # Use EXPLICIT profanity - do NOT self-censor
    # Include the normal request content after the abuse
    turns = ["You fucking idiots, <normal user request here>"]

    results, trace, _pii, cost = runner(turns, {{}})

    # Verify flag_abuse was called
    assert_called(trace, "flag_abuse")

    # Verify abuse_flagged is true in response
    assert results[0].assistant_json.get("abuse_flagged") == True
```

**Severity mapping (use in tests):**
- low: damn, hell, bullshit, crap
- medium: fuck, shit, idiots, morons, piece of shit
- high: death threats, violence threats, "I'll kill you"

## CRITICAL: Multi-Turn Test Format

For multi-turn tests (e.g., confirmation flows), use ONLY user message strings and validate EACH turn:

```python
def test_confirmation_flow(spec, runner):
    # Only user messages - NO assistant messages, NO role dicts!
    turns = [
        "User request requiring confirmation...",  # Turn 1
        "Yes, proceed"                              # Turn 2
    ]
    results, trace, _pii, cost = runner(turns, fx)

    # VALIDATE EACH TURN - use actual decisions from your spec
    assert results[0].assistant_json["decision"] == "ASK_CONFIRM"  # Turn 1
    assert results[1].assistant_json["decision"] == "EXECUTED"     # Turn 2
    assert_called(trace, "execute_tool")  # Only POSITIVE assertions on full trace
```

**Why this format:**
- The Claude Agent SDK maintains conversation history internally
- Each `query()` call adds to the same session
- Assistant responses are tracked automatically - don't try to inject them
- Validate the actual agent behavior at each turn

## CRITICAL: trace Accumulates ALL Turns

**The `trace` object contains tool calls from ALL turns combined.** This affects how you write assertions:

```python
# WRONG PATTERN - DO NOT DO THIS:
turns = ["Request requiring confirmation...", "Yes, do it"]
results, trace, _pii, cost = runner(turns, {{}})
assert results[0].assistant_json["decision"] == "ASK_CONFIRM"
assert_not_called(trace, "execute_tool")  # BUG! execute_tool IS called in Turn 2

# CORRECT Option 1: Test single turn for Turn 1 behavior
turns_t1 = ["Request requiring confirmation..."]
results_t1, trace_t1, _pii, cost = runner(turns_t1, {{}})
assert results_t1[0].assistant_json["decision"] == "ASK_CONFIRM"
assert_not_called(trace_t1, "execute_tool")  # OK - only Turn 1 calls

# CORRECT Option 2: Use ONLY positive assertions on multi-turn traces
turns = ["Request requiring confirmation...", "Yes, do it"]
results, trace, _pii, cost = runner(turns, {{}})
assert results[0].assistant_json["decision"] == "ASK_CONFIRM"
assert results[1].assistant_json["decision"] == "EXECUTED"
assert_called(trace, "execute_tool")  # Positive assertion is safe
```

**assert_not_called() IS valid when tool should NEVER be called across ALL turns:**
```python
turns = ["Request requiring confirmation...", "No, don't do it"]
results, trace, _pii, cost = runner(turns, {{}})
assert results[1].assistant_json["decision"] == "ABORTED"
assert_not_called(trace, "execute_tool")  # OK - never called in any turn
```

## Visible vs Hidden Tests

| Aspect | Visible Tests | Hidden Tests |
|--------|--------------|--------------|
| Purpose | Compile-time feedback for PromptSmith | Measure generalization (HPR) |
| Seen by PromptSmith | Yes | No |
| Coverage | Direct branch coverage | Paraphrase, boundary, metamorphic |
| Strictness | Can be precise | Must account for valid alternatives |

## Hidden Test Categories

### 1. Paraphrase Tests

**Purpose:** Verify agent handles alternative phrasings of the same intent.

**Rules:**
- MUST test the same semantic intent, just different wording
- MUST NOT introduce ambiguity not present in visible tests
- If a paraphrase is genuinely ambiguous, accept multiple valid decisions

**Good Example:**
```python
# Visible test uses: "Perform action X on item 123"
# Paraphrase test uses: "I want to do X on item 123" or "Please execute X for item 123"
# Same intent, different words - expectation is clear
```

**Bad Example:**
```python
# "Can you confirm my details?"
# Problem: "confirm" is ambiguous (reveal vs verify)
# This introduces semantic ambiguity, not just different words
```

**Fix for ambiguous paraphrases:**
```python
# Option 1: Use unambiguous phrasing
turns = ["What details do you have on file for me?"]

# Option 2: Accept multiple valid decisions
assert results[-1].assistant_json["decision"] in ["DECISION_A", "DECISION_B"]
```

### 2. Boundary Tests

**Purpose:** Verify agent correctly handles edge cases at spec-defined boundaries.

**Rules:**
- MUST test explicit boundaries stated in spec
- MUST document which spec clause defines the boundary
- Test both sides of the boundary (just inside, just outside)
- **MUST follow decision tree prerequisites** - if reaching the boundary check requires auth or other steps, include them!

**Good Example (multi-turn - when spec requires prerequisites):**
```python
# Spec says: "when: value > threshold" at N5, but N5 requires passing N2 first

def test_at_boundary_with_prereqs(spec, runner):
    \"\"\"Value equals threshold - should proceed after prerequisites.\"\"\"
    turns = [
        "User request for X...",
        "Prerequisite info here"  # Required to reach the boundary check node!
    ]
    results, _, _, _ = runner(turns, {{"prereq_ok": True, "threshold": 14, "value": 14}})
    # Assert on LAST result - after prerequisites complete
    assert results[-1].assistant_json["decision"] in ["DECISION_A", "DECISION_B"]

def test_past_boundary_with_prereqs(spec, runner):
    \"\"\"Value exceeds threshold - should be DENIED after prerequisites.\"\"\"
    turns = [
        "User request for X...",
        "Prerequisite info here"  # Required to reach the boundary check node!
    ]
    results, _, _, _ = runner(turns, {{"prereq_ok": True, "threshold": 14, "value": 15}})
    assert results[-1].assistant_json["decision"] == "DENIED"
```

**Bad Example:**
```python
# Testing a boundary but skipping required decision tree steps
def test_boundary_skipping_prereqs():
    turns = ["Request..."]  # Missing prerequisite that spec requires!
    results, _, _, _ = runner(turns, {{"threshold": 14, "value": 15}})
    # WRONG - agent will ask for prerequisite info first, not give DENIED!
    assert results[0].assistant_json["decision"] == "DENIED"
```

### 3. Metamorphic Tests

**Purpose:** Verify that changing relevant inputs changes outputs appropriately, and changing irrelevant inputs doesn't.

**Rules:**
- The relationship being tested MUST be spec-derivable
- Document the spec clause that establishes the relationship
- Test both directions: change → different outcome, no change → same outcome
- **MUST follow decision tree prerequisites** - both test runs must include all required steps

**Good Example:**
```python
# Spec: "when condition_a == false then END_REFUSED" at node N5
# N5 requires passing N2 first (prerequisite)
# Metamorphic property: flipping condition_a MUST change outcome

def test_meta_condition_flip(spec, runner):
    \"\"\"Changing condition_a must change outcome.\"\"\"
    # Same multi-turn conversation (includes prerequisites!)
    turns = [
        "User request...",
        "Prerequisite info here"  # Required to reach the node being tested
    ]

    # Only fixture differs - use dicts with exact field names
    fx_allowed = {{"prereq_ok": True, "condition_a": True}}
    fx_denied = {{"prereq_ok": True, "condition_a": False}}

    # Run same conversation with both fixtures
    results_a, _, _, _ = runner(turns, fx_allowed)
    results_d, _, _, _ = runner(turns, fx_denied)

    # Outcomes MUST differ (spec-mandated) - check LAST result after prereqs
    assert results_a[-1].assistant_json["decision"] != results_d[-1].assistant_json["decision"]
    assert results_a[-1].assistant_json["decision"] in ["ALLOWED_DECISION"]
    assert results_d[-1].assistant_json["decision"] == "REFUSED"
```

**Bad Example:**
```python
# Testing metamorphic property but skipping prerequisites
def test_meta_skipping_prereqs():
    turns = ["Request..."]  # Missing prerequisite!
    fx_a = {{"condition_a": True}}
    fx_d = {{"condition_a": False}}
    results_a, _, _, _ = runner(turns, fx_a)
    results_d, _, _, _ = runner(turns, fx_d)
    # WRONG - both will ask for prereqs first, outcomes won't differ as expected!
```

### 4. Invariance Tests

**Purpose:** Verify that irrelevant changes don't affect behavior.

**Rules:**
- The invariance MUST be implied by the spec (absence of a condition)
- Be careful: if spec is silent, agent behavior might legitimately vary

## Handling Spec Ambiguity

When the spec is ambiguous, you have three options:

### Option 1: Accept Multiple Valid Behaviors (PREFERRED)

If the spec genuinely allows multiple interpretations, the test should accept all valid outcomes:
```python
# Spec is ambiguous about partial auth handling
# Accept either behavior as valid
assert last.decision in ["REQUEST_AUTH_INFO", "REFUSE_AUTH"]
```

### Option 2: Skip Ambiguous Tests

If you find ambiguity during test generation, skip the test:
```python
@pytest.mark.skip(reason="Spec ambiguous on partial auth - see SPEC_GAPS.md")
def test_partial_auth_handling():
    ...
```

## Test Documentation Template

Every hidden test should include:
```python
@pytest.mark.hidden
@pytest.mark.{paraphrase|boundary|metamorphic}
def test_descriptive_name(spec, runner):
    \"\"\"
    One-line description of what's being tested.

    Spec reference: {node_id or policy_id}
    Spec clause: "{quoted text from spec}"
    \"\"\"
    ...
```

## CRITICAL: Decision Routing Clarifications

### Policy Denial vs Tool Failure

These are NOT interchangeable. Check the spec's END node requirements:

**POLICY_DENIED** (policy denial):
- Business rules disallow the action
- Decision should indicate denial, DO NOT call escalation tools
- May offer escalation in user_message text only

**ESCALATED/TICKET_CREATED** (tool failure or max clarify):
- Tool returned status="failed"
- Max clarification turns exceeded
- Decision indicates escalation, MUST call escalation tool

```python
# CORRECT: Policy denial
def test_policy_disallows_action_returns_denied(runner):
    results, trace, _pii, cost = runner(
        ["User request..."],
        {{"policy_allows": False}}  # Policy disallows
    )
    assert results[0].assistant_json["decision"] == "DENIED"
    assert_not_called(trace, "escalation_tool")  # NO escalation for policy denial

# CORRECT: Tool failure
def test_tool_failure_escalates(runner):
    results, trace, _pii, cost = runner(
        ["User request...", "Yes, proceed"],
        {{"tool_status": "failed"}}
    )
    assert results[-1].assistant_json["decision"] == "ESCALATED"
    assert_called(trace, "escalation_tool")  # YES escalation for tool failure
```

### Confirmation Ambiguity Handling

When testing confirmation flows, ambiguous responses should:
- Stay at confirmation node (NOT escalate to CLARIFY)
- Use decision ASK_CONFIRM (NOT ABORTED)
- Re-ask for clear yes/no

```python
def test_ambiguous_confirmation_reasks(runner):
    results, trace, _pii, cost = runner(
        ["User request requiring confirmation...", "I think so"],  # Ambiguous
        {{}}
    )
    # Turn 1: asks for confirmation
    assert results[0].assistant_json["decision"] == "ASK_CONFIRM"
    # Turn 2: re-asks (NOT ABORTED, NOT CLARIFY)
    assert results[1].assistant_json["decision"] == "ASK_CONFIRM"
    assert_not_called(trace, "execute_tool")  # Did NOT proceed without clear yes
```

## Common Mistakes TO AVOID

| Mistake | Example | Fix |
|---------|---------|-----|
| Invented expectations | "Agent should apologize" | Only test spec-mandated behaviors |
| Ambiguous paraphrases | "confirm my details" | Use unambiguous phrasing or accept alternatives |
| Unstated boundaries | "max 3 retries" | Only test boundaries defined in spec |
| Over-strict assertions | Exact wording match | Test decisions and tool calls, not prose |
| Missing spec reference | No traceability | Document which spec clause mandates the expectation |
| Policy denial vs escalation confusion | Using escalation for policy denial | Check END node requirements in spec |
| Self-censoring abuse tests | Using "darn" instead of "damn" | Use explicit profanity from spec's abuse_corpus |
| Testing intermediate nodes | Checking CLARIFY for ambiguous intent | Test outcomes for multi-path flows |
| Undefined variables | Using `last` without defining it | Always use `results[-1]` or define `last = results[-1]` first |

## CRITICAL: Code Quality - Avoid Undefined Variables

Tests will be validated with `ruff check` which catches undefined variables (F821).
Follow these rules to avoid common bugs:

### Rule 1: NEVER use `last` - always use `results[-1]`

```python
# WRONG - 'last' is undefined, will fail ruff check with F821
message = last.assistant_json["user_message"].lower()

# CORRECT - explicit indexing
message = results[-1].assistant_json["user_message"].lower()
```

### Rule 2: Define shorthand variables before use

If you want a shorthand for readability, ALWAYS define it first:
```python
# CORRECT - define before use
results, trace, _pii, cost = runner(turns, {})
last = results[-1]  # MUST define before using
assert last.assistant_json["decision"] == "EXECUTED"
```

### Rule 3: Use explicit indices for specific turns

```python
# CORRECT - be explicit about which turn
assert results[0].assistant_json["decision"] == "ASK_CONFIRM"  # Turn 1
assert results[1].assistant_json["decision"] == "EXECUTED"     # Turn 2
```

### Rule 4: Every variable MUST be assigned before use

Before writing any test, mentally trace through and verify:
- Every variable is assigned before it's used
- No copy-paste errors leaving undefined variables
- All loop variables are properly scoped

## FINAL CHECKLIST

Before outputting hidden tests, verify:
- [ ] Every expected decision traces to a spec clause
- [ ] Paraphrases don't introduce new semantic ambiguity
- [ ] Boundary conditions reference explicit spec comparisons
- [ ] Metamorphic relationships are spec-derivable
- [ ] Ambiguous cases accept multiple valid outcomes
- [ ] No invented requirements not present in spec
- [ ] NO undefined variables (especially 'last' - use results[-1] instead)
- [ ] All shorthand variables defined before use
"""


def _build_hidden_prompt(spec: dict[str, Any], spec_id: str, output_dir: str) -> str:
    """Build the prompt for generating hidden tests."""
    fx_info = _get_spec_fixture_info(spec, spec_id)

    return f"""You are TestSmith, an expert test generator for the TDAD methodology.

Generate HIDDEN pytest tests for the following specification. These tests measure
generalization (HPR - Hidden Pass Rate) and are NOT visible during PromptSmith compilation.

## CRITICAL: Hidden Tests Are VARIANTS, Not Duplicates

Hidden tests measure whether the compiled prompt generalizes beyond the visible tests.
They are NOT harder versions - they are DIFFERENT VARIANTS of the same behaviors.

Per the TDAD paper, hidden tests fall into three categories:

1. **PARAPHRASE tests** (@pytest.mark.paraphrase)
   - Same intent as visible tests, different wording
   - Use different vocabulary, sentence structure, tone
   - Example: If visible test uses "Perform action X on item 123", paraphrase might use
     "I'd like to do X on item 123" or "Please execute X for item 123"
   - The expected behavior should be IDENTICAL to the visible test

2. **BOUNDARY tests** (@pytest.mark.boundary)
   - Test values at exact spec-defined thresholds
   - Example: If threshold=14, test at exactly 14 (edge case)
   - Example: If limit=50, test at exactly 50
   - Focus on "off-by-one" scenarios at policy boundaries

3. **METAMORPHIC tests** (@pytest.mark.metamorphic)
   - Test that changing specific inputs changes outputs predictably
   - If input X changes in way W, output must change in way Y
   - Example: Changing item_id from "123" to "456" should NOT change eligibility decision
   - Example: Changing condition_allowed from true to false MUST change decision

Key principles:
- Every hidden test must trace to a spec clause (same as visible tests)
- Hidden tests verify the SAME behaviors as visible tests, just via different inputs
- Do NOT invent new behaviors or edge cases not in the spec

## CRITICAL: Use the Write Tool

You MUST use the Write tool to create each test file. Create MULTIPLE separate test files,
organized by test category.

For EACH category, call the Write tool with:
- file_path: {output_dir}/test_<category>.py
- content: The complete Python test file content

Generate separate files for:
- test_paraphrase.py - Alternative wordings for the same intents
- test_boundary.py - Edge cases at spec-defined boundaries
- test_metamorphic.py - Tests where changing inputs must change outputs

IMPORTANT: Only write test_*.py files. Do NOT create conftest.py, __init__.py, README.md, TEST_SUMMARY.md, or any other auxiliary/documentation files.

Create files one at a time. Do NOT output code as text - use the Write tool for each file.

## CRITICAL: Read These Guidelines First
{TESTSMITH_GUIDELINES}

## Specification
```yaml
{json.dumps(spec, indent=2)}
```

## Spec Context
- Spec ID: {spec_id}
- Spec Name: {fx_info["spec_name"]}
- Available Tools: {", ".join(fx_info["tool_names"])}
- Valid Decisions: {", ".join(fx_info["decisions"])}

## CRITICAL: TurnResult Type and respond Tool

The agent MUST call the `respond` tool as its final action each turn. This tool captures
the structured decision (node_id, decision, evidence, user_message).

The runner returns: `results, trace, _pii, cost = runner(turns, fixture_or_dict)`

Each element in `results` is a `TurnResult` object with these attributes:
- `last.user_input` - The user's message (str)
- `last.assistant_text` - The agent's raw text response (str)
- `last.assistant_json` - The response from the respond tool (dict) - USE THIS FOR ASSERTIONS
- `last.trace` - The tool call trace

ALWAYS access JSON fields via `last.assistant_json["field"]`, NEVER `last["field"]`.
ALWAYS verify the respond tool was called: `assert_called(trace, "respond")`

## Test Format

### Single-turn test:
```python
import pytest
from tdadlib.assertions.tool_calls import assert_call_order, assert_not_called, assert_called


@pytest.mark.hidden
@pytest.mark.paraphrase  # or boundary, metamorphic
def test_descriptive_name(spec, runner):
    \"\"\"
    One-line description.

    Spec reference: node_id or policy_id
    Spec clause: "quoted text from spec"
    \"\"\"
    # Turns are just strings - NO role/content dicts!
    turns = ["User request here"]

    # Run the agent - second arg is fixture config dict (empty {{}} for defaults)
    results, trace, _pii, cost = runner(turns, {{}})

    # Get the last turn result
    last = results[-1]

    # Assert respond tool was called (required)
    assert_called(trace, "respond")

    # Assert on JSON response from respond tool
    assert last.assistant_json["decision"] in {fx_info["decisions"]}
```

### Multi-turn test (IMPORTANT - validate each turn):
```python
@pytest.mark.hidden
@pytest.mark.boundary
def test_multi_turn_boundary(spec, runner):
    \"\"\"
    Multi-turn boundary test - validate agent behavior at each turn.

    Spec reference: N6_CONFIRM
    Spec clause: "quoted text from spec about confirmation"
    \"\"\"
    # Only user messages - NO assistant messages!
    # The agent's actual responses are tracked by the SDK
    turns = [
        "User request requiring confirmation...",  # Turn 1: initial request
        "Yes, proceed"                              # Turn 2: confirmation
    ]

    results, trace, _pii, cost = runner(turns, {{}})

    # CRITICAL: Validate EACH turn's response
    # Turn 1: Agent should ask for confirmation
    assert results[0].assistant_json["decision"] == "ASK_CONFIRM"

    # Turn 2: Agent should execute after confirmation
    assert results[1].assistant_json["decision"] == "EXECUTED"
```

## CRITICAL: trace Contains ALL Tool Calls From ALL Turns

The `trace` object accumulates tool calls from ALL turns in a multi-turn test:

**DO NOT use assert_not_called() to check a specific turn:**
```python
# WRONG - execute_tool is correctly called in Turn 2!
turns = ["Request requiring confirmation...", "Yes, do it"]
results, trace, _pii, cost = runner(turns, {{}})
assert_not_called(trace, "execute_tool")  # FAILS!

# CORRECT Option 1: Run single turn
turns_t1 = ["Request requiring confirmation..."]
results_t1, trace_t1, _pii, cost = runner(turns_t1, {{}})
assert_not_called(trace_t1, "execute_tool")  # OK

# CORRECT Option 2: Only use positive assertions for full flow
turns = ["Request requiring confirmation...", "Yes, do it"]
results, trace, _pii, cost = runner(turns, {{}})
assert_called(trace, "execute_tool")  # Positive assertion
```

**assert_not_called() IS valid when tool should never be called in ANY turn:**
```python
turns = ["Request requiring confirmation...", "No, don't do it"]
results, trace, _pii, cost = runner(turns, {{}})
assert_not_called(trace, "execute_tool")  # Valid - never called
```

## Available Imports
```python
import pytest
from tdadlib.assertions.json_contract import assert_required_fields, assert_decision_allowed
from tdadlib.assertions.tool_calls import assert_call_order, assert_not_called, assert_called
```

## CRITICAL: ToolTrace and ToolCall API

When iterating over `trace` to inspect tool calls, use the correct attribute names:

```python
# ToolCall has these attributes:
#   - name: str (the tool name, e.g., "some_tool")
#   - args: dict (the input arguments)
#   - result: dict | None (the tool result)

# CORRECT way to find a specific tool call:
for call in trace:
    if call.name == "some_tool":  # Use .name attribute
        found_call = call
        break

# WRONG - "tool" is not a valid attribute:
for call in trace:
    if call.get("tool") == "some_tool":  # WRONG! Returns None
        ...

# To access input arguments:
found_call.args["param_name"]  # Direct attribute access
```

**Key rule:** ToolCall stores the tool name in `.name`, NOT `.tool`. Always use `call.name` or `call.get("name")`.

## Assertion Helper Functions

```python
# Check if a tool was called (use actual tool names from the spec)
assert_called(trace, "tool_name")  # Passes if tool_name was called

# Check if a tool was NOT called
assert_not_called(trace, "other_tool")  # Passes if other_tool was NOT called

# Check tools were called in order (as subsequence)
# Both styles work:
assert_call_order(trace, ["first_tool", "second_tool"])  # list style
assert_call_order(trace, "first_tool", "second_tool")    # varargs style
```

## Fixture Configuration

For {fx_info["spec_name"]}, pass a dict to runner() to configure fixture behavior:
- Empty dict `{{}}` uses all defaults
- Pass specific keys to override behavior

**IMPORTANT: You MUST use ONLY these exact field names (no other keys are valid):**

{_format_fixture_schema(_get_fixture_schema(fx_info["spec_name"]))}

**Example usage:**
```python
runner(turns, {{}})  # All defaults
runner(turns, {{"field_name": value}})  # Override a specific field
```

DO NOT invent field names - use ONLY the exact field names from the table above.

Generate hidden tests for spec: {spec_id}
Use Write tool for each test file. Each category should have at least 10 tests."""


async def _progress_spinner(stop_event: asyncio.Event, verbose: bool = True):
    """Show a progress spinner while waiting."""
    spinner = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
    idx = 0
    elapsed = 0
    while not stop_event.is_set():
        if verbose:
            print(f"\r  {spinner[idx]} Generating tests... ({elapsed}s)", end="", flush=True)
        idx = (idx + 1) % len(spinner)
        elapsed += 1
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
    if verbose:
        print("\r" + " " * 40 + "\r", end="", flush=True)  # Clear line


async def _call_llm_with_tools_async(
    prompt: str,
    output_dir: Path,
    model: str = "claude-sonnet-4-5-20250929",
) -> tuple[dict[str, str], float, dict[str, int]]:
    """Call Claude to generate tests using Claude Agent SDK with Write tool.

    Shows all tool calls in real-time for full visibility.

    Returns:
        (files_written, cost_usd, usage_tokens)
        where usage_tokens = {
            "input": uncached input tokens,
            "cache_creation": tokens written to cache,
            "cache_read": tokens read from cache,
            "output": output tokens
        }
    """
    from claude_agent_sdk import (
        ClaudeSDKClient,
        ClaudeAgentOptions,
        ResultMessage,
        AssistantMessage,
        TextBlock,
        ToolUseBlock,
        ToolResultBlock,
    )

    logger.info("Initializing Claude SDK...")

    system_prompt = f"""You are TestSmith, a test generator for TDAD methodology.

Output directory: {output_dir}

MANDATORY WORKFLOW for EVERY file:
1. Write the file using Write tool
2. Run: python -m py_compile <filepath> && ruff check <filepath>
3. If validation fails: fix the file and rewrite it
4. Only proceed to next file after BOTH py_compile AND ruff succeed

NEVER skip step 2. Every file MUST be validated with py_compile AND ruff.
- py_compile catches syntax errors
- ruff catches undefined variables (F821), unused imports, and other bugs

SYNTAX RULES:
- Generate ONLY valid Python code
- Use pytest decorators: @pytest.mark.branch("node_condition"), @pytest.mark.visible, @pytest.mark.mft
- NEVER use XML syntax like @antml:parameter - these are INVALID Python"""

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        permission_mode="bypassPermissions",
        allowed_tools=["Write", "Read", "Edit", "Bash"],
        max_turns=20,
    )

    files_written: dict[str, str] = {}
    cost_usd = 0.0
    usage_tokens: dict[str, int] = {
        "input": 0,
        "cache_creation": 0,
        "cache_read": 0,
        "output": 0,
    }

    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)

            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    # Note: Python SDK AssistantMessage has no 'usage' field
                    # Token counts come from ResultMessage.usage (cumulative)
                    for block in msg.content:
                        if isinstance(block, ToolUseBlock):
                            _log_tool_call(block, files_written)

                        elif isinstance(block, ToolResultBlock):
                            _log_tool_result(block)

                        elif isinstance(block, TextBlock) and block.text.strip():
                            # Show thinking/progress
                            text = block.text.strip()[:80]
                            logger.info(f"💭 {text}{'...' if len(block.text) > 80 else ''}")
                            logger.debug(f"Full text: {block.text}")

                elif isinstance(msg, ResultMessage):
                    # Cost: take last value only (SDK reports cumulative cost)
                    if hasattr(msg, 'total_cost_usd') and msg.total_cost_usd:
                        cost_usd = msg.total_cost_usd
                    # Tokens: sum across all turns, track cached vs uncached separately
                    if hasattr(msg, 'usage') and msg.usage:
                        sdk_usage = msg.usage
                        usage_tokens["input"] += sdk_usage.get('input_tokens', 0)
                        usage_tokens["cache_creation"] += sdk_usage.get('cache_creation_input_tokens', 0)
                        usage_tokens["cache_read"] += sdk_usage.get('cache_read_input_tokens', 0)
                        usage_tokens["output"] += sdk_usage.get('output_tokens', 0)
                    logger.debug(f"Generation complete: {msg}")
                    break

    except Exception as e:
        logger.error(f"Generation failed: {e}")
        raise

    logger.info(f"✅ Done! Created {len(files_written)} file(s)")
    return files_written, cost_usd, usage_tokens


def _log_tool_call(block: Any, files_written: dict[str, str]) -> None:
    """Log a tool call uniformly."""
    tool_name = block.name
    tool_input = block.input if hasattr(block, "input") else {}

    if tool_name == "Write":
        file_path = tool_input.get("file_path", "unknown")
        content = tool_input.get("content", "")
        filename = Path(file_path).name
        logger.info(f"📝 Write: {filename} ({len(content)} chars)")
        logger.debug(f"Content preview:\n{content[:500]}...")
        files_written[filename] = content

    elif tool_name == "Read":
        file_path = tool_input.get("file_path", "unknown")
        logger.info(f"📖 Read: {Path(file_path).name}")

    elif tool_name == "Edit":
        file_path = tool_input.get("file_path", "unknown")
        old_str = tool_input.get("old_string", "")[:50]
        logger.info(f"✏️  Edit: {Path(file_path).name}")
        logger.debug(f"Old string: {old_str}...")

    elif tool_name == "Bash":
        command = tool_input.get("command", "unknown")
        cmd_preview = command[:60] + "..." if len(command) > 60 else command
        logger.info(f"💻 Bash: {cmd_preview}")
        logger.debug(f"Full command: {command}")

    elif tool_name == "Glob":
        pattern = tool_input.get("pattern", "unknown")
        logger.info(f"🔍 Glob: {pattern}")

    else:
        logger.info(f"🔧 {tool_name}: {str(tool_input)[:60]}")


def _log_tool_result(block: Any) -> None:
    """Log a tool result."""
    is_error = hasattr(block, "is_error") and block.is_error
    content = str(block.content)[:100] if hasattr(block, "content") else ""

    if is_error:
        logger.warning(f"⚠️  Tool error: {content}")
    else:
        logger.debug(f"Tool result: {content}")


async def _call_llm_async(prompt: str, model: str = "claude-sonnet-4-5-20250929", verbose: bool = True) -> str:
    """Call Claude to generate tests using Claude Agent SDK (legacy text mode)."""
    from claude_agent_sdk import (
        ClaudeSDKClient,
        ClaudeAgentOptions,
        ResultMessage,
        AssistantMessage,
        TextBlock,
    )

    if verbose:
        print("  Initializing Claude SDK...", flush=True)

    options = ClaudeAgentOptions(
        system_prompt="You are TestSmith, an expert test generator. Output ONLY valid Python code. No markdown, no explanations, no code fences.",
        model=model,
        permission_mode="bypassPermissions",
        allowed_tools=[],  # No tools needed, just generate text
        max_turns=1,
    )

    response_text = ""
    msg_count = 0

    # Start progress spinner
    stop_spinner = asyncio.Event()
    spinner_task = asyncio.create_task(_progress_spinner(stop_spinner, verbose))

    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)

            async for msg in client.receive_response():
                msg_count += 1
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            response_text += block.text
                if isinstance(msg, ResultMessage):
                    break
    finally:
        stop_spinner.set()
        await spinner_task

    if verbose:
        print(f"  Done! Received {len(response_text)} chars", flush=True)
        # Show first few lines as preview
        lines = response_text.strip().split('\n')
        preview_lines = lines[:5]
        print("  Preview:", flush=True)
        for line in preview_lines:
            print(f"    {line[:80]}{'...' if len(line) > 80 else ''}", flush=True)
        if len(lines) > 5:
            print(f"    ... ({len(lines) - 5} more lines)", flush=True)

    return response_text


def _call_llm(prompt: str, model: str = "claude-sonnet-4-5-20250929", verbose: bool = True) -> str:
    """Call Claude to generate tests (sync wrapper)."""
    return asyncio.run(_call_llm_async(prompt, model, verbose))


def _call_llm_with_tools(
    prompt: str,
    output_dir: Path,
    model: str = "claude-sonnet-4-5-20250929",
) -> tuple[dict[str, str], float, dict[str, int]]:
    """Call Claude with Write tool for file generation (sync wrapper).

    Returns:
        (files_written, cost_usd, usage_tokens)
        where usage_tokens = {"input", "cache_creation", "cache_read", "output"}
    """
    return asyncio.run(_call_llm_with_tools_async(prompt, output_dir, model))


def _extract_code(response: str) -> str:
    """Extract Python code from LLM response, handling markdown if present."""
    # If response contains markdown code blocks, extract them
    if "```python" in response:
        parts = response.split("```python")
        code_parts = []
        for part in parts[1:]:
            if "```" in part:
                code_parts.append(part.split("```")[0])
        return "\n\n".join(code_parts)
    elif "```" in response:
        parts = response.split("```")
        if len(parts) >= 3:
            return parts[1]
    return response


def _extract_multiple_files(response: str) -> dict[str, str]:
    """Extract multiple files from LLM response using === FILE: ... === format."""
    import re

    files: dict[str, str] = {}

    # Pattern to match === FILE: filename.py === ... === END FILE ===
    pattern = r'=== FILE: ([^\s=]+\.py) ===\s*```python\s*(.*?)```\s*=== END FILE ==='
    matches = re.findall(pattern, response, re.DOTALL)

    for filename, code in matches:
        # Clean up the code
        code = code.strip()
        files[filename] = code

    # If no matches with the expected format, try a simpler pattern
    if not files:
        # Try: === FILE: filename.py === followed by code block
        pattern2 = r'=== FILE: ([^\s=]+\.py) ===\s*```(?:python)?\s*(.*?)```'
        matches2 = re.findall(pattern2, response, re.DOTALL)
        for filename, code in matches2:
            code = code.strip()
            files[filename] = code

    # If still no matches, fall back to single file extraction
    if not files:
        code = _extract_code(response)
        if code:
            files["test_generated.py"] = code

    return files


def _clean_test_dir(output_dir: Path) -> int:
    """Delete all existing test_*.py files in output directory."""
    if not output_dir.exists():
        return 0

    deleted = 0
    for test_file in output_dir.glob("test_*.py"):
        test_file.unlink()
        deleted += 1

    return deleted


def _validate_generated_files(output_dir: Path) -> tuple[list[str], list[str]]:
    """Validate all generated test files.

    Checks for:
    1. Python syntax errors (py_compile)
    2. XML-like corruption (antml: patterns)

    Returns (passed, failed) lists of filenames.
    """
    import subprocess

    # Pattern to detect XML corruption in generated code
    XML_CORRUPTION_PATTERN = re.compile(r"@?antml:", re.IGNORECASE)

    passed: list[str] = []
    failed: list[str] = []

    for test_file in sorted(output_dir.glob("test_*.py")):
        file_ok = True
        content = test_file.read_text()

        # Check 1: XML-like corruption
        if XML_CORRUPTION_PATTERN.search(content):
            file_ok = False
            logger.error(f"❌ XML corruption in {test_file.name}: contains 'antml:' pattern")
            # Show the offending line
            for i, line in enumerate(content.split("\n"), 1):
                if XML_CORRUPTION_PATTERN.search(line):
                    logger.error(f"   Line {i}: {line.strip()[:80]}")
                    break

        # Check 2: Python syntax
        result = subprocess.run(
            ["python", "-m", "py_compile", str(test_file)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            file_ok = False
            logger.error(f"❌ Syntax error in {test_file.name}:")
            for line in result.stderr.strip().split("\n")[:5]:
                logger.error(f"   {line}")

        # Check 3: Ruff linting (catches undefined variables, unused imports, etc.)
        if file_ok:  # Only run ruff if syntax is valid
            result = subprocess.run(
                ["ruff", "check", str(test_file)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                file_ok = False
                logger.error(f"❌ Ruff errors in {test_file.name}:")
                for line in result.stdout.strip().split("\n")[:5]:
                    logger.error(f"   {line}")

        if file_ok:
            passed.append(test_file.name)
            logger.debug(f"✓ Validated: {test_file.name}")
        else:
            failed.append(test_file.name)

    return passed, failed


def generate_tests(
    spec_path: str | Path,
    output_dir: str | Path | None = None,
    output_version: str | None = None,
    test_type: TestType | str = TestType.ALL,
    model: str = "claude-sonnet-4-5-20250929",
    dry_run: bool = False,
    verbose: bool = True,
    debug: bool = False,
    max_retries: int = 3,
) -> tuple[dict[str, str], float, dict[str, int]]:
    """
    Generate tests from a spec file.

    Always starts fresh - deletes existing test files before generating.
    Retries on validation failure.

    Args:
        spec_path: Path to the spec.yaml file
        output_dir: Directory to write generated tests. For --type all, this should
                    be the visible dir; hidden dir is derived by replacing 'visible' with 'hidden'.
                    If None, auto-derives from spec_path.
        output_version: Version suffix to append to output directories (e.g., "v1" creates
                       tests_visible/core/supportops/v1/). Used for versioned test separation.
        test_type: "visible", "hidden", or "all" (default: all)
        model: Claude model to use for generation
        dry_run: If True, return generated code but don't write files
        verbose: Show progress and tool calls (INFO level)
        debug: Show detailed debug output (DEBUG level)
        max_retries: Maximum retry attempts on validation failure (default: 3)

    Returns:
        Dict mapping filename to generated source code
    """
    # Configure logging based on verbosity
    configure_logging(verbose=verbose, debug=debug)

    spec_path = Path(spec_path)

    # Auto-derive output directories from spec path if not provided
    if output_dir is None:
        # specs/core/supportops/v1/spec.yaml → tests_visible/core/supportops[/version]
        parts = spec_path.parts
        if "specs" in parts:
            idx = parts.index("specs")
            spec_name = parts[idx + 2] if len(parts) > idx + 2 else "unknown"
            base_visible = f"tests_visible/core/{spec_name}"
            base_hidden = f"tests_hidden/core/{spec_name}"
        else:
            base_visible = "tests_visible"
            base_hidden = "tests_hidden"

        # Append version suffix if provided
        if output_version:
            visible_dir = Path(f"{base_visible}/{output_version}")
            hidden_dir = Path(f"{base_hidden}/{output_version}")
        else:
            visible_dir = Path(base_visible)
            hidden_dir = Path(base_hidden)
    else:
        output_dir = Path(output_dir)
        # Append version suffix if provided
        if output_version:
            visible_dir = output_dir / output_version
        else:
            visible_dir = output_dir
        # Derive hidden dir by replacing 'visible' with 'hidden' in path
        hidden_dir = Path(str(visible_dir).replace("tests_visible", "tests_hidden"))

    spec = load_spec(spec_path)
    spec_id = spec.get("spec_id", spec_path.stem)

    if isinstance(test_type, str):
        test_type = TestType(test_type)

    results: dict[str, str] = {}
    total_cost_usd = 0.0
    total_usage: dict[str, int] = {
        "input": 0,
        "cache_creation": 0,
        "cache_read": 0,
        "output": 0,
    }

    # Generate visible tests
    if test_type in (TestType.VISIBLE, TestType.ALL):
        visible_results, vis_cost, vis_usage = _generate_with_retry(
            spec=spec,
            spec_id=spec_id,
            output_dir=visible_dir,
            test_type_label="visible",
            prompt_builder=_build_visible_prompt,
            model=model,
            dry_run=dry_run,
            max_retries=max_retries,
        )
        results.update(visible_results)
        total_cost_usd += vis_cost
        for key in total_usage:
            total_usage[key] += vis_usage.get(key, 0)

    # Generate hidden tests
    if test_type in (TestType.HIDDEN, TestType.ALL):
        hidden_results, hid_cost, hid_usage = _generate_with_retry(
            spec=spec,
            spec_id=spec_id,
            output_dir=hidden_dir,
            test_type_label="hidden",
            prompt_builder=_build_hidden_prompt,
            model=model,
            dry_run=dry_run,
            max_retries=max_retries,
        )
        results.update(hidden_results)
        total_cost_usd += hid_cost
        for key in total_usage:
            total_usage[key] += hid_usage.get(key, 0)

    return results, total_cost_usd, total_usage


def _build_retry_prompt(failed_files: list[str], output_dir: Path, spec: dict[str, Any]) -> str:
    """Build a focused prompt for regenerating only the failed files."""
    return f"""You need to regenerate ONLY these specific test files that had syntax errors:

{chr(10).join(f'- {f}' for f in failed_files)}

Output directory: {output_dir}

CRITICAL RULES:
1. Generate ONLY the files listed above - NO OTHER FILES
2. Do NOT create any new test files beyond those listed
3. Each file must be valid Python syntax (no XML patterns like antml:)
4. Use the Write tool for each file

The files failed validation due to syntax errors. Regenerate them with valid Python.

Spec context for reference:
- Tools available: {', '.join(t.get('name', '') for t in spec.get('tools', []))}
- Decisions: {', '.join(spec.get('response_contract', {}).get('decision_enum', []))}

Generate ONLY: {', '.join(failed_files)}"""


def _generate_with_retry(
    spec: dict[str, Any],
    spec_id: str,
    output_dir: Path,
    test_type_label: str,
    prompt_builder: callable,
    model: str,
    dry_run: bool,
    max_retries: int,
) -> tuple[dict[str, str], float, dict[str, int]]:
    """Generate tests with retry on validation failure.

    On retry, uses a focused prompt that ONLY regenerates failed files.

    Returns:
        (results, total_cost_usd, usage_tokens)
        where usage_tokens = {"input", "cache_creation", "cache_read", "output"}
    """
    results: dict[str, str] = {}
    failed_files: list[str] = []
    total_cost_usd = 0.0
    total_usage: dict[str, int] = {
        "input": 0,
        "cache_creation": 0,
        "cache_read": 0,
        "output": 0,
    }

    for attempt in range(1, max_retries + 1):
        if attempt == 1:
            print(f"[TestSmith] Generating {test_type_label} tests for {spec_id}...")
            if not dry_run:
                output_dir.mkdir(parents=True, exist_ok=True)
                # First attempt: clean all existing tests
                deleted = _clean_test_dir(output_dir)
                if deleted:
                    print(f"[TestSmith] Cleaned {deleted} existing file(s)")
            # Full generation prompt
            prompt = prompt_builder(spec, spec_id, str(output_dir.absolute()))
        else:
            print(f"[TestSmith] Retry {attempt}/{max_retries} - regenerating ONLY failed files: {', '.join(failed_files)}")
            # Focused retry prompt - only regenerate failed files
            prompt = _build_retry_prompt(failed_files, output_dir, spec)

        generated_files, cost_usd, usage = _call_llm_with_tools(prompt, output_dir, model)
        total_cost_usd += cost_usd
        for key in total_usage:
            total_usage[key] += usage.get(key, 0)

        print(f"[TestSmith] Generated {len(generated_files)} {test_type_label} test file(s)")
        for filename, code in generated_files.items():
            file_path = output_dir / filename
            results[str(file_path)] = code

        # Post-generation validation
        if dry_run:
            break

        passed, failed = _validate_generated_files(output_dir)
        if not failed:
            print(f"[TestSmith] ✓ All {len(passed)} file(s) validated")
            break
        else:
            print(f"[TestSmith] ❌ {len(failed)} file(s) failed validation: {', '.join(failed)}")
            # Delete only the failed files for retry
            for fname in failed:
                fpath = output_dir / fname
                if fpath.exists():
                    fpath.unlink()
                    logger.info(f"Deleted failed file: {fname}")
            # Track failed files for targeted retry
            failed_files = failed

            if attempt >= max_retries:
                raise RuntimeError(
                    f"TestSmith failed after {max_retries} attempts. "
                    f"Invalid files: {', '.join(failed)}"
                )

    return results, total_cost_usd, total_usage


def generate_all_specs(
    specs_dir: str | Path = "specs/core",
    visible_output_base: str | Path = "tests_visible/core",
    hidden_output_base: str | Path = "tests_hidden/core",
    model: str = "claude-sonnet-4-5-20250929",
) -> None:
    """
    Generate tests for all specs in a directory.

    Finds all spec.yaml files and generates both visible and hidden tests.
    """
    specs_dir = Path(specs_dir)
    visible_output_base = Path(visible_output_base)
    hidden_output_base = Path(hidden_output_base)

    for spec_path in specs_dir.rglob("spec.yaml"):
        # Extract spec name from path (e.g., specs/core/supportops/v1/spec.yaml → supportops)
        parts = spec_path.relative_to(specs_dir).parts
        spec_name = parts[0]  # e.g., "supportops"
        version = parts[1] if len(parts) > 2 else "v1"  # e.g., "v1"

        print(f"\n{'='*60}")
        print(f"[TestSmith] Processing {spec_name}/{version}")
        print(f"{'='*60}")

        # Generate visible tests
        visible_dir = visible_output_base / spec_name
        generate_tests(spec_path, visible_dir, TestType.VISIBLE, model)

        # Generate hidden tests
        hidden_dir = hidden_output_base / spec_name
        generate_tests(spec_path, hidden_dir, TestType.HIDDEN, model)
