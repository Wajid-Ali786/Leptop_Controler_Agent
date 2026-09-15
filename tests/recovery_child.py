"""
Child process for tests/test_recovery.py - a helper, NOT a test module.

Runs the real app code against a temp folder and, depending on the mode, kills itself
with os._exit(CRASH_EXIT) at an exact dangerous moment. os._exit skips all cleanup -
no finally blocks, no flushing, no closing - so on disk it looks like a power cut.

    python -m tests.recovery_child <mode> <tmp_dir>
"""
import logging
import os
import sqlite3
import sys
from pathlib import Path

CRASH_EXIT = 77
USAGE = {"input_tokens": 1000, "output_tokens": 100}  # $0.0075 at test-model prices


def point_app_at(tmp: Path) -> None:
    """Every settings/log/ledger path goes to the temp folder - never the real project data."""
    from app import logging_setup
    from config import settings
    settings.CONFIG_PATH = tmp / "config.yaml"
    settings.ENV_PATH = tmp / ".env"
    logging_setup.PROJECT_ROOT = tmp


def reply():
    from app.brain.models import ClaudeReply
    return ClaudeReply(text="ok", model="test-model", stop_reason="end_turn", **USAGE)


def complete_request(prompt: str = "hello") -> int:
    from app.brain import cost_controls
    request_id = cost_controls.authorize(model="test-model", prompt=prompt, max_tokens=100)
    cost_controls.record_usage(request_id, reply())
    return request_id


def crash_at_next_commit() -> None:
    """Die the moment SQLite starts the next COMMIT: the transaction's writes are done, not committed."""
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(lambda sql: os._exit(CRASH_EXIT) if sql.strip().upper() == "COMMIT" else None)
        return conn

    sqlite3.connect = connect


def crash_at_rename(n: int, *, before: bool) -> None:
    """Die just before or just after the n-th os.rename (RotatingFileHandler's rotation steps)."""
    real_rename = os.rename
    calls = [0]

    def rename(src, dst, *args, **kwargs):
        calls[0] += 1
        if calls[0] == n and before:
            os._exit(CRASH_EXIT)
        real_rename(src, dst, *args, **kwargs)
        if calls[0] == n:
            os._exit(CRASH_EXIT)

    os.rename = rename


def run(mode: str, tmp: Path) -> int:
    point_app_at(tmp)
    from app.brain import cost_controls

    if mode == "ledger-crash-during-authorize":
        complete_request()
        complete_request()
        crash_at_next_commit()
        cost_controls.authorize(model="test-model", prompt="doomed", max_tokens=100)

    elif mode == "ledger-crash-during-usage":
        complete_request()
        complete_request()
        request_id = cost_controls.authorize(model="test-model", prompt="sent", max_tokens=100)
        crash_at_next_commit()  # the request was "sent"; now die while recording its usage
        cost_controls.record_usage(request_id, reply())

    elif mode in ("log-crash-before-base-rename", "log-crash-after-base-rename"):
        from app import logging_setup
        logging_setup.setup_logging()
        # Rollover 1: companion.log -> .1 (rename 1). Rollover 2: .1 -> .2 (rename 2),
        # then companion.log -> .1 (rename 3) - the crash point.
        crash_at_rename(3, before=mode == "log-crash-before-base-rename")
        log = logging.getLogger("app.recovery")
        for i in range(100_000):
            log.info("line %05d", i)

    elif mode == "restart":
        import main as app_main
        exit_code = app_main.main([])
        complete_request("after restart")
        logging.getLogger("app.recovery").info("after restart")
        print(f"MAIN_EXIT={exit_code}")
        return 0

    else:
        raise SystemExit(f"unknown mode: {mode}")
    return 1  # reaching here means the crash never happened


if __name__ == "__main__":
    sys.exit(run(sys.argv[1], Path(sys.argv[2])))
