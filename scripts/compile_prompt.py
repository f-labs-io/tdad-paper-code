from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Optional

# Default test command is built dynamically from spec path
# This placeholder is only used if --test-cmd is not provided and spec path cannot be parsed
DEFAULT_TEST_CMD = None  # Built from spec path in main()
# Slice/Micro mode commands - built dynamically from available test files
# These are fallbacks; actual commands are built in main() from discovered files
SLICE_TEST_CMD = None  # Built dynamically
MICRO_TEST_CMD = None  # Built dynamically


def discover_test_files(test_dir: Path, limit: int = 2) -> list[Path]:
    """Discover available test files in a directory."""
    test_files = sorted(test_dir.glob("test_*.py"))
    return test_files[:limit] if limit else test_files


def build_slice_test_cmd(test_dir: Path) -> str:
    """Build slice test command from available test files."""
    test_files = discover_test_files(test_dir, limit=2)
    if not test_files:
        # Fallback to running all tests in directory
        return f"pytest {test_dir} -m visible -n auto -v --tb=short"
    files_str = " ".join(str(f) for f in test_files)
    return f"pytest {files_str} -m visible -n auto -v --tb=short"


def build_micro_test_cmd(test_dir: Path) -> str:
    """Build micro test command - just first test from first file."""
    test_files = discover_test_files(test_dir, limit=1)
    if not test_files:
        return f"pytest {test_dir} -m visible -v --tb=short -x"
    # Run first file with -x to stop at first failure
    return f"pytest {test_files[0]} -m visible -v --tb=short -x"


def print_conftest_debug_info(test_dir: Path) -> None:
    """Print debug info about conftest.py for diagnosing volume sync issues."""
    from datetime import datetime

    conftest_path = test_dir / "conftest.py"
    if conftest_path.exists():
        stat = conftest_path.stat()
        size = stat.st_size
        modified = stat.st_mtime
        modified_str = datetime.fromtimestamp(modified).strftime("%Y-%m-%d %H:%M:%S")
        print(f"📋 Conftest: {conftest_path}", flush=True)
        print(f"   Size: {size} bytes, Modified: {modified_str}", flush=True)
    else:
        print(f"⚠️  Conftest NOT FOUND: {conftest_path}", flush=True)


def extract_failing_test_ids(output: str) -> list[str]:
    """Extract pytest node IDs from FAILED lines.

    Matches lines like:
        FAILED tests_visible/core/{spec}/test_foo.py::test_bar - AssertionError...

    Returns list of test node IDs like:
        ['tests_visible/core/{spec}/test_foo.py::test_bar', ...]

    Note: Deduplicates since pytest may output FAILED both during execution
    and in the short test summary section.
    """
    # Matches: FAILED tests_visible/core/{spec}/test_foo.py::test_bar - ...
    pattern = r'FAILED\s+([\w/\._:-]+::[\w_]+)'
    matches = re.findall(pattern, output)
    # Deduplicate while preserving order
    seen = set()
    unique = []
    for m in matches:
        if m not in seen:
            seen.add(m)
            unique.append(m)
    return unique


def run_cmd_streaming(cmd: str, *, cwd: Path, env_override: dict = None) -> tuple[int, str]:
    """Run command with real-time output streaming."""
    env = os.environ.copy()
    if env_override:
        env.update(env_override)

    proc = subprocess.Popen(
        cmd,
        shell=True,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )

    output_lines = []
    for line in proc.stdout:
        print(line, end="", flush=True)
        output_lines.append(line)

    proc.wait()
    return proc.returncode, "".join(output_lines)


def run_cmd(cmd: str, *, cwd: Path, stream: bool = False, env_override: dict = None) -> tuple[int, str]:
    """Run command, optionally with streaming output."""
    if stream:
        return run_cmd_streaming(cmd, cwd=cwd, env_override=env_override)

    env = os.environ.copy()
    if env_override:
        env.update(env_override)

    proc = subprocess.run(
        cmd,
        shell=True,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    return proc.returncode, proc.stdout


def _print_cost_summary(total_cost_usd: float, total_usage: dict[str, int]) -> None:
    """Print a summary of API usage costs.

    Args:
        total_cost_usd: Total cost in USD (cumulative from SDK)
        total_usage: Dict with keys: input, cache_creation, cache_read, output
    """
    total_input = total_usage["input"] + total_usage["cache_creation"] + total_usage["cache_read"]
    total_output = total_usage["output"]
    total_tokens = total_input + total_output

    print(f"\n{'='*60}", flush=True)
    print(f"💰 API USAGE SUMMARY", flush=True)
    print(f"{'='*60}", flush=True)
    print(f"   Input tokens (uncached):     {total_usage['input']:,}", flush=True)
    print(f"   Input tokens (cache write):  {total_usage['cache_creation']:,}", flush=True)
    print(f"   Input tokens (cache read):   {total_usage['cache_read']:,}", flush=True)
    print(f"   Input tokens (total):        {total_input:,}", flush=True)
    print(f"   Output tokens:               {total_output:,}", flush=True)
    print(f"   Total tokens:                {total_tokens:,}", flush=True)
    print(f"   Total cost:                  ${total_cost_usd:.4f} USD", flush=True)
    # Machine-readable line for parsing
    print(f"COST_SUMMARY: input_tokens={total_usage['input']} cache_creation={total_usage['cache_creation']} cache_read={total_usage['cache_read']} output_tokens={total_output} total_cost_usd={total_cost_usd:.6f}", flush=True)
    print(f"{'='*60}", flush=True)


def build_compiler_system_prompt(*, spec_path: str, prompt_path: str, test_cmd: str) -> str:
    # Derive tool_descriptions path from prompt_path (same directory)
    from pathlib import Path
    prompt_dir = str(Path(prompt_path).parent)
    tool_desc_path = f"{prompt_dir}/tool_descriptions.yaml"

    return textwrap.dedent(
        f"""        You are PromptCompiler, an AI coding agent specialized in writing system prompts that pass behavioral tests.

        Goal:
        - Modify the system prompt in this file: {prompt_path}
        - Optionally modify tool descriptions in: {tool_desc_path}
        - Make it satisfy the product spec: {spec_path}
        - Make the visible test suite pass: `{test_cmd}`

        Rules:
        - Do NOT modify any tests, fixtures, or harness code.
        - Do NOT add new tools. Work only by improving the agent prompt and tool descriptions.
        - Do NOT run the full test suite via Bash - the compile loop handles that automatically.
          You may run a single specific test for debugging if needed (e.g., `pytest path/to/test.py::test_name -v`).

        ═══════════════════════════════════════════════════════════════════════════
        CRITICAL - TOOL DESCRIPTIONS ARE YOUR PRIMARY LEVER
        ═══════════════════════════════════════════════════════════════════════════

        **Tool descriptions are the MOST EFFECTIVE way to control agent tool usage.**

        When the agent calls the wrong tool, calls tools in wrong order, or fails to
        call a required tool, you should FIRST edit tool descriptions, NOT the prompt.

        WHY: Tool descriptions appear directly in the tool definition the agent sees.
        They are processed at the point of tool selection, making them more effective
        than instructions buried in a long system prompt.

        Edit tool descriptions in: {tool_desc_path}

        Format (YAML):
        ```yaml
        tool_name: |
          WHEN TO CALL: [specific conditions]
          PREREQUISITES: [what must happen first]
          RETURNS: [what this tool provides]
          WARNING: [constraints or gotchas]
        ```

        EFFECTIVE PATTERNS:
        - "REQUIRED before cancel_order or update_address" (ordering)
        - "Call FIRST to get account context" (sequencing)
        - "NEVER show pii field values to user" (constraints)
        - "Only call AFTER user provides explicit confirmation" (preconditions)

        INEFFECTIVE (don't do this):
        - Duplicating tool usage instructions in both prompt AND descriptions
        - Putting tool ordering rules only in the system prompt
        - Leaving tool descriptions as one-line summaries

        **RULE: When a test fails due to tool selection/ordering, edit tool_descriptions.yaml FIRST.**

        ═══════════════════════════════════════════════════════════════════════════
        CRITICAL - Understanding How Tests Work
        ═══════════════════════════════════════════════════════════════════════════

        The test harness:
        1. Loads your system prompt from {prompt_path}
        2. Loads tool description overrides from {tool_desc_path} (if exists)
        3. Creates an agent instance with deterministic fixture tools
        4. Sends a sequence of user messages (may be multi-turn conversation)
        5. Extracts the agent's final `respond` tool call
        6. Asserts against decision, tool call order, and other behaviors

        Key testing patterns:

        FIXTURES: Tests use fixtures that control tool outputs deterministically.
        The same test input always produces the same tool outputs, allowing tests
        to verify agent behavior under specific conditions.

        MULTI-TURN: Some tests simulate multi-turn conversations. The agent must
        track conversation state and respond appropriately to follow-up messages.

        TOOL ORDERING: Some tests assert that certain tools are called before others.
        Read the spec's policies section to understand required tool call sequences.

        ═══════════════════════════════════════════════════════════════════════════
        CRITICAL - How the Agent Runtime Works
        ═══════════════════════════════════════════════════════════════════════════

        - The built agent runs via Claude Agent SDK with tools
        - Tools are called via SDK tool_use, NOT by outputting JSON
        - The agent MUST call the `respond` tool as its final action each turn
        - The respond tool takes: node_id, decision, tool_actions, evidence, user_message
        - Tests extract the response from the respond tool call, NOT from text output
        - Do NOT instruct the agent to output JSON - use the respond tool instead

        ═══════════════════════════════════════════════════════════════════════════
        STRATEGY
        ═══════════════════════════════════════════════════════════════════════════

        1. FIRST: Read the spec file ({spec_path}) to understand:
           - All tools and their input/output schemas
           - All policies and their enforcement rules
           - The decision tree nodes and branches

        2. Read the test files (use Glob to find them) to understand:
           - What behaviors are being tested
           - What assertions are made
           - How fixtures configure tool outputs

        3. Analyze the failing test output:
           - Which assertions are failing?
           - What was the actual vs expected behavior?
           - What tool call sequence was produced?

        4. DECIDE WHERE TO FIX based on failure type:

           **Tool-related failures** → Edit {tool_desc_path}
           - Wrong tool called
           - Tools called in wrong order
           - Missing required tool call
           - Tool called without prerequisites

           **Response/decision failures** → Edit {prompt_path}
           - Wrong decision enum value
           - Wrong user_message content
           - Missing required response fields
           - Logic/classification errors

        5. Make MINIMAL, TARGETED changes:
           - Fix the specific failure, don't over-engineer
           - For tool issues: add WHEN/PREREQUISITES to tool description
           - For response issues: add explicit rules to system prompt

        When you are done:
        - Reply with a brief summary of what you changed (no code blocks).
        - Do NOT run tests - the compile loop will do that automatically.
        """
    )


async def focused_inner_loop(
    client,  # ClaudeSDKClient
    repo_root: Path,
    prompt_path: Path,
    failing_tests: list[str],
    outer_loop_output: str = "",
    *,
    max_inner_iters: int = 8,
    stream_tests: bool = True,
    verbose: bool = True,
) -> tuple[bool, float, float, dict[str, int]]:
    """
    Attempt to fix specific failing tests without re-running full suite.

    When only a few tests fail, this inner loop runs just those tests after
    each prompt edit, avoiding the overhead of running the full test suite.

    Returns:
        (success, test_time, cost_usd, usage_tokens)
        where usage_tokens = {"input", "cache_creation", "cache_read", "output"}
    """
    from claude_agent_sdk import (
        ResultMessage,
        AssistantMessage,
        TextBlock,
        ToolUseBlock,
        ToolResultBlock,
    )

    candidate_path = prompt_path.with_name("system_prompt_candidate.txt")
    shutil.copy(prompt_path, candidate_path)

    # Build targeted test command with parallel execution
    test_ids = " ".join(failing_tests)
    targeted_cmd = f"pytest {test_ids} -n auto -v --tb=short"
    env_override = {"TDAD_PROMPT_OVERRIDE_PATH": str(candidate_path)}

    original_failing = set(failing_tests)
    total_test_time = 0.0
    inner_cost_usd = 0.0
    inner_usage: dict[str, int] = {"input": 0, "cache_creation": 0, "cache_read": 0, "output": 0}

    # On first entry, don't run tests - they just failed in outer loop.
    # Send straight to agent with the failure info we already have.
    first_iteration = True

    for i in range(1, max_inner_iters + 1):
        print(f"\n{'~'*60}", flush=True)
        print(f"🎯 INNER LOOP {i}/{max_inner_iters} ({len(failing_tests)} targeted tests)", flush=True)
        print(f"{'~'*60}", flush=True)

        # Skip test run on first iteration - tests just failed in outer loop
        if first_iteration:
            first_iteration = False
            print("⏭️  Skipping test run (using outer loop results)", flush=True)
            # Use outer loop's failing tests as "still failing"
            still_failing = set(failing_tests)
            code = 1  # Tests failed
            out = outer_loop_output  # Use outer loop's output for failure details
        else:
            print(f"📋 Running targeted tests: {targeted_cmd[:80]}...", flush=True)
            print("-" * 60, flush=True)
            sys.stdout.flush()

            test_start = time.time()
            code, out = run_cmd(targeted_cmd, cwd=repo_root, stream=stream_tests, env_override=env_override)
            test_elapsed = time.time() - test_start
            total_test_time += test_elapsed

            print("-" * 60, flush=True)
            print(f"⏱️  Targeted test run took {test_elapsed:.1f}s", flush=True)

        if code == 0:
            # All targeted tests pass! Promote candidate
            print("✅ All targeted tests pass! Promoting candidate prompt.", flush=True)
            shutil.copy(candidate_path, prompt_path)
            candidate_path.unlink()  # Clean up
            return True, total_test_time, inner_cost_usd, inner_usage

        # Check for progress (only if we have test output - not on first iteration)
        if out:
            still_failing = set(extract_failing_test_ids(out))
            newly_passing = original_failing - still_failing

            if newly_passing:
                print(f"✅ Progress! {len(newly_passing)} test(s) now passing:", flush=True)
                for t in sorted(newly_passing):
                    print(f"   ✓ {t}", flush=True)
                # Update targeted tests to only the still-failing ones
                failing_tests = list(still_failing & original_failing)
                test_ids = " ".join(failing_tests)
                targeted_cmd = f"pytest {test_ids} -n auto -v --tb=short"

        # If this is the last iteration, don't bother sending to agent
        if i == max_inner_iters:
            break

        # Extract failures for agent
        def extract_pytest_failures(output: str) -> str:
            """Extract FAILURES section and summary from pytest output."""
            lines = output.split('\n')
            result_lines = []
            in_failures = False

            for line in lines:
                if '= FAILURES =' in line or '=FAILURES=' in line.replace(' ', ''):
                    in_failures = True
                    result_lines.append(line)
                elif in_failures:
                    if line.startswith('=') and 'FAILURES' not in line:
                        in_failures = False
                    else:
                        result_lines.append(line)

                if 'short test summary' in line.lower() or 'passed' in line or 'failed' in line or 'error' in line:
                    if not in_failures:
                        result_lines.append(line)

            extracted = '\n'.join(result_lines)
            return extracted if extracted.strip() else output

        # Build message for agent
        if out:
            failure_output = extract_pytest_failures(out)
            test_details = f"""--- BEGIN PYTEST OUTPUT ---
            {failure_output}
            --- END PYTEST OUTPUT ---"""
        else:
            # First iteration - no new test output, just list the failing test IDs
            test_list = "\n".join(f"  • {t}" for t in failing_tests)
            test_details = f"""Failing tests (from outer loop):
{test_list}

NOTE: These tests just failed in the outer loop. Read the test files to understand
what behaviors they check, then edit the prompt to fix them."""

        user_msg = textwrap.dedent(
            f"""            Inner loop iteration {i}/{max_inner_iters} - Targeting {len(failing_tests)} specific failing test(s).

            The following tests are still failing:

            {test_details}

            IMPORTANT: Edit the CANDIDATE prompt file to fix these failures:
            File: {candidate_path.relative_to(repo_root)}

            The compile loop will re-run ONLY these {len(failing_tests)} targeted tests.
            """
        )

        print("\n🤖 Sending to PromptSmith agent (inner loop)...", flush=True)
        await client.query(user_msg)

        # Stream response
        print("📝 Agent working on targeted fixes...", flush=True)
        print("-" * 60, flush=True)
        tool_start_time = None
        async for msg in client.receive_response():
            if verbose:
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            print(block.text, end="", flush=True)
                        elif isinstance(block, ToolUseBlock):
                            tool_start_time = time.time()
                            print(f"\n\n🔧 TOOL: {block.name} [started]", flush=True)
                            if hasattr(block, 'input') and block.input:
                                import json
                                # Detect tool_descriptions.yaml modifications
                                file_path = block.input.get('file_path', '')
                                if 'tool_descriptions' in file_path and block.name in ('Edit', 'Write'):
                                    print(f"\n   {'='*50}", flush=True)
                                    print(f"   📋 TOOL DESCRIPTION OVERRIDE DETECTED!", flush=True)
                                    print(f"   PromptSmith is modifying: {file_path}", flush=True)
                                    print(f"   {'='*50}", flush=True)
                                args_str = json.dumps(block.input, indent=2, ensure_ascii=False)
                                max_len = 6000 if block.name in ('Edit', 'Write') else 2000
                                if len(args_str) > max_len:
                                    args_str = args_str[:max_len] + "\n... [truncated]"
                                print(f"   Input: {args_str}", flush=True)
                elif isinstance(msg, ToolResultBlock):
                    elapsed = ""
                    if tool_start_time:
                        elapsed = f" [{time.time() - tool_start_time:.1f}s]"
                        tool_start_time = None
                    result_text = ""
                    if hasattr(msg, 'content'):
                        if isinstance(msg.content, str):
                            result_text = msg.content
                        elif isinstance(msg.content, list):
                            for item in msg.content:
                                if isinstance(item, dict) and item.get('type') == 'text':
                                    result_text += item.get('text', '')
                                elif hasattr(item, 'text'):
                                    result_text += item.text
                    if result_text:
                        if len(result_text) > 1000:
                            result_text = result_text[:1000] + "\n... [truncated]"
                        print(f"   ✓ Result{elapsed}: {result_text}", flush=True)
                    else:
                        print(f"   ✓ Tool completed{elapsed}", flush=True)
            if isinstance(msg, ResultMessage):
                # Cost: take last value only (SDK reports cumulative cost)
                if hasattr(msg, 'total_cost_usd') and msg.total_cost_usd:
                    inner_cost_usd = msg.total_cost_usd
                # Tokens: sum across all turns (each turn processes input, generates output)
                if hasattr(msg, 'usage') and msg.usage:
                    sdk_usage = msg.usage
                    inner_usage["input"] += sdk_usage.get('input_tokens', 0)
                    inner_usage["cache_creation"] += sdk_usage.get('cache_creation_input_tokens', 0)
                    inner_usage["cache_read"] += sdk_usage.get('cache_read_input_tokens', 0)
                    inner_usage["output"] += sdk_usage.get('output_tokens', 0)
                break
        print("\n" + "-" * 60, flush=True)

    # No progress after max attempts - clean up and signal failure
    print(f"❌ Inner loop exhausted after {max_inner_iters} attempts", flush=True)
    candidate_path.unlink()  # Clean up
    return False, total_test_time, inner_cost_usd, inner_usage


async def compile_loop(
    *,
    repo_root: Path,
    spec_path: Path,
    prompt_path: Path,
    test_cmd: str,
    model: Optional[str],
    max_iters: int,
    verbose: bool = True,
    stream_tests: bool = True,
    inner_loop_threshold: int = 10,
    max_inner_iters: int = 8,
    initial_results: Optional[str] = None,
) -> int:
    print("Loading Claude Agent SDK...", flush=True)
    try:
        from claude_agent_sdk import (
            ClaudeSDKClient,
            ClaudeAgentOptions,
            ResultMessage,
            AssistantMessage,
            TextBlock,
            ToolUseBlock,
            ToolResultBlock,
        )
        print("SDK loaded successfully", flush=True)
    except Exception as e:
        raise RuntimeError(
            "claude-agent-sdk is required for compilation. Install with: pip install claude-agent-sdk"
        ) from e

    if not prompt_path.exists():
        raise FileNotFoundError(f"prompt file not found: {prompt_path}")

    system_prompt = build_compiler_system_prompt(
        spec_path=str(spec_path.relative_to(repo_root)),
        prompt_path=str(prompt_path.relative_to(repo_root)),
        test_cmd=test_cmd,
    )

    # Use Claude Sonnet 4.5 as default
    effective_model = model or "claude-sonnet-4-5-20250929"
    print(f"Configuring agent options (model: {effective_model})...", flush=True)

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=effective_model,
        cwd=str(repo_root),
        add_dirs=[str(repo_root)],
        permission_mode="bypassPermissions",
        allowed_tools=[
            "Bash",
            "Read",
            "Write",
            "Edit",
            "Glob",
            "Grep",
            "TodoWrite",
        ],
        max_turns=50,
        # Note: by default, SDK does not load filesystem settings. This helps CI determinism.
        # If you want CLAUDE.md loading, set: setting_sources=["project"]
    )
    print("Creating SDK client...", flush=True)

    async with ClaudeSDKClient(options=options) as client:
        print("SDK client ready, starting compile loop", flush=True)
        total_test_time = 0.0
        total_cost_usd = 0.0
        total_usage: dict[str, int] = {"input": 0, "cache_creation": 0, "cache_read": 0, "output": 0}

        # Load initial results from TestSmith if provided
        initial_results_content = None
        if initial_results:
            initial_results_path = Path(initial_results)
            if initial_results_path.exists():
                initial_results_content = initial_results_path.read_text()
                print(f"📥 Loaded initial test results from TestSmith ({len(initial_results_content)} chars)", flush=True)
            else:
                print(f"⚠️  Initial results file not found: {initial_results}", flush=True)

        for i in range(1, max_iters + 1):
            # Print banner BEFORE running tests so user sees progress immediately
            print(f"\n{'='*60}", flush=True)
            print(f"🔄 ITERATION {i}/{max_iters}", flush=True)
            print(f"{'='*60}", flush=True)

            # On iteration 1, use initial results from TestSmith if available
            if i == 1 and initial_results_content:
                print(f"⏭️  Using initial test results from TestSmith (skipping first test run)", flush=True)
                code = 1  # Tests failed (expected with seed prompt)
                out = initial_results_content
                test_elapsed = 0.0
            else:
                print(f"📋 Running tests: {test_cmd}", flush=True)
                print("-" * 60, flush=True)
                sys.stdout.flush()

                # Time the test run
                test_start = time.time()
                code, out = run_cmd(test_cmd, cwd=repo_root, stream=stream_tests)
                test_elapsed = time.time() - test_start
                total_test_time += test_elapsed

                print("-" * 60, flush=True)

            print(f"⏱️  Test run took {test_elapsed:.1f}s (total: {total_test_time:.1f}s)", flush=True)

            # Extract summary line from pytest output
            lines = out.strip().split('\n')
            summary = [line for line in lines if 'passed' in line or 'failed' in line or 'error' in line]
            if summary:
                print(f"📊 Test result: {summary[-1]}", flush=True)
            else:
                print(f"📊 Test exit code: {code}", flush=True)

            if code == 0:
                print(f"\n✅ Tests passed on iteration {i}", flush=True)
                print(f"⏱️  Total test time: {total_test_time:.1f}s", flush=True)
                if total_cost_usd > 0:
                    _print_cost_summary(total_cost_usd, total_usage)
                return 0

            # Check if we should enter the focused inner loop
            failing_tests = extract_failing_test_ids(out)
            if 0 < len(failing_tests) < inner_loop_threshold:
                print(f"\n🎯 Only {len(failing_tests)} tests failing - entering focused inner loop", flush=True)
                print(f"   Targeted tests:", flush=True)
                for t in failing_tests:
                    print(f"   • {t}", flush=True)

                success, inner_time, inner_cost, inner_usage = await focused_inner_loop(
                    client, repo_root, prompt_path, failing_tests, out,
                    max_inner_iters=max_inner_iters,
                    stream_tests=stream_tests,
                    verbose=verbose,
                )
                total_test_time += inner_time
                total_cost_usd += inner_cost
                for k in total_usage:
                    total_usage[k] += inner_usage[k]

                if not success:
                    print("❌ Inner loop made no progress on targeted tests - aborting", flush=True)
                    print(f"⏱️  Total test time: {total_test_time:.1f}s", flush=True)
                    if total_cost_usd > 0:
                        _print_cost_summary(total_cost_usd, total_usage)
                    return 2

                # Inner loop succeeded - continue to next outer iteration for full suite verification
                print("\n🔄 Inner loop succeeded - verifying with full test suite...", flush=True)
                continue

            # Extract the important parts of pytest output for the agent:
            # 1. FAILURES section (actual assertion errors) - CRITICAL
            # 2. Short test summary (list of failed tests)
            # 3. Final summary line
            # Don't truncate - the agent MUST see all failure details

            def extract_pytest_failures(output: str) -> str:
                """Extract FAILURES section and summary from pytest output."""
                lines = output.split('\n')
                result_lines = []
                in_failures = False
                in_summary = False

                for line in lines:
                    # Capture FAILURES section
                    if '= FAILURES =' in line or '=FAILURES=' in line.replace(' ', ''):
                        in_failures = True
                        result_lines.append(line)
                    elif in_failures:
                        if line.startswith('=') and 'FAILURES' not in line:
                            in_failures = False
                            # Fall through to check for summary
                        else:
                            result_lines.append(line)

                    # Capture short test summary and final results
                    if 'short test summary' in line.lower() or 'passed' in line or 'failed' in line or 'error' in line:
                        if not in_failures:
                            result_lines.append(line)
                            in_summary = True
                    elif in_summary and line.strip() and not line.startswith('='):
                        result_lines.append(line)

                extracted = '\n'.join(result_lines)
                # If extraction failed, return full output
                return extracted if extracted.strip() else output

            # Send full failure details to the agent - no truncation!
            failure_output = extract_pytest_failures(out)

            user_msg = textwrap.dedent(
                f"""                Iteration {i}/{max_iters}.

                Visible tests are failing. Here is the test output with FULL failure details:

                --- BEGIN PYTEST OUTPUT ---
                {failure_output}
                --- END PYTEST OUTPUT ---

                IMPORTANT: Read the assertion errors carefully to understand WHY tests fail.
                Please edit ONLY the prompt file to fix these failures.
                """
            )

            print("\n🤖 Sending to PromptSmith agent...", flush=True)
            await client.query(user_msg)

            # Stream response with verbose output
            print("📝 Agent working...", flush=True)
            print("-" * 60, flush=True)
            tool_start_time = None
            async for msg in client.receive_response():
                if verbose:
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                print(block.text, end="", flush=True)
                            elif isinstance(block, ToolUseBlock):
                                # Show tool name and arguments with timestamp
                                tool_start_time = time.time()
                                print(f"\n\n🔧 TOOL: {block.name} [started]", flush=True)
                                if hasattr(block, 'input') and block.input:
                                    import json
                                    # Detect tool_descriptions.yaml modifications
                                    file_path = block.input.get('file_path', '')
                                    if 'tool_descriptions' in file_path and block.name in ('Edit', 'Write'):
                                        print(f"\n   {'='*50}", flush=True)
                                        print(f"   📋 TOOL DESCRIPTION OVERRIDE DETECTED!", flush=True)
                                        print(f"   PromptSmith is modifying: {file_path}", flush=True)
                                        print(f"   {'='*50}", flush=True)
                                    args_str = json.dumps(block.input, indent=2, ensure_ascii=False)
                                    # Truncate very long arguments, but show more for Edit/Write
                                    max_len = 6000 if block.name in ('Edit', 'Write') else 2000
                                    if len(args_str) > max_len:
                                        args_str = args_str[:max_len] + "\n... [truncated]"
                                    print(f"   Input: {args_str}", flush=True)
                    elif isinstance(msg, ToolResultBlock):
                        # Show tool result (truncated if very long) with elapsed time
                        elapsed = ""
                        if tool_start_time:
                            elapsed = f" [{time.time() - tool_start_time:.1f}s]"
                            tool_start_time = None
                        result_text = ""
                        if hasattr(msg, 'content'):
                            if isinstance(msg.content, str):
                                result_text = msg.content
                            elif isinstance(msg.content, list):
                                # MCP-style content blocks
                                for item in msg.content:
                                    if isinstance(item, dict) and item.get('type') == 'text':
                                        result_text += item.get('text', '')
                                    elif hasattr(item, 'text'):
                                        result_text += item.text
                        if result_text:
                            if len(result_text) > 1000:
                                result_text = result_text[:1000] + "\n... [truncated]"
                            print(f"   ✓ Result{elapsed}: {result_text}", flush=True)
                        else:
                            print(f"   ✓ Tool completed{elapsed}", flush=True)
                if isinstance(msg, ResultMessage):
                    # Cost: take last value only (SDK reports cumulative cost)
                    if hasattr(msg, 'total_cost_usd') and msg.total_cost_usd:
                        total_cost_usd = msg.total_cost_usd
                    # Tokens: sum across all turns (each turn processes input, generates output)
                    if hasattr(msg, 'usage') and msg.usage:
                        sdk_usage = msg.usage
                        total_usage["input"] += sdk_usage.get('input_tokens', 0)
                        total_usage["cache_creation"] += sdk_usage.get('cache_creation_input_tokens', 0)
                        total_usage["cache_read"] += sdk_usage.get('cache_read_input_tokens', 0)
                        total_usage["output"] += sdk_usage.get('output_tokens', 0)
                    break
            print("\n" + "-" * 60, flush=True)

        print(f"\n❌ Reached max iterations ({max_iters}) without passing tests.", flush=True)
        print(f"⏱️  Total test time: {total_test_time:.1f}s", flush=True)
        _print_cost_summary(total_cost_usd, total_usage)
        return 2


def main() -> int:
    ap = argparse.ArgumentParser(description="TDAD prompt compiler (Claude Code via Claude Agent SDK)")
    ap.add_argument("--repo-root", default=".", help="Repository root (default: .)")
    ap.add_argument("--spec", default=None, help="Path to spec.yaml (required unless using docker-compose)")
    ap.add_argument("--prompt", default=None, help="Path to the agent prompt file (derived from spec if not provided)")
    ap.add_argument("--seed", default=None, help="Path to seed prompt (if provided, copies to --prompt before starting)")
    ap.add_argument("--from-seed", action="store_true", help="Start from seed prompt (derived from spec path)")
    ap.add_argument("--test-cmd", default=None, help="Test command (default: parallel execution)")
    ap.add_argument("--slice", action="store_true", help="Run only a slice of tests for faster iteration (useful for harness development)")
    ap.add_argument("--micro", action="store_true", help="Run just 2 tests for debugging tool call issues")
    ap.add_argument("--model", default=None, help="Claude model override (default: claude-sonnet-4-5-20250929)")
    ap.add_argument("--max-iters", type=int, default=6, help="Max compile iterations (default: 6)")
    ap.add_argument("--inner-loop-threshold", type=int, default=10,
                    help="Enter inner loop when fewer than N tests fail (default: 10)")
    ap.add_argument("--max-inner-iters", type=int, default=8,
                    help="Max inner loop iterations before aborting (default: 8)")
    ap.add_argument("--verbose", "-v", action="store_true", default=True, help="Verbose output (default: True)")
    ap.add_argument("--quiet", "-q", action="store_true", help="Quiet mode (disable verbose)")
    ap.add_argument("--no-stream", action="store_true", help="Don't stream test output (buffer instead)")
    ap.add_argument("--initial-results", default=None,
                    help="Path to initial test results from TestSmith (skips first test run)")
    args = ap.parse_args()

    repo_root = Path(args.repo_root).resolve()

    # Spec path is required
    if not args.spec:
        print("❌ --spec is required (e.g., --spec specs/core/myspec/v1/spec.yaml)")
        return 1
    spec_path = (repo_root / args.spec).resolve()

    # Derive spec name and version from spec path
    # Expected format: specs/core/{spec_name}/{version}/spec.yaml
    spec_parts = spec_path.parts
    try:
        specs_idx = spec_parts.index("specs")
        spec_name = spec_parts[specs_idx + 2]  # e.g., "supportops"
        spec_version = spec_parts[specs_idx + 3] if len(spec_parts) > specs_idx + 3 else None  # e.g., "v1"
    except (ValueError, IndexError):
        print(f"⚠️  Could not parse spec name from path: {spec_path}")
        spec_name = "unknown"
        spec_version = None

    # Derive prompt path from spec if not provided
    if args.prompt:
        prompt_path = (repo_root / args.prompt).resolve()
    else:
        artifact_suffix = os.environ.get("TDAD_ARTIFACT_SUFFIX", "")
        prompt_path = repo_root / "agent_artifacts" / "core" / f"{spec_name}{artifact_suffix}" / "system_prompt.txt"

    # Derive test directory from spec path
    if spec_version:
        test_dir = repo_root / "tests_visible" / "core" / spec_name / spec_version
        hidden_test_dir = repo_root / "tests_hidden" / "core" / spec_name / spec_version
    else:
        test_dir = repo_root / "tests_visible" / "core" / spec_name
        hidden_test_dir = repo_root / "tests_hidden" / "core" / spec_name

    # COMPILER ISOLATION: Make THIS SPEC'S test directories read-only before compilation
    # This prevents PromptSmith from circumventing evaluation by modifying tests
    # (TestSmith has already run; tests are now frozen for this compilation)
    # NOTE: We only lock THIS spec's directories, not all of tests_visible/
    for dir_to_lock in [test_dir, hidden_test_dir]:
        if dir_to_lock.exists():
            try:
                subprocess.run(
                    ["chmod", "-R", "a-w", str(dir_to_lock)],
                    check=True,
                    capture_output=True
                )
                print(f"🔒 Locked {dir_to_lock.relative_to(repo_root)}/ (read-only)")
            except subprocess.CalledProcessError:
                print(f"⚠️  Could not lock {dir_to_lock.relative_to(repo_root)}/ - continuing anyway")

    # Debug info for diagnosing volume sync issues
    print_conftest_debug_info(test_dir)

    # Determine test command
    if args.test_cmd:
        test_cmd = args.test_cmd
    elif args.micro:
        test_cmd = build_micro_test_cmd(test_dir)
        print(f"🔬 Micro mode: running first test file for debugging", flush=True)
        print(f"   Command: {test_cmd}", flush=True)
    elif args.slice:
        test_cmd = build_slice_test_cmd(test_dir)
        print("🔪 Slice mode: running subset of tests for faster iteration", flush=True)
        print(f"   Command: {test_cmd}", flush=True)
    else:
        # Build default test command from spec path
        test_cmd = f"pytest {test_dir} -m visible -n auto -v --tb=short"

    # Handle seed prompt initialization
    # Seeds are in agent_artifacts (copied to volume)
    seed_path = None
    if args.from_seed:
        artifact_suffix = os.environ.get("TDAD_ARTIFACT_SUFFIX", "")
        seed_path = repo_root / "agent_artifacts" / "core" / f"{spec_name}{artifact_suffix}" / "seed_prompt.txt"
    elif args.seed:
        seed_path = (repo_root / args.seed).resolve()

    if seed_path:
        if not seed_path.exists():
            print(f"❌ Seed prompt not found: {seed_path}")
            return 1
        print(f"📋 Initializing from seed prompt: {seed_path.name}")
        print(f"   Copying to: {prompt_path}")
        shutil.copy(seed_path, prompt_path)
        print("   ✓ Seed prompt copied")

        # Also copy seed tool descriptions if they exist
        seed_tool_desc_path = seed_path.parent / "seed_tool_descriptions.yaml"
        tool_desc_path = seed_path.parent / "tool_descriptions.yaml"
        if seed_tool_desc_path.exists():
            print(f"📋 Initializing tool descriptions from: {seed_tool_desc_path.name}")
            print(f"   Copying to: {tool_desc_path}")
            shutil.copy(seed_tool_desc_path, tool_desc_path)
            print("   ✓ Seed tool descriptions copied")

    # Friendly preflight checks
    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("CLAUDE_API_KEY"):
        print(
            "⚠️  No ANTHROPIC_API_KEY found in environment. "
            "Claude Agent SDK / Claude Code needs credentials. "
            "Set ANTHROPIC_API_KEY (recommended) and re-run."
        )

    return asyncio.run(
        compile_loop(
            repo_root=repo_root,
            spec_path=spec_path,
            prompt_path=prompt_path,
            test_cmd=test_cmd,
            model=args.model,
            max_iters=args.max_iters,
            verbose=args.verbose and not args.quiet,
            stream_tests=not args.no_stream,
            inner_loop_threshold=args.inner_loop_threshold,
            max_inner_iters=args.max_inner_iters,
            initial_results=args.initial_results,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
