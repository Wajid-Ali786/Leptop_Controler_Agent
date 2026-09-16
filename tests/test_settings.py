"""
Tests for config/settings.py - the single source of truth for settings and secrets.
Every test points settings at temporary files; the real config.yaml and .env are never used.
"""
import ast
import os
from pathlib import Path

import pytest

from config import settings
from config.settings import MissingSettingError, SettingsError, get_setting

FAKE_KEY = "sk-test-not-a-real-key"


@pytest.fixture
def sources(tmp_path, monkeypatch):
    """Point settings at a temp config.yaml and .env; return a writer for each."""
    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / ".env"
    config_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_PATH", config_path)
    monkeypatch.setattr(settings, "ENV_PATH", env_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def write(config: str | None = None, env: str | None = None):
        if config is not None:
            config_path.write_text(config, encoding="utf-8")
        if env is not None:
            env_path.write_text(env, encoding="utf-8")

    return write


# --- Non-secret settings from config.yaml ---

def test_reads_top_level_setting_from_yaml(sources):
    sources(config="language: en\n")
    assert get_setting("language") == "en"


def test_reads_nested_setting_with_dotted_name(sources):
    sources(config="cost:\n  rate_limit: 30\n")
    assert get_setting("cost.rate_limit") == 30


def test_missing_yaml_setting_fails_clearly(sources):
    sources(config="language: en\n")
    with pytest.raises(MissingSettingError, match="'cost.rate_limit' is not defined"):
        get_setting("cost.rate_limit")


def test_empty_yaml_value_counts_as_missing(sources):
    sources(config="language:\n")
    with pytest.raises(MissingSettingError):
        get_setting("language")


def test_missing_config_file_fails_clearly(sources, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CONFIG_PATH", tmp_path / "nope.yaml")
    with pytest.raises(SettingsError, match="Config file not found"):
        get_setting("language")


def test_malformed_yaml_fails_clearly(sources):
    sources(config="language: [unclosed\n")
    with pytest.raises(SettingsError, match="not valid YAML"):
        get_setting("language")


def test_real_config_yaml_is_valid(monkeypatch):
    """The committed config/config.yaml parses (it holds headings only for now)."""
    monkeypatch.setattr(settings, "CONFIG_PATH", settings.PROJECT_ROOT / "config" / "config.yaml")
    assert isinstance(settings._load_config(), dict)


# --- Secrets from .env ---

def test_reads_api_key_from_env_file(sources):
    sources(env=f"ANTHROPIC_API_KEY={FAKE_KEY}\n")
    assert get_setting("ANTHROPIC_API_KEY") == FAKE_KEY


def test_missing_env_file_fails_clearly(sources):
    with pytest.raises(MissingSettingError, match="'ANTHROPIC_API_KEY' is missing"):
        get_setting("ANTHROPIC_API_KEY")


def test_empty_api_key_fails_clearly(sources):
    sources(env="ANTHROPIC_API_KEY=\n")
    with pytest.raises(MissingSettingError, match="'ANTHROPIC_API_KEY' is missing"):
        get_setting("ANTHROPIC_API_KEY")


def test_secret_is_never_read_from_yaml(sources):
    sources(config=f"ANTHROPIC_API_KEY: {FAKE_KEY}\n")
    with pytest.raises(MissingSettingError):
        get_setting("ANTHROPIC_API_KEY")


def test_secret_is_never_read_from_process_environment(sources, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    with pytest.raises(MissingSettingError):
        get_setting("ANTHROPIC_API_KEY")


def test_loading_env_file_does_not_leak_into_os_environ(sources):
    sources(env=f"ANTHROPIC_API_KEY={FAKE_KEY}\n")
    get_setting("ANTHROPIC_API_KEY")
    assert "ANTHROPIC_API_KEY" not in os.environ


# --- Rule: only config/settings.py reads config.yaml / .env ---

def _project_python_files():
    root = settings.PROJECT_ROOT
    files = list((root / "app").rglob("*.py")) + list((root / "config").rglob("*.py"))
    files += list((root / "scripts").rglob("*.py")) + [root / "main.py"]
    return [f for f in files if f != Path(settings.__file__).resolve()]


def test_no_other_module_reads_settings_sources_directly():
    offenders = []
    for path in _project_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            elif isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv"):
                names = [f"os.{node.attr}"]
            else:
                continue
            for name in names:
                if name.split(".")[0] in ("yaml", "dotenv") or name.startswith("os."):
                    offenders.append(f"{path.relative_to(settings.PROJECT_ROOT)}: {name}")
    assert offenders == [], f"Only config/settings.py may read config.yaml/.env: {offenders}"


def test_config_never_imports_from_app():
    """config/ is the bottom layer: app/ modules import from it, never the other way round."""
    root = settings.PROJECT_ROOT
    offenders = []
    for path in (root / "config").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            else:
                continue
            offenders += [f"{path.relative_to(root)}: {n}" for n in names if n.split(".")[0] == "app"]
    assert offenders == [], f"config/ must not import from app/: {offenders}"
