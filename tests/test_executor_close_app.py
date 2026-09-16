"""
Tests for close_app (app/executor/logic.py + adapter.request_close) - closing ONLY windows the
assistant opened in this session, politely, and verifying they actually closed.

No real application or window is ever touched: launches and close requests are recorded, and the
desktop is faked. A fake close request does whatever the test says the app does - close, show a
"Save changes?" dialog, ignore it, or refuse. The REAL safety gate and the REAL Verifier logic
decide every action.
"""
import ast
import re
import subprocess
from types import SimpleNamespace

import pytest

from app.executor import adapter, emergency_stop, logic
from app.executor.emergency_stop import EmergencyStopError
from app.executor.logic import execute, execute_with_recovery
from app.executor.models import CLOSE_APP, OPEN_APP, ActionResult, ExecutorAction, Outcome
from app.safety import logic as safety_logic
from app.safety.logic import ActionDeniedError
from app.verifier import adapter as verifier_adapter
from app.verifier.models import WindowInfo
from config import settings

CONFIG = (
    "safety:\n"
    '  risky_keywords: [delete, shutdown, "shut down", send, close]\n'
    "  safe_words: [sender]\n"
    "executor:\n"
    "  apps:\n"
    "    notepad: notepad.exe\n"
    "    calculator: calc.exe\n"
    "    paint: mspaint.exe\n"
    "  max_attempts: 3\n"
    "verifier:\n"
    "  window_timeout_seconds: 0.2\n"
    "  poll_interval_seconds: 0.01\n"
    "  app_windows:\n"
    '    notepad: "Notepad$"\n'
    '    calculator: "^Calculator$"\n'
    '    paint: "Paint$"\n'
)
FRAME, CORE = "ApplicationFrameWindow", "Windows.UI.Core.CoreWindow"
USERS_NOTEPAD = WindowInfo(1, "Claude Code Response.txt - Notepad", "Notepad")


def yes(*args):
    return True


class FakeDesktop:
    """Windows keyed by handle. A launch opens the app's window(s); a close request runs the app's
    configured reaction. `after_polls` schedules a change a few desktop reads later."""

    def __init__(self, calls):
        self.calls = calls
        self.windows = {USERS_NOTEPAD.handle: USERS_NOTEPAD}  # the user's own, already open
        self.next_handle = 1000
        self.reaction = "close"  # close | dialog | ignore | gone | denied | blink
        self.scheduled = []
        self.error = None

    def list_windows(self):
        if self.error:
            raise self.error
        for item in list(self.scheduled):
            item[0] -= 1
            if item[0] <= 0:
                self.scheduled.remove(item)
                item[1]()
        return list(self.windows.values())

    def after_polls(self, polls, change):
        self.scheduled.append([polls, change])

    def add(self, title, class_name=""):
        self.next_handle += 1
        self.windows[self.next_handle] = WindowInfo(self.next_handle, title, class_name)
        return self.next_handle

    def launch(self, executable):
        self.calls.append(("launch", executable))
        if executable == "calc.exe":  # a Store app: outer frame + inner content window, same title
            self.add("Calculator", FRAME)
            self.add("Calculator", CORE)
        elif executable == "mspaint.exe":  # two same-titled windows, neither a Store frame
            self.add("Untitled - Paint", "MSPaintApp")
            self.add("Untitled - Paint", "MSPaintView")
        else:
            self.add("Untitled - Notepad", "Notepad")
        return 4242

    def request_close(self, handle):
        self.calls.append(("close", handle))
        if self.reaction == "gone":
            raise adapter.WindowGoneError("the window is already closed")
        if self.reaction == "denied":
            raise adapter.WindowCloseError("it runs with administrator rights, and the assistant doesn't run elevated")
        window = self.windows[handle]
        if self.reaction == "close":
            self.close_family(window)
        elif self.reaction == "dialog":
            self.windows[handle] = WindowInfo(handle, window.title, window.class_name, enabled=False)
        elif self.reaction == "blink":  # disabled for one read while shutting down, then gone
            self.windows[handle] = WindowInfo(handle, window.title, window.class_name, enabled=False)
            self.after_polls(2, lambda: self.close_family(window))

    def close_family(self, window):
        """Closing a Store frame closes its inner window too; any other window closes alone."""
        doomed = [h for h, w in self.windows.items()
                  if h == window.handle or (window.class_name == FRAME and w.class_name == CORE
                                            and w.title == window.title)]
        for handle in doomed:
            del self.windows[handle]

    def handles(self, title_part):
        return sorted(h for h, w in self.windows.items() if title_part in w.title)


@pytest.fixture
def world(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_PATH", config_path)
    calls = []
    desktop = FakeDesktop(calls)
    real_authorize = safety_logic.authorize

    def recording_authorize(action, confirm=None):
        calls.append(("safety", action.description))
        return real_authorize(action, confirm)

    def forbidden_popen(*args, **kwargs):
        raise AssertionError("a real process must not be started in this test")

    monkeypatch.setattr(logic, "authorize", recording_authorize)
    monkeypatch.setattr(adapter, "launch_app", desktop.launch)
    monkeypatch.setattr(adapter, "request_close", desktop.request_close)
    monkeypatch.setattr(verifier_adapter, "list_windows", desktop.list_windows)
    monkeypatch.setattr(subprocess, "Popen", forbidden_popen)
    emergency_stop.reset("test-setup")
    yield SimpleNamespace(calls=calls, config_path=config_path, desktop=desktop)
    emergency_stop.reset("test-teardown")


def open_app(name):
    result = execute(ExecutorAction(OPEN_APP, name))
    assert result.ok, result.message
    return result


def close_app(name, confirm=yes, **kwargs):
    return execute(ExecutorAction(CLOSE_APP, name), confirm, **kwargs)


def close_requests(world):
    return [call for call in world.calls if call[0] == "close"]


def calls_after_open(world):
    return world.calls[2:]  # skip ("safety", "open app ..."), ("launch", ...)


# --- Happy path: safety confirmation, a polite close request, then verified gone ---

def test_close_an_app_the_assistant_opened(world):
    open_app("notepad")
    handle = world.desktop.handles("Untitled - Notepad")[0]
    result = close_app("notepad")
    assert result.ok and result.outcome is Outcome.DONE and not result.retryable
    assert re.fullmatch(r"Closed notepad after \d+\.\ds\.", result.message)
    assert calls_after_open(world) == [("safety", "close app notepad"), ("close", handle)]
    assert world.desktop.handles("Untitled - Notepad") == []


def test_users_own_window_is_untouched_when_closing_the_assistants(world):
    open_app("notepad")
    assert close_app("  NotePad ").ok
    assert USERS_NOTEPAD.handle in world.desktop.windows
    assert all(handle != USERS_NOTEPAD.handle for _, handle in close_requests(world))


def test_most_recently_opened_window_closes_first(world):
    del world.desktop.windows[USERS_NOTEPAD.handle]
    open_app("notepad")
    open_app("notepad")
    first, second = world.desktop.handles("Untitled - Notepad")
    assert close_app("notepad").ok
    assert close_app("notepad").ok
    assert close_requests(world) == [("close", second), ("close", first)]
    third = close_app("notepad")
    assert third.ok and third.outcome is Outcome.ALREADY_CLOSED
    assert len(close_requests(world)) == 2


# --- Session-only scope: windows the assistant didn't open are never closed ---

def test_window_the_assistant_did_not_open_is_left_alone(world):
    result = close_app("notepad")
    assert not result.ok and result.outcome is Outcome.FAILED and not result.retryable
    assert result.message == ("I only close windows I opened in this session. 1 notepad window is open, "
                              "but I didn't open it, so I left it alone.")
    assert world.calls == []  # no confirmation prompt, no close request


def test_several_windows_the_assistant_did_not_open_are_left_alone(world):
    world.desktop.add("notes.txt - Notepad", "Notepad")
    result = close_app("notepad")
    assert not result.ok
    assert "2 notepad windows are open, but I didn't open them, so I left them alone." in result.message
    assert world.calls == []


def test_forgotten_session_windows_are_not_closed(world):
    open_app("notepad")
    logic.forget_session_windows()
    result = close_app("notepad")
    assert not result.ok and "I didn't open them" in result.message
    assert close_requests(world) == []


def test_window_that_appeared_too_late_to_verify_is_not_the_assistants(world, monkeypatch):
    def slow_launch(executable):
        world.calls.append(("launch", executable))  # nothing appears during verification
        return 4242
    monkeypatch.setattr(adapter, "launch_app", slow_launch)
    assert not execute(ExecutorAction(OPEN_APP, "notepad")).ok
    world.desktop.add("Untitled - Notepad", "Notepad")  # shows up after open_app gave up
    result = close_app("notepad")
    assert not result.ok and "I didn't open them" in result.message
    assert close_requests(world) == []


def test_reused_handle_now_belonging_to_another_window_is_not_closed(world):
    open_app("notepad")
    handle = world.desktop.handles("Untitled - Notepad")[0]
    world.desktop.windows[handle] = WindowInfo(handle, "Bank statement - Excel", "XLMAIN")  # Windows reused it
    result = close_app("notepad")
    assert not result.ok and "1 notepad window is open, but I didn't open it" in result.message  # the user's
    assert close_requests(world) == []
    assert handle in world.desktop.windows


# --- Already closed: success, nothing to do, nothing asked ---

def test_nothing_open_at_all_is_already_closed(world):
    del world.desktop.windows[USERS_NOTEPAD.handle]
    result = close_app("notepad")
    assert result.ok and result.outcome is Outcome.ALREADY_CLOSED
    assert result.message == "notepad is already closed."
    assert world.calls == []


def test_window_the_user_already_closed_is_already_closed(world):
    open_app("notepad")
    del world.desktop.windows[USERS_NOTEPAD.handle]
    world.desktop.close_family(world.desktop.windows[world.desktop.handles("Untitled - Notepad")[0]])
    result = close_app("notepad")
    assert result.ok and result.outcome is Outcome.ALREADY_CLOSED
    assert calls_after_open(world) == []  # no confirmation prompt


def test_window_closed_during_the_confirmation_prompt_is_already_closed(world):
    open_app("notepad")
    handle = world.desktop.handles("Untitled - Notepad")[0]

    def user_closes_it_while_asked(action, assessment):
        del world.desktop.windows[handle]
        return True
    result = close_app("notepad", confirm=user_closes_it_while_asked)
    assert result.ok and result.outcome is Outcome.ALREADY_CLOSED
    assert close_requests(world) == []


def test_window_gone_as_the_request_is_sent_is_verified_closed(world):
    open_app("notepad")
    handle = world.desktop.handles("Untitled - Notepad")[0]
    world.desktop.reaction = "gone"
    world.desktop.after_polls(3, lambda: world.desktop.windows.pop(handle))  # gone by the first verification read
    result = close_app("notepad")
    assert result.ok and result.outcome is Outcome.DONE


# --- Unsaved work: a distinct outcome, never retried, never forced ---

def test_save_dialog_is_reported_as_needing_the_user(world):
    open_app("notepad")
    handle = world.desktop.handles("Untitled - Notepad")[0]
    world.desktop.reaction = "dialog"
    result = close_app("notepad")
    assert not result.ok and result.outcome is Outcome.NEEDS_USER and not result.retryable
    assert result.message == ("notepad is showing a dialog (probably asking whether to save your changes), "
                              "so I left it open for you to decide.")
    assert close_requests(world) == [("close", handle)]  # asked once, nothing more
    assert handle in world.desktop.windows


def test_save_dialog_is_reported_without_waiting_for_the_timeout(world):
    world.config_path.write_text(CONFIG.replace("window_timeout_seconds: 0.2", "window_timeout_seconds: 30"),
                                 encoding="utf-8")
    open_app("notepad")
    world.desktop.reaction = "dialog"
    result = close_app("notepad")
    assert result.outcome is Outcome.NEEDS_USER  # returned after two polls, not 30 seconds


def test_save_dialog_is_never_offered_for_retry(world):
    open_app("notepad")
    world.desktop.reaction = "dialog"
    offers = []
    result = execute_with_recovery(ExecutorAction(CLOSE_APP, "notepad"), yes,
                                   offer_retry=lambda r: offers.append(r) or True)
    assert result.outcome is Outcome.NEEDS_USER
    assert offers == [] and len(close_requests(world)) == 1


def test_window_briefly_disabled_while_shutting_down_is_not_misreported(world):
    open_app("notepad")
    world.desktop.reaction = "blink"
    result = close_app("notepad")
    assert result.ok and result.outcome is Outcome.DONE


def test_window_that_stays_open_is_still_open_and_not_retryable(world):
    open_app("notepad")
    world.desktop.reaction = "ignore"
    offers = []
    result = execute_with_recovery(ExecutorAction(CLOSE_APP, "notepad"), yes,
                                   offer_retry=lambda r: offers.append(r) or True)
    assert not result.ok and result.outcome is Outcome.STILL_OPEN and not result.retryable
    assert result.message == "Asked notepad to close, but it's still open after 0.2 seconds. It may be waiting for you."
    assert offers == [] and len(close_requests(world)) == 1


def test_still_open_window_stays_the_assistants_to_close_later(world):
    open_app("notepad")
    world.desktop.reaction = "ignore"
    assert close_app("notepad").outcome is Outcome.STILL_OPEN
    world.desktop.reaction = "close"
    assert close_app("notepad").outcome is Outcome.DONE


# --- Calculator: one app, two same-titled windows ---

def test_calculator_closes_through_its_frame_window_and_both_windows_must_go(world):
    open_app("calculator")
    frame = next(h for h, w in world.desktop.windows.items() if w.class_name == FRAME)
    result = close_app("calculator")
    assert result.ok and result.outcome is Outcome.DONE
    assert close_requests(world) == [("close", frame)]  # only the frame is asked
    assert world.desktop.handles("Calculator") == []


def test_calculator_is_not_closed_while_its_inner_window_remains(world, monkeypatch):
    open_app("calculator")
    inner = next(h for h, w in world.desktop.windows.items() if w.class_name == CORE)

    def frame_only(handle):
        world.calls.append(("close", handle))
        del world.desktop.windows[handle]
    monkeypatch.setattr(adapter, "request_close", frame_only)
    result = close_app("calculator")
    assert not result.ok and result.outcome is Outcome.STILL_OPEN
    assert world.desktop.handles("Calculator") == [inner]


def test_window_group_without_a_frame_asks_every_window(world):
    open_app("paint")
    handles = world.desktop.handles("Paint")
    assert len(handles) == 2
    result = close_app("paint")
    assert result.ok
    assert sorted(h for _, h in close_requests(world)) == handles


# --- Safety gate: closing is Medium risk and always confirmed ---

def test_close_without_a_confirmation_method_is_denied(world):
    open_app("notepad")
    with pytest.raises(ActionDeniedError, match="risky keyword 'close'"):
        close_app("notepad", confirm=None)
    assert close_requests(world) == []


def test_close_declined_by_the_user_sends_nothing(world):
    open_app("notepad")
    with pytest.raises(ActionDeniedError, match="did not confirm"):
        close_app("notepad", confirm=lambda *args: False)
    assert close_requests(world) == []
    assert len(world.desktop.handles("Untitled - Notepad")) == 1


# --- Emergency stop ---

def test_emergency_stop_blocks_the_close(world):
    open_app("notepad")
    emergency_stop.trigger("hotkey")
    with pytest.raises(EmergencyStopError):
        close_app("notepad")
    assert calls_after_open(world) == []


def test_stop_during_confirmation_prevents_the_close_request(world):
    open_app("notepad")

    def confirm_then_stop(action, assessment):
        emergency_stop.trigger("hotkey")
        return True
    with pytest.raises(EmergencyStopError):
        close_app("notepad", confirm=confirm_then_stop)
    assert close_requests(world) == []


def test_stop_while_verifying_stops_waiting_and_says_the_request_was_sent(world, caplog):
    world.config_path.write_text(CONFIG.replace("window_timeout_seconds: 0.2", "window_timeout_seconds: 30"),
                                 encoding="utf-8")
    open_app("notepad")
    world.desktop.reaction = "ignore"
    world.desktop.after_polls(3, lambda: emergency_stop.trigger("hotkey"))  # after the request is sent
    with pytest.raises(EmergencyStopError):
        close_app("notepad")
    assert len(close_requests(world)) == 1
    assert "the close request was already sent and can't be taken back" in caplog.text


# --- Failures: clear, no crash, nothing forced ---

def test_close_request_refused_by_windows_is_a_clear_failure(world):
    open_app("notepad")
    world.desktop.reaction = "denied"
    result = close_app("notepad")
    assert not result.ok and result.outcome is Outcome.FAILED and not result.retryable
    assert result.message == ("I couldn't ask notepad to close: it runs with administrator rights, "
                              "and the assistant doesn't run elevated.")


def test_unobservable_desktop_closes_nothing_and_asks_nothing(world):
    open_app("notepad")
    world.desktop.error = verifier_adapter.VerifierAdapterError("checking windows is only supported on Windows")
    result = close_app("notepad")
    assert not result.ok and not result.retryable
    assert result.message.startswith("Didn't close notepad: I can't check its windows")
    assert calls_after_open(world) == []


def test_desktop_unobservable_while_verifying_is_a_failure_not_success(world):
    open_app("notepad")
    world.desktop.reaction = "ignore"
    world.desktop.after_polls(3, lambda: setattr(world.desktop, "error",
                                                 verifier_adapter.VerifierAdapterError("desktop locked")))
    result = close_app("notepad")
    assert not result.ok and result.outcome is Outcome.FAILED and not result.retryable
    assert result.message == "Asked notepad to close, but couldn't check whether it closed (desktop locked)."


@pytest.mark.parametrize("name, message", [
    ("", "Which app should I close?"),
    ("  ", "Which app should I close?"),
    ("explorer", "I don't know an app called 'explorer'. Apps I can close: calculator, notepad, paint."),
])
def test_missing_or_unknown_app_fails_cleanly(world, name, message):
    result = close_app(name)
    assert not result.ok and result.message == message
    assert world.calls == []


def test_app_whose_close_cannot_be_verified_is_not_closed(world):
    open_app("notepad")
    world.config_path.write_text(CONFIG.replace('    notepad: "Notepad$"\n', ""), encoding="utf-8")
    result = close_app("notepad")
    assert not result.ok and "No window title pattern for 'notepad'" in result.message
    assert calls_after_open(world) == []


# --- Result shape: distinct outcomes that can't contradict ok / retryable ---

def test_outcome_defaults_follow_ok():
    action = ExecutorAction(CLOSE_APP, "notepad")
    assert ActionResult(action, True, "done").outcome is Outcome.DONE
    assert ActionResult(action, False, "failed").outcome is Outcome.FAILED


@pytest.mark.parametrize("ok, retryable, outcome", [
    (True, False, Outcome.NEEDS_USER),
    (False, False, Outcome.ALREADY_CLOSED),
    (False, True, Outcome.NEEDS_USER),
    (False, True, Outcome.STILL_OPEN),
])
def test_contradictory_results_are_rejected(ok, retryable, outcome):
    with pytest.raises(ValueError):
        ActionResult(ExecutorAction(CLOSE_APP, "notepad"), ok, "x", retryable, outcome)


# --- Adapter: a polite WM_CLOSE request, with Windows errors turned into clear ones ---

class FakePostMessage:
    """Stands in for user32.PostMessageW (the adapter sets argtypes/restype on it)."""

    def __init__(self, result):
        self.result, self.posted = result, []

    def __call__(self, handle, message, wparam, lparam):
        self.posted.append((handle, message, wparam, lparam))
        return self.result


@pytest.fixture
def fake_user32(monkeypatch):
    def install(result, last_error=0):
        user32 = SimpleNamespace(PostMessageW=FakePostMessage(result))
        monkeypatch.setattr(adapter.sys, "platform", "win32")
        monkeypatch.setattr(adapter.ctypes, "WinDLL", lambda name, use_last_error=False: user32, raising=False)
        monkeypatch.setattr(adapter.ctypes, "get_last_error", lambda: last_error, raising=False)
        return user32
    return install


def test_adapter_sends_wm_close_to_the_window(fake_user32):
    user32 = fake_user32(result=1)
    adapter.request_close(777)
    assert user32.PostMessageW.posted == [(777, 0x0010, 0, 0)]


@pytest.mark.parametrize("last_error, error, message", [
    (1400, adapter.WindowGoneError, "the window is already closed"),
    (5, adapter.WindowCloseError, "it runs with administrator rights, and the assistant doesn't run elevated"),
    (87, adapter.WindowCloseError, "Windows refused the request (error 87)"),
])
def test_adapter_turns_windows_errors_into_clear_messages(fake_user32, last_error, error, message):
    fake_user32(result=0, last_error=last_error)
    with pytest.raises(error, match=re.escape(message)):
        adapter.request_close(777)


def test_adapter_refuses_off_windows(monkeypatch):
    monkeypatch.setattr(adapter.sys, "platform", "linux")
    with pytest.raises(adapter.WindowCloseError, match="only supported on Windows"):
        adapter.request_close(777)


# --- Architecture rule: nothing in the app can end a process ---

_PROCESS_ENDERS = {"kill", "terminate", "killpg", "TerminateProcess", "ExitProcess", "psutil"}


def test_no_code_path_can_force_terminate_a_process():
    root = settings.PROJECT_ROOT
    offenders = []
    for path in [*(root / "app").rglob("*.py"), root / "main.py"]:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Attribute):
                names.append(node.attr)
            elif isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names += [(getattr(node, "module", None) or "")] + [alias.name for alias in node.names]
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if re.search(r"taskkill|TerminateProcess|Stop-Process", node.value, re.IGNORECASE):
                    names.append(node.value)
            offenders += [f"{path.relative_to(root)}: {name}" for name in names
                          if name.split(".")[0] in _PROCESS_ENDERS or "taskkill" in name.lower()
                          or "TerminateProcess" in name or "Stop-Process" in name]
    assert offenders == [], f"Closing must be a polite request, never ending a process: {offenders}"
