"""
Tests for the test suite's own isolation rules (tests/conftest.py):
- offline tests record Claude usage in a temporary ledger, never the real data/;
- only tests marked real_api use the real ledger, and they run only with RUN_REAL_CLAUDE_TEST=1;
- `pytest -m real_api` selects exactly those tests and nothing else.

No test here makes a Claude API call. The subprocess runs always drop RUN_REAL_CLAUDE_TEST
from their environment, so they can never trigger a real request.
"""
import os
import subprocess
import sys

import pytest

from app.brain import cost_controls
from config import settings
from config.settings import get_setting
from tests.conftest import REAL_API_OPT_IN

REAL_API_TESTS = {
    "tests/test_brain.py::test_real_claude_ping",
    "tests/test_health.py::test_real_claude_health_check",
    "tests/test_test_isolation.py::test_real_api_tests_use_the_real_ledger",
}


def run_pytest(*args):
    env = {name: value for name, value in os.environ.items() if name != REAL_API_OPT_IN}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args],
        cwd=settings.PROJECT_ROOT, capture_output=True, text=True, env=env, timeout=300,
    )


def test_offline_tests_record_usage_in_a_temporary_ledger(tmp_path):
    ledger = cost_controls._ledger_path()  # the real config's relative data/claude_usage.db
    assert ledger.is_relative_to(tmp_path)
    assert not ledger.is_relative_to(settings.PROJECT_ROOT / "data")


def test_real_api_marker_selects_only_the_real_api_tests():
    result = run_pytest("--collect-only", "-q", "-m", "real_api")
    assert result.returncode == 0, result.stdout + result.stderr
    collected = {line.strip() for line in result.stdout.splitlines() if "::" in line}
    assert collected == REAL_API_TESTS


def test_real_api_tests_are_skipped_without_the_opt_in():
    result = run_pytest("-q", "-rs", "-m", "real_api")
    assert result.returncode == 0, result.stdout + result.stderr
    summary = [line for line in result.stdout.splitlines() if line.strip()][-1]
    assert f"{len(REAL_API_TESTS)} skipped" in summary and "passed" not in summary
    assert f"set {REAL_API_OPT_IN}=1 to run" in result.stdout  # the skip reason explains the opt-in


@pytest.mark.real_api
def test_real_api_tests_use_the_real_ledger():
    """Opt-in only, but makes no API call: real API tests record spend in the real ledger."""
    assert cost_controls.PROJECT_ROOT == settings.PROJECT_ROOT
    assert cost_controls._ledger_path() == settings.PROJECT_ROOT / get_setting("cost.usage_db_path")
