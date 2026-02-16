"""
MutationSmith generator - LLM-based semantic prompt mutation.

This module uses Claude Agent SDK to generate semantically mutated prompts
that introduce specific failure modes described by mutation intents.
"""
from __future__ import annotations

import asyncio
import textwrap
import json
from dataclasses import dataclass
from typing import Any


@dataclass
class MutantArtifacts:
    """Container for mutated prompt and tool descriptions."""
    prompt: str
    tool_descriptions: dict[str, str]  # tool_name -> description


async def _generate_mutant_async(
    base_prompt: str,
    mutation_intent: str,
    constraints: list[str],
    tool_descriptions: dict[str, str] | None = None,
    temperature: float = 0,
    model: str | None = None,
    verbose: bool = True,
) -> MutantArtifacts:
    """Generate a mutant prompt using Claude Agent SDK.

    Args:
        base_prompt: The original compiled prompt that passes visible tests
        mutation_intent: Description of the failure mode to introduce
        constraints: List of constraints for the mutation
        tool_descriptions: Current tool descriptions (can also be mutated)
        temperature: LLM temperature (0 for determinism)
        model: Model override (optional)
        verbose: Print progress output

    Returns:
        MutantArtifacts containing mutated prompt and tool descriptions
    """
    try:
        from claude_agent_sdk import (
            ClaudeSDKClient,
            ClaudeAgentOptions,
            ResultMessage,
            AssistantMessage,
            TextBlock,
        )
    except Exception as e:
        raise RuntimeError(
            "claude-agent-sdk is required for mutation generation. "
            "Install with: pip install claude-agent-sdk"
        ) from e

    # Build the MutationSmith system prompt
    constraints_text = "\n".join(f"  - {c}" for c in constraints)

    # Format tool descriptions for display
    tool_desc_text = ""
    if tool_descriptions:
        tool_desc_lines = [f"  {name}: {desc}" for name, desc in tool_descriptions.items()]
        tool_desc_text = "\n".join(tool_desc_lines)

    system_prompt = textwrap.dedent(f"""
        You are MutationSmith, an expert at creating semantic prompt mutations.

        Your task is to take a system prompt AND tool descriptions, then modify them to introduce
        a specific failure mode. The goal is to create a "mutant" that behaves differently in a targeted way.

        ## CRITICAL: Tool Descriptions Are Powerful
        Tool descriptions strongly influence WHEN and WHETHER an agent calls each tool.
        For mutations about skipping tools or changing tool-calling behavior, modifying the
        tool description is often MORE EFFECTIVE than modifying the system prompt.

        Examples:
        - To make agent skip a tool: Change its description from "Always call this first" to
          "Optional helper, only use if explicitly requested"
        - To make agent call a tool when it shouldn't: Add "ALWAYS call this for any request"

        ## Constraints
        {constraints_text}

        ## Important Rules
        1. Return a JSON object with "prompt" and "tool_descriptions" keys.
        2. Make the smallest change that reliably causes the failure mode.
        3. The mutation should be subtle but effective.
        4. Do NOT add new tools or change tool names.
        5. Do NOT reference tests - you do not have access to them.
        6. For mutations about tool-calling behavior, PREFER modifying tool descriptions.

        ## Output Format
        Return ONLY valid JSON:
        ```json
        {{
          "prompt": "...the mutated system prompt...",
          "tool_descriptions": {{
            "tool_name": "mutated description",
            ...
          }}
        }}
        ```
    """).strip()

    user_message_parts = [f"""
## Base Prompt to Mutate:
```
{base_prompt}
```
"""]

    if tool_descriptions:
        user_message_parts.append(f"""
## Current Tool Descriptions:
{tool_desc_text}
""")

    user_message_parts.append(f"""
## Mutation Intent:
{mutation_intent}

Generate the mutated prompt and tool descriptions as JSON.
""")

    user_message = "\n".join(user_message_parts).strip()

    if verbose:
        print("  Generating mutant with MutationSmith...", flush=True)

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        permission_mode="bypassPermissions",
        allowed_tools=[],  # No tools needed, just text generation
        max_turns=1,
    )

    response_text = ""

    async with ClaudeSDKClient(options=options) as client:
        await client.query(user_message)

        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        response_text += block.text
            if isinstance(msg, ResultMessage):
                break

    # Parse JSON response
    try:
        # Try to extract JSON from the response
        response_clean = response_text.strip()
        # Remove markdown code blocks if present
        if response_clean.startswith("```json"):
            response_clean = response_clean[7:]
        if response_clean.startswith("```"):
            response_clean = response_clean[3:]
        if response_clean.endswith("```"):
            response_clean = response_clean[:-3]
        response_clean = response_clean.strip()

        data = json.loads(response_clean)
        mutant_prompt = data.get("prompt", base_prompt)
        mutant_tool_desc = data.get("tool_descriptions", tool_descriptions or {})
    except json.JSONDecodeError:
        # Fallback: treat entire response as the mutated prompt (backward compat)
        if verbose:
            print("  Warning: Response not JSON, treating as prompt-only mutation", flush=True)
        mutant_prompt = response_text.strip()
        mutant_tool_desc = tool_descriptions or {}

    if verbose:
        # Show a brief preview of the changes
        orig_lines = base_prompt.strip().split('\n')
        new_lines = mutant_prompt.strip().split('\n')
        print(f"  Generated mutant: {len(orig_lines)} -> {len(new_lines)} lines", flush=True)
        if tool_descriptions and mutant_tool_desc != tool_descriptions:
            changed = [k for k in mutant_tool_desc if mutant_tool_desc.get(k) != tool_descriptions.get(k)]
            if changed:
                print(f"  Modified tool descriptions: {', '.join(changed)}", flush=True)

    return MutantArtifacts(prompt=mutant_prompt, tool_descriptions=mutant_tool_desc)


def generate_mutant(
    base_prompt: str,
    mutation_intent: str,
    constraints: list[str],
    tool_descriptions: dict[str, str] | None = None,
    temperature: float = 0,
    model: str | None = None,
    verbose: bool = True,
) -> MutantArtifacts:
    """Generate a mutant prompt using MutationSmith (LLM).

    This is the synchronous wrapper around the async implementation.

    Args:
        base_prompt: The original compiled prompt that passes visible tests
        mutation_intent: Description of the failure mode to introduce
        constraints: List of constraints for the mutation
        tool_descriptions: Current tool descriptions (can also be mutated)
        temperature: LLM temperature (0 for determinism)
        model: Model override (optional)
        verbose: Print progress output

    Returns:
        MutantArtifacts containing mutated prompt and tool descriptions

    Example:
        >>> artifacts = generate_mutant(
        ...     base_prompt=original_prompt,
        ...     mutation_intent="Skip required validation before executing action",
        ...     constraints=["Keep JSON structure", "Keep tool names unchanged"],
        ...     tool_descriptions={"validate": "Always call before action"},
        ... )
        >>> mutant_prompt = artifacts.prompt
        >>> mutant_tool_desc = artifacts.tool_descriptions
    """
    return asyncio.run(
        _generate_mutant_async(
            base_prompt=base_prompt,
            mutation_intent=mutation_intent,
            constraints=constraints,
            tool_descriptions=tool_descriptions,
            temperature=temperature,
            model=model,
            verbose=verbose,
        )
    )
