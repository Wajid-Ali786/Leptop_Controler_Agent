"""
The ONE file that reads config/config.yaml and .env and exposes get_setting(name).
Every module calls get_setting() - never os.environ or the YAML file directly
(docs/step3 Section 3).

- Secrets (SECRET_NAMES) come only from .env - never from config.yaml.
- Everything else comes from config.yaml; dotted names reach into sections,
  e.g. get_setting("cost.rate_limit").
- A missing or empty setting raises MissingSettingError - never a silent default.

Phase 9 swaps the .env source for Windows Credential Manager here, and only here.
"""
from pathlib import Path

import yaml
from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
ENV_PATH = PROJECT_ROOT / ".env"

SECRET_NAMES = frozenset({"ANTHROPIC_API_KEY"})


class SettingsError(Exception):
    """A settings source (config.yaml or .env) could not be read."""


class MissingSettingError(SettingsError):
    """A requested setting is not defined or is empty."""


def get_setting(name: str):
    """Return the value of a setting by name. Raises MissingSettingError if absent."""
    if name in SECRET_NAMES:
        return _get_secret(name)
    return _get_config_value(name)


def _get_secret(name: str) -> str:
    values = dotenv_values(ENV_PATH) if ENV_PATH.is_file() else {}
    value = (values.get(name) or "").strip()
    if not value:
        raise MissingSettingError(
            f"Required setting '{name}' is missing. Add it to {ENV_PATH} "
            f"(copy .env.example to .env and fill in the real value)."
        )
    return value


def _get_config_value(name: str):
    value = _load_config()
    for part in name.split("."):
        if not isinstance(value, dict) or part not in value:
            value = None
            break
        value = value[part]
    if value is None:
        raise MissingSettingError(f"Setting '{name}' is not defined in {CONFIG_PATH}.")
    return value


def _load_config() -> dict:
    if not CONFIG_PATH.is_file():
        raise SettingsError(f"Config file not found: {CONFIG_PATH}")
    try:
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SettingsError(f"Config file {CONFIG_PATH} is not valid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise SettingsError(f"Config file {CONFIG_PATH} must contain a mapping at the top level.")
    return data
