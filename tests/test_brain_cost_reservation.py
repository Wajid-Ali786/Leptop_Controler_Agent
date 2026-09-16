"""
Tests for the worst-case cost reservation in app/brain/cost_controls.py and its
settlement in app/brain/adapter.py.

authorize() reserves the request's worst-case cost (the budget check's own figure);
success replaces it with the actual cost; a failure that certainly wasn't billed
releases it; anything else - including a crash - leaves it counted (fail closed).

Offline only: fake_claude (tests/conftest.py). With the test config a ping's worst
case is 15 estimated input x $5/M + 16 output x $25/M = $0.000475, and a successful
ping costs 12 x $5/M + 1 x $25/M = $0.000085.
"""
import sqlite3

import httpx2
import pytest

from app.brain import adapter, cost_controls
from app.brain.adapter import ClaudeError, ClaudeUnavailableError
from app.brain.cost_controls import BudgetExceededError
from tests.conftest import OK_BODY

PING_WORST_CASE = 0.000475
PING_ACTUAL = 0.000085


def ledger_rows(fake):
    """(model, input_tokens, output_tokens, cost_usd) for every recorded request."""
    conn = sqlite3.connect(fake.ledger_path)
    try:
        return conn.execute(
            "SELECT model, input_tokens, output_tokens, cost_usd FROM claude_requests ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def total_spend(fake):
    return sum(row[3] for row in ledger_rows(fake))


def authorize_ping():
    return cost_controls.authorize(model="test-model", prompt=adapter.PING_PROMPT, max_tokens=16)


def error_status(status, error_type):
    return lambda request: httpx2.Response(
        status, json={"type": "error", "error": {"type": error_type, "message": "test"}})


def raise_transport(error_class):
    def respond(request):
        raise error_class("simulated", request=request)
    return respond


# --- The reservation itself ---

def test_authorize_reserves_the_budget_checks_own_worst_case(fake_claude):
    authorize_ping()
    assert ledger_rows(fake_claude) == [("test-model", None, None, pytest.approx(PING_WORST_CASE))]
    # The same figure the budget check quotes when it blocks:
    fake_claude.configure(daily_usd=0.0009)
    with pytest.raises(BudgetExceededError, match=r"could cost up to \$0\.0005"):
        authorize_ping()


# 1. Normal success: reservation replaced by the actual cost

def test_successful_request_replaces_reservation_with_actual_cost(fake_claude):
    adapter.ping()
    assert ledger_rows(fake_claude) == [("test-model", 12, 1, pytest.approx(PING_ACTUAL))]


# 2. Crash after the response, before usage is recorded

def test_crash_before_usage_is_recorded_keeps_worst_case_counted(fake_claude):
    """authorize() with no record_usage() is exactly what such a crash leaves behind
    (tests/test_recovery.py reproduces it with a real killed process)."""
    authorize_ping()
    assert total_spend(fake_claude) == pytest.approx(PING_WORST_CASE)


# 3. Budget checks use the reservation

def test_budget_check_counts_a_crashed_requests_reservation(fake_claude):
    fake_claude.configure(daily_usd=0.0009)  # room for one ping's worst case, not two
    authorize_ping()                          # never settled - as after a crash
    with pytest.raises(BudgetExceededError, match=r"Daily budget: \$0\.0005 spent today"):
        adapter.ping()
    assert fake_claude.requests == []


def test_budget_check_counts_requests_still_in_flight(fake_claude):
    fake_claude.configure(daily_usd=0.0009)
    outcomes = []

    def respond(request):  # while request 1 is pending, request 2 is checked against the budget
        try:
            authorize_ping()
            outcomes.append("allowed")
        except BudgetExceededError:
            outcomes.append("blocked")
        return httpx2.Response(200, json=OK_BODY)

    fake_claude.respond = respond
    adapter.ping()
    assert outcomes == ["blocked"]
    fake_claude.respond = lambda request: httpx2.Response(200, json=OK_BODY)
    adapter.ping()  # once request 1 settled at its actual cost, there's room again


# 4. No double-counting after a successful update

def test_no_double_counting_after_usage_is_recorded(fake_claude):
    reply = adapter.ping()
    cost_controls.record_usage(1, reply)      # recording again overwrites, never adds
    cost_controls.release_reservation(1)      # a recorded cost can't be released away
    assert ledger_rows(fake_claude) == [("test-model", 12, 1, pytest.approx(PING_ACTUAL))]
    assert total_spend(fake_claude) == pytest.approx(PING_ACTUAL)


# 5. Failed requests: released only when certainly not billed

@pytest.mark.parametrize("respond", [
    error_status(401, "authentication_error"),
    error_status(403, "permission_error"),
    error_status(404, "not_found_error"),
    error_status(400, "invalid_request_error"),
    error_status(429, "rate_limit_error"),
    error_status(500, "api_error"),
    error_status(529, "overloaded_error"),
    raise_transport(httpx2.ConnectError),    # network down: never reached Claude
    raise_transport(httpx2.ConnectTimeout),  # timed out connecting: never reached Claude
], ids=["401", "403", "404", "400", "429", "500", "529", "connect-error", "connect-timeout"])
def test_unbilled_failure_releases_the_reservation(fake_claude, respond):
    fake_claude.respond = respond
    with pytest.raises(ClaudeError):
        adapter.ping()
    assert ledger_rows(fake_claude) == [("test-model", None, None, 0.0)]  # row kept for the rate limit


def test_failing_retry_loop_does_not_consume_the_budget(fake_claude):
    fake_claude.configure(daily_usd=0.0009, rate_limit=10)  # room for one worst case at a time
    fake_claude.respond = raise_transport(httpx2.ConnectError)
    for _ in range(6):
        with pytest.raises(ClaudeUnavailableError):
            adapter.ping()
    assert total_spend(fake_claude) == 0
    fake_claude.respond = lambda request: httpx2.Response(200, json=OK_BODY)
    adapter.ping()  # still allowed: six failures consumed no budget


@pytest.mark.parametrize("respond", [
    raise_transport(httpx2.ReadTimeout),  # sent, then no answer in time: may have been billed
    raise_transport(httpx2.ReadError),    # connection lost mid-response: may have been billed
    lambda request: httpx2.Response(200, json={"unexpected": True}),  # billed, usage unreadable
], ids=["read-timeout", "lost-mid-response", "malformed-200"])
def test_possibly_billed_failure_keeps_the_reservation(fake_claude, respond):
    fake_claude.respond = respond
    with pytest.raises(ClaudeError):
        adapter.ping()
    assert ledger_rows(fake_claude) == [("test-model", None, None, pytest.approx(PING_WORST_CASE))]


def test_release_failure_keeps_reservation_and_surfaces_the_original_error(fake_claude, monkeypatch):
    def broken_release(request_id):
        raise cost_controls.CostLimitError("ledger unavailable")
    monkeypatch.setattr(cost_controls, "release_reservation", broken_release)
    fake_claude.respond = error_status(401, "authentication_error")
    with pytest.raises(adapter.ClaudeAuthError):
        adapter.ping()
    assert ledger_rows(fake_claude) == [("test-model", None, None, pytest.approx(PING_WORST_CASE))]


# --- Unchanged: rate and token limits ---

def test_rate_limit_still_counts_released_requests(fake_claude):
    fake_claude.respond = raise_transport(httpx2.ConnectError)
    for _ in range(5):
        with pytest.raises(ClaudeUnavailableError):
            adapter.ping()
    with pytest.raises(cost_controls.RateLimitExceededError):
        adapter.ping()
    assert len(fake_claude.requests) == 5


def test_token_limit_blocks_before_any_reservation(fake_claude):
    with pytest.raises(cost_controls.TokenLimitError):
        adapter.send_message("hi", max_tokens=101)
    assert not fake_claude.ledger_path.exists() or ledger_rows(fake_claude) == []
