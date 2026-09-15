"""
Tests for app/logging_setup.py - rotating file logging, config-driven settings,
secret redaction, console fallback - and the log lines written by main.py, the
health check and the brain adapter.

All log files go under tmp_path (the autouse isolated_logging fixture in conftest.py);
the real logs/ folder is never touched. No real API calls.
"""
import fnmatch
import logging

import httpx2
import pytest

import main
from app import logging_setup
from app.brain import adapter
from app.brain.cost_controls import CostLimitError
from config import settings
from config.settings import get_setting
from tests.conftest import FAKE_KEY, OK_BODY


@pytest.fixture
def log_config(tmp_path, monkeypatch):
    """Point settings at a temp config.yaml with a logging section; return a rewriter."""
    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / ".env"
    monkeypatch.setattr(settings, "CONFIG_PATH", config_path)
    monkeypatch.setattr(settings, "ENV_PATH", env_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def write(file="logs/companion.log", level="INFO", max_bytes=10_000, backup_count=2, key=None):
        config_path.write_text(
            f'logging:\n  file: "{file}"\n  level: {level}\n'
            f"  max_bytes: {max_bytes}\n  backup_count: {backup_count}\n",
            encoding="utf-8",
        )
        if key:
            env_path.write_text(f"ANTHROPIC_API_KEY={key}\n", encoding="utf-8")

    write()
    return write


def read_log(tmp_path) -> str:
    return (tmp_path / "logs" / "companion.log").read_text(encoding="utf-8")


# --- Log file creation, level, rotation ---

def test_log_file_is_created_under_logs(log_config, tmp_path):
    path = logging_setup.setup_logging()
    logging.getLogger("app.example").info("hello from a module")
    assert path == tmp_path / "logs" / "companion.log"
    assert "INFO     app.example: hello from a module" in read_log(tmp_path)


def test_level_comes_from_config(log_config, tmp_path):
    log_config(level="WARNING")
    logging_setup.setup_logging()
    logging.getLogger("app.example").info("quiet detail")
    logging.getLogger("app.example").warning("important warning")
    text = read_log(tmp_path)
    assert "quiet detail" not in text
    assert "important warning" in text


def test_rotation_keeps_configured_number_of_files(log_config, tmp_path):
    log_config(max_bytes=300, backup_count=2)
    logging_setup.setup_logging()
    for i in range(60):
        logging.getLogger("app.example").info("line %03d %s", i, "x" * 40)
    names = sorted(p.name for p in (tmp_path / "logs").iterdir())
    assert names == ["companion.1.log", "companion.2.log", "companion.log"]
    assert all(fnmatch.fnmatch(n, "*.log") for n in names)  # all covered by the logs/*.log git-ignore rule
    assert "line 059" in read_log(tmp_path)  # newest entries stay in the active file


def test_setting_up_twice_does_not_duplicate_lines(log_config, tmp_path):
    logging_setup.setup_logging()
    logging_setup.setup_logging()
    logging.getLogger("app.example").info("only once")
    assert read_log(tmp_path).count("only once") == 1


def test_real_config_has_valid_logging_settings():
    level, path, max_bytes, backup_count = logging_setup._read_settings()
    assert level in (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL)
    assert max_bytes > 0 and backup_count > 0
    assert get_setting("logging.file").startswith("logs/")


# --- Sensitive-data protection ---

def test_api_key_pattern_is_redacted(log_config, tmp_path):
    logging_setup.setup_logging()
    logging.getLogger("app.example").info("oops, key is %s", FAKE_KEY)
    text = read_log(tmp_path)
    assert FAKE_KEY not in text
    assert "oops, key is [REDACTED]" in text


def test_configured_key_is_redacted_even_without_the_usual_prefix(log_config, tmp_path):
    log_config(key="plain-secret-value-9876")
    logging_setup.setup_logging()
    logging.getLogger("app.example").info("value: plain-secret-value-9876")
    assert "plain-secret-value-9876" not in read_log(tmp_path)


def test_secret_inside_an_exception_traceback_is_redacted(log_config, tmp_path):
    logging_setup.setup_logging()
    try:
        raise ValueError(f"request failed with header x-api-key: {FAKE_KEY}")
    except ValueError:
        logging.getLogger("app.example").exception("something broke")
    text = read_log(tmp_path)
    assert "ValueError" in text
    assert FAKE_KEY not in text


def test_bad_format_arguments_are_still_written(log_config, tmp_path):
    """Stdlib logging never raises to the caller on bad args (it reports via handleError);
    our handler goes further and still writes the line. pytest's own capture handler
    deliberately raises on bad args, so the record is handed to our handler directly."""
    logging_setup.setup_logging()
    record = logging.LogRecord("app.example", logging.INFO, __file__, 1,
                               "two placeholders %s %s", ("only-one",), None)
    logging_setup._state["handlers"][0].handle(record)
    assert "two placeholders" in read_log(tmp_path)


# --- Fail-soft: degrade to the console, never crash ---

def test_unwritable_log_location_falls_back_to_console(log_config, tmp_path, capsys):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the logs folder should be", encoding="utf-8")
    log_config(file=f"{blocker.as_posix()}/companion.log")
    assert logging_setup.setup_logging() is None
    logging.getLogger("app.example").warning("still running")
    err = capsys.readouterr().err
    assert "File logging unavailable" in err
    assert "still running" in err


@pytest.mark.parametrize("override, setting", [
    ({"level": "LOUD"}, "logging.level"),
    ({"max_bytes": 0}, "logging.max_bytes"),
    ({"backup_count": "many"}, "logging.backup_count"),
])
def test_invalid_setting_falls_back_to_console(log_config, capsys, override, setting):
    log_config(**override)
    assert logging_setup.setup_logging() is None
    assert setting in capsys.readouterr().err


def test_missing_logging_section_falls_back_to_console(log_config, tmp_path, capsys):
    (tmp_path / "config.yaml").write_text("brain:\n  model: x\n", encoding="utf-8")
    assert logging_setup.setup_logging() is None
    assert "logging.level" in capsys.readouterr().err


def test_fallback_console_still_redacts_secrets(log_config, capsys):
    log_config(level="LOUD")
    logging_setup.setup_logging()
    logging.getLogger("app.example").warning("key %s", FAKE_KEY)
    assert FAKE_KEY not in capsys.readouterr().err


# --- What the app actually logs ---

def test_main_logs_startup_health_results_and_exit(fake_claude):
    assert main.main([]) == 0
    text = fake_claude.log_path.read_text(encoding="utf-8")
    assert "main: Startup (check_claude=False)" in text
    assert "config.health: Health check API key: OK" in text
    assert "main: Exit code 0" in text


def test_claude_request_log_has_metadata_but_no_content_or_key(fake_claude):
    body = {**OK_BODY, "content": [{"type": "text", "text": "PRIVATE-REPLY-TEXT"}]}
    fake_claude.respond = lambda request: httpx2.Response(200, json=body)
    logging_setup.setup_logging()
    adapter.send_message("PRIVATE-PROMPT-TEXT", max_tokens=16)
    text = fake_claude.log_path.read_text(encoding="utf-8")
    assert "Claude request #1 sent: model=test-model max_tokens=16" in text
    assert "Claude request #1 done: input_tokens=12 output_tokens=1" in text
    assert "PRIVATE-PROMPT-TEXT" not in text
    assert "PRIVATE-REPLY-TEXT" not in text
    assert FAKE_KEY not in text


def test_blocked_and_failed_claude_requests_are_logged(fake_claude):
    logging_setup.setup_logging()
    fake_claude.respond = lambda request: httpx2.Response(
        401, json={"type": "error", "error": {"type": "authentication_error", "message": "bad key"}})
    with pytest.raises(adapter.ClaudeAuthError):
        adapter.ping()
    fake_claude.configure(daily_usd=0.0004)
    with pytest.raises(CostLimitError):
        adapter.ping()
    text = fake_claude.log_path.read_text(encoding="utf-8")
    assert "Claude request #1 failed (ClaudeAuthError)" in text
    assert "blocked by cost controls: Daily budget" in text
    assert FAKE_KEY not in text
