#!/usr/bin/env python3
"""
Generate numerical results for the TDAD paper.
Outputs key metrics in formats suitable for inclusion in academic papers.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
import yaml


def run_cmd(cmd: str, cwd: Path) -> tuple[int, str]:
    """Run a shell command and return (exit_code, output)."""
    proc = subprocess.run(
        cmd,
        shell=True,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return proc.returncode, proc.stdout


def count_tests_from_output(output: str) -> dict:
    """Parse pytest output to extract pass/fail counts."""
    lines = output.strip().split('\n')
    for line in reversed(lines):
        if 'passed' in line or 'failed' in line:
            # Parse line like "15 passed in 217.09s" or "1 failed, 14 passed in 238.85s"
            parts = line.split()
            result = {'passed': 0, 'failed': 0, 'total': 0}
            for i, part in enumerate(parts):
                if part == 'passed' and i > 0:
                    result['passed'] = int(parts[i-1])
                elif part.rstrip(',') == 'failed' and i > 0:
                    result['failed'] = int(parts[i-1])
            result['total'] = result['passed'] + result['failed']
            return result
    return {'passed': 0, 'failed': 0, 'total': 0}


def run_baseline_tests(repo_root: Path, test_cmd: str) -> dict:
    """Run baseline test suite and return results."""
    print("Running baseline test suite...", flush=True)
    code, output = run_cmd(test_cmd, cwd=repo_root)
    counts = count_tests_from_output(output)
    return {
        'exit_code': code,
        'passed': counts['passed'],
        'failed': counts['failed'],
        'total': counts['total'],
        'all_pass': code == 0,
    }


def run_mutation_tests(repo_root: Path, manifest_path: Path, test_cmd: str) -> dict:
    """Run mutation testing and return results."""
    print("Running mutation tests...", flush=True)

    with open(manifest_path) as f:
        manifest = yaml.safe_load(f)

    mutation_dir = manifest_path.parent
    target_file = repo_root / "agent_artifacts" / "core" / "supportops" / "system_prompt.txt"

    # Backup original
    with open(target_file) as f:
        original_content = f.read()

    results = []
    for mutant in manifest['mutants']:
        mutant_id = mutant['id']
        patch_path = mutation_dir / mutant['patch']

        print(f"  Testing mutant: {mutant_id}...", flush=True)

        # Apply patch
        code, _ = run_cmd(f"patch -p1 < {patch_path}", cwd=repo_root)
        if code != 0:
            results.append({'id': mutant_id, 'killed': False, 'error': 'patch_failed'})
            continue

        # Run tests
        code, output = run_cmd(test_cmd, cwd=repo_root)
        counts = count_tests_from_output(output)
        killed = code != 0

        results.append({
            'id': mutant_id,
            'killed': killed,
            'tests_failed': counts['failed'],
            'tests_passed': counts['passed'],
        })

        # Restore original
        with open(target_file, 'w') as f:
            f.write(original_content)

    killed_count = sum(1 for r in results if r.get('killed', False))
    total_count = len(results)
    mutation_score = killed_count / total_count if total_count > 0 else 0.0

    return {
        'mutants': results,
        'killed': killed_count,
        'total': total_count,
        'mutation_score': mutation_score,
    }


def generate_latex_table(baseline: dict, mutation: dict) -> str:
    """Generate LaTeX table for paper."""
    lines = [
        "\\begin{table}[h]",
        "\\centering",
        "\\caption{TDAD Evaluation Results for SupportOps Agent}",
        "\\begin{tabular}{lc}",
        "\\toprule",
        "Metric & Value \\\\",
        "\\midrule",
        f"Visible Tests Passed & {baseline['passed']}/{baseline['total']} \\\\",
        "CSB (Compile Success) & " + ("\\checkmark" if baseline['all_pass'] else "\\texttimes") + " \\\\",
        f"Mutants Killed & {mutation['killed']}/{mutation['total']} \\\\",
        f"Mutation Score (MS) & {mutation['mutation_score']:.1%} \\\\",
        "\\bottomrule",
        "\\end{tabular}",
        "\\label{tab:tdad-results}",
        "\\end{table}",
    ]
    return '\n'.join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate numerical results for TDAD paper")
    ap.add_argument("--repo-root", default=".", help="Repository root")
    ap.add_argument(
        "--test-cmd",
        default="pytest -q tests_visible/core/supportops -m visible",
        help="Test command",
    )
    ap.add_argument(
        "--manifest",
        default="mutation_packs/core/supportops/manifest.yaml",
        help="Mutation pack manifest",
    )
    ap.add_argument("--output", "-o", default=None, help="Output file (default: stdout)")
    ap.add_argument("--format", choices=["json", "latex", "markdown"], default="markdown")
    ap.add_argument("--skip-mutations", action="store_true", help="Skip mutation testing")
    args = ap.parse_args()

    repo_root = Path(args.repo_root).resolve()
    manifest_path = repo_root / args.manifest

    print("=" * 60, flush=True)
    print("TDAD Results Generation", flush=True)
    print("=" * 60, flush=True)

    # Run baseline tests
    baseline = run_baseline_tests(repo_root, args.test_cmd)
    print(f"Baseline: {baseline['passed']}/{baseline['total']} passed", flush=True)

    # Run mutation tests (optional)
    if args.skip_mutations:
        mutation = {'mutants': [], 'killed': 0, 'total': 0, 'mutation_score': 0.0}
    else:
        mutation = run_mutation_tests(repo_root, manifest_path, args.test_cmd)
        print(f"Mutation Score: {mutation['mutation_score']:.1%}", flush=True)

    # Generate output
    results = {
        'timestamp': datetime.now().isoformat(),
        'baseline': baseline,
        'mutation': mutation,
        'metrics': {
            'CSB': 1 if baseline['all_pass'] else 0,
            'MS': mutation['mutation_score'],
            'visible_pass_rate': baseline['passed'] / baseline['total'] if baseline['total'] > 0 else 0,
        }
    }

    if args.format == 'json':
        output = json.dumps(results, indent=2)
    elif args.format == 'latex':
        output = generate_latex_table(baseline, mutation)
    else:  # markdown
        lines = [
            "# TDAD Evaluation Results",
            "",
            f"**Generated:** {results['timestamp']}",
            "",
            "## Summary Metrics",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| CSB (Compile Success) | {results['metrics']['CSB']} |",
            f"| Visible Pass Rate | {results['metrics']['visible_pass_rate']:.1%} |",
            f"| Mutation Score (MS) | {results['metrics']['MS']:.1%} |",
            "",
            "## Baseline Test Results",
            "",
            f"- Passed: {baseline['passed']}",
            f"- Failed: {baseline['failed']}",
            f"- Total: {baseline['total']}",
            "",
        ]

        if mutation['mutants']:
            lines.extend([
                "## Mutation Testing Results",
                "",
                "| Mutant | Status | Tests Failed |",
                "|--------|--------|--------------|",
            ])
            for m in mutation['mutants']:
                status = "KILLED" if m.get('killed') else "SURVIVED"
                failed = m.get('tests_failed', 'N/A')
                lines.append(f"| {m['id']} | {status} | {failed} |")
            lines.append("")

        output = '\n'.join(lines)

    if args.output:
        with open(args.output, 'w') as f:
            f.write(output)
        print(f"\nResults written to: {args.output}", flush=True)
    else:
        print("\n" + output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
