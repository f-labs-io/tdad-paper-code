#!/usr/bin/env python3
"""
Interactive runner for the compiled SupportOps agent.
Chat with the agent to test its behavior.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from tdadlib.runtime.runner import run_agent_conversation
from tdadlib.runtime.prompt_loader import load_prompt_and_config
from tdadlib.harness.fixtures.supportops_tools import build_tools, SupportOpsFixture
from tdadlib.harness.trace import ToolTrace


async def run_interactive():
    """Run an interactive chat session with the agent."""
    repo_root = Path(__file__).parent.parent

    # Load the compiled prompt
    agent_dir = repo_root / "agent_artifacts" / "core" / "supportops"

    prompt, config = load_prompt_and_config(agent_dir)

    print("=" * 60)
    print("SupportOps Agent - Interactive Mode")
    print("=" * 60)
    print(f"Model: {config.get('model', 'default')}")
    print(f"Agent: {agent_dir.name}")
    print()
    print("This agent simulates a customer support system.")
    print("The user's account_id is 'A1' with test fixture data.")
    print()
    print("Example interactions:")
    print("  - 'I want to cancel my order'")
    print("  - 'What's my account balance?'")
    print("  - 'I need to change my address'")
    print()
    print("Type 'quit' or 'exit' to end the session.")
    print("Type 'reset' to start a new conversation.")
    print("=" * 60)
    print()

    # Default fixture (verified user, cancellation allowed)
    fixture = SupportOpsFixture(
        verified=True,
        can_cancel=True,
        address_change_allowed=True,
    )

    while True:
        # Collect user turns for this conversation
        user_turns = []

        print("--- New Conversation ---")
        while True:
            try:
                user_input = input("\nYou: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye!")
                return

            if not user_input:
                continue

            if user_input.lower() in ('quit', 'exit'):
                print("Goodbye!")
                return

            if user_input.lower() == 'reset':
                print("\n--- Resetting conversation ---")
                break

            user_turns.append(user_input)

            # Run the agent
            print("\nAgent is thinking...", end="", flush=True)

            try:
                trace = ToolTrace()
                tool_impls, _pii_canaries = build_tools(trace, fixture)

                results, _cost = await run_agent_conversation(
                    system_prompt=prompt,
                    user_turns=user_turns,
                    tool_impls=tool_impls,
                    allowed_tools=config.get("allowed_tools", []),
                    model=config.get("model"),
                    trace=trace,
                )

                # Get the last result (response to latest user turn)
                result = results[-1]

                print("\r" + " " * 30 + "\r", end="")  # Clear "thinking" message

                # Show the response
                print(f"\nAgent: {result.assistant_json.get('user_message', result.assistant_text)}")
                print(f"\n  [Decision: {result.assistant_json.get('decision', 'N/A')}]")
                print(f"  [Tools called: {result.assistant_json.get('tool_actions', [])}]")

            except Exception as e:
                print(f"\r\nError: {e}")
                print("Try again or type 'reset' to start over.")


def main():
    """Entry point."""
    asyncio.run(run_interactive())


if __name__ == "__main__":
    main()
