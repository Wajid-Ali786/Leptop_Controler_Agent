"""
Tests for app/brain/cost_controls.py - rate limit, token limit, money budget -
exercised through the real adapter request path.

Offline only: fake_claude (tests/conftest.py) supplies a mock transport, a temp usage
ledger and a controllable clock. No real API calls. With the test config, a ping has
an estimated 15 input tokens and max_tokens=16, so its worst case is $0.000475.
"""
import sqlite3

import httpx2
import pytest

from app.brain import adapter, cost_controls
from app.brain.adapter import ClaudeUnavailableError
from app.brain.cost_controls import (
    BudgetExceededError, CostLimitError, RateLimitExceededError, TokenLimitError,
)
from config.settings import SettingsError, get_setting

DAY = 24 * 60 * 60


def ledger_rows(fake):
    """(model, input_tokens, output_tokens, cost_usd) for every recorded request.

    A blocked first request rolls back the whole transaction (schema included), so the
    file can exist without the table - that means nothing was recorded.
    """
    if not fake.ledger_path.exists():
        return []
    conn = sqlite3.connect(fake.ledger_path)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'claude_requests'").fetchone():
            return []
        return conn.execute(
            "SELECT model, input_tokens, output_tokens, cost_usd FROM claude_requests ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


# --- 1. Token limit ---

def test_output_token_limit_blocks_before_sending(fake_claude):
    with pytest.raises(TokenLimitError,
                       match=r"up to 101 output tokens; the limit is 100 \(cost.max_output_tokens_per_request\)"):
        adapter.send_message("hi", max_tokens=101)
    assert fake_claude.requests == []
    assert ledger_rows(fake_claude) == []


def test_input_token_limit_blocks_before_sending(fake_claude):
    with pytest.raises(TokenLimitError,
                       match=r"estimated at 1001 input tokens; the limit is 1000 \(cost.max_input_tokens_per_request\)"):
        adapter.send_message("x" * 2001, max_tokens=10)
    assert fake_claude.requests == []


def test_request_exactly_at_token_limits_is_sent(fake_claude):
    adapter.send_message("x" * 2000, max_tokens=100)
    assert len(fake_claude.requests) == 1


def test_input_estimate_is_conservative_for_non_latin_script():
    assert cost_controls.estimate_input_tokens("abcd") == 2
    assert cost_controls.estimate_input_tokens("سلام") == 4  # 4 Urdu letters = 8 UTF-8 bytes


# --- 2. Rate limit ---

def test_rate_limit_blocks_the_request_over_the_limit(fake_claude):
    for _ in range(5):
        adapter.ping()
    with pytest.raises(RateLimitExceededError,
                       match=r"5 Claude requests in the last 60 seconds; the limit is 5 per minute "
                             r"\(cost.rate_limit_per_minute\)"):
        adapter.ping()
    assert len(fake_claude.requests) == 5


def test_rate_limit_window_rolls_forward(fake_claude):
    for _ in range(5):
        adapter.ping()
    fake_claude.now += 30
    with pytest.raises(RateLimitExceededError, match="try again in 30s"):
        adapter.ping()
    fake_claude.now += 31
    adapter.ping()
    assert len(fake_claude.requests) == 6


def test_failed_requests_still_count_toward_rate_limit(fake_claude):
    fake_claude.respond = lambda request: httpx2.Response(
        500, json={"type": "error", "error": {"type": "api_error", "message": "boom"}})
    for _ in range(5):
        with pytest.raises(ClaudeUnavailableError):
            adapter.ping()
    with pytest.raises(RateLimitExceededError):
        adapter.ping()
    assert len(fake_claude.requests) == 5
    assert [row[3] for row in ledger_rows(fake_claude)] == [0] * 5  # failed requests cost nothing


# --- 3. Money budget ---

def test_actual_usage_and_cost_are_recorded(fake_claude):
    adapter.ping()  # 12 input x $5/M + 1 output x $25/M
    assert ledger_rows(fake_claude) == [("test-model", 12, 1, pytest.approx(0.000085))]


def test_daily_budget_blocks_once_spent(fake_claude):
    fake_claude.reply_with_usage(input_tokens=200_000, output_tokens=0)  # costs exactly $1.00
    adapter.ping()
    with pytest.raises(BudgetExceededError,
                       match=r"Daily budget: \$1\.0000 spent today of \$1\.0000 \(cost.daily_budget_usd\)"):
        adapter.ping()
    assert len(fake_claude.requests) == 1


def test_daily_budget_resets_the_next_day(fake_claude):
    fake_claude.reply_with_usage(input_tokens=200_000, output_tokens=0)
    adapter.ping()
    with pytest.raises(BudgetExceededError):
        adapter.ping()
    fake_claude.now += DAY
    adapter.ping()
    assert len(fake_claude.requests) == 2


def test_monthly_budget_blocks_across_days(fake_claude):
    fake_claude.configure(daily_usd=2.0, monthly_usd=3.0)
    fake_claude.reply_with_usage(input_tokens=200_000, output_tokens=0)  # $1.00 per request
    for _ in range(3):
        adapter.ping()
        fake_claude.now += DAY
    with pytest.raises(BudgetExceededError,
                       match=r"Monthly budget: \$3\.0000 spent this month of \$3\.0000 \(cost.monthly_budget_usd\)"):
        adapter.ping()
    assert len(fake_claude.requests) == 3


def test_worst_case_cost_blocks_before_any_spend(fake_claude):
    fake_claude.configure(daily_usd=0.0004)  # a ping could cost up to $0.000475
    with pytest.raises(BudgetExceededError, match=r"\$0\.0000 spent today .* could cost up to \$0\.0005"):
        adapter.ping()
    assert fake_claude.requests == []


def test_spend_persists_in_the_ledger_file(fake_claude):
    """Spend lives in the configured SQLite file, so it survives a restart."""
    fake_claude.reply_with_usage(input_tokens=200_000, output_tokens=0)
    adapter.ping()
    conn = sqlite3.connect(fake_claude.ledger_path)  # a fresh connection, as after a restart
    try:
        assert conn.execute("SELECT SUM(cost_usd) FROM claude_requests").fetchone()[0] == pytest.approx(1.0)
    finally:
        conn.close()
    with pytest.raises(BudgetExceededError):
        adapter.ping()


def test_unpriced_model_is_blocked(fake_claude):
    fake_claude.configure(model="unpriced-model")
    with pytest.raises(CostLimitError, match="No valid price for model 'unpriced-model'"):
        adapter.ping()
    assert fake_claude.requests == []


def test_unusable_ledger_blocks_requests(fake_claude):
    fake_claude.ledger_path.write_bytes(b"this is not a sqlite database " * 100)
    with pytest.raises(CostLimitError, match="could not be used"):
        adapter.ping()
    assert fake_claude.requests == []


@pytest.mark.parametrize("value", [0, -5, "abc"])
def test_invalid_limit_setting_fails_clearly(fake_claude, value):
    fake_claude.configure(rate_limit=value)
    with pytest.raises(SettingsError, match="cost.rate_limit_per_minute"):
        adapter.ping()
    assert fake_claude.requests == []


# --- Combined behavior ---

def test_request_passing_all_controls_is_sent_and_recorded(fake_claude):
    reply = adapter.ping()
    assert len(fake_claude.requests) == 1
    assert ledger_rows(fake_claude) == [("test-model", reply.input_tokens, reply.output_tokens, pytest.approx(0.000085))]


def _hit_token_limit(fake):
    adapter.send_message("hi", max_tokens=101)


def _hit_rate_limit(fake):
    for _ in range(5):
        adapter.ping()
    adapter.ping()


def _hit_budget(fake):
    fake.configure(daily_usd=0.0004)
    adapter.ping()


@pytest.mark.parametrize("trigger, setting", [
    (_hit_token_limit, "cost.max_output_tokens_per_request"),
    (_hit_rate_limit, "cost.rate_limit_per_minute"),
    (_hit_budget, "cost.daily_budget_usd"),
])
def test_blocked_request_is_never_sent_retried_or_recorded(fake_claude, trigger, setting):
    with pytest.raises(CostLimitError) as info:
        trigger(fake_claude)
    sent, rows = len(fake_claude.requests), len(ledger_rows(fake_claude))
    assert setting in str(info.value)          # names the limit that was hit
    assert "Request not sent" in str(info.value)
    with pytest.raises(CostLimitError):        # still blocked - nothing retried or queued
        adapter.send_message("hi", max_tokens=101) if trigger is _hit_token_limit else adapter.ping()
    assert len(fake_claude.requests) == sent   # the blocked requests never reached the API
    assert len(ledger_rows(fake_claude)) == rows


def test_real_config_has_valid_cost_controls():
    for name in ("cost.rate_limit_per_minute", "cost.max_input_tokens_per_request",
                 "cost.max_output_tokens_per_request", "cost.daily_budget_usd", "cost.monthly_budget_usd"):
        assert cost_controls._positive_number(name) > 0
    assert get_setting("brain.model") in get_setting("cost.prices_usd_per_million_tokens")
    assert get_setting("brain.ping_max_tokens") <= get_setting("cost.max_output_tokens_per_request")
    assert get_setting("cost.usage_db_path").startswith("data/")
