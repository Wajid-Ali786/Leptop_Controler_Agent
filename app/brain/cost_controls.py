"""
Claude cost controls (docs/build-plan Section 5.5, CLAUDE.md rule 8).

authorize() runs before every Claude request. If any control fails it raises a
CostLimitError naming the limit and its current value, and the request is NOT
sent (and not retried):
  1. Token limit  - estimated input tokens, and the requested max_tokens
  2. Rate limit   - requests sent in the last 60 seconds
  3. Money budget - spend today / this month plus this request's worst-case cost

Each authorized request is written to a SQLite usage ledger under data/
(git-ignored), so the rate window and budgets survive restarts. The row starts out
holding the request's worst-case cost - the same figure the budget check used - as a
reservation:
  - record_usage() replaces it with the actual cost from Claude's reply;
  - release_reservation() zeroes it when the request certainly wasn't billed;
  - if the process dies in between, the worst case stays counted (fail closed).
If the ledger can't be used, requests are blocked (fail closed) - spend is never untracked.

check_limits(), model_prices() and check_ledger() let the offline health check validate
the same settings and ledger without sending or recording anything.

All limits and prices come from config/config.yaml via get_setting().
"""
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from app.brain.models import ClaudeReply
from config.settings import PROJECT_ROOT, SettingsError, get_setting

RATE_WINDOW_SECONDS = 60
# Deliberately conservative input-token estimate: over-counts English (~4 bytes per
# token) and roughly matches Urdu/Hindi script (~2 UTF-8 bytes per character).
BYTES_PER_TOKEN_ESTIMATE = 2

LIMIT_SETTINGS = (
    "cost.rate_limit_per_minute",
    "cost.max_input_tokens_per_request",
    "cost.max_output_tokens_per_request",
    "cost.daily_budget_usd",
    "cost.monthly_budget_usd",
)

_now = time.time  # replaced in tests to control the clock

_SCHEMA = """
CREATE TABLE IF NOT EXISTS claude_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at REAL NOT NULL,                  -- unix time the request was authorized
    model TEXT NOT NULL,
    max_tokens INTEGER NOT NULL,
    estimated_input_tokens INTEGER NOT NULL,
    input_tokens INTEGER,                   -- actual, from Claude's reply (NULL until recorded)
    output_tokens INTEGER,
    cost_usd REAL NOT NULL DEFAULT 0        -- worst-case reservation until recorded; 0 if released
)
"""


class CostLimitError(Exception):
    """A cost control blocked the request locally; it was NOT sent to Claude."""


class TokenLimitError(CostLimitError):
    """The request is larger than the per-request token limit."""


class RateLimitExceededError(CostLimitError):
    """Too many Claude requests in the last minute."""


class BudgetExceededError(CostLimitError):
    """The daily or monthly money budget would be exceeded."""


def estimate_input_tokens(prompt: str) -> int:
    return math.ceil(len(prompt.encode("utf-8")) / BYTES_PER_TOKEN_ESTIMATE)


def authorize(*, model: str, prompt: str, max_tokens: int) -> int:
    """Run all three controls; record the request with its worst-case cost reserved and
    return its id, or raise CostLimitError."""
    estimated_input = estimate_input_tokens(prompt)
    _check_token_limit(estimated_input, max_tokens)
    worst_case_usd = _cost(model, estimated_input, max_tokens)
    now = _now()
    with _ledger() as conn:
        _check_rate_limit(conn, now)
        _check_budgets(conn, now, worst_case_usd)
        cursor = conn.execute(
            "INSERT INTO claude_requests (sent_at, model, max_tokens, estimated_input_tokens, cost_usd) "
            "VALUES (?, ?, ?, ?, ?)",
            (now, model, max_tokens, estimated_input, worst_case_usd),  # the budget check's own figure
        )
        return cursor.lastrowid


def record_usage(request_id: int, reply: ClaudeReply) -> float:
    """Replace the request's reservation with its actual cost; return that cost in USD."""
    with _ledger() as conn:
        row = conn.execute("SELECT model FROM claude_requests WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            raise CostLimitError(f"Usage ledger has no record of request #{request_id}.")
        cost_usd = _cost(row[0], reply.input_tokens, reply.output_tokens)
        conn.execute(
            "UPDATE claude_requests SET input_tokens = ?, output_tokens = ?, cost_usd = ? WHERE id = ?",
            (reply.input_tokens, reply.output_tokens, cost_usd, request_id),
        )
    return cost_usd


def release_reservation(request_id: int) -> None:
    """Zero the reservation of a request that certainly wasn't billed. The row stays (it still
    counts toward the rate limit), and a cost already recorded by record_usage() is never touched."""
    with _ledger() as conn:
        conn.execute(
            "UPDATE claude_requests SET cost_usd = 0 WHERE id = ? AND input_tokens IS NULL",
            (request_id,),
        )


# --- Read-only checks for the offline health check ---------------------------------------

def check_limits() -> dict:
    """Validate the rate/token/budget settings exactly as authorize() will; return them by name."""
    return {name: _positive_number(name) for name in LIMIT_SETTINGS}


def model_prices(model: str) -> tuple[float, float]:
    """(input, output) USD per million tokens for `model`; CostLimitError if it has no valid price."""
    return _prices_for(model)


def check_ledger() -> str:
    """Confirm the usage ledger is usable, without creating, modifying, or leaving behind
    anything. Returns a short status; raises CostLimitError describing any problem."""
    path = _ledger_path()
    if not path.exists():
        nearest = next(p for p in path.parents if p.exists())
        if not nearest.is_dir() or not os.access(nearest, os.W_OK):
            raise CostLimitError(f"Usage ledger location {path} can't be created ({nearest} is not a "
                                 f"writable folder), so spend can't be tracked.")
        return f"No ledger yet at {path}; it will be created on the first Claude request."
    if not path.is_file():
        raise CostLimitError(f"Usage ledger {path} is not a file, so spend can't be tracked.")
    if not os.access(path, os.W_OK):
        raise CostLimitError(f"Usage ledger {path} is read-only, so spend can't be recorded.")
    if path.with_name(path.name + "-journal").exists():
        # Opening it would have to roll the transaction back (a write), so leave that to authorize().
        return (f"{path} has an interrupted transaction (e.g. from a crash); it will be rolled back "
                f"automatically on the next Claude request.")
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        try:
            integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'claude_requests'").fetchone()
            count = conn.execute("SELECT COUNT(*) FROM claude_requests").fetchone()[0] if has_table else 0
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise _ledger_error(path, exc) from None
    if integrity != "ok":
        raise CostLimitError(f"Usage ledger {path} failed its integrity check, so spend can't be trusted.")
    return f"{path} is readable ({count} request(s) recorded)."


# --- The three controls ---------------------------------------------------------------

def _check_token_limit(estimated_input: int, max_tokens: int) -> None:
    max_output = _positive_number("cost.max_output_tokens_per_request")
    if max_tokens > max_output:
        raise TokenLimitError(
            f"Token limit: this request asks for up to {max_tokens} output tokens; "
            f"the limit is {max_output:g} (cost.max_output_tokens_per_request). Request not sent."
        )
    max_input = _positive_number("cost.max_input_tokens_per_request")
    if estimated_input > max_input:
        raise TokenLimitError(
            f"Token limit: this request is estimated at {estimated_input} input tokens; "
            f"the limit is {max_input:g} (cost.max_input_tokens_per_request). Request not sent."
        )


def _check_rate_limit(conn: sqlite3.Connection, now: float) -> None:
    limit = _positive_number("cost.rate_limit_per_minute")
    count, oldest = conn.execute(
        "SELECT COUNT(*), MIN(sent_at) FROM claude_requests WHERE sent_at > ?",
        (now - RATE_WINDOW_SECONDS,),
    ).fetchone()
    if count >= limit:
        wait = max(1, math.ceil(oldest + RATE_WINDOW_SECONDS - now))
        raise RateLimitExceededError(
            f"Rate limit: {count} Claude requests in the last {RATE_WINDOW_SECONDS} seconds; "
            f"the limit is {limit:g} per minute (cost.rate_limit_per_minute). "
            f"Request not sent - try again in {wait}s."
        )


def _check_budgets(conn: sqlite3.Connection, now: float, worst_case_usd: float) -> None:
    """Spend includes reservations of requests still in flight or interrupted by a crash."""
    today = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    periods = (
        ("Daily", "today", today, "cost.daily_budget_usd"),
        ("Monthly", "this month", today.replace(day=1), "cost.monthly_budget_usd"),
    )
    for label, period, start, setting in periods:
        budget = _positive_number(setting)
        spent = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM claude_requests WHERE sent_at >= ?",
            (start.timestamp(),),
        ).fetchone()[0]
        if spent + worst_case_usd > budget:
            raise BudgetExceededError(
                f"{label} budget: ${spent:.4f} spent {period} of ${budget:.4f} ({setting}); "
                f"this request could cost up to ${worst_case_usd:.4f}. Request not sent."
            )


# --- Helpers ----------------------------------------------------------------------------

def _cost(model: str, input_tokens: int, output_tokens: int) -> float:
    input_price, output_price = _prices_for(model)
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


def _prices_for(model: str) -> tuple[float, float]:
    prices = get_setting("cost.prices_usd_per_million_tokens")
    entry = prices.get(model) if isinstance(prices, dict) else None
    if isinstance(entry, dict) and all(_is_number(entry.get(k)) and entry[k] >= 0 for k in ("input", "output")):
        return float(entry["input"]), float(entry["output"])
    raise CostLimitError(
        f"No valid price for model '{model}' in cost.prices_usd_per_million_tokens, "
        f"so the money budget can't be enforced. Request not sent."
    )


def _positive_number(name: str):
    value = get_setting(name)
    if not _is_number(value) or value <= 0:
        raise SettingsError(f"Setting '{name}' must be a positive number, got {value!r}.")
    return value


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _ledger_path() -> Path:
    path = Path(get_setting("cost.usage_db_path"))
    return path if path.is_absolute() else PROJECT_ROOT / path


@contextmanager
def _ledger():
    """One short transaction on the usage ledger. Any ledger failure blocks requests."""
    path = _ledger_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=5, isolation_level=None)
    except (OSError, sqlite3.Error) as exc:
        raise _ledger_error(path, exc) from None
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(_SCHEMA)
        yield conn
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        _rollback(conn)
        raise _ledger_error(path, exc) from None
    except BaseException:
        _rollback(conn)
        raise
    finally:
        conn.close()


def _rollback(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass


def _ledger_error(path: Path, exc: Exception) -> CostLimitError:
    return CostLimitError(
        f"Usage ledger {path} could not be used ({type(exc).__name__}), so spend can't be "
        f"tracked. Claude requests are blocked until it is fixed."
    )
