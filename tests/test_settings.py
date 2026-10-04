"""
Tests for config/settings.py - the single source of truth for settings and secrets.

Every test points settings at temporary files, EXCEPT the "shipped app list" section near the bottom,
which reads the committed config/config.yaml on purpose: a half-configured app (launchable but not
verifiable, or a path that has moved) can only be caught by checking the real file. Those tests read it
and launch nothing. The real .env is never used anywhere.
"""
import ast
import os
import re
import shutil
import sys
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
    """The committed config/config.yaml parses."""
    monkeypatch.setattr(settings, "CONFIG_PATH", settings.PROJECT_ROOT / "config" / "config.yaml")
    assert isinstance(settings._load_config(), dict)


# --- The shipped app list ------------------------------------------------------------------------------
# Opening an app needs BOTH halves: executor.apps says what may be launched, verifier.app_windows says
# what proves it opened. An entry with only one half fails at runtime - either "not a configured app" or
# "opening it can't be verified" - so the two maps agreeing is worth pinning rather than discovering.


@pytest.fixture
def real_config(monkeypatch):
    monkeypatch.setattr(settings, "CONFIG_PATH", settings.PROJECT_ROOT / "config" / "config.yaml")
    return settings


def test_every_configured_app_can_also_be_verified(real_config):
    apps = set(real_config.get_setting("executor.apps"))
    patterns = set(real_config.get_setting("verifier.app_windows"))
    assert apps == patterns, f"only in apps: {apps - patterns}; only in app_windows: {patterns - apps}"
    assert len(apps) >= 2


def test_every_window_pattern_is_a_valid_regular_expression(real_config):
    for name, pattern in real_config.get_setting("verifier.app_windows").items():
        assert isinstance(pattern, str) and pattern.strip(), name
        re.compile(pattern, re.IGNORECASE)


def test_no_configured_executable_is_a_batch_shim(real_config):
    """app/executor/adapter.py launches with no shell, and Windows cannot start a .cmd or .bat that way.
    A `code.cmd`-style shim resolves on PATH and then fails at launch, so it is refused here instead."""
    for name, executable in real_config.get_setting("executor.apps").items():
        assert not executable.lower().endswith((".cmd", ".bat")), f"{name} points at a shell script"


def test_no_configured_executable_carries_arguments(real_config):
    """The adapter runs Popen([path]) - one element, no command line - so an entry with a flag or a URL
    in it would be looked up as a single filename and never resolve."""
    for name, executable in real_config.get_setting("executor.apps").items():
        assert " -" not in executable and " /" not in executable, f"{name} looks like a command line"
        assert "&" not in executable and "|" not in executable, f"{name} looks like a shell command"


@pytest.mark.skipif(sys.platform != "win32", reason="the configured paths are this machine's")
def test_every_configured_executable_resolves_the_way_the_adapter_resolves_it(real_config):
    """shutil.which() is exactly what app/executor/adapter.launch_app() uses, so a typo in a path or an
    app that has moved is caught here rather than as a failed open. Nothing is launched."""
    unresolved = {name: executable
                  for name, executable in real_config.get_setting("executor.apps").items()
                  if shutil.which(executable) is None}
    assert unresolved == {}, f"configured but not found on this computer: {unresolved}"


def test_the_window_patterns_do_not_match_each_other(real_config):
    """Chrome, VS Code and other Electron apps share the window class Chrome_WidgetWin_1, so the TITLE is
    the only thing that tells them apart. Two patterns that matched the same window would let one app's
    open latch onto another app's window."""
    patterns = real_config.get_setting("verifier.app_windows")
    samples = {
        "notepad": "Untitled - Notepad",
        "calculator": "Calculator",
        "chrome": "New Tab - Google Chrome",
        "vscode": "config.yaml - Leptop_Controler_Agent - Visual Studio Code",
        "explorer": "File Explorer",
        "powershell": "Windows PowerShell",
    }
    for name, title in samples.items():
        if name not in patterns:
            continue
        matched = [other for other, pattern in patterns.items()
                   if re.compile(pattern, re.IGNORECASE).search(title)]
        assert matched == [name], f"{title!r} matches {matched}, not only {name!r}"


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

SETTINGS_LIBRARIES = ("yaml", "dotenv")


def _application_python_files():
    """Application code, which must take every setting from get_setting()."""
    root = settings.PROJECT_ROOT
    files = list((root / "app").rglob("*.py")) + list((root / "config").rglob("*.py")) + [root / "main.py"]
    return [f for f in files if f != Path(settings.__file__).resolve()]


def _settings_source_reads(path, *, include_environment: bool):
    """Names in `path` that read a settings source directly."""
    offenders = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        elif isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv"):
            names = [f"os.{node.attr}"]
        else:
            continue
        offenders += [n for n in names if n.split(".")[0] in SETTINGS_LIBRARIES
                      or (include_environment and n.startswith("os."))]
    return offenders


def test_application_code_never_reads_settings_sources_directly():
    offenders = [f"{path.relative_to(settings.PROJECT_ROOT)}: {name}"
                 for path in _application_python_files()
                 for name in _settings_source_reads(path, include_environment=True)]
    assert offenders == [], f"Only config/settings.py may read config.yaml/.env: {offenders}"


def test_dev_scripts_never_read_settings_sources_directly():
    """scripts/ may set environment variables for the subprocesses it launches, but must not
    read config.yaml or .env itself - settings still come from get_setting()."""
    scripts = (settings.PROJECT_ROOT / "scripts").rglob("*.py")
    offenders = [f"{path.relative_to(settings.PROJECT_ROOT)}: {name}"
                 for path in scripts
                 for name in _settings_source_reads(path, include_environment=False)]
    assert offenders == [], f"scripts/ must not read config.yaml/.env directly: {offenders}"


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
