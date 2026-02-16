"""
Root conftest for visible tests - handles cost file cleanup and aggregation.
"""
from __future__ import annotations

import pytest
from tdadlib.runtime.cost_tracker import clear_cost_files, print_cost_summary


def pytest_configure(config):
    """Clear any existing cost files at the start of the test session."""
    clear_cost_files()


def pytest_unconfigure(config):
    """Print aggregated cost summary at the end of the test session."""
    print_cost_summary()
