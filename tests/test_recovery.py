"""
Crash/restart recovery (docs/step4 Section 3 - Phase 0 "Recovery" test case:
restarting after a crash doesn't corrupt config or logs).

Each test runs the real app code in a child process (tests/recovery_child.py) against a
temp folder and kills it with os._exit() at a genuinely dangerous moment:
  - the usage ledger mid-transaction (writes done, COMMIT started but not finished)
  - the log file mid-rotation (between RotatingFileHandler's renames)
Each test first proves the crash really hit that moment (crash exit code, a leftover
SQLite hot journal, a half-rotated log folder). A fresh process then restarts the app
and the files are checked: config unchanged, logs readable and complete, ledger intact
with exactly the right rows and spend. No real project files, no API calls.
"""
import re
import sqlite3
import subprocess
import sys

import pytest

from app.brain.cost_controls import estimate_input_tokens
from config import settings
from tests.conftest import FAKE_KEY, config_text
from tests.recovery_child import CRASH_EXIT

REQUEST_COST = (1000 * 5.0 + 100 * 25.0) / 1_000_000  # recovery_child.USAGE at test-model prices
# Worst case reserved for the request that crashed mid-usage (prompt "sent", max_tokens=100).
RESERVED_COST = (estimate_input_tokens("sent") * 5.0 + 100 * 25.0) / 1_000_000
LOG_LINE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} [A-Z]+ +[\w.]+: .*$")
NUMBERED = re.compile(r"app\.recovery: line (\d{5})$")


@pytest.fixture
def app_dir(tmp_path):
    """A temp 'installation': config.yaml, .env, and (created on use) ledger + logs."""
    ledger = tmp_path / "claude_usage.db"
    (tmp_path / "config.yaml").write_text(
        config_text(ledger, rate_limit=100, daily_usd=100.0, monthly_usd=1000.0), encoding="utf-8")
    (tmp_path / ".env").write_text(f"ANTHROPIC_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    return tmp_path


def run_child(mode, tmp):
    return subprocess.run(
        [sys.executable, "-m", "tests.recovery_child", mode, str(tmp)],
        cwd=settings.PROJECT_ROOT, capture_output=True, text=True, timeout=120,
    )


def snapshot_config(tmp):
    return {name: (tmp / name).read_bytes() for name in ("config.yaml", ".env")}


def restart(tmp):
    result = run_child("restart", tmp)
    assert result.returncode == 0, result.stderr
    assert "MAIN_EXIT=0" in result.stdout and "All systems OK." in result.stdout
    return result


def read_ledger(tmp):
    conn = sqlite3.connect(tmp / "claude_usage.db")
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        rows = conn.execute(
            "SELECT id, input_tokens, output_tokens, cost_usd FROM claude_requests ORDER BY id").fetchall()
    finally:
        conn.close()
    return integrity, rows


def completed(request_id):
    return (request_id, 1000, 100, pytest.approx(REQUEST_COST))


# --- Usage ledger: killed mid-transaction ---

def test_ledger_survives_crash_mid_authorize_transaction(app_dir):
    config_before = snapshot_config(app_dir)
    crashed = run_child("ledger-crash-during-authorize", app_dir)
    assert crashed.returncode == CRASH_EXIT, crashed.stderr
    assert (app_dir / "claude_usage.db-journal").exists()  # proof: died mid-transaction (hot journal)

    restart(app_dir)  # the app itself opens the ledger first and must recover it

    integrity, rows = read_ledger(app_dir)
    assert integrity == "ok"
    assert not (app_dir / "claude_usage.db-journal").exists()  # rolled back, not left dangling
    # Requests 1-2 intact; the crashed authorize (never sent) left no trace; the restart's
    # request reuses id 3. A committed crash row would have made this 4 rows.
    assert rows == [completed(1), completed(2), completed(3)]
    assert sum(r[3] for r in rows) == pytest.approx(3 * REQUEST_COST)
    assert snapshot_config(app_dir) == config_before


def test_ledger_survives_crash_mid_usage_transaction(app_dir):
    config_before = snapshot_config(app_dir)
    crashed = run_child("ledger-crash-during-usage", app_dir)
    assert crashed.returncode == CRASH_EXIT, crashed.stderr
    assert (app_dir / "claude_usage.db-journal").exists()  # proof: died mid-transaction (hot journal)

    restart(app_dir)

    integrity, rows = read_ledger(app_dir)
    assert integrity == "ok"
    assert not (app_dir / "claude_usage.db-journal").exists()
    # Request 3 was authorized (committed) and "sent", but the crash hit while recording its
    # usage: the UPDATE rolled back as a whole - no half-written row. Its worst-case
    # reservation from authorize() stays counted (fail closed), nothing is lost or double-counted.
    assert rows == [completed(1), completed(2), (3, None, None, pytest.approx(RESERVED_COST)), completed(4)]
    assert sum(r[3] for r in rows) == pytest.approx(3 * REQUEST_COST + RESERVED_COST)
    assert snapshot_config(app_dir) == config_before


# --- Log file: killed mid-rotation ---

def read_logs(tmp):
    """(file names, all lines) - every file must decode as UTF-8 and every line be well-formed."""
    files = sorted((tmp / "logs").glob("*.log"))
    lines = []
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            assert LOG_LINE.match(line), f"malformed line in {path.name}: {line!r}"
            lines.append(line)
    return [p.name for p in files], lines


def numbered(lines):
    return sorted(int(m.group(1)) for line in lines if (m := NUMBERED.search(line)))


@pytest.mark.parametrize("mode, files_after_crash", [
    # Backup shift done (.1 -> .2), active file not yet moved: .1 is missing.
    ("log-crash-before-base-rename", ["companion.2.log", "companion.log"]),
    # Active file moved to .1, new one not yet created: companion.log is missing.
    ("log-crash-after-base-rename", ["companion.1.log", "companion.2.log"]),
])
def test_logs_survive_crash_mid_rotation(app_dir, mode, files_after_crash):
    config_path = app_dir / "config.yaml"
    config_path.write_text(config_path.read_text(encoding="utf-8").replace(
        "max_bytes: 100000", "max_bytes: 400"), encoding="utf-8")
    config_before = snapshot_config(app_dir)

    crashed = run_child(mode, app_dir)
    assert crashed.returncode == CRASH_EXIT, crashed.stderr
    names, lines = read_logs(app_dir)
    assert names == files_after_crash  # proof: died mid-rotation
    before = numbered(lines)
    assert before and before == list(range(len(before)))  # every line written so far, no gaps/dups
    assert snapshot_config(app_dir) == config_before

    # The restart logs well over 400 bytes (startup + every health check), so it rotates
    # again - starting from the half-rotated state. With only 2 backups that rotation would
    # (correctly) delete the pre-crash files inspected below, so keep more backups.
    config_path.write_text(config_path.read_text(encoding="utf-8").replace(
        "backup_count: 2", "backup_count: 10"), encoding="utf-8")
    config_before = snapshot_config(app_dir)

    restart(app_dir)

    names, lines = read_logs(app_dir)
    assert "companion.log" in names
    assert len(names) > len(files_after_crash)  # the restart rotated again, from the half-rotated state
    assert numbered(lines) == before  # nothing lost or duplicated by the restart
    assert any(line.endswith("main: Startup (check_claude=False)") for line in lines)
    active = (app_dir / "logs" / "companion.log").read_text(encoding="utf-8")
    assert active.rstrip().endswith("app.recovery: after restart")  # newest line in the active file
    assert snapshot_config(app_dir) == config_before
