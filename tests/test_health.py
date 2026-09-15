"""
Tests for config/health.py and the main.py startup health check.
Offline only: every test points settings at a temporary .env; no API calls are made.
"""
import pytest

import main
from config import health, settings

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
