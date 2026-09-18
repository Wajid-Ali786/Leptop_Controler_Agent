"""
Tests for app/executor/logic.py and adapter.py - the action pipeline with open_app
(emergency stop -> validate -> safety gate -> stop check -> adapter -> Verifier) and the
Action -> Result -> Recovery loop.

No real application or window is ever touched: launches are recorded and the desktop is faked
(a launch "opens" a fake window). The REAL safety gate and the REAL Verifier logic decide every
action - nothing bypasses them. (The Phase 0 emergency-stop tests live in tests/test_executor.py.)
"""
import ast
import re
import subprocess
from types import SimpleNamespace

import pytest

from app.executor import adapter, emergency_stop, logic
from app.executor.emergency_stop import EmergencyStopError
from app.executor.logic import execute, execute_with_recovery
from app.executor.models import OPEN_APP, ExecutorAction
from app.safety import logic as safety_logic
from app.safety.logic import ActionDeniedError
from app.verifier import adapter as verifier_adapter
from app.verifier import logic as verifier_logic
from app.verifier.models import WindowInfo
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
    "  max_attempts: 3\n"
    "verifier:\n"
    "  window_timeout_seconds: 0.2\n"
    "  poll_interval_seconds: 0.01\n"
    "  app_windows:\n"
    '    notepad: "Notepad$"\n'
    '    calculator: "^Calculator$"\n'
    '    deleter: "^Deleter$"\n'
)
WINDOW_TITLES = {"notepad.exe": "Untitled - Notepad", "calc.exe": "Calculator", "deleter.exe": "Deleter"}


def always(answer):
    return lambda *args: answer


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Temp config; the real safety gate wrapped in a recorder; launches recorded, never run;
    a fake desktop where a launch opens a window unless `window_appears(launch_number)` says no."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_PATH", config_path)
    calls = []
    desktop = SimpleNamespace(
        windows=[WindowInfo(1, "Claude Code Response.txt - Notepad")],  # the user's own, already open
        launches=0,
        window_appears=lambda launch_number: True,
    )
    real_authorize = safety_logic.authorize

    def recording_authorize(action, confirm=None):
        calls.append(("safety", action.description))
        return real_authorize(action, confirm)

    def recording_launch(executable):
        calls.append(("launch", executable))
        desktop.launches += 1
        if desktop.window_appears(desktop.launches):
            desktop.windows.append(WindowInfo(1000 + desktop.launches, WINDOW_TITLES[executable]))
        return 4242

    def forbidden_popen(*args, **kwargs):
        raise AssertionError("a real process must not be started in this test")

    monkeypatch.setattr(logic, "authorize", recording_authorize)
    monkeypatch.setattr(adapter, "launch_app", recording_launch)
    monkeypatch.setattr(verifier_adapter, "list_windows", lambda: list(desktop.windows))
    monkeypatch.setattr(subprocess, "Popen", forbidden_popen)
    emergency_stop.reset("test-setup")
    yield SimpleNamespace(calls=calls, config_path=config_path, desktop=desktop)
    emergency_stop.reset("test-teardown")


def open_app(name):
    return ExecutorAction(OPEN_APP, name)


def launches(world):
    return [call for call in world.calls if call[0] == "launch"]


# --- Happy path: safety, launch, then the Verifier confirms the window ---

def test_open_known_app_passes_safety_launches_and_is_verified(world):
    result = execute(open_app("notepad"))
    assert result.ok and not result.retryable
    assert re.fullmatch(r"Opened notepad; its window appeared after \d+\.\ds\.", result.message)
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


def test_app_that_cannot_be_verified_is_not_opened(world):
    world.config_path.write_text(CONFIG.replace('    calculator: "^Calculator$"\n', ""), encoding="utf-8")
    result = execute(open_app("calculator"))
    assert not result.ok and "No window title pattern for 'calculator'" in result.message
    assert world.calls == []


# --- App unavailable / permission denied: clear failure, no crash, no hang, no retry ---

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
    assert not result.ok and not result.retryable and result.message == str(error)
    assert world.calls == [("safety", "open app notepad"), ("launch", "notepad.exe")]


# --- Verifier: success only when a NEW window is observed ---

def test_window_that_never_appears_is_a_retryable_failure_not_false_success(world):
    world.desktop.window_appears = lambda n: False
    result = execute(open_app("notepad"))
    assert not result.ok and result.retryable
    assert result.message == "notepad was started, but no new window appeared within 0.2 seconds."
    assert world.calls == [("safety", "open app notepad"), ("launch", "notepad.exe")]


def test_already_open_notepad_window_is_not_mistaken_for_success(world):
    world.desktop.windows.append(WindowInfo(2, "Untitled - Notepad"))  # open before the action
    world.desktop.window_appears = lambda n: False
    assert not execute(open_app("notepad")).ok


def test_unobservable_desktop_means_nothing_is_launched(world, monkeypatch):
    def unavailable():
        raise verifier_adapter.VerifierAdapterError("checking windows is only supported on Windows")
    monkeypatch.setattr(verifier_adapter, "list_windows", unavailable)
    result = execute(open_app("notepad"))
    assert not result.ok and not result.retryable
    assert result.message.startswith("Didn't open notepad: I can't check whether its window appears")
    assert launches(world) == []


# --- Safety gate: every action, no bypass ---

def test_risky_action_without_confirmation_is_denied_and_nothing_launches(world):
    with pytest.raises(ActionDeniedError, match="no confirmation method"):
        execute(open_app("deleter"))
    assert world.calls == [("safety", "open app deleter")]


def test_risky_action_declined_by_the_user_launches_nothing(world):
    with pytest.raises(ActionDeniedError, match="did not confirm"):
        execute(open_app("deleter"), confirm=always(False))
    assert launches(world) == []


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


# --- Action -> Result -> Recovery ---

def test_failed_attempt_is_offered_for_retry_and_the_retry_succeeds(world):
    world.desktop.window_appears = lambda n: n >= 2
    offers = []
    result = execute_with_recovery(open_app("notepad"), offer_retry=lambda r: offers.append(r) or True)
    assert result.ok
    assert len(offers) == 1 and offers[0].retryable and "no new window appeared" in offers[0].message
    assert world.calls == [("safety", "open app notepad"), ("launch", "notepad.exe")] * 2


def test_without_a_retry_prompt_nothing_is_retried_silently(world):
    world.desktop.window_appears = lambda n: False
    result = execute_with_recovery(open_app("notepad"))
    assert not result.ok and result.message.endswith("Not retried.")
    assert len(launches(world)) == 1


@pytest.mark.parametrize("answer", [False, "yes", 1, None])
def test_retry_happens_only_on_an_explicit_yes(world, answer):
    world.desktop.window_appears = lambda n: False
    result = execute_with_recovery(open_app("notepad"), offer_retry=always(answer))
    assert not result.ok and result.message.endswith("Not retried.")
    assert len(launches(world)) == 1


def test_retries_stop_at_max_attempts(world):
    world.desktop.window_appears = lambda n: False
    offers = []
    result = execute_with_recovery(open_app("notepad"), offer_retry=lambda r: offers.append(r) or True)
    assert not result.ok and not result.retryable
    assert result.message.endswith("Gave up after 3 attempts.")
    assert len(launches(world)) == 3 and len(offers) == 2


def test_broken_retry_prompt_does_not_crash_or_retry(world):
    world.desktop.window_appears = lambda n: False

    def broken(result):
        raise RuntimeError("prompt window closed")
    result = execute_with_recovery(open_app("notepad"), offer_retry=broken)
    assert not result.ok and result.message.endswith("Not retried.")
    assert len(launches(world)) == 1


def test_non_retryable_failure_is_never_offered(world, monkeypatch):
    def not_installed(executable):
        world.calls.append(("launch", executable))
        raise adapter.AppNotFoundError("'notepad.exe' isn't installed or can't be found on this computer.")
    monkeypatch.setattr(adapter, "launch_app", not_installed)
    offers = []
    result = execute_with_recovery(open_app("notepad"), offer_retry=lambda r: offers.append(r) or True)
    assert not result.ok and offers == [] and len(launches(world)) == 1


def test_every_retry_passes_the_safety_gate_again(world):
    world.desktop.window_appears = lambda n: n >= 2
    confirmations = []
    result = execute_with_recovery(open_app("deleter"),
                                   confirm=lambda action, assessment: confirmations.append(action) or True,
                                   offer_retry=always(True))
    assert result.ok
    assert len(confirmations) == 2  # the risky action was confirmed again before the retry


def test_emergency_stop_during_the_retry_prompt_blocks_the_retry(world):
    world.desktop.window_appears = lambda n: False

    def stop_then_accept(result):
        emergency_stop.trigger("hotkey")
        return True
    with pytest.raises(EmergencyStopError):
        execute_with_recovery(open_app("notepad"), offer_retry=stop_then_accept)
    assert len(launches(world)) == 1


@pytest.mark.parametrize("value", ["0", "-1", "two", "true"])
def test_invalid_max_attempts_fails_cleanly(world, value):
    world.config_path.write_text(CONFIG.replace("max_attempts: 3", f"max_attempts: {value}"), encoding="utf-8")
    result = execute_with_recovery(open_app("notepad"))
    assert not result.ok and "executor.max_attempts" in result.message
    assert world.calls == []


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

def test_real_config_lists_openable_apps_and_retry_limit():
    assert logic._configured_apps()["notepad"] == "notepad.exe"
    assert logic._max_attempts() >= 1
    assert verifier_logic.expect_window("notepad").timeout_seconds > 0


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


@pytest.mark.parametrize("package, allowed", [
    # hotkey.py registers the global emergency-stop hotkey, which sends nothing and acts on nothing.
    # tests/test_executor_hotkey.py limits it to the adapter's five hotkey functions.
    ("app.executor", ("app/executor/logic.py", "app/executor/hotkey.py")),
    ("app.verifier", ("app/verifier/logic.py",)),
])
def test_only_each_modules_logic_uses_its_adapter(package, allowed):
    """Every real action must pass the safety gate in executor.execute(), and desktop reads go
    through verifier logic; nothing may call either adapter directly."""
    root = settings.PROJECT_ROOT
    allowed_paths = {root / name for name in allowed}
    offenders = [
        f"{path.relative_to(root)}: {module} {name}".strip()
        for path in _python_files() if path not in allowed_paths
        for module, name in _imports(path)
        if module == f"{package}.adapter" or (module == package and name == "adapter")
    ]
    assert offenders == [], f"Only {', '.join(allowed)} may use {package}.adapter: {offenders}"


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


def test_only_the_two_adapters_use_the_windows_api_through_ctypes():
    """The Verifier adapter reads the desktop; the Executor adapter sends close requests."""
    root = settings.PROJECT_ROOT
    allowed = {root / "app" / "verifier" / "adapter.py", root / "app" / "executor" / "adapter.py"}
    offenders = [f"{path.relative_to(root)}: {module}"
                 for path in (root / "app").rglob("*.py") if path not in allowed
                 for module, _ in _imports(path) if module.split(".")[0] == "ctypes"]
    assert offenders == [], f"Only the verifier and executor adapters may use ctypes: {offenders}"
