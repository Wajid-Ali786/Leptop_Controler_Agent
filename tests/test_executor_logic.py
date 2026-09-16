"""
Tests for app/executor/logic.py and adapter.py - Phase 1, task 1: the action pipeline with
open_app (emergency stop -> validate -> safety gate -> stop check -> adapter).

No real application is ever started: the adapter's launch is replaced by a recorder. The REAL
safety gate (with a temp config) decides every action - nothing bypasses it.
(The Phase 0 emergency-stop tests live in tests/test_executor.py.)
"""
import ast
import re
import subprocess
from types import SimpleNamespace

import pytest

from app.executor import adapter, emergency_stop, logic
from app.executor.emergency_stop import EmergencyStopError
from app.executor.logic import execute
from app.executor.models import OPEN_APP, ExecutorAction
from app.safety import logic as safety_logic
from app.safety.logic import ActionDeniedError
from config import settings

CONFIG = (
    "safety:\n"
    '  risky_keywords: [delete, shutdown, "shut down", send]\n'
    "  safe_words: [sender]\n"
    "executor:\n"
    "  apps:\n"
    "    notepad: notepad.exe\n"
    "    calculator: calc.exe\n"
    "    deleter: deleter.exe\n"  # a name the safety gate treats as risky
)


def always(answer):
    return lambda action, assessment: answer


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Temp config; the real safety gate wrapped in a recorder; launches recorded, never run."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_PATH", config_path)
    calls = []
    real_authorize = safety_logic.authorize

    def recording_authorize(action, confirm=None):
        calls.append(("safety", action.description))
        return real_authorize(action, confirm)

    def recording_launch(executable):
        calls.append(("launch", executable))
        return 4242

    monkeypatch.setattr(logic, "authorize", recording_authorize)
    monkeypatch.setattr(adapter, "launch_app", recording_launch)
    emergency_stop.reset("test-setup")
    yield SimpleNamespace(calls=calls, config_path=config_path)
    emergency_stop.reset("test-teardown")


def open_app(name):
    return ExecutorAction(OPEN_APP, name)


# --- Happy path ---

def test_open_known_app_passes_safety_then_launches(world):
    result = execute(open_app("notepad"))
    assert result.ok
    assert result.message == "Started notepad (notepad.exe, process 4242)."
    assert world.calls == [("safety", "open app notepad"), ("launch", "notepad.exe")]


def test_app_names_ignore_case_and_spaces(world):
    assert execute(open_app("  Calculator ")).ok
    assert world.calls[-1] == ("launch", "calc.exe")


# --- Wrong input / missing info: fail cleanly, ask nothing, launch nothing ---

def test_unknown_app_fails_cleanly(world):
    result = execute(open_app("nonexistentapp123"))
    assert not result.ok
    assert result.message == ("I don't know an app called 'nonexistentapp123'. "
                              "Apps I can open: calculator, deleter, notepad.")
    assert world.calls == []


@pytest.mark.parametrize("name", ["", "   "])
def test_missing_app_name_asks_which_app(world, name):
    result = execute(open_app(name))
    assert not result.ok and result.message == "Which app should I open?"
    assert world.calls == []


def test_unknown_action_kind_fails_cleanly(world):
    result = execute(ExecutorAction("format_disk", "C"))
    assert not result.ok and "don't know how to do 'format_disk'" in result.message
    assert world.calls == []


@pytest.mark.parametrize("bad_apps", ["notepad", "{}", "{notepad: 42}", "{'': notepad.exe}"])
def test_invalid_executor_config_fails_cleanly(world, bad_apps):
    world.config_path.write_text(CONFIG.split("executor:")[0] + f"executor:\n  apps: {bad_apps}\n",
                                 encoding="utf-8")
    result = execute(open_app("notepad"))
    assert not result.ok and "executor.apps" in result.message
    assert world.calls == []


# --- App unavailable / permission denied: clear failure, no crash, no hang ---

@pytest.mark.parametrize("error", [
    adapter.AppNotFoundError("'notepad.exe' isn't installed or can't be found on this computer."),
    adapter.AppLaunchError("'notepad.exe' needs administrator rights, and the assistant doesn't run elevated."),
])
def test_launch_failure_is_reported_cleanly(world, monkeypatch, error):
    def failing_launch(executable):
        world.calls.append(("launch", executable))
        raise error
    monkeypatch.setattr(adapter, "launch_app", failing_launch)
    result = execute(open_app("notepad"))
    assert not result.ok and result.message == str(error)
    assert world.calls == [("safety", "open app notepad"), ("launch", "notepad.exe")]


# --- Safety gate: every action, no bypass ---

def test_risky_action_without_confirmation_is_denied_and_nothing_launches(world):
    with pytest.raises(ActionDeniedError, match="no confirmation method"):
        execute(open_app("deleter"))
    assert world.calls == [("safety", "open app deleter")]


def test_risky_action_declined_by_the_user_launches_nothing(world):
    with pytest.raises(ActionDeniedError, match="did not confirm"):
        execute(open_app("deleter"), confirm=always(False))
    assert ("launch", "deleter.exe") not in world.calls


def test_risky_action_confirmed_by_the_user_launches(world):
    assert execute(open_app("deleter"), confirm=always(True)).ok
    assert world.calls == [("safety", "open app deleter"), ("launch", "deleter.exe")]


# --- Emergency stop ---

def test_emergency_stop_blocks_before_anything_happens(world):
    emergency_stop.trigger("hotkey")
    with pytest.raises(EmergencyStopError):
        execute(open_app("notepad"))
    assert world.calls == []


def test_stop_during_confirmation_prevents_the_action(world):
    def confirm_then_stop(action, assessment):
        emergency_stop.trigger("hotkey")  # user hits the stop while the prompt is open
        return True
    with pytest.raises(EmergencyStopError):
        execute(open_app("deleter"), confirm=confirm_then_stop)
    assert world.calls == [("safety", "open app deleter")]


# --- Adapter (no process is ever started) ---

@pytest.fixture
def no_popen(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("a real process must not be started in this test")
    monkeypatch.setattr(subprocess, "Popen", forbidden)


def test_adapter_reports_a_missing_executable_without_starting_anything(no_popen):
    with pytest.raises(adapter.AppNotFoundError, match="nonexistentapp123.exe"):
        adapter.launch_app("nonexistentapp123.exe")


def test_adapter_starts_the_resolved_path_without_a_shell(monkeypatch):
    seen = {}

    class FakeProcess:
        pid = 99

    def fake_popen(args, **kwargs):
        seen.update(args=args, kwargs=kwargs)
        return FakeProcess()

    monkeypatch.setattr(adapter.shutil, "which", lambda name: r"C:\Windows\System32\notepad.exe")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    assert adapter.launch_app("notepad.exe") == 99
    assert seen["args"] == [r"C:\Windows\System32\notepad.exe"]
    assert not seen["kwargs"].get("shell")


@pytest.mark.parametrize("error, message", [
    (OSError(22, "The requested operation requires elevation", None, 740), "needs administrator rights"),
    (PermissionError(13, "Access is denied"), "could not be started (PermissionError)"),
])
def test_adapter_turns_launch_errors_into_clear_messages(monkeypatch, error, message):
    def failing_popen(*args, **kwargs):
        raise error
    monkeypatch.setattr(adapter.shutil, "which", lambda name: r"C:\Tools\app.exe")
    monkeypatch.setattr(subprocess, "Popen", failing_popen)
    with pytest.raises(adapter.AppLaunchError, match=re.escape(message)) as info:
        adapter.launch_app("app.exe")
    assert info.value.__cause__ is None


# --- Real config + architecture rules ---

def test_real_config_lists_openable_apps():
    assert logic._configured_apps()["notepad"] == "notepad.exe"


def _imports(path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            yield from ((alias.name, "") for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            yield from ((node.module or "", alias.name) for alias in node.names)


def _python_files():
    root = settings.PROJECT_ROOT
    return [*(root / "app").rglob("*.py"), *(root / "config").rglob("*.py"),
            *(root / "scripts").rglob("*.py"), root / "main.py"]


def test_only_executor_logic_uses_the_executor_adapter():
    """Every real action must pass the safety gate in logic.execute(); nothing may call the adapter directly."""
    root = settings.PROJECT_ROOT
    allowed = root / "app" / "executor" / "logic.py"
    offenders = [
        f"{path.relative_to(root)}: {module} {name}".strip()
        for path in _python_files() if path != allowed
        for module, name in _imports(path)
        if module == "app.executor.adapter" or (module == "app.executor" and name == "adapter")
    ]
    assert offenders == [], f"Only app/executor/logic.py may use the executor adapter: {offenders}"


def test_only_executor_adapter_controls_the_computer():
    root = settings.PROJECT_ROOT
    allowed = root / "app" / "executor" / "adapter.py"
    controllers = ("subprocess", "pyautogui", "pywinauto", "playwright")
    offenders = [
        f"{path.relative_to(root)}: {module}"
        for path in (root / "app").rglob("*.py") if path != allowed
        for module, _ in _imports(path)
        if module.split(".")[0] in controllers
    ]
    assert offenders == [], f"Only app/executor/adapter.py may control the computer: {offenders}"
