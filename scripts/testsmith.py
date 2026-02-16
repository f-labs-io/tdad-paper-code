#!/usr/bin/env python3
"""
TestSmith CLI - Generate tests from TDAD specifications.

Usage:
    # Generate visible tests for a spec
    python scripts/testsmith.py --spec specs/core/supportops/v1/spec.yaml --type visible

    # Generate hidden tests
    python scripts/testsmith.py --spec specs/core/supportops/v1/spec.yaml --type hidden

    # Generate all tests for all specs
    python scripts/testsmith.py --all

    # Dry run (show generated code but don't write)
    python scripts/testsmith.py --spec specs/core/supportops/v1/spec.yaml --dry-run
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# Add parent dir to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tdadlib.testsmith import generate_tests
from tdadlib.testsmith.generator import generate_all_specs


def get_test_dirs(spec_path: Path, test_type: str, output_version: str | None) -> list[str]:
    """Get list of test directories to validate."""
    spec_name = spec_path.parent.parent.name

    test_dirs = []
    if test_type in ("visible", "all"):
        visible_dir = Path(f"tests_visible/core/{spec_name}")
        if output_version:
            visible_dir = visible_dir / output_version
        if visible_dir.exists():
            test_dirs.append(str(visible_dir))

    if test_type in ("hidden", "all"):
        hidden_dir = Path(f"tests_hidden/core/{spec_name}")
        if output_version:
            hidden_dir = hidden_dir / output_version
        if hidden_dir.exists():
            test_dirs.append(str(hidden_dir))

    return test_dirs


def classify_test_failures(pytest_output: str) -> tuple[list[str], list[str]]:
    """
    Classify test failures into infrastructure bugs vs expected assertion failures.

    Returns:
        (infrastructure_bugs, expected_failures) - lists of error descriptions
    """
    infrastructure_bugs = []
    expected_failures = []

    # Patterns for infrastructure bugs (test code is broken)
    infra_patterns = [
        "AttributeError:",
        "NameError:",
        "ImportError:",
        "ModuleNotFoundError:",
        "TypeError:",  # wrong number of args, wrong types
        "SyntaxError:",
        "IndentationError:",
        "fixture '",  # missing fixtures
        "F821",  # ruff undefined variable
        "E999",  # ruff syntax error
    ]

    # Split output into test failure blocks
    lines = pytest_output.split("\n")

    current_test = None
    current_error_lines = []

    for line in lines:
        # Detect test failure header like "FAILED tests_visible/core/supportops/v1/test_foo.py::test_bar"
        if line.startswith("FAILED ") or "::test_" in line and "FAILED" in line:
            if current_test and current_error_lines:
                # Process previous test
                error_text = "\n".join(current_error_lines)
                is_infra_bug = any(p in error_text for p in infra_patterns)
                if is_infra_bug:
                    infrastructure_bugs.append(f"{current_test}:\n{error_text}")
                else:
                    expected_failures.append(current_test)

            current_test = line.replace("FAILED ", "").strip()
            current_error_lines = []
        elif current_test:
            current_error_lines.append(line)

    # Process last test
    if current_test and current_error_lines:
        error_text = "\n".join(current_error_lines)
        is_infra_bug = any(p in error_text for p in infra_patterns)
        if is_infra_bug:
            infrastructure_bugs.append(f"{current_test}:\n{error_text}")
        else:
            expected_failures.append(current_test)

    return infrastructure_bugs, expected_failures


def fix_infrastructure_bugs(
    test_dirs: list[str],
    bugs: list[str],
    max_retries: int = 3,
) -> tuple[bool, float, int, int]:
    """
    Use LLM agent to fix infrastructure bugs in generated tests.

    Returns (success, cost_usd, input_tokens, output_tokens)
    """
    from claude_agent_sdk import (
        ClaudeSDKClient,
        ClaudeAgentOptions,
        ResultMessage,
        AssistantMessage,
        TextBlock,
        ToolUseBlock,
    )
    import asyncio

    async def fix_async() -> tuple[bool, float, int, int]:
        system_prompt = """You are a test fixer agent. Your job is to fix infrastructure bugs in pytest test files.

Infrastructure bugs include:
- AttributeError (calling methods/attributes that don't exist)
- NameError (undefined variables)
- ImportError (missing imports)
- TypeError (wrong function signatures)
- Missing fixtures

DO NOT change test assertions or test logic - only fix infrastructure issues that prevent tests from running.

For each bug:
1. Read the test file to understand the context
2. Identify the root cause
3. Fix it using the Edit tool
4. Run py_compile and ruff check to verify the fix

Common fixes:
- pytest.agent_query() doesn't exist → use the `runner` fixture instead
- Undefined variables → add imports or define them
- Missing fixtures → check conftest.py or use existing fixtures"""

        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            model="claude-sonnet-4-5-20250929",
            permission_mode="bypassPermissions",
            allowed_tools=["Read", "Edit", "Bash", "Glob"],
            max_turns=30,
        )

        prompt = f"""Fix the following infrastructure bugs in the test files:

{chr(10).join(bugs)}

Test directories: {', '.join(test_dirs)}

For each bug:
1. Read the relevant test file
2. Fix the infrastructure issue (don't change test logic/assertions)
3. Verify with py_compile and ruff check

Work through each bug systematically."""

        print(f"[TestSmith] Calling LLM to fix {len(bugs)} infrastructure bug(s)...")

        cost_usd = 0.0
        usage_tokens: dict[str, int] = {"input": 0, "cache_creation": 0, "cache_read": 0, "output": 0}

        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)

                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, ToolUseBlock):
                                tool_name = block.name
                                tool_input = block.input if hasattr(block, "input") else {}
                                if tool_name == "Edit":
                                    file_path = tool_input.get("file_path", "unknown")
                                    print(f"[TestSmith] ✏️  Fixing: {Path(file_path).name}")
                                elif tool_name == "Bash":
                                    cmd = tool_input.get("command", "")[:60]
                                    print(f"[TestSmith] 💻 {cmd}")
                            elif isinstance(block, TextBlock) and block.text.strip():
                                text = block.text.strip()[:100]
                                print(f"[TestSmith] 💭 {text}...")

                    elif isinstance(msg, ResultMessage):
                        if hasattr(msg, 'total_cost_usd') and msg.total_cost_usd:
                            cost_usd = msg.total_cost_usd
                        if hasattr(msg, 'usage') and msg.usage:
                            sdk_usage = msg.usage
                            usage_tokens["input"] += sdk_usage.get('input_tokens', 0)
                            usage_tokens["cache_creation"] += sdk_usage.get('cache_creation_input_tokens', 0)
                            usage_tokens["cache_read"] += sdk_usage.get('cache_read_input_tokens', 0)
                            usage_tokens["output"] += sdk_usage.get('output_tokens', 0)
                        break

            return True, cost_usd, usage_tokens

        except Exception as e:
            print(f"[TestSmith] Error fixing bugs: {e}")
            return False, cost_usd, usage_tokens

    return asyncio.run(fix_async())


def validate_generated_tests(
    spec_path: Path,
    test_type: str,
    output_version: str | None,
    max_fix_attempts: int = 3,
    save_results_to: Path | None = None,
) -> tuple[bool, float, dict[str, int]]:
    """
    Validate generated tests by running them and fixing infrastructure bugs.

    This is an agentic process:
    1. Run pytest to identify failures
    2. Classify failures: infrastructure bugs vs expected assertion failures
    3. If infrastructure bugs exist, use LLM to fix them
    4. Repeat until no infrastructure bugs remain or max attempts reached

    Expected assertion failures (tests failing because seed prompt doesn't implement
    the spec) are ignored - that's expected behavior.

    Args:
        save_results_to: If provided, save the final pytest output to this path.
                        This can be passed to the compiler to skip its first test run.

    Returns (success, cost_usd, usage_tokens)
    """
    total_cost_usd = 0.0
    total_usage: dict[str, int] = {"input": 0, "cache_creation": 0, "cache_read": 0, "output": 0}

    # Get visible and hidden test dirs separately
    visible_dirs = get_test_dirs(spec_path, "visible", output_version)
    hidden_dirs = get_test_dirs(spec_path, "hidden", output_version)

    # Determine which dirs to validate based on test_type
    if test_type == "visible":
        test_dirs = visible_dirs
    elif test_type == "hidden":
        test_dirs = hidden_dirs
    else:  # "all"
        test_dirs = visible_dirs + hidden_dirs

    if not test_dirs:
        print("[TestSmith] No test directories found to validate")
        return True, 0.0, {"input": 0, "cache_creation": 0, "cache_read": 0, "output": 0}

    print(f"\n[TestSmith] Validating generated tests: {' '.join(test_dirs)}")

    # Track the latest visible test output (captured during validation, not re-run)
    latest_visible_output = None

    for attempt in range(1, max_fix_attempts + 1):
        print(f"[TestSmith] Running pytest (attempt {attempt}/{max_fix_attempts})...")

        # Run visible and hidden separately to capture visible output without re-running
        visible_output = ""
        hidden_output = ""
        visible_infra_bugs = []
        hidden_infra_bugs = []
        visible_expected_fails = []
        hidden_expected_fails = []

        # Run visible tests
        if visible_dirs and (test_type in ("visible", "all")):
            visible_cmd = ["python", "-m", "pytest", "-n", "auto", "--tb=short", "-v", "-m", "visible"] + visible_dirs
            visible_result = subprocess.run(visible_cmd, capture_output=True, text=True)
            visible_output = visible_result.stdout + visible_result.stderr
            latest_visible_output = visible_output  # Capture for saving later
            print(f"[TestSmith] Captured {len(visible_output)} chars of visible test output (exit={visible_result.returncode})")
            visible_infra_bugs, visible_expected_fails = classify_test_failures(visible_output)

        # Run hidden tests
        if hidden_dirs and (test_type in ("hidden", "all")):
            hidden_cmd = ["python", "-m", "pytest", "-n", "auto", "--tb=short", "-v", "-m", "hidden"] + hidden_dirs
            hidden_result = subprocess.run(hidden_cmd, capture_output=True, text=True)
            hidden_output = hidden_result.stdout + hidden_result.stderr
            hidden_infra_bugs, hidden_expected_fails = classify_test_failures(hidden_output)

        # Combine results
        infra_bugs = visible_infra_bugs + hidden_infra_bugs
        expected_fails = visible_expected_fails + hidden_expected_fails

        print(f"[TestSmith] Results: {len(infra_bugs)} infrastructure bug(s), {len(expected_fails)} expected failure(s)")
        if test_type == "all":
            print(f"  Visible: {len(visible_infra_bugs)} infra bugs, {len(visible_expected_fails)} expected fails")
            print(f"  Hidden:  {len(hidden_infra_bugs)} infra bugs, {len(hidden_expected_fails)} expected fails")

        # If no infrastructure bugs, validation passes
        if not infra_bugs:
            print("[TestSmith] ✅ No infrastructure bugs - validation passes")
            print(f"[TestSmith] (Expected failures due to seed prompt: {len(expected_fails)})")

            # Output seed baseline metrics for pipeline parsing
            # Use same extraction approach as compile_prompt.py for consistency
            def parse_pytest_summary(output: str) -> tuple[int, int]:
                """Extract passed/failed/error counts from pytest output.

                Finds the pytest summary line (e.g., "52 errors in 1.30s" or "10 passed, 5 failed")
                and extracts counts. Errors are counted as failures for seed baseline purposes.
                """
                import re
                if not output:
                    return 0, 0

                # Find lines containing summary keywords (same approach as compile_prompt.py)
                lines = output.strip().split('\n')
                summary_lines = [l for l in lines if 'passed' in l or 'failed' in l or 'error' in l]

                if not summary_lines:
                    return 0, 0

                # Use the last summary line (the final pytest summary)
                summary = summary_lines[-1]

                passed = 0
                failed = 0

                # Extract "X passed", "Y failed", "Z errors" from the summary line
                passed_match = re.search(r'(\d+)\s+passed', summary)
                failed_match = re.search(r'(\d+)\s+failed', summary)
                errors_match = re.search(r'(\d+)\s+errors?', summary)

                if passed_match:
                    passed = int(passed_match.group(1))
                if failed_match:
                    failed = int(failed_match.group(1))
                if errors_match:
                    failed += int(errors_match.group(1))  # Count errors as failures

                return passed, failed

            vis_passed, vis_failed = parse_pytest_summary(visible_output)
            hid_passed, hid_failed = parse_pytest_summary(hidden_output)

            # Debug: show what we're parsing
            if visible_output:
                vis_lines = [l for l in visible_output.strip().split('\n') if 'passed' in l or 'failed' in l or 'error' in l]
                if vis_lines:
                    print(f"[TestSmith] Visible pytest summary: {vis_lines[-1][:100]}")
            if hidden_output:
                hid_lines = [l for l in hidden_output.strip().split('\n') if 'passed' in l or 'failed' in l or 'error' in l]
                if hid_lines:
                    print(f"[TestSmith] Hidden pytest summary: {hid_lines[-1][:100]}")
            print(f"SEED_BASELINE: visible_passed={vis_passed} visible_failed={vis_failed} hidden_passed={hid_passed} hidden_failed={hid_failed}")
            if save_results_to and latest_visible_output:
                # Save the already-captured visible output (no re-run needed!)
                save_results_to.parent.mkdir(parents=True, exist_ok=True)
                save_results_to.write_text(latest_visible_output)
                print(f"[TestSmith] Saved VISIBLE test results to {save_results_to}")
            return True, total_cost_usd, total_usage

        # Show the infrastructure bugs
        print("\n[TestSmith] Infrastructure bugs to fix:")
        for i, bug in enumerate(infra_bugs[:5], 1):  # Show first 5
            # Just show first 3 lines of each bug
            bug_preview = "\n".join(bug.split("\n")[:3])
            print(f"  {i}. {bug_preview}")
        if len(infra_bugs) > 5:
            print(f"  ... and {len(infra_bugs) - 5} more")

        # Use LLM to fix infrastructure bugs
        if attempt < max_fix_attempts:
            success, fix_cost, fix_usage = fix_infrastructure_bugs(test_dirs, infra_bugs)
            total_cost_usd += fix_cost
            for key in total_usage:
                total_usage[key] += fix_usage.get(key, 0)
            if not success:
                print("[TestSmith] ⚠️  Fix attempt failed, retrying...")
        else:
            print(f"\n[TestSmith] ❌ Max fix attempts ({max_fix_attempts}) reached")
            print("[TestSmith] Remaining infrastructure bugs:")
            for bug in infra_bugs:
                print(f"  - {bug.split(':')[0]}")
            return False, total_cost_usd, total_usage

    return False, total_cost_usd, total_usage


def print_conftest_debug_info(spec_path: Path) -> None:
    """Print debug info about conftest.py files for diagnosing volume sync issues."""
    # Derive the spec name from path (e.g., specs/core/supportops/v1/spec.yaml -> supportops)
    spec_name = spec_path.parent.parent.name

    # Check both visible and hidden conftest files
    for test_type in ["visible", "hidden"]:
        conftest_path = Path(f"tests_{test_type}/core/{spec_name}/conftest.py")
        if conftest_path.exists():
            stat = conftest_path.stat()
            size = stat.st_size
            modified = stat.st_mtime
            from datetime import datetime
            modified_str = datetime.fromtimestamp(modified).strftime("%Y-%m-%d %H:%M:%S")
            print(f"[TestSmith] conftest ({test_type}): {size} bytes, modified: {modified_str}")
        else:
            print(f"[TestSmith] conftest ({test_type}): NOT FOUND at {conftest_path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="TestSmith - Generate tests from TDAD specifications",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument(
        "--spec",
        type=str,
        help="Path to spec.yaml file",
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Output directory for generated tests (default: auto from spec path)",
    )
    parser.add_argument(
        "--type",
        choices=["visible", "hidden", "all"],
        default="all",
        help="Type of tests to generate (default: all - generates both visible and hidden)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="claude-sonnet-4-5-20250929",
        help="Claude model to use (default: claude-sonnet-4-5-20250929)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="all_specs",
        help="Generate tests for all specs in specs/core/",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show generated code but don't write files",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show tool calls and progress (INFO level)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show detailed debug output (DEBUG level)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Maximum retry attempts on validation failure (default: 3)",
    )
    parser.add_argument(
        "--output-version",
        type=str,
        default=None,
        help="Version suffix for output directories (e.g., 'v1' → tests_visible/core/supportops/v1/)",
    )
    parser.add_argument(
        "--max-fix-attempts",
        type=int,
        default=3,
        help="Maximum attempts to fix infrastructure bugs in generated tests (default: 3)",
    )
    parser.add_argument(
        "--save-results",
        type=str,
        default=None,
        help="Save final pytest output to this file (can be passed to compiler --initial-results)",
    )

    args = parser.parse_args()

    # Validate args
    if not args.spec and not args.all_specs:
        parser.error("Either --spec or --all is required")

    if args.all_specs:
        print("[TestSmith] Generating tests for all specs...")
        generate_all_specs(model=args.model)
        print("\n[TestSmith] Done!")
        return 0

    # Single spec mode
    spec_path = Path(args.spec)
    if not spec_path.exists():
        print(f"[TestSmith] Error: Spec file not found: {spec_path}", file=sys.stderr)
        return 1

    print(f"[TestSmith] Spec: {spec_path}")
    print(f"[TestSmith] Type: {args.type}")
    print(f"[TestSmith] Model: {args.model}")
    if args.output:
        print(f"[TestSmith] Output: {args.output}")
    if args.output_version:
        print(f"[TestSmith] Output version: {args.output_version}")

    # Debug info for diagnosing volume sync issues
    print_conftest_debug_info(spec_path)
    print()

    results, gen_cost, gen_usage = generate_tests(
        spec_path=spec_path,
        output_dir=args.output,  # None = auto-derive from spec path
        output_version=args.output_version,  # Version suffix for directories
        test_type=args.type,
        model=args.model,
        dry_run=args.dry_run,
        verbose=args.verbose,
        debug=args.debug,
        max_retries=args.max_retries,
    )

    if args.dry_run:
        print("\n[TestSmith] Dry run - generated code:")
        for filename, code in results.items():
            print(f"\n{'='*60}")
            print(f"File: {filename}")
            print(f"{'='*60}")
            print(code[:2000] + "..." if len(code) > 2000 else code)
        print("\n[TestSmith] Done!")
        return 0

    # Validate generated tests by running them and fix infrastructure bugs
    save_results_path = Path(args.save_results) if args.save_results else None
    success, val_cost, val_usage = validate_generated_tests(
        spec_path, args.type, args.output_version, args.max_fix_attempts, save_results_path
    )

    # Combine usage from generation and validation
    total_cost = gen_cost + val_cost
    total_usage: dict[str, int] = {"input": 0, "cache_creation": 0, "cache_read": 0, "output": 0}
    for key in total_usage:
        total_usage[key] = gen_usage.get(key, 0) + val_usage.get(key, 0)

    if total_cost > 0:
        total_input = total_usage["input"] + total_usage["cache_creation"] + total_usage["cache_read"]
        total_output = total_usage["output"]
        print(f"\n{'='*60}")
        print("💰 TESTSMITH API USAGE SUMMARY")
        print(f"{'='*60}")
        print(f"   Input tokens (uncached):     {total_usage['input']:,}")
        print(f"   Input tokens (cache write):  {total_usage['cache_creation']:,}")
        print(f"   Input tokens (cache read):   {total_usage['cache_read']:,}")
        print(f"   Output tokens:               {total_usage['output']:,}")
        print(f"   Total input (all types):     {total_input:,}")
        print(f"   Total cost:                  ${total_cost:.4f} USD")
        if val_cost > 0:
            print(f"   (Generation: ${gen_cost:.4f}, Validation fixes: ${val_cost:.4f})")
        # Machine-readable line for parsing
        print(f"COST_SUMMARY: input_tokens={total_usage['input']} cache_creation={total_usage['cache_creation']} cache_read={total_usage['cache_read']} output_tokens={total_output} total_cost_usd={total_cost:.6f}")
        print(f"{'='*60}")

    if not success:
        print("\n[TestSmith] FAILED - generated tests have infrastructure bugs that couldn't be fixed")
        return 1

    print("\n[TestSmith] Done!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
