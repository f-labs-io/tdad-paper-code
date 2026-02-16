"""
MutationSmith - Semantic prompt mutation testing for TDAD.

This module implements the MutationSmith workflow:
1. generate_mutant() - Use LLM to create semantically mutated prompts
2. run_activation_probe() - Run agent with mutated prompt and check for violations
3. Predicate evaluation for expect_violation checks
"""
from __future__ import annotations

from tdadlib.mutationsmith.generator import generate_mutant, MutantArtifacts
from tdadlib.mutationsmith.probe import run_activation_probe
from tdadlib.mutationsmith.predicates import evaluate_violation

__all__ = ["generate_mutant", "MutantArtifacts", "run_activation_probe", "evaluate_violation"]
