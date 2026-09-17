"""
Tests for window_control (app/executor/logic.py + adapter.request_window_state) - minimize, maximize,
restore and close the ACTIVE window - and for the single close mechanism it shares with close_app.

No real window is touched: the active window, its owning executable, its state and style, the open windows
and the requests are all faked, and real input is blocked. The REAL safety gate and the REAL Verifier logic
decide every action.
"""
import ast
import dataclasses
import inspect
import logging
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from app.executor import adapter, emergency_stop, logic
from app.executor.emergency_stop import EmergencyStopError
from app.executor.logic import execute, execute_with_recovery
from app.executor.models import CLOSE_APP, SHORTCUT, WINDOW_CONTROL, ExecutorAction, Outcome
from app.safety import logic as safety_logic
from app.safety.logic import ActionDeniedError
from app.safety.models import RiskLevel
from app.verifier import adapter as verifier_adapter
from app.verifier import logic as verifier_logic
from app.verifier.models import ActiveTarget, WindowInfo, WindowState
from config import settings

CONFIG = (
    "safety:\n"
    '  risky_keywords: [delete, shutdown, "shut down", send, close]\n'
    "  safe_words: [sender]\n"
    "executor:\n"
    "  apps:\n"
    "    notepad: notepad.exe\n"
    "  max_attempts: 3\n"
    "verifier:\n"
    "  window_timeout_seconds: 0.2\n"
    "  poll_interval_seconds: 0.01\n"
    "  window_state_settle_seconds: 0.05\n"
    "  app_windows:\n"
    '    notepad: "Notepad$"\n'
)
NOTEPAD = WindowInfo(500, "Untitled - Notepad", "Notepad")
NORMAL = WindowState(minimized=False, maximized=False, has_minimize_box=True, has_maximize_box=True,
                     tool_window=False, hung=False)
MAXIMIZED = dataclasses.replace(NORMAL, maximized=True)
MINIMIZED = dataclasses.replace(NORMAL, minimized=True)
CLOSE_PROMPT = ('close window "Untitled - Notepad" (notepad, opened by the assistant this session) - closing can '
                "lose unsaved work")


class FakeDesktop:
    """on_target_read[n] runs before the n-th active-window read. `reaction` is what the app does with a
    minimize/maximize/restore request: apply | ignore | gone | late | unreadable | denied | vanished."""

    def __init__(self, calls):
        self.calls = calls
        self.target = ActiveTarget(NOTEPAD, 501, "Edit")
        self.executables = {500: "notepad.exe", 900: "mail.exe"}
        self.states = {500: NORMAL, 900: NORMAL}
        self.windows = {500: NOTEPAD}
        self.target_reads = 0
        self.on_target_read = {}
        self.reaction = "apply"
        self.state_reads_after_request = 0
        self.state_error = None
        self.close_reaction = "close"

    def active_target(self):
        self.target_reads += 1
        self.on_target_read.get(self.target_reads, lambda: None)()
        return self.target

    def process_image_name(self, handle):
        return self.executables.get(handle)

    def window_state(self, handle):
        if self.state_error:
            raise self.state_error
        if self.reaction == "late" and self.pending:
            self.state_reads_after_request += 1
            if self.state_reads_after_request >= 3:
                self.states[handle] = self.pending
                self.pending = None
        return self.states.get(handle)

    pending = None

    def list_windows(self):
        return list(self.windows.values())

    def list_child_windows(self, handle):
        return []

    def request_window_state(self, handle, operation):
        self.calls.append(("request", operation, handle))
        target = {"minimize": MINIMIZED, "maximize": MAXIMIZED, "restore": NORMAL}[operation]
        target = dataclasses.replace(target, has_minimize_box=self.states[handle].has_minimize_box,
                                     has_maximize_box=self.states[handle].has_maximize_box)
        if self.reaction == "apply":
            self.states[handle] = target
        elif self.reaction == "gone":
            self.states[handle] = None
        elif self.reaction == "late":
            self.pending = target
        elif self.reaction == "unreadable":
            self.state_error = verifier_adapter.VerifierAdapterError("desktop locked")
        elif self.reaction == "denied":
            raise adapter.WindowControlError("it runs with administrator rights, and the assistant doesn't run elevated")
        elif self.reaction == "vanished":
            raise adapter.WindowGoneError("the window is already closed")

    def request_close(self, handle):
        self.calls.append(("close", handle))
        if self.close_reaction == "close":
            self.windows.pop(handle, None)
            self.states[handle] = None
        elif self.close_reaction == "dialog":
            self.windows[handle] = WindowInfo(handle, self.windows[handle].title, "Notepad", enabled=False)


@pytest.fixture
def world(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_PATH", config_path)
    calls = []
    desktop = FakeDesktop(calls)
    for name in ("active_target", "process_image_name", "window_state", "list_windows", "list_child_windows"):
        monkeypatch.setattr(verifier_adapter, name, getattr(desktop, name))
    monkeypatch.setattr(adapter, "request_window_state", desktop.request_window_state)
    monkeypatch.setattr(adapter, "request_close", desktop.request_close)

    def no_real_input():
        raise AssertionError("real input must not be sent in this test")
    monkeypatch.setattr(adapter, "_keyboard_api", no_real_input)
    authorized = []
    real_authorize = safety_logic.authorize
    monkeypatch.setattr(logic, "authorize",
                        lambda action, confirm=None: authorized.append(action) or real_authorize(action, confirm))
    emergency_stop.reset("test-setup")
    yield SimpleNamespace(calls=calls, desktop=desktop, config_path=config_path, authorized=authorized)
    emergency_stop.reset("test-teardown")


def approve(world, answer=True):
    def confirm(action, assessment):
        world.calls.append(("confirm", action.description, assessment.level, assessment.rule))
        return answer
    return confirm


def control(world, operation, answer=True):
    return execute(ExecutorAction(WINDOW_CONTROL, operation), approve(world, answer))


def requests(world):
    return [call for call in world.calls if call[0] in ("request", "close")]


def confirmations(world):
    return [call for call in world.calls if call[0] == "confirm"]


def in_session(world):
    logic._remember_opened("notepad", frozenset({NOTEPAD.handle}))


# --- Operations ---

@pytest.mark.parametrize("target, message", [
    ("", "Which window control? minimize, maximize, restore or close."),
    ("   ", "Which window control? minimize, maximize, restore or close."),
    ("shrink", "I can't do 'shrink' to a window. Window controls: minimize, maximize, restore or close."),
    ("minimise", "I can't do 'minimise' to a window. Window controls: minimize, maximize, restore or close."),
])
def test_unknown_operations_are_refused(world, target, message):
    result = control(world, target)
    assert not result.ok and result.message == message
    assert world.calls == [] and world.authorized == [] and world.desktop.target_reads == 0


# --- Minimize / maximize / restore: LOW, WM_SYSCOMMAND, done only on read-back ---

@pytest.mark.parametrize("operation, start, message", [
    ("minimize", NORMAL, "Minimized the window."),
    ("maximize", NORMAL, "Maximized the window."),
    ("restore", MAXIMIZED, "Restored the window."),
])
def test_state_changes_are_low_and_done_when_read_back(world, operation, start, message):
    world.desktop.states[500] = start
    result = control(world, f"  {operation.upper()} ")
    assert result.ok and result.verified and result.outcome is Outcome.DONE and result.message == message
    assert confirmations(world) == []
    assert [a.description for a in world.authorized] == [f"{operation} the active window"]
    assert safety_logic.assess(world.authorized[0]).level == RiskLevel.LOW
    assert requests(world) == [("request", operation, 500)]


@pytest.mark.parametrize("operation, state, word", [
    ("minimize", MINIMIZED, "minimized"), ("maximize", MAXIMIZED, "maximized"), ("restore", NORMAL, "restored"),
])
def test_already_in_the_requested_state_is_done_and_sends_nothing(world, operation, state, word):
    world.desktop.states[500] = state
    result = control(world, operation)
    assert result.outcome is Outcome.DONE and result.message == f"The window is already {word}, so I didn't change anything."
    assert requests(world) == []


def test_slow_app_is_given_time_to_change(world):
    world.desktop.reaction = "late"
    assert control(world, "maximize").outcome is Outcome.DONE


def test_state_that_doesnt_change_is_a_failure_not_success(world):
    world.desktop.reaction = "ignore"
    result = control(world, "maximize")
    assert not result.ok and result.outcome is Outcome.FAILED and not result.retryable
    assert result.message == "I asked the window to maximize, but it didn't maximize within 0.05 seconds."


def test_state_unreadable_afterwards_is_unverified(world):
    world.desktop.reaction = "unreadable"
    result = control(world, "minimize")
    assert result.ok and not result.verified and result.outcome is Outcome.UNVERIFIED
    assert result.message == "I asked the window to minimize, but I couldn't read its state afterwards."


def test_window_disappearing_during_the_action_is_a_failure(world):
    world.desktop.reaction = "gone"
    result = control(world, "minimize")
    assert result.outcome is Outcome.FAILED and result.message == "The window closed during the action."


@pytest.mark.parametrize("reaction, message", [
    ("vanished", "The window closed before I could maximize it."),
    ("denied", "I couldn't maximize the window: it runs with administrator rights, and the assistant doesn't run elevated."),
])
def test_request_refused_by_windows(world, reaction, message):
    world.desktop.reaction = reaction
    result = control(world, "maximize")
    assert not result.ok and result.outcome is Outcome.FAILED and result.message == message


# --- Refused before the safety gate ---

@pytest.mark.parametrize("setup, message", [
    (lambda d: setattr(d, "target", ActiveTarget(None)), "Didn't minimize anything: there's no active window."),
    (lambda d: setattr(d, "target", ActiveTarget(WindowInfo(1, "", "Progman"))),
     "The desktop or taskbar is active, so I didn't minimize anything."),
    (lambda d: setattr(d, "target", ActiveTarget(WindowInfo(2, "", "Shell_TrayWnd"))),
     "The desktop or taskbar is active, so I didn't minimize anything."),
    (lambda d: d.states.update({500: dataclasses.replace(NORMAL, tool_window=True)}),
     "The active window is a tool window, so I didn't minimize it."),
    (lambda d: d.states.update({500: dataclasses.replace(NORMAL, hung=True)}),
     "The active window isn't responding, so I didn't minimize it."),
    (lambda d: d.states.update({500: None}), "The active window closed before I could minimize it."),
    (lambda d: d.states.update({500: dataclasses.replace(NORMAL, has_minimize_box=False)}),
     "This window doesn't offer minimize, so I didn't change it."),
], ids=["no-window", "desktop", "taskbar", "tool-window", "hung", "gone", "no-minimize-box"])
def test_refusals_happen_before_the_safety_gate(world, setup, message):
    setup(world.desktop)
    result = control(world, "minimize")
    assert not result.ok and result.message == message
    assert world.authorized == [] and requests(world) == []


def test_no_maximize_box_refuses_only_maximize(world):
    world.desktop.states[500] = dataclasses.replace(NORMAL, has_maximize_box=False, maximized=False)
    assert control(world, "maximize").message == "This window doesn't offer maximize, so I didn't change it."
    assert control(world, "minimize").outcome is Outcome.DONE  # minimize is unaffected


def test_no_minimize_box_doesnt_block_maximize(world):
    world.desktop.states[500] = dataclasses.replace(NORMAL, has_minimize_box=False)
    assert control(world, "maximize").outcome is Outcome.DONE


def test_unobservable_desktop(world, monkeypatch):
    def unavailable():
        raise verifier_adapter.VerifierAdapterError("desktop locked")
    monkeypatch.setattr(verifier_adapter, "active_target", unavailable)
    assert control(world, "maximize").message == "Didn't maximize anything: I can't check the active window (desktop locked)."


# --- Identity: handle + executable + class, re-checked immediately before sending ---

@pytest.mark.parametrize("change", [
    lambda d: setattr(d, "target", ActiveTarget(WindowInfo(900, "Inbox", "Mail"), 901, "Edit")),
    lambda d: d.executables.update({500: "evil.exe"}),
    lambda d: setattr(d, "target", ActiveTarget(WindowInfo(500, "Untitled - Notepad", "OtherClass"), 501, "Edit")),
    lambda d: setattr(d, "target", ActiveTarget(None)),
], ids=["window", "executable", "class", "no-window"])
def test_identity_change_before_sending_sends_nothing(world, change):
    world.desktop.on_target_read[2] = lambda: change(world.desktop)
    result = control(world, "maximize")
    assert not result.ok and result.outcome is Outcome.FAILED
    assert result.message == "The active window changed, so I didn't maximize it."
    assert requests(world) == []


def test_title_change_alone_does_not_invalidate_the_target(world):
    world.desktop.on_target_read[2] = lambda: setattr(
        world.desktop, "target", ActiveTarget(WindowInfo(500, "*Untitled - Notepad", "Notepad"), 999, "Edit"))
    assert control(world, "maximize").outcome is Outcome.DONE


def test_window_closing_before_sending_sends_nothing(world):
    world.desktop.on_target_read[2] = lambda: world.desktop.states.update({500: None})
    result = control(world, "maximize")
    assert result.message == "The window closed before I could maximize it." and requests(world) == []


# --- Emergency stop ---

def test_emergency_stop_before_anything(world):
    emergency_stop.trigger("hotkey")
    with pytest.raises(EmergencyStopError):
        control(world, "minimize")
    assert world.calls == []


def test_stop_right_before_sending_sends_nothing(world):
    world.desktop.on_target_read[2] = lambda: emergency_stop.trigger("hotkey")
    with pytest.raises(EmergencyStopError):
        control(world, "maximize")
    assert requests(world) == []


def test_stop_while_checking_the_state(world, caplog):
    world.config_path.write_text(CONFIG.replace("window_state_settle_seconds: 0.05", "window_state_settle_seconds: 5"),
                                 encoding="utf-8")
    world.desktop.reaction = "ignore"
    threading.Timer(0.1, emergency_stop.trigger, args=("hotkey",)).start()
    started = time.monotonic()
    with pytest.raises(EmergencyStopError):
        control(world, "maximize")
    assert time.monotonic() - started < 2 and len(requests(world)) == 1
    assert "the request was already sent and can't be taken back" in caplog.text


# --- Never retried ---

@pytest.mark.parametrize("operation, setup", [
    ("maximize", lambda d: setattr(d, "reaction", "ignore")),
    ("maximize", lambda d: setattr(d, "reaction", "denied")),
    ("minimize", lambda d: setattr(d, "reaction", "unreadable")),
    ("minimize", lambda d: None),
    ("close", lambda d: setattr(d, "close_reaction", "ignore")),
], ids=["didnt-change", "denied", "unverified", "done", "close-still-open"])
def test_window_controls_are_never_retried(world, operation, setup):
    in_session(world)
    setup(world.desktop)
    offers = []
    result = execute_with_recovery(ExecutorAction(WINDOW_CONTROL, operation), approve(world),
                                   offer_retry=lambda r: offers.append(r) or True)
    assert offers == [] and not result.retryable and len(requests(world)) <= 1


# --- Close: session windows only, MEDIUM, one confirmation, the shared close mechanism ---

def test_close_a_session_window_is_medium_confirmed_once_and_closed(world):
    in_session(world)
    result = control(world, "close")
    assert result.ok and result.outcome is Outcome.DONE and result.message.startswith("Closed notepad after")
    assert [c[1:] for c in confirmations(world)] == [(CLOSE_PROMPT, RiskLevel.MEDIUM, "closing a window can lose unsaved work")]
    assert requests(world) == [("close", 500)]
    assert logic._session_windows["notepad"] == []


def test_close_refuses_a_window_the_assistant_did_not_open(world):
    result = control(world, "close")
    assert not result.ok and result.outcome is Outcome.FAILED
    assert result.message == ("I only close windows I opened in this session, and the active window isn't one of them, "
                              "so I left it alone.")
    assert world.authorized == [] and requests(world) == []


def test_close_ignores_minimize_and_maximize_capabilities(world):
    in_session(world)
    world.desktop.states[500] = dataclasses.replace(NORMAL, has_minimize_box=False, has_maximize_box=False)
    assert control(world, "close").outcome is Outcome.DONE


def test_close_declined_sends_nothing(world):
    in_session(world)
    with pytest.raises(ActionDeniedError, match="did not confirm"):
        control(world, "close", answer=False)
    assert requests(world) == []


def test_close_without_a_confirmation_method_is_denied(world):
    in_session(world)
    with pytest.raises(ActionDeniedError, match="closing a window can lose unsaved work"):
        execute(ExecutorAction(WINDOW_CONTROL, "close"))
    assert requests(world) == []


@pytest.mark.parametrize("kind, target", [(WINDOW_CONTROL, "close"), (CLOSE_APP, "notepad")])
def test_close_is_medium_even_without_the_close_keyword(world, kind, target):
    world.config_path.write_text(CONFIG.replace("send, close]", "send]"), encoding="utf-8")
    in_session(world)
    execute(ExecutorAction(kind, target), approve(world))
    assert [c[2] for c in confirmations(world)] == [RiskLevel.MEDIUM]


def test_close_minimum_is_a_floor_other_rules_can_raise(world, monkeypatch):
    in_session(world)
    real_assess = safety_logic.assess
    monkeypatch.setattr(safety_logic, "assess", lambda action: dataclasses.replace(
        real_assess(action), level=RiskLevel.HIGH, rule="a stricter future rule"))
    control(world, "close")
    assert confirmations(world)[0][2:] == (RiskLevel.HIGH, "a stricter future rule")


def test_close_identity_change_after_approval_closes_nothing(world):
    in_session(world)
    world.desktop.on_target_read[2] = lambda: world.desktop.executables.update({500: "other.exe"})
    result = control(world, "close")
    assert result.message == "The active window changed, so I didn't close it." and requests(world) == []


@pytest.mark.parametrize("close_reaction, outcome", [("dialog", Outcome.NEEDS_USER), ("ignore", Outcome.STILL_OPEN)])
def test_close_keeps_needs_user_and_still_open(world, close_reaction, outcome):
    in_session(world)
    world.desktop.close_reaction = close_reaction
    assert control(world, "close").outcome is outcome


def test_stop_right_before_the_close_request_sends_nothing(world):
    in_session(world)

    def confirm_then_stop_later(action, assessment):
        world.desktop.on_target_read[world.desktop.target_reads + 1] = lambda: emergency_stop.trigger("hotkey")
        return True
    with pytest.raises(EmergencyStopError):
        execute(ExecutorAction(WINDOW_CONTROL, "close"), confirm_then_stop_later)
    assert requests(world) == []


@pytest.mark.parametrize("kind, target", [(WINDOW_CONTROL, "close"), (CLOSE_APP, "notepad")])
def test_both_close_actions_use_the_one_close_mechanism_with_one_confirmation(world, monkeypatch, kind, target):
    in_session(world)
    used = []
    real_core = logic._close_session_group
    monkeypatch.setattr(logic, "_close_session_group", lambda action, session: used.append(session.app) or real_core(action, session))
    result = execute(ExecutorAction(kind, target), approve(world))
    assert result.outcome is Outcome.DONE and used == ["notepad"] and len(confirmations(world)) == 1


def test_close_core_refuses_a_group_this_session_did_not_open(world):
    """A future caller can't hand the close mechanism an unrelated window group."""
    forged = logic._SessionGroup("notepad", frozenset({500}), verifier_logic.expect_window("notepad"))
    result = logic._close_session_group(ExecutorAction(WINDOW_CONTROL, "close"), forged)
    assert not result.ok and result.message == "I only close windows I opened in this session, so I left it alone."
    assert requests(world) == []


def test_session_forgotten_during_confirmation_closes_nothing(world):
    in_session(world)
    result = execute(ExecutorAction(WINDOW_CONTROL, "close"),
                     lambda action, assessment: logic.forget_session_windows() or True)
    assert not result.ok and "I only close windows I opened in this session" in result.message
    assert requests(world) == []


def test_alt_f4_cant_be_a_second_close_path(world):
    in_session(world)
    result = execute(ExecutorAction(SHORTCUT, "alt+f4"), approve(world))
    assert not result.ok and "use close_app or window control close" in result.message
    assert world.calls == []


def _calls_in(function_node, name):
    return any((isinstance(n, ast.Attribute) and n.attr == name) or (isinstance(n, ast.Name) and n.id == name)
               for n in ast.walk(function_node))


def test_there_is_exactly_one_close_mechanism():
    """Only _request_close sends a close request; only _close_session_group uses it; only the two close actions
    reach _close_session_group - each after resolving a session group and passing execute()."""
    tree = ast.parse(inspect.getsource(logic))
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert sorted(f for f, node in functions.items() if _calls_in(node, "request_close")) == ["_request_close"]
    assert sorted(f for f, node in functions.items() if f != "_request_close" and _calls_in(node, "_request_close")) == \
        ["_close_session_group"]
    assert sorted(f for f, node in functions.items() if f != "_close_session_group"
                  and _calls_in(node, "_close_session_group")) == ["_prepare_close_app", "_prepare_window_control"]


# --- Privacy ---

TITLE = "Divorce-papers-zq4417.docx - Word"


def _collect(world, caplog, operation, answer=True):
    caplog.set_level(logging.DEBUG)
    world.desktop.windows = {500: WindowInfo(500, f"{TITLE} - Notepad", "Notepad")}
    world.desktop.target = ActiveTarget(world.desktop.windows[500], 501, "Edit")
    in_session(world)
    outputs = []
    try:
        result = control(world, operation, answer)
        outputs += [result, result.message]
    except ActionDeniedError as exc:
        outputs += [exc, exc.args, exc.assessment]
    outputs += [repr(a) for a in world.authorized] + [c[3] for c in confirmations(world)]
    return outputs + [caplog.text, *[r.getMessage() for r in caplog.records]]


def _no_leak(*things):
    for thing in things:
        assert TITLE not in str(thing) and TITLE not in repr(thing), "window title leaked"


@pytest.mark.parametrize("operation, answer", [("close", True), ("close", False), ("minimize", True)])
def test_window_titles_never_leak(world, caplog, operation, answer):
    _no_leak(*_collect(world, caplog, operation, answer))
    if operation == "close":  # proves the title was really in play: the on-screen prompt has it
        assert TITLE in confirmations(world)[0][1]


def test_the_leak_check_catches_a_planted_leak(world, caplog, monkeypatch):
    real_result = logic._result
    monkeypatch.setattr(logic, "_result", lambda action, ok, message, *a, **k:
                        real_result(action, ok, f"{message} {TITLE}", *a, **k))
    with pytest.raises(AssertionError, match="leaked"):
        _no_leak(*_collect(world, caplog, "close"))


# --- Adapter: WM_SYSCOMMAND requests (a fake PostMessageW - no real window touched) ---

@pytest.fixture
def fake_post(monkeypatch):
    posted, answer = [], {"result": 1, "error": 0}

    class FakeUser32:
        class PostMessageW:  # noqa: N801 - mirrors the Windows function the adapter configures
            argtypes = restype = None

            def __new__(cls, handle, message, wparam, lparam):
                posted.append((handle, message, wparam, lparam))
                return answer["result"]
    monkeypatch.setattr(adapter.sys, "platform", "win32")
    monkeypatch.setattr(adapter.ctypes, "WinDLL", lambda name, use_last_error=False: FakeUser32, raising=False)
    monkeypatch.setattr(adapter.ctypes, "get_last_error", lambda: answer["error"], raising=False)
    return SimpleNamespace(posted=posted, answer=answer)


@pytest.mark.parametrize("operation, command", [("minimize", 0xF020), ("maximize", 0xF030), ("restore", 0xF120)])
def test_adapter_posts_the_title_bar_system_command(fake_post, operation, command):
    adapter.request_window_state(123, operation)
    assert fake_post.posted == [(123, 0x0112, command, 0)]


@pytest.mark.parametrize("error, exception, message", [
    (1400, adapter.WindowGoneError, "the window is already closed"),
    (5, adapter.WindowControlError, "it runs with administrator rights"),
    (87, adapter.WindowControlError, "Windows refused the request (error 87)"),
])
def test_adapter_turns_windows_errors_into_clear_ones(fake_post, error, exception, message):
    fake_post.answer.update(result=0, error=error)
    with pytest.raises(exception, match=message.replace("(", r"\(").replace(")", r"\)")):
        adapter.request_window_state(123, "maximize")


def test_adapter_refuses_off_windows(monkeypatch):
    monkeypatch.setattr(adapter.sys, "platform", "linux")
    with pytest.raises(adapter.WindowControlError, match="only supported on Windows"):
        adapter.request_window_state(123, "minimize")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_real_window_state_read_is_read_only():
    target = verifier_adapter.active_target()
    if target.window is None:
        pytest.skip("no active window to read")
    state = verifier_adapter.window_state(target.window.handle)
    assert state is None or isinstance(state, WindowState)
    assert verifier_adapter.window_state(0) is None
