#!/usr/bin/env python3
"""Test subprocess AFTER Claude SDK is initialized."""
import subprocess
import sys
import os
from pathlib import Path

def run_test():
    """Run pytest -n auto via subprocess."""
    cmd = "pytest tests_visible/core/supportops -m visible -n auto --collect-only"
    print(f"Running: {cmd}")
    proc = subprocess.Popen(
        cmd,
        shell=True,
        cwd="/workspace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    for line in proc.stdout:
        print(line, end="", flush=True)
    proc.wait()
    return proc.returncode

print("=" * 60)
print("BEFORE SDK IMPORT")
print("=" * 60)
print(f"PATH: {os.environ.get('PATH')}")
print()

print("Test 1: Before SDK import")
print("-" * 60)
rc = run_test()
print(f"Exit code: {rc}")
print()

print("=" * 60)
print("IMPORTING SDK")
print("=" * 60)
try:
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
    print("SDK imported successfully")
except Exception as e:
    print(f"SDK import failed: {e}")
    sys.exit(1)

print()
print(f"PATH after import: {os.environ.get('PATH')}")
print()

print("Test 2: After SDK import")
print("-" * 60)
rc = run_test()
print(f"Exit code: {rc}")
print()

print("=" * 60)
print("CREATING SDK CLIENT")
print("=" * 60)
import asyncio

async def test_with_client():
    options = ClaudeAgentOptions(
        system_prompt="test",
        model="claude-sonnet-4-5-20250929",
        cwd="/workspace",
        permission_mode="bypassPermissions",
        allowed_tools=["Bash", "Read"],
        max_turns=1,
    )

    print("Creating client...")
    async with ClaudeSDKClient(options=options) as client:
        print("Client created!")
        print(f"PATH with client: {os.environ.get('PATH')}")
        print()
        print("Test 3: Inside SDK client context")
        print("-" * 60)
        rc = run_test()
        print(f"Exit code: {rc}")

    print()
    print("Test 4: After client context closed")
    print("-" * 60)
    rc = run_test()
    print(f"Exit code: {rc}")

asyncio.run(test_with_client())
