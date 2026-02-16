# TDAD Reference Implementation (SpecSuite-Core)

This repo is the **reference implementation** for the TDAD paper, containing SpecSuite-Core with 4 specs. The pipeline generates all artifacts (tests, fixtures, mutations) from the spec alone; SupportOps includes generated outputs as a worked example.

| Spec | Domain | Status |
|------|--------|--------|
| **SupportOps** | Customer support | Worked example (spec + generated tests, fixtures, mutations, results) |
| **DataInsights** | SQL analytics | Spec + mutation intents (run pipeline to generate tests) |
| **IncidentRunbook** | Incident response | Spec + mutation intents (run pipeline to generate tests) |
| **ExpenseGuard** | Expense approval | Spec + mutation intents (run pipeline to generate tests) |

Key directories:
- `specs/core/{spec}/v{1,2}/spec.yaml` — Behavioral specifications (v1 base + v2 evolution)
- `tests_visible/core/{spec}/*.py` — Visible tests (used during compilation)
- `tests_hidden/core/{spec}/*.py` — Hidden tests (held out for HPR metric)
- `mutation_packs/core/{spec}/*.patch` — Mutation packs (for MS metric)
- `tdadlib/` — Runtime harness (Claude Agent SDK + MCP tools)

## Mental model

- The *product team* writes a spec.
- The *test builder* (future work) translates spec → executable tests.
- A *coding agent* iterates on the prompt until tests pass.
- The resulting prompt is your shipped agent, with a regression suite that you can extend over time.

## Prerequisites

- Python 3.10+
- An Anthropic API key for Claude Code / Claude Agent SDK:
  - set `ANTHROPIC_API_KEY` (recommended), or `CLAUDE_API_KEY`
- Install Python deps:
  ```bash
  pip install -e .
  ```

The runtime uses the Claude Agent SDK (Python) and exposes our deterministic fixtures as an in-process MCP tool server.

## Run visible tests

```bash
pytest -q tests_visible -m visible
```

## Compile loop (coding agent edits the prompt until tests pass)

This repo includes a simple compiler driver that uses Claude Agent SDK to run a Claude Code-backed agent with file/Bash tools enabled. The agent is instructed to edit **only** the prompt file.

```bash
python scripts/compile_prompt.py \
  --spec specs/core/supportops/v1/spec.yaml \
  --prompt agent_artifacts/core/supportops/system_prompt.txt \
  --test-cmd "pytest -q tests_visible/core/supportops -m visible" \
  --max-iters 8
```

## Docker (Required for Full Pipeline)

Docker provides a reproducible environment for the full TDAD workflow.

### ⚠️ CRITICAL: Volume-Based Architecture

**The pipeline operates exclusively on Docker volumes, NOT local files.**

```
Local Repo (your machine)          Docker Volumes (where pipeline runs)
─────────────────────────         ────────────────────────────────────
tests_visible/                 →   tests-visible volume
tests_hidden/                  →   tests-hidden volume
agent_artifacts/               →   agent-artifacts volume
```

**Key rules:**
1. Files created locally are **NOT** available to Docker containers
2. After modifying local files, you **MUST** rebuild + reinitialize volumes
3. The pipeline reads/writes ONLY to mounted volumes

```bash
# After ANY local file changes:
docker compose build
docker compose run --rm init-volumes
```

### Quick Start

```bash
# 1. Build all images
docker compose build

# 2. One-time Claude authentication (stores credentials in volume)
docker compose run --rm login

# 3. Initialize volumes (copies files from image to volumes)
docker compose run --rm init-volumes

# 4. Generate tests from spec
docker compose run --rm testsmith

# 5. Compile prompt until tests pass
docker compose run --rm compiler

# 6. Measure HPR (run hidden tests)
docker compose run --rm evaluate

# 7. Run mutation testing
docker compose run --rm mutation

# 8. Chat with compiled agent
docker compose run --rm agent
```

### Services

| Service | Purpose |
|---------|---------|
| `testsmith` | Generate tests from spec (TestSmith) |
| `compiler` | Compile prompt until visible tests pass (PromptSmith) |
| `evaluate` | Run hidden tests for HPR measurement |
| `mutation` | Run mutation testing for MS measurement |
| `agent` | Interactive chat with compiled agent |
| `login` | One-time Claude authentication |
| `init-volumes` | Sync files from image to volumes |

### Example: Full Pipeline Run

```bash
# Generate visible tests for SupportOps v1
docker compose run --rm testsmith python scripts/testsmith.py \
  --spec specs/core/supportops/v1/spec.yaml --type visible --verbose

# Compile prompt (iterates until tests pass)
docker compose run --rm compiler

# Measure HPR (hidden pass rate)
docker compose run --rm evaluate

# Run mutation testing
docker compose run --rm mutation
```

### Customizing Commands

Override default commands by passing arguments:

```bash
# Generate hidden tests only
docker compose run --rm testsmith python scripts/testsmith.py \
  --spec specs/core/supportops/v1/spec.yaml --type hidden

# Run specific test directory
docker compose run --rm evaluate pytest tests_hidden/core/supportops -v

# Compile with more iterations
docker compose run --rm compiler python scripts/compile_prompt.py --max-iters 20
```

### V1 → V2 Spec Evolution (SURS Measurement)

The pipeline supports spec evolution testing to measure backward compatibility.

**Environment variables for versioning:**
- `TDAD_SPEC_VERSION` - Which spec version to use (default: `v1`)
- `TDAD_ARTIFACT_SUFFIX` - Suffix for artifact directory (default: empty)

**V2 Evolution workflow:**
```bash
# 1. Ensure v1 is compiled first (v2 uses v1 prompt as seed)
docker compose run --rm compiler

# 2. Generate v2-specific tests (abuse detection)
docker compose run --rm testsmith-v2

# 3. Compile v2 prompt (warm start from v1)
docker compose run --rm compiler-v2

# 4. Measure SURS: v1 tests against v2 prompt
docker compose run --rm evaluate-surs
# SURS = passed / total
```

**How it works:**
- `testsmith-v2`: Generates tests for v2 spec features (e.g., abuse detection)
- `compiler-v2`: Compiles v2 prompt, starting from compiled v1 as seed
- `evaluate-surs`: Runs v1 visible tests against v2 prompt to measure backward compatibility

## Mutation Testing

Mutation patches live under `mutation_packs/core/supportops/`.

```bash
# Run automated mutation testing
docker compose run --rm mutation-test
```

**Results (SupportOps v1, 3 trials):** 100% mutation score (6/6 activated mutants killed across all runs; 7 total intents, 1 non-activating excluded).

See `results/supportops_v1_*.json` for per-run details and the paper (Table B1) for mutation intents across all four specs.

## Key Files

| Path | Description |
|------|-------------|
| `specs/core/supportops/v1/spec.yaml` | Product specification |
| `agent_artifacts/core/supportops/seed_prompt.txt` | Minimal starting prompt |
| `agent_artifacts/core/supportops/seed_prompt.txt` | Seed prompt (input to compiler) |
| `tests_visible/core/supportops/*.py` | Visible test suite (15 tests) |
| `scripts/compile_prompt.py` | PromptSmith compiler driver |
| `scripts/run_mutation_testing.py` | Mutation testing runner |
