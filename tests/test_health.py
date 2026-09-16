"""
Tests for app/health.py and the main.py startup health check.
Offline only: every test points settings at temporary files; no API calls are made.
The real Claude connection check is tested through fake_claude (tests/conftest.py),
except one real-API test marked real_api (skipped unless RUN_REAL_CLAUDE_TEST=1).
"""
import sqlite3

import httpx2
import pytest

import main
from app import health
from config import settings
from tests.conftest import OK_BODY

FAKE_KEY = "sk-ant-test-not-a-real-key-12345"


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """Point settings at a temp .env; return a writer for its contents."""
    env_path = tmp_path / ".env"
    monkeypatch.setattr(settings, "ENV_PATH", env_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return lambda text: env_path.write_text(text, encoding="utf-8")


# --- check_api_key ---

def test_valid_key_passes(env_file):
    env_file(f"ANTHROPIC_API_KEY={FAKE_KEY}\n")
    result = health.check_api_key()
    assert result.ok
    assert FAKE_KEY not in result.message


def test_missing_env_file_fails_and_names_the_fix(env_file):
    result = health.check_api_key()
    assert not result.ok
    assert "ANTHROPIC_API_KEY" in result.message
    assert ".env.example" in result.message


def test_empty_key_fails(env_file):
    env_file("ANTHROPIC_API_KEY=\n")
    assert not health.check_api_key().ok


@pytest.mark.parametrize("placeholder", ["your-key-here", "  YOUR-KEY-HERE  ", "changeme"])
def test_placeholder_key_fails(env_file, placeholder):
    env_file(f"ANTHROPIC_API_KEY={placeholder}\n")
    result = health.check_api_key()
    assert not result.ok
    assert "placeholder" in result.message


def test_env_example_placeholder_is_detected():
    """The value shipped in .env.example must always count as a placeholder."""
    example = (settings.PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    value = example.split("ANTHROPIC_API_KEY=", 1)[1].splitlines()[0].strip()
    assert value.lower() in health.PLACEHOLDER_API_KEYS


def test_unreadable_env_file_fails_without_quoting_contents(env_file, tmp_path):
    # Invalid UTF-8 containing the key: the decode error must not leak file bytes.
    (tmp_path / ".env").write_bytes(b"ANTHROPIC_API_KEY=" + FAKE_KEY.encode() + b"\xff\xfe\n")
    result = health.check_api_key()
    assert not result.ok
    assert FAKE_KEY not in result.message


# --- report + main.py ---

def test_report_says_all_systems_ok_when_everything_passes(env_file):
    env_file(f"ANTHROPIC_API_KEY={FAKE_KEY}\n")
    report = health.format_report(health.run_health_check())
    assert "All systems OK." in report
    assert FAKE_KEY not in report


def test_report_names_the_failing_check(env_file):
    report = health.format_report(health.run_health_check())
    assert "[FAIL] API key" in report
    assert "problem(s) found" in report


def test_main_exits_zero_when_healthy(env_file, capsys):
    env_file(f"ANTHROPIC_API_KEY={FAKE_KEY}\n")
    assert main.main() == 0
    out = capsys.readouterr().out
    assert "All systems OK." in out
    assert FAKE_KEY not in out


def test_main_exits_one_with_clear_message_not_traceback(env_file, capsys):
    env_file("ANTHROPIC_API_KEY=your-key-here\n")
    assert main.main() == 1
    captured = capsys.readouterr()
    assert "[FAIL] API key" in captured.out
    assert "Traceback" not in captured.out + captured.err


# --- Real Claude connection check: explicit opt-in only ---
# fake_claude routes the real adapter + real cost controls to a mock transport.

def claude_result(results):
    return next(r for r in results if r.name == health.CLAUDE_CHECK_NAME)


def no_network(request):
    raise httpx2.ConnectError("network unreachable", request=request)


def test_claude_check_is_not_run_by_default(fake_claude):
    results = health.run_health_check()
    assert health.CLAUDE_CHECK_NAME not in [r.name for r in results]
    assert fake_claude.requests == []


def test_main_without_flag_never_contacts_claude(fake_claude, capsys):
    assert main.main([]) == 0
    assert fake_claude.requests == []
    assert health.CLAUDE_CHECK_NAME not in capsys.readouterr().out


def test_claude_check_success_reports_without_sensitive_data(fake_claude):
    body = {**OK_BODY, "content": [{"type": "text", "text": "PRIVATE-REPLY-TEXT"}]}
    fake_claude.respond = lambda request: httpx2.Response(200, json=body)
    results = health.run_health_check(check_claude=True)
    assert claude_result(results).ok
    assert "test-model" in claude_result(results).message
    report = health.format_report(results)
    assert "All systems OK." in report
    assert "PRIVATE-REPLY-TEXT" not in report
    assert FAKE_KEY not in report
    assert len(fake_claude.requests) == 1


def test_claude_check_is_recorded_by_cost_controls(fake_claude):
    health.run_health_check(check_claude=True)
    conn = sqlite3.connect(fake_claude.ledger_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM claude_requests").fetchone()[0] == 1
    finally:
        conn.close()


def test_claude_check_is_blocked_by_cost_controls(fake_claude):
    fake_claude.configure(daily_usd=0.0004)  # a ping could cost up to $0.000475
    result = claude_result(health.run_health_check(check_claude=True))
    assert not result.ok
    assert "Blocked by cost controls" in result.message
    assert "cost.daily_budget_usd" in result.message
    assert fake_claude.requests == []


def test_claude_check_auth_failure_is_reported_cleanly(fake_claude):
    fake_claude.respond = lambda request: httpx2.Response(
        401, json={"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})
    results = health.run_health_check(check_claude=True)
    assert not claude_result(results).ok
    assert "rejected the API key" in claude_result(results).message
    assert FAKE_KEY not in health.format_report(results)


def test_claude_check_network_failure_is_reported_cleanly(fake_claude):
    fake_claude.respond = no_network
    result = claude_result(health.run_health_check(check_claude=True))
    assert not result.ok
    assert "Can't reach Claude" in result.message


def test_claude_check_is_skipped_when_api_key_check_fails(fake_claude):
    fake_claude.env_path.write_text("ANTHROPIC_API_KEY=your-key-here\n", encoding="utf-8")
    result = claude_result(health.run_health_check(check_claude=True))
    assert not result.ok
    assert "Not run" in result.message
    assert fake_claude.requests == []


def test_unexpected_error_is_reported_without_details(fake_claude, monkeypatch):
    def broken():
        raise RuntimeError("internal detail that must not be shown")
    monkeypatch.setattr(health.adapter, "ping", broken)
    result = claude_result(health.run_health_check(check_claude=True))
    assert not result.ok
    assert result.message == "Unexpected error (RuntimeError)."


def test_main_with_flag_exits_zero_when_claude_reachable(fake_claude, capsys):
    assert main.main(["--check-claude"]) == 0
    assert f"[OK] {health.CLAUDE_CHECK_NAME}" in capsys.readouterr().out
    assert len(fake_claude.requests) == 1


def test_main_with_flag_exits_one_without_traceback_when_offline(fake_claude, capsys):
    fake_claude.respond = no_network
    assert main.main(["--check-claude"]) == 1
    captured = capsys.readouterr()
    assert f"[FAIL] {health.CLAUDE_CHECK_NAME}" in captured.out
    assert "Traceback" not in captured.out + captured.err


@pytest.mark.real_api
def test_real_claude_health_check():
    results = health.run_health_check(check_claude=True)
    assert all(r.ok for r in results), health.format_report(results)
