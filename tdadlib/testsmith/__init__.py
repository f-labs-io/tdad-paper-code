"""
TestSmith: Automated test generation from TDAD specifications.

TestSmith generates both visible and hidden tests from a spec.yaml:
- Visible tests: MFT, INV, DIR per branch (used during PromptSmith compilation)
- Hidden tests: paraphrase, boundary, metamorphic (held out for HPR metric)

Usage:
    from tdadlib.testsmith import generate_tests

    generate_tests(
        spec_path="specs/core/supportops/v1/spec.yaml",
        output_dir="tests_generated/core/supportops",
        test_type="visible",  # or "hidden" or "all"
    )
"""

from tdadlib.testsmith.generator import generate_tests, TestType

__all__ = ["generate_tests", "TestType"]
