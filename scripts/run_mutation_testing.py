#!/usr/bin/env python3
"""
Run semantic mutation testing against the visible test suite.

This script implements the MutationSmith workflow:
1. Load compiled prompt P*
2. For each mutation intent in mutations.yaml:
   a. Generate mutant P*_m using MutationSmith (LLM)
   b. Run activation probe to verify mutation changes behavior
   c. Run visible test suite against P*_m
3. Compute Mutation Score = (activated mutants killed) / (activated mutants)

Mutants that never activate after max_attempts are marked inconclusive.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tdadlib.mutationsmith import generate_mutant, MutantArtifacts
from tdadlib.mutationsmith.probe import run_activation_probe_with_prompt, ProbeResult
from tdadlib.runtime.prompt_loader import load_tool_description_overrides


@dataclass
class MutationResult:
    """Result of testing a single mutation."""
    mutant_id: str
    severity: str
    category: str
    intent: str
    activated: bool
    killed: bool
    activation_attempts: int
    test_output: str = ""


def load_mutations(mutations_path: Path) -> dict[str, Any]:
    """Load mutations.yaml for a spec."""
    with open(mutations_path) as f:
        return yaml.safe_load(f)


def load_prompt(prompt_path: Path) -> str:
    """Load the compiled prompt."""
    with open(prompt_path) as f:
        return f.read()


def save_prompt(prompt_path: Path, content: str) -> None:
    """Save a prompt to file."""
    with open(prompt_path, 'w') as f:
        f.write(content)


def prompt_hash(prompt: str) -> str:
    """Generate a short hash of prompt content for caching."""
    return hashlib.sha256(prompt.encode()).hexdigest()[:12]


def run_tests(
    test_cmd: str,
    repo_root: Path,
    env_override: dict[str, str] | None = None,
    stream: bool = True,
) -> tuple[int, str]:
    """Run the test suite and return (exit_code, output)."""
    env = os.environ.copy()
    if env_override:
        env.update(env_override)

    if stream:
        # Stream output in real-time
        proc = subprocess.Popen(
            test_cmd,
            shell=True,
            cwd=str(repo_root),
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
    else:
        proc = subprocess.run(
            test_cmd,
            shell=True,
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        return proc.returncode, proc.stdout


def run_mutation_test(
    repo_root: Path,
    spec_name: str,
    mutation: dict[str, Any],
    base_prompt: str,
    base_tool_descriptions: dict[str, str],
    generator_config: dict[str, Any],
    prompt_path: Path,
    test_cmd: str,
    cache_dir: Path | None,
    verbose: bool,
    model: str | None = None,
    spec_version: str = "v1",
) -> MutationResult:
    """Run a single mutation test.

    IMPORTANT: This function never modifies the original prompt file.
    - Probes run with mutant prompt passed directly in memory
    - Tests run with mutant written to a temp file, using TDAD_PROMPT_OVERRIDE_PATH
    """
    mutant_id = mutation["id"]
    severity = mutation.get("severity", "unknown")
    category = mutation.get("category", "unknown")
    intent = mutation["intent"]
    probe_spec = mutation.get("activation_probe", {})

    max_attempts = generator_config.get("max_attempts", 5)
    constraints = generator_config.get("constraints", [])
    temperature = generator_config.get("temperature", 0)

    # Get spec path for probe
    spec_path = repo_root / "specs" / "core" / spec_name / spec_version / "spec.yaml"

    print(f"\n{'='*60}", flush=True)
    print(f"Testing: {mutant_id} [{severity}]", flush=True)
    print(f"Category: {category}", flush=True)
    print(f"Intent: {intent[:80]}...", flush=True)
    print(f"{'='*60}", flush=True)

    activated = False
    killed = False
    attempts = 0
    test_output = ""
    mutant_artifacts: MutantArtifacts | None = None

    for attempt in range(max_attempts):
        attempts = attempt + 1
        print(f"\nAttempt {attempts}/{max_attempts}...", flush=True)

        # Generate mutant using MutationSmith (prompt + tool descriptions)
        try:
            mutant_artifacts = generate_mutant(
                base_prompt=base_prompt,
                mutation_intent=intent,
                constraints=constraints,
                tool_descriptions=base_tool_descriptions,
                temperature=temperature,
                model=model,
                verbose=verbose,
            )
        except Exception as e:
            print(f"  ERROR generating mutant: {e}", flush=True)
            continue

        # Cache mutant if cache_dir provided
        if cache_dir:
            cache_file = cache_dir / f"{mutant_id}_attempt{attempts}.txt"
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            save_prompt(cache_file, mutant_artifacts.prompt)
            print(f"  Cached mutant: {cache_file.name}", flush=True)

        # Run activation probe with mutant prompt and tool descriptions
        try:
            probe_result: ProbeResult = run_activation_probe_with_prompt(
                system_prompt=mutant_artifacts.prompt,
                probe_spec=probe_spec,
                spec_name=spec_name,
                spec_path=spec_path if spec_path.exists() else None,
                tool_description_overrides=mutant_artifacts.tool_descriptions,
                model=model,
                verbose=verbose,
            )
            activated = probe_result.activated
        except Exception as e:
            print(f"  ERROR in activation probe: {e}", flush=True)
            activated = False

        if activated:
            print(f"  Mutation ACTIVATED on attempt {attempts}", flush=True)
            break
        else:
            print("  Mutation did not activate, retrying...", flush=True)

    if not activated or mutant_artifacts is None:
        print(f"INCONCLUSIVE: Mutation never activated after {max_attempts} attempts", flush=True)
        return MutationResult(
            mutant_id=mutant_id,
            severity=severity,
            category=category,
            intent=intent,
            activated=False,
            killed=False,
            activation_attempts=attempts,
        )

    # Run visible tests against mutant using temp files + env var overrides
    print("Running visible tests against mutant...", flush=True)

    # Write mutant prompt to a temp file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        f.write(mutant_artifacts.prompt)
        temp_prompt_path = f.name

    # Write mutant tool descriptions to a temp file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        yaml.dump(mutant_artifacts.tool_descriptions, f)
        temp_tool_desc_path = f.name

    try:
        # Pass temp file paths via environment variables
        env_override = {
            "TDAD_PROMPT_OVERRIDE_PATH": temp_prompt_path,
            "TDAD_TOOL_DESC_OVERRIDE_PATH": temp_tool_desc_path,
        }
        exit_code, test_output = run_tests(test_cmd, repo_root, env_override=env_override)
    finally:
        # Clean up temp files
        os.unlink(temp_prompt_path)
        os.unlink(temp_tool_desc_path)

    killed = exit_code != 0

    if killed:
        print("KILLED: Visible tests caught the mutation", flush=True)
    else:
        print("SURVIVED: Visible tests did NOT catch the mutation", flush=True)

    if verbose:
        # Show truncated test output
        output_preview = test_output[:2000] if len(test_output) > 2000 else test_output
        print(f"\n--- Test Output ---\n{output_preview}\n---", flush=True)

    return MutationResult(
        mutant_id=mutant_id,
        severity=severity,
        category=category,
        intent=intent,
        activated=True,
        killed=killed,
        activation_attempts=attempts,
        test_output=test_output,
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run semantic mutation testing on TDAD visible test suite"
    )
    ap.add_argument(
        "--spec",
        default="supportops",
        help="Spec name (supportops, datainsights, incidentrunbook, expenseguard)",
    )
    ap.add_argument(
        "--spec-version",
        default="v1",
        help="Spec version (v1, v2)",
    )
    ap.add_argument(
        "--repo-root",
        default=".",
        help="Repository root (default: .)",
    )
    ap.add_argument(
        "--mutations",
        help="Path to mutations.yaml (default: mutation_packs/core/<spec>/mutations.yaml)",
    )
    ap.add_argument(
        "--prompt",
        help="Path to compiled prompt (default: agent_artifacts/core/<spec>/system_prompt.txt)",
    )
    ap.add_argument(
        "--test-cmd",
        help="Test command (default: pytest -q tests_visible/core/<spec> -m visible)",
    )
    ap.add_argument(
        "--cache-dir",
        help="Directory to cache generated mutants (optional)",
    )
    ap.add_argument(
        "--model",
        help="Claude model override for MutationSmith",
    )
    ap.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show detailed test output",
    )
    ap.add_argument(
        "--output", "-o",
        help="Write JSON report to file",
    )
    ap.add_argument(
        "--single",
        help="Run only a single mutation by ID",
    )
    args = ap.parse_args()

    repo_root = Path(args.repo_root).resolve()
    spec_name = args.spec

    # Set defaults based on spec
    mutations_path = Path(args.mutations) if args.mutations else \
        repo_root / "mutation_packs" / "core" / spec_name / "mutations.yaml"
    prompt_path = Path(args.prompt) if args.prompt else \
        repo_root / "agent_artifacts" / "core" / spec_name / "system_prompt.txt"
    test_cmd = args.test_cmd or f"pytest tests_visible/core/{spec_name} -m visible -n auto -v --tb=short --maxfail=1"
    cache_dir = Path(args.cache_dir) if args.cache_dir else None

    # Validate paths
    if not mutations_path.exists():
        print(f"ERROR: Mutations file not found: {mutations_path}", flush=True)
        return 1

    if not prompt_path.exists():
        print(f"ERROR: Compiled prompt not found: {prompt_path}", flush=True)
        print("Run PromptSmith first to compile the prompt.", flush=True)
        return 1

    # Load mutations, prompt, and tool descriptions
    mutations_config = load_mutations(mutations_path)
    base_prompt = load_prompt(prompt_path)

    # Load tool descriptions from agent artifacts
    agent_dir = prompt_path.parent
    base_tool_descriptions = load_tool_description_overrides(agent_dir)
    if base_tool_descriptions:
        print(f"Loaded {len(base_tool_descriptions)} tool description override(s)", flush=True)

    generator_config = mutations_config.get("generator", {})
    mutations = mutations_config.get("mutations", [])

    # Filter to single mutation if specified
    if args.single:
        mutations = [m for m in mutations if m.get("id") == args.single]
        if not mutations:
            print(f"ERROR: Mutation '{args.single}' not found in {mutations_path}", flush=True)
            return 1

    print("=" * 60, flush=True)
    print("TDAD Semantic Mutation Testing (MutationSmith)", flush=True)
    print("=" * 60, flush=True)
    print(f"Spec: {spec_name}", flush=True)
    print(f"Pack ID: {mutations_config.get('mutation_pack_id', 'unknown')}", flush=True)
    print(f"Mutations: {len(mutations)}", flush=True)
    print(f"Prompt hash: {prompt_hash(base_prompt)}", flush=True)
    print(f"Test command: {test_cmd}", flush=True)
    if args.model:
        print(f"Model: {args.model}", flush=True)

    # Run all mutations
    results: list[MutationResult] = []

    for mutation in mutations:
        result = run_mutation_test(
            repo_root=repo_root,
            spec_name=spec_name,
            mutation=mutation,
            base_prompt=base_prompt,
            base_tool_descriptions=base_tool_descriptions,
            generator_config=generator_config,
            prompt_path=prompt_path,
            test_cmd=test_cmd,
            cache_dir=cache_dir,
            verbose=args.verbose,
            model=args.model,
            spec_version=args.spec_version,
        )
        results.append(result)

    # Compute statistics
    activated_results = [r for r in results if r.activated]
    inconclusive_results = [r for r in results if not r.activated]
    killed_results = [r for r in activated_results if r.killed]
    survived_results = [r for r in activated_results if not r.killed]

    activated_count = len(activated_results)
    killed_count = len(killed_results)
    mutation_score = killed_count / activated_count if activated_count > 0 else 0.0

    # Summary report
    print("\n" + "=" * 60, flush=True)
    print("MUTATION TESTING RESULTS", flush=True)
    print("=" * 60, flush=True)

    print(f"\n{'Mutant ID':<30} {'Severity':<10} {'Status':<15}", flush=True)
    print("-" * 60, flush=True)

    for r in results:
        if not r.activated:
            status = "INCONCLUSIVE"
        elif r.killed:
            status = "KILLED"
        else:
            status = "SURVIVED"
        print(f"{r.mutant_id:<30} {r.severity:<10} {status:<15}", flush=True)

    print(f"\n{'='*60}", flush=True)
    print(f"Total mutations: {len(results)}", flush=True)
    print(f"Activated: {activated_count}", flush=True)
    print(f"Inconclusive: {len(inconclusive_results)}", flush=True)
    print(f"Killed: {killed_count}", flush=True)
    print(f"Survived: {len(survived_results)}", flush=True)
    print(f"\nMutation Score: {mutation_score:.1%}", flush=True)
    print(f"{'='*60}", flush=True)

    # Highlight survivors (test suite weaknesses)
    if survived_results:
        print("\nSURVIVING MUTANTS (test suite gaps):", flush=True)
        for r in survived_results:
            print(f"  - {r.mutant_id} [{r.severity}]: {r.intent[:60]}...", flush=True)

    # Write JSON report if requested
    if args.output:
        report = {
            "spec": spec_name,
            "spec_version": args.spec_version,
            "mutation_pack_id": mutations_config.get("mutation_pack_id"),
            "prompt_hash": prompt_hash(base_prompt),
            "summary": {
                "total": len(results),
                "activated": activated_count,
                "inconclusive": len(inconclusive_results),
                "killed": killed_count,
                "survived": len(survived_results),
                "mutation_score": mutation_score,
            },
            "results": [
                {
                    "id": r.mutant_id,
                    "severity": r.severity,
                    "category": r.category,
                    "intent": r.intent,
                    "activated": r.activated,
                    "killed": r.killed,
                    "attempts": r.activation_attempts,
                }
                for r in results
            ],
        }
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w') as f:
            json.dump(report, f, indent=2)
        print(f"\nReport written to: {output_path}", flush=True)

    # Return non-zero if any mutant survived
    return 0 if len(survived_results) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
