"""
Logging foundation (docs/step4 Section 3): one rotating log file under logs/, shared
by every module. Modules just do:  log = logging.getLogger(__name__)

- File, level and rotation come from config/config.yaml (logging.*) via get_setting().
- Rotated copies are named companion.1.log, companion.2.log, ... so they still match
  the logs/*.log git-ignore rule.
- A redaction filter scrubs API-key-like strings and the configured key itself from
  every record, as a safety net. Code must still never log secrets, prompts, or
  Claude's replies.
- Fail-soft: if the log file can't be set up, logging falls back to the console and
  the app keeps running - deliberately the opposite of cost_controls' fail-closed rule.
"""
import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config.settings import PROJECT_ROOT, SettingsError, get_setting

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
REDACTED = "[REDACTED]"
_API_KEY_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")
_MIN_SECRET_LENGTH = 8  # shorter "secrets" would redact ordinary words

_state = {"handlers": [], "previous_level": None}
log = logging.getLogger(__name__)


class RedactSecretsFilter(logging.Filter):
    """Safety net: scrub API-key-like strings and known secret values from every record."""

    def __init__(self, secrets=()):
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= _MIN_SECRET_LENGTH]

    def redact(self, text: str) -> str:
        text = _API_KEY_PATTERN.sub(REDACTED, text)
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # bad format args must not crash the caller
            message = str(record.msg)
        record.msg, record.args = self.redact(message), None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self.redact(record.stack_info)
        return True


def setup_logging() -> Path | None:
    """Install the shared log handler. Returns the log file path, or None if logging fell
    back to the console. Never raises - a logging problem must not stop the app."""
    reset_logging()
    level, log_path, problem = logging.INFO, None, None
    try:
        level, path, max_bytes, backup_count = _read_settings()
        handler = _rotating_file_handler(path, max_bytes, backup_count)
        log_path = path
    except Exception as exc:
        handler = logging.StreamHandler(sys.stderr)
        problem = f"{type(exc).__name__}: {exc}"
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(RedactSecretsFilter(_known_secrets()))

    root = logging.getLogger()
    _state["previous_level"] = root.level
    root.setLevel(level)
    root.addHandler(handler)
    _state["handlers"].append(handler)
    if problem:
        log.warning("File logging unavailable (%s); logging to the console instead.", problem)
    return log_path


def reset_logging() -> None:
    """Remove and close the handlers installed by setup_logging()."""
    root = logging.getLogger()
    for handler in _state["handlers"]:
        root.removeHandler(handler)
        handler.close()
    _state["handlers"].clear()
    if _state["previous_level"] is not None:
        root.setLevel(_state["previous_level"])
        _state["previous_level"] = None


def _read_settings():
    level_name = get_setting("logging.level")
    if not isinstance(level_name, str) or level_name.upper() not in LEVELS:
        raise SettingsError(f"Setting 'logging.level' must be one of {', '.join(LEVELS)}, got {level_name!r}.")
    max_bytes = _positive_int("logging.max_bytes")
    backup_count = _positive_int("logging.backup_count")
    path = Path(get_setting("logging.file"))
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return getattr(logging, level_name.upper()), path, max_bytes, backup_count


def _positive_int(name: str) -> int:
    value = get_setting(name)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise SettingsError(f"Setting '{name}' must be a positive whole number, got {value!r}.")
    return value


def _rotating_file_handler(path: Path, max_bytes: int, backup_count: int) -> RotatingFileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
    handler.namer = _rotated_name
    return handler


def _rotated_name(default_name: str) -> str:
    """companion.log.2 -> companion.2.log, so rotated files still match logs/*.log."""
    base, _, index = default_name.rpartition(".")
    path = Path(base)
    return str(path.with_name(f"{path.stem}.{index}{path.suffix}"))


def _known_secrets() -> list[str]:
    try:
        return [get_setting("ANTHROPIC_API_KEY")]
    except Exception:
        return []
