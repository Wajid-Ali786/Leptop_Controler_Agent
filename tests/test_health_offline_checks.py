"""
Tests for the offline health checks in app/health.py: cost limits, model price,
usage ledger (read-only), safety rules, and logging fallback.

No network, no API calls (fake_claude only records requests; these checks make none).
Every file lives under tmp_path - conftest's autouse fixture keeps data/ and logs/ untouched.
"""
import os
import stat

import pytest

import main
from app import health, logging_setup
from app.brain import adapter
from config import settings
from tests.conftest import FAKE_KEY

OFFLINE_CHECKS = ["API key", "Cost limits", "Model price", "Usage ledger", "Safety rules"]


def result(results, name):
    return next(r for r in results if r.name == name)


def check(name):
    return result(health.run_health_check(), name)


def rewrite_config(fake, old, new):
    text = fake.config_path.read_text(encoding="utf-8")
    assert old in text
    fake.config_path.write_text(text.replace(old, new), encoding="utf-8")


def ledger_family(fake):
    """Every file in the ledger's folder that belongs to it (db, -journal, -wal, ...)."""
    return sorted(p.name for p in fake.ledger_path.parent.iterdir() if p.name.startswith("claude_usage"))


def fingerprint(path):
    return path.read_bytes(), path.stat().st_mtime_ns


# --- Everything valid ---

def test_valid_configuration_passes_every_offline_check(fake_claude):
    results = health.run_health_check()
    assert [r.name for r in results] == OFFLINE_CHECKS
    assert all(r.ok for r in results), health.format_report(results)
    assert "All systems OK." in health.format_report(results)
    assert fake_claude.requests == []  # fully offline


def test_ok_messages_summarize_the_configuration(fake_claude):
    results = health.run_health_check()
    assert "rate 5/min; tokens per request 1000 in / 100 out" in result(results, "Cost limits").message
    assert "test-model: $5 per million input tokens" in result(results, "Model price").message
    assert "4 risky keyword(s): delete, shutdown, shut down, send" in result(results, "Safety rules").message


def test_every_problem_is_reported_at_once(fake_claude):
    text = fake_claude.config_path.read_text(encoding="utf-8")
    fake_claude.config_path.write_text(text.split("cost:\n")[0], encoding="utf-8")  # drop the cost section
    results = health.run_health_check()
    assert not result(results, "Cost limits").ok
    assert not result(results, "Model price").ok
    assert not result(results, "Usage ledger").ok
    assert result(results, "Safety rules").ok and result(results, "API key").ok
    assert "3 problem(s) found" in health.format_report(results)


# --- 1 & 3. Cost-control configuration: rate / token / budget values ---

@pytest.mark.parametrize("overrides, setting", [
    ({"rate_limit": 0}, "cost.rate_limit_per_minute"),
    ({"rate_limit": "abc"}, "cost.rate_limit_per_minute"),
    ({"max_input_tokens": -5}, "cost.max_input_tokens_per_request"),
    ({"max_output_tokens": 0}, "cost.max_output_tokens_per_request"),
    ({"daily_usd": -1}, "cost.daily_budget_usd"),
    ({"monthly_usd": "lots"}, "cost.monthly_budget_usd"),
])
def test_invalid_limit_is_reported(fake_claude, overrides, setting):
    fake_claude.configure(**overrides)
    r = check("Cost limits")
    assert not r.ok
    assert setting in r.message


def test_ping_larger_than_the_output_limit_is_reported(fake_claude):
    fake_claude.configure(max_output_tokens=10)  # the test ping asks for 16
    r = check("Cost limits")
    assert not r.ok
    assert "brain.ping_max_tokens" in r.message and "--check-claude would always be blocked" in r.message


# --- 2. Configured model has a known price ---

def test_unpriced_model_is_reported(fake_claude):
    fake_claude.configure(model="unpriced-model")
    r = check("Model price")
    assert not r.ok
    assert "'unpriced-model'" in r.message and "every Claude request would be blocked" in r.message


def test_missing_model_is_reported(fake_claude):
    fake_claude.configure(model=None)
    r = check("Model price")
    assert not r.ok and "brain.model" in r.message


# --- 4. Usage ledger is usable - checked read-only ---

def test_missing_ledger_is_fine_and_is_not_created(fake_claude):
    r = check("Usage ledger")
    assert r.ok and "created on the first Claude request" in r.message
    assert ledger_family(fake_claude) == []


def test_existing_ledger_is_checked_without_being_modified(fake_claude):
    adapter.ping()  # a real ledger with one request (mock network)
    before = fingerprint(fake_claude.ledger_path)
    r = check("Usage ledger")
    assert r.ok and "1 request(s) recorded" in r.message
    assert fingerprint(fake_claude.ledger_path) == before
    assert ledger_family(fake_claude) == ["claude_usage.db"]  # no journal or temp files left behind


def test_ledger_without_a_table_yet_is_fine(fake_claude):
    fake_claude.ledger_path.write_bytes(b"")  # what a blocked first request can leave behind
    r = check("Usage ledger")
    assert r.ok and "0 request(s) recorded" in r.message
    assert fake_claude.ledger_path.read_bytes() == b""


def test_corrupt_ledger_is_reported_and_left_untouched(fake_claude):
    junk = b"this is not a sqlite database " * 100
    fake_claude.ledger_path.write_bytes(junk)
    r = check("Usage ledger")
    assert not r.ok and "could not be used" in r.message
    assert fake_claude.ledger_path.read_bytes() == junk
    assert ledger_family(fake_claude) == ["claude_usage.db"]


def test_unwritable_ledger_location_is_reported(fake_claude, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the ledger folder should be", encoding="utf-8")
    rewrite_config(fake_claude, fake_claude.ledger_path.as_posix(), (blocker / "claude_usage.db").as_posix())
    r = check("Usage ledger")
    assert not r.ok and "not a writable folder" in r.message
    assert blocker.is_file()


def test_read_only_ledger_is_reported(fake_claude):
    adapter.ping()
    os.chmod(fake_claude.ledger_path, stat.S_IREAD)
    try:
        r = check("Usage ledger")
        assert not r.ok and "read-only" in r.message
    finally:
        os.chmod(fake_claude.ledger_path, stat.S_IREAD | stat.S_IWRITE)


def test_ledger_with_an_interrupted_transaction_is_reported_and_left_alone(fake_claude):
    adapter.ping()
    journal = fake_claude.ledger_path.with_name("claude_usage.db-journal")
    journal.write_bytes(b"stand-in for a hot journal")  # a real one: tests/test_recovery.py
    before = fingerprint(fake_claude.ledger_path)
    r = check("Usage ledger")
    assert r.ok and "rolled back automatically on the next Claude request" in r.message
    assert fingerprint(fake_claude.ledger_path) == before
    assert journal.read_bytes() == b"stand-in for a hot journal"


def test_health_check_with_the_real_config_never_touches_real_data(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text(f"ANTHROPIC_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    monkeypatch.setattr(settings, "ENV_PATH", env_path)
    real_data = settings.PROJECT_ROOT / "data"
    before = sorted(p.name for p in real_data.iterdir())
    results = health.run_health_check()  # real config.yaml; relative paths redirected to tmp_path
    assert all(r.ok for r in results), health.format_report(results)
    assert str(tmp_path) in result(results, "Usage ledger").message
    assert sorted(p.name for p in real_data.iterdir()) == before


# --- 5. Safety configuration ---

@pytest.mark.parametrize("old, new", [
    ('risky_keywords: [delete, shutdown, "shut down", send]', "risky_keywords: []"),
    ('risky_keywords: [delete, shutdown, "shut down", send]', 'risky_keywords: ["rm -rf"]'),
    ("  safe_words: [sender, senders]\n", ""),
    ("safety:\n", "not_safety:\n"),
], ids=["empty-keywords", "invalid-keyword", "no-safe-words", "no-safety-section"])
def test_invalid_safety_config_is_reported(fake_claude, old, new):
    rewrite_config(fake_claude, old, new)
    r = check("Safety rules")
    assert not r.ok
    assert "every action will require confirmation" in r.message


# --- Logging fallback ---

def test_logging_is_not_reported_before_it_is_set_up(fake_claude):
    assert "Logging" not in [r.name for r in health.run_health_check()]


def test_working_file_logging_is_reported(fake_claude):
    logging_setup.setup_logging()
    r = check("Logging")
    assert r.ok and "companion.log" in r.message


def test_console_fallback_is_reported_clearly(fake_claude, capsys):
    rewrite_config(fake_claude, "level: INFO", "level: LOUD")
    assert main.main([]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] Logging: File logging unavailable" in out
    assert "logging to the console only" in out
