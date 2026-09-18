"""
Tests for the typed-command entry point (app/console.py): the console loop, the confirmation prompt,
the focus hand-over and how a typed line reaches the Executor.

No real window is touched: the active window, the screens, the pointer and the requests are faked,
and real input is blocked. The REAL parser, the REAL Executor pipeline, the REAL safety gate and the
REAL Verifier logic decide everything else - the console only asks and reports.
"""
import ast
import dataclasses
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import main
from app import console
from app.console import CommandReply, FocusHandover, Status, handle_command, run_console
from app.executor import adapter, commands, emergency_stop, hotkey
from app.executor import logic as executor_logic
from app.executor.emergency_stop import ActionInterruptedError, EmergencyStopError
from app.executor.models import CLICK, CLOSE_APP, OPEN_APP, TYPE_TEXT, WINDOW_CONTROL, ActionResult, ExecutorAction, \
    Outcome
from app.safety import logic as safety_logic
from app.safety.models import Action, RiskAssessment, RiskLevel
from app.verifier import adapter as verifier_adapter
from app.verifier.models import ActiveTarget, Screen, WindowInfo, WindowState
from config import settings

SECRET = "hunter2-correct-horse-battery"
CONSOLE_WINDOW = WindowInfo(100, "Windows PowerShell", "ConsoleWindowClass")
NOTEPAD = WindowInfo(500, "Untitled - Notepad", "Notepad")
OTHER = WindowInfo(900, "Inbox - Mail", "MailWindow")
NORMAL = WindowState(minimized=False, maximized=False, has_minimize_box=True, has_maximize_box=True,
                     tool_window=False, hung=False)
MINIMIZED = dataclasses.replace(NORMAL, minimized=True)
SCREEN = Screen(0, 0, 1366, 768, primary=True)

CONFIG = (
    "safety:\n"
    '  risky_keywords: [delete, shutdown, send, close]\n'
    "  safe_words: [sender]\n"
    "console:\n"
    "  focus_handover_seconds: 0.5\n"
    "  focus_settle_seconds: 0.05\n"
    "  poll_interval_seconds: 0.01\n"
    "executor:\n"
    "  apps:\n"
    "    notepad: notepad.exe\n"
    "  max_attempts: 3\n"
    "  max_type_characters: 1000\n"
    "  typing_interval_seconds: 0\n"
    "verifier:\n"
    "  window_timeout_seconds: 0.3\n"
    "  poll_interval_seconds: 0.01\n"
    "  window_state_settle_seconds: 0.05\n"
    "  app_windows:\n"
    '    notepad: "Notepad$"\n'
)


class FakeDesktop:
    """The desktop the console watches and the Executor acts on. `front` is the window in front."""

    def __init__(self, calls):
        self.calls = calls
        self.front = CONSOLE_WINDOW
        self.held = []
        self.windows = {100: CONSOLE_WINDOW, 500: NOTEPAD, 900: OTHER}
        self.states = {100: NORMAL, 500: NORMAL, 900: NORMAL}
        self.executables = {100: "powershell.exe", 500: "notepad.exe", 900: "mail.exe"}
        self.pointer = (10, 10)
        self.error = None

    # --- read-only (Verifier) ---
    def active_target(self):
        if self.error:
            raise self.error
        return ActiveTarget(self.front, self.front.handle + 1 if self.front else None, "Edit")

    def modifier_keys_down(self):
        if self.error:
            raise self.error
        return list(self.held)

    def list_windows(self):
        return list(self.windows.values())

    def list_child_windows(self, handle):
        return []

    def window_state(self, handle):
        return self.states.get(handle)

    def process_image_name(self, handle):
        return self.executables.get(handle)

    def window_at(self, x, y):
        return self.front

    def cursor_position(self):
        return self.pointer

    def list_screens(self):
        return [SCREEN]

    # --- acting (Executor adapter) ---
    def request_window_state(self, handle, operation):
        self.calls.append(("request", operation, handle))
        self.states[handle] = MINIMIZED if operation == "minimize" else NORMAL

    def request_close(self, handle):
        self.calls.append(("close", handle))
        self.windows.pop(handle, None)
        self.states[handle] = None

    def read_text(self, handle, max_characters):
        return None  # nothing readable: typing comes back unverified, and no text is ever stored

    def send_character(self, character):
        self.calls.append(("typed", len(character)))  # the character itself is never recorded

    def click(self, x, y):
        self.calls.append(("click", x, y))
        self.pointer = (x, y)

    def launch_app(self, executable):
        self.calls.append(("launch", executable))
        self.windows[700] = WindowInfo(700, "Untitled - Notepad", "Notepad")
        self.states[700] = NORMAL
        self.executables[700] = "notepad.exe"
        return 1234


@pytest.fixture
def world(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_PATH", config_path)
    calls = []
    desktop = FakeDesktop(calls)
    for name in ("active_target", "modifier_keys_down", "list_windows", "list_child_windows", "window_state",
                 "process_image_name", "window_at", "cursor_position", "list_screens", "read_text"):
        monkeypatch.setattr(verifier_adapter, name, getattr(desktop, name))
    for name in ("request_window_state", "request_close", "click", "launch_app", "send_character"):
        monkeypatch.setattr(adapter, name, getattr(desktop, name))

    def no_real_input():
        raise AssertionError("real input must not be sent in this test")
    monkeypatch.setattr(adapter, "_keyboard_api", no_real_input)
    emergency_stop.reset("test-setup")
    yield SimpleNamespace(calls=calls, desktop=desktop, config_path=config_path)
    emergency_stop.reset("test-teardown")


class ScriptedConsole:
    """Stands in for the person at the keyboard: `answers` are read in order; output is collected."""

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.prompts = []
        self.written = []

    def read(self, prompt=""):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def write(self, text=""):
        self.written.append(str(text))

    @property
    def output(self):
        return "\n".join(self.written)


class ScriptedFocus:
    """A hand-over that doesn't wait: it records each call and answers with `problems` in order."""

    def __init__(self, problems=()):
        self.problems = list(problems)
        self.calls = []

    def note_console_window(self):
        self.calls.append("noted")

    def hand_over(self, prompt):
        self.calls.append(prompt)
        return self.problems.pop(0) if self.problems else None


def in_session(handle=500, app="notepad"):
    executor_logic._remember_opened(app, frozenset({handle}))


def requests(world):
    return [call for call in world.calls if call[0] in ("request", "close", "click", "launch")]


# --- Every typed command goes through the Executor, and nothing else does --------------------------

def test_a_parsed_command_is_run_through_execute_with_recovery(world, monkeypatch):
    seen = []
    monkeypatch.setattr(console, "execute_with_recovery",
                        lambda action, confirm=None, offer_retry=None:
                        seen.append(action) or ActionResult(action, True, "ok"))
    reply = handle_command("open notepad")
    assert seen == [ExecutorAction(OPEN_APP, "notepad")]
    assert reply.status is Status.RAN and reply.message == "ok" and reply.result.ok


@pytest.mark.parametrize("line", ["close", "minimize notepad", 'type "abc', "nonsense", "", "   "])
def test_a_refused_line_runs_nothing(world, monkeypatch, line):
    monkeypatch.setattr(console, "execute_with_recovery",
                        lambda *a, **k: pytest.fail("a refused line must not reach the Executor"))
    reply = handle_command(line)
    assert reply.status is Status.REFUSED and reply.action is None
    assert reply.message == commands.parse(line).message


def test_low_action_runs_with_no_prompt_and_is_verified(world):
    scripted = ScriptedConsole()
    reply = handle_command("minimize", confirm=console._confirm(scripted.read, scripted.write))
    assert reply.status is Status.RAN and reply.result.outcome is Outcome.DONE
    assert reply.message == "Minimized the window."
    assert requests(world) == [("request", "minimize", 100)]
    assert scripted.prompts == []  # LOW never asks


def test_medium_action_asks_once_and_runs_on_yes(world):
    in_session()
    world.desktop.front = NOTEPAD
    scripted = ScriptedConsole(["yes"])
    reply = handle_command("close window", confirm=console._confirm(scripted.read, scripted.write))
    assert reply.status is Status.RAN and reply.result.outcome is Outcome.DONE
    assert len(scripted.prompts) == 1
    assert "MEDIUM risk" in scripted.output and "closing can lose unsaved work" in scripted.output
    assert requests(world) == [("close", 500)]


def test_a_denied_action_runs_nothing(world):
    in_session()
    world.desktop.front = NOTEPAD
    scripted = ScriptedConsole(["no"])
    reply = handle_command("close window", confirm=console._confirm(scripted.read, scripted.write))
    assert reply.status is Status.DENIED and "MEDIUM risk" in reply.message
    assert requests(world) == []


def test_without_a_confirmation_method_a_medium_action_is_denied(world):
    in_session()
    world.desktop.front = NOTEPAD
    reply = handle_command("close window")
    assert reply.status is Status.DENIED and "no confirmation method is available" in reply.message
    assert requests(world) == []


def test_the_executors_own_refusal_comes_back_as_a_result(world):
    reply = handle_command("open")
    assert reply.status is Status.RAN and not reply.result.ok
    assert reply.message == "Which app should I open?"
    assert requests(world) == []


def test_an_unknown_app_is_refused_by_the_executor_not_the_parser(world):
    reply = handle_command("open nonexistentapp123")
    assert reply.status is Status.RAN and not reply.result.ok
    assert "I don't know an app called 'nonexistentapp123'" in reply.message


# --- Confirmation: only the exact answer -----------------------------------------------------------

@pytest.mark.parametrize("answer, allowed", [
    ("yes", True), ("YES", True), ("Yes", True), ("  yes  ", True), ("yEs", True),
    ("y", False), ("ok", False), ("yes please", False), ("", False), ("no", False), ("Y", False),
    ("yeah", False), ("true", False), ("1", False), ("si", False), ("yes.", False),
    (EOFError(), False), (KeyboardInterrupt(), False),
])
def test_only_yes_confirms(answer, allowed):
    scripted = ScriptedConsole([answer])
    confirm = console._confirm(scripted.read, scripted.write)
    decision = confirm(Action("close app notepad"), RiskAssessment(RiskLevel.MEDIUM, "risky keyword 'close'"))
    assert decision is (True if allowed else False)  # the safety gate allows only a literal True


def test_the_prompt_shows_the_level_the_rule_and_what_will_happen():
    scripted = ScriptedConsole(["no"])
    console._confirm(scripted.read, scripted.write)(
        Action('close window "Untitled - Notepad"'), RiskAssessment(RiskLevel.MEDIUM, "closing can lose unsaved work"))
    assert scripted.output == ('Needs your OK - MEDIUM risk (closing can lose unsaved work):\n'
                               '  close window "Untitled - Notepad"')
    assert scripted.prompts == ["Type yes to go ahead (anything else cancels): "]


@pytest.mark.parametrize("answer, retried", [("yes", True), ("y", False), ("", False), (EOFError(), False)])
def test_the_retry_offer_uses_the_same_answer(answer, retried):
    scripted = ScriptedConsole([answer])
    offer = console._offer_retry(scripted.read, scripted.write)
    result = ActionResult(ExecutorAction(OPEN_APP, "notepad"), False, "Didn't open notepad.", retryable=True)
    assert offer(result) is retried
    assert "Didn't open notepad." in scripted.output


# --- Focus hand-over --------------------------------------------------------------------------------

def test_every_action_kind_is_classified_for_hand_over():
    kinds = set(executor_logic._PREPARERS)
    assert console.HANDS_OVER | console.NO_HANDOVER == kinds
    assert not console.HANDS_OVER & console.NO_HANDOVER
    assert console.HANDS_OVER == {CLICK, TYPE_TEXT, "shortcut", "scroll", "refresh", WINDOW_CONTROL}
    assert console.NO_HANDOVER == {OPEN_APP, CLOSE_APP}


def test_an_unclassified_action_kind_fails_safe_by_asking_for_hand_over(caplog):
    with caplog.at_level(logging.WARNING):
        assert console.needs_handover("some_future_action") is True
    assert "isn't classified" in caplog.text


@pytest.mark.parametrize("line, hands_over", [
    ("open notepad", False), ("close notepad", False),
    ("click 500, 300", True), ("type hi", True), ("shortcut ctrl+a", True), ("scroll down 3", True),
    ("refresh", True), ("minimize", True), ("close window", True),
])
def test_only_actions_that_land_on_the_desktop_wait_for_hand_over(world, monkeypatch, line, hands_over):
    monkeypatch.setattr(console, "execute_with_recovery",
                        lambda action, confirm=None, offer_retry=None: ActionResult(action, True, "ok"))
    focus = ScriptedFocus()
    handle_command(line, focus=focus)
    assert focus.calls == ([console.HAND_OVER_PROMPT] if hands_over else [])


def test_a_click_waits_for_hand_over_before_and_after_the_confirmation(world):
    world.desktop.front = NOTEPAD
    scripted = ScriptedConsole(["yes"])
    focus = ScriptedFocus()
    reply = handle_command("click 500, 300", confirm=console._confirm(scripted.read, scripted.write), focus=focus)
    assert focus.calls == [console.HAND_OVER_PROMPT, console.HAND_BACK_PROMPT]
    assert reply.status is Status.RAN and reply.result.outcome is Outcome.UNVERIFIED
    assert requests(world) == [("click", 500, 300)]


def test_nothing_runs_when_focus_is_never_handed_over(world):
    scripted = ScriptedConsole(["yes"])
    focus = ScriptedFocus([console.DIDNT_SWITCH])
    reply = handle_command("click 500, 300", confirm=console._confirm(scripted.read, scripted.write), focus=focus)
    assert reply.status is Status.NOT_HANDED_OVER and reply.message == console.DIDNT_SWITCH
    assert requests(world) == [] and scripted.prompts == []  # not even asked


def test_nothing_runs_when_the_hand_back_after_yes_doesnt_happen(world):
    in_session()
    world.desktop.front = NOTEPAD
    scripted = ScriptedConsole(["yes"])
    focus = ScriptedFocus([None, console.DIDNT_SWITCH])
    reply = handle_command("close window", confirm=console._confirm(scripted.read, scripted.write), focus=focus)
    assert reply.status is Status.NOT_HANDED_OVER and reply.message == console.DIDNT_SWITCH
    assert requests(world) == []  # approved, but nothing was sent


def test_a_declined_action_doesnt_ask_for_a_hand_back(world):
    in_session()
    world.desktop.front = NOTEPAD
    scripted = ScriptedConsole(["no"])
    focus = ScriptedFocus()
    reply = handle_command("close window", confirm=console._confirm(scripted.read, scripted.write), focus=focus)
    assert reply.status is Status.DENIED and focus.calls == [console.HAND_OVER_PROMPT]


def test_the_executor_still_refuses_a_window_that_changed_after_approval(world):
    """The hand-over is a convenience; the Executor's own re-check is the final word."""
    in_session()
    world.desktop.front = NOTEPAD
    scripted = ScriptedConsole(["yes"])

    class SwitchAway(ScriptedFocus):
        def hand_over(self, prompt):
            if prompt == console.HAND_BACK_PROMPT:
                world.desktop.front = OTHER  # the user landed on a different window
            return super().hand_over(prompt)

    reply = handle_command("close window", confirm=console._confirm(scripted.read, scripted.write), focus=SwitchAway())
    assert reply.status is Status.RAN and not reply.result.ok
    assert "changed" in reply.message and requests(world) == []


# --- The real hand-over watches, and never switches windows ------------------------------------------

def hand_over(world, write=None):
    focus = FocusHandover(write or (lambda text: None))
    focus.note_console_window()
    return focus


def test_the_hand_over_waits_for_another_window_and_never_activates_one(world):
    written = []
    focus = hand_over(world, written.append)

    def switch():
        time.sleep(0.05)
        world.desktop.front = NOTEPAD
    threading.Thread(target=switch).start()
    assert focus.hand_over(console.HAND_OVER_PROMPT) is None
    assert world.desktop.front is NOTEPAD  # the user switched; the console only watched
    assert requests(world) == [] and "Waiting up to 0.5s" in written[0]


def test_staying_in_the_console_hands_nothing_over(world):
    assert hand_over(world).hand_over(console.HAND_OVER_PROMPT) == console.DIDNT_SWITCH


def test_a_held_modifier_means_the_user_is_still_switching(world):
    focus = hand_over(world)
    world.desktop.front, world.desktop.held = NOTEPAD, ["Alt"]  # the Alt+Tab switcher is up
    assert focus.hand_over(console.HAND_OVER_PROMPT) == console.DIDNT_SWITCH


def test_a_window_must_stay_in_front_to_count(world, monkeypatch):
    """Flicking through windows (Alt+Tab held) never settles on one."""
    focus = hand_over(world)
    seen = []

    def flickering():
        seen.append(len(seen))
        return ActiveTarget(NOTEPAD if len(seen) % 2 else OTHER, 501, "Edit")
    monkeypatch.setattr(verifier_adapter, "active_target", flickering)
    assert focus.hand_over(console.HAND_OVER_PROMPT) == console.DIDNT_SWITCH
    assert len(seen) > 2  # it really did keep looking


def test_an_unreadable_desktop_hands_nothing_over(world):
    focus = hand_over(world)
    world.desktop.error = verifier_adapter.VerifierAdapterError("desktop locked")
    message = focus.hand_over(console.HAND_OVER_PROMPT)
    assert message and "I can't check which window is in front" in message


def test_without_a_console_window_nothing_is_handed_over(world):
    focus = FocusHandover(lambda text: None)
    world.desktop.error = verifier_adapter.VerifierAdapterError("desktop locked")
    focus.note_console_window()
    world.desktop.error = None
    assert focus.hand_over(console.HAND_OVER_PROMPT) == console.NO_FRONT_WINDOW


def test_a_broken_console_setting_hands_nothing_over(world):
    world.config_path.write_text(CONFIG.replace("focus_handover_seconds: 0.5", "focus_handover_seconds: 0"),
                                 encoding="utf-8")
    message = hand_over(world).hand_over(console.HAND_OVER_PROMPT)
    assert message and "console.focus_handover_seconds" in message


# --- Emergency stop -----------------------------------------------------------------------------------

def test_a_stop_before_the_command_runs_nothing(world):
    emergency_stop.trigger("test")
    reply = handle_command("minimize", focus=ScriptedFocus())
    assert reply.status is Status.STOPPED and reply.message == console.STOPPED_MESSAGE
    assert requests(world) == []


def test_the_console_never_resets_the_stop(world):
    emergency_stop.trigger("test")
    handle_command("minimize")
    scripted = ScriptedConsole(["minimize", "exit"])
    run_console(read=scripted.read, write=scripted.write, focus=ScriptedFocus())
    assert emergency_stop.is_stopped()
    assert console.STOPPED_MESSAGE in scripted.output


def test_a_stop_during_the_hand_over_runs_nothing(world):
    focus = hand_over(world)

    def stop_soon():
        time.sleep(0.02)
        emergency_stop.trigger("test")
    threading.Thread(target=stop_soon).start()
    with pytest.raises(EmergencyStopError):
        focus.hand_over(console.HAND_OVER_PROMPT)
    assert requests(world) == []


def test_a_stop_while_confirming_is_reported_as_a_stop_not_a_denial(world):
    in_session()
    world.desktop.front = NOTEPAD

    def confirm(action, assessment):
        emergency_stop.trigger("test")
        return True
    reply = handle_command("close window", confirm=confirm)
    assert reply.status is Status.STOPPED and requests(world) == []


def test_an_interrupted_action_says_how_much_happened(world, monkeypatch):
    action = ExecutorAction(TYPE_TEXT, "hello")
    partial = ActionResult(action, False, "Typed 2 of 5 characters.", outcome=Outcome.PARTIAL, progress=(2, 5))

    def interrupted(*args, **kwargs):
        raise ActionInterruptedError("stopped", partial)
    monkeypatch.setattr(console, "execute_with_recovery", interrupted)
    reply = handle_command("type hello")
    assert reply.status is Status.STOPPED and reply.result is partial
    assert "Typed 2 of 5 characters." in reply.message and "hello" not in reply.message


# --- The console loop ---------------------------------------------------------------------------------

def test_the_loop_runs_commands_until_the_user_leaves(world):
    scripted = ScriptedConsole(["minimize", "", "   ", "nonsense", "exit"])
    assert run_console(read=scripted.read, write=scripted.write, focus=ScriptedFocus()) == 0
    assert "Minimized the window." in scripted.output
    assert commands.UNKNOWN_MESSAGE in scripted.output
    assert scripted.prompts.count("> ") == 5  # blank lines are simply ignored


def test_help_and_the_welcome_explain_the_commands(world):
    scripted = ScriptedConsole(["help", "exit"])
    run_console(read=scripted.read, write=scripted.write, focus=ScriptedFocus())
    assert commands.HELP in scripted.output and console.WELCOME in scripted.output


def test_end_of_input_leaves_the_console(world):
    scripted = ScriptedConsole([])
    assert run_console(read=scripted.read, write=scripted.write, focus=ScriptedFocus()) == 0


def test_ctrl_c_at_the_prompt_leaves_the_console(world):
    scripted = ScriptedConsole([KeyboardInterrupt()])
    assert run_console(read=scripted.read, write=scripted.write, focus=ScriptedFocus()) == 0


def test_ctrl_c_during_an_action_says_it_is_not_the_emergency_stop(world, monkeypatch):
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(console, "handle_command", interrupt)
    scripted = ScriptedConsole(["minimize"])
    assert run_console(read=scripted.read, write=scripted.write, focus=ScriptedFocus()) == 1
    assert console.INTERRUPTED in scripted.output


def test_an_unexpected_failure_reports_its_type_only_and_keeps_going(world, monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError(SECRET)
    monkeypatch.setattr(console, "handle_command", broken)
    scripted = ScriptedConsole([f"type {SECRET}", "exit"])
    with caplog.at_level(logging.DEBUG):
        assert run_console(read=scripted.read, write=scripted.write, focus=ScriptedFocus()) == 0
    assert "Something went wrong (RuntimeError)" in scripted.output
    assert SECRET not in scripted.output and SECRET not in caplog.text


def test_the_loop_notes_its_own_window_after_every_line(world):
    scripted = ScriptedConsole(["minimize", "minimize", "exit"])
    focus = ScriptedFocus()
    run_console(read=scripted.read, write=scripted.write, focus=focus)
    assert focus.calls == ["noted", console.HAND_OVER_PROMPT, "noted", console.HAND_OVER_PROMPT]


# --- Privacy ------------------------------------------------------------------------------------------

def test_typed_text_never_reaches_the_console_output_or_the_logs(world, caplog):
    world.desktop.front = NOTEPAD
    scripted = ScriptedConsole([f"type {SECRET}", "yes", "exit"])
    with caplog.at_level(logging.DEBUG):
        run_console(read=scripted.read, write=scripted.write, focus=ScriptedFocus())
    assert SECRET not in scripted.output, scripted.output
    assert SECRET not in caplog.text
    assert "29 characters" in scripted.output  # it really did try to type it


def test_the_privacy_check_would_catch_a_leak(world, caplog):
    scripted = ScriptedConsole(["exit"])
    with caplog.at_level(logging.DEBUG):
        run_console(read=scripted.read, write=scripted.write, focus=ScriptedFocus())
        scripted.write(f"planted leak: {SECRET}")
        logging.getLogger(__name__).info("planted leak: %s", SECRET)
    assert SECRET in scripted.output and SECRET in caplog.text


def test_the_console_never_logs_the_line_that_was_typed(world, caplog):
    """The Executor still logs an app name it was given, as it always has (an app name is its target);
    the console and the parser add nothing of their own."""
    scripted = ScriptedConsole(["open nonexistentapp123", "exit"])
    with caplog.at_level(logging.DEBUG):
        run_console(read=scripted.read, write=scripted.write, focus=ScriptedFocus())
    ours = [record.getMessage() for record in caplog.records
            if record.name in ("app.console", "app.executor.commands")]
    assert ours and not any("nonexistentapp123" in message for message in ours), ours


# --- No bypass ------------------------------------------------------------------------------------------

def console_source():
    return ast.parse(Path(console.__file__).read_text(encoding="utf-8"))


def test_the_console_imports_no_adapter_and_only_one_way_to_act():
    imported = {}
    for node in ast.walk(console_source()):
        if isinstance(node, ast.ImportFrom):
            imported.setdefault(node.module, set()).update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.setdefault("", set()).update(alias.name for alias in node.names)
    assert not any("adapter" in module for module in imported), imported
    assert not any(name in ("pyautogui", "ctypes", "pywinauto") for names in imported.values() for name in names)
    assert imported["app.executor.logic"] == {"execute_with_recovery"}


def test_the_console_only_reads_from_the_verifier():
    used = {node.attr for node in ast.walk(console_source())
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "verifier"}
    assert used == {"active_target", "modifiers_held", "VerifierUnavailableError"}, used


def test_the_console_never_triggers_or_resets_the_emergency_stop():
    used = {node.attr for node in ast.walk(console_source())
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "emergency_stop"}
    assert used == {"is_stopped", "wait", "check"}, used


def test_the_safety_gate_is_the_real_one(world):
    """The console must not carry its own idea of what is risky."""
    assert executor_logic.authorize is safety_logic.authorize
    assert safety_logic.CONFIRMATION_REQUIRED_AT is RiskLevel.MEDIUM


# --- main.py ---------------------------------------------------------------------------------------------

def test_main_console_starts_the_console(monkeypatch):
    started = []
    monkeypatch.setattr(main, "run_console", lambda: started.append(True) or 0)
    monkeypatch.setattr(main, "run_health_check", lambda **kwargs: pytest.fail("the console must not run checks"))
    assert main.main(["--console"]) == 0 and started == [True]


def test_main_without_console_still_runs_the_health_check(monkeypatch):
    monkeypatch.setattr(main, "run_console", lambda: pytest.fail("the console must not start"))
    monkeypatch.setattr(main, "run_health_check", lambda **kwargs: [])
    monkeypatch.setattr(main, "format_report", lambda results: "report")
    assert main.main([]) == 0


def test_console_and_check_claude_cannot_be_combined():
    with pytest.raises(SystemExit):
        main.main(["--console", "--check-claude"])


# --- The emergency-stop hotkey, as the console sees it ---------------------------------------------

def hotkey_state(monkeypatch, *states):
    """Make hotkey.status() answer with each state in turn (the last one repeats)."""
    seen = list(states)

    def status():
        return seen[0] if len(seen) == 1 else seen.pop(0)
    monkeypatch.setattr(hotkey, "status", status)


def test_the_startup_line_says_what_to_press(world, monkeypatch):
    hotkey_state(monkeypatch, hotkey.HotkeyStatus(hotkey.ACTIVE, "Ctrl+Alt+Backspace"))
    scripted = ScriptedConsole(["exit"])
    run_console(read=scripted.read, write=scripted.write, focus=ScriptedFocus())
    assert "Emergency stop: press Ctrl+Alt+Backspace at any time" in scripted.output


def test_the_startup_line_says_when_there_is_no_hotkey(world, monkeypatch):
    hotkey_state(monkeypatch, hotkey.HotkeyStatus(hotkey.UNAVAILABLE, "Ctrl+Alt+Backspace",
                                                  "another program has already registered that key combination"))
    scripted = ScriptedConsole(["exit"])
    run_console(read=scripted.read, write=scripted.write, focus=ScriptedFocus())
    assert "is NOT active" in scripted.output and "another program" in scripted.output


def test_a_hotkey_that_stops_working_is_reported_once(world, monkeypatch):
    active = hotkey.HotkeyStatus(hotkey.ACTIVE, "Ctrl+Alt+Backspace")
    broken = hotkey.HotkeyStatus(hotkey.FAILED, "Ctrl+Alt+Backspace", "Windows stopped delivering messages.")
    hotkey_state(monkeypatch, active, active, broken)
    scripted = ScriptedConsole(["minimize", "minimize", "minimize", "exit"])
    run_console(read=scripted.read, write=scripted.write, focus=ScriptedFocus())
    assert scripted.output.count("has stopped working") == 1, scripted.output  # said once, not before every command
    assert "Windows stopped delivering messages." in scripted.output


def test_the_console_never_starts_or_stops_the_hotkey(world, monkeypatch):
    for name in ("start", "stop"):
        monkeypatch.setattr(hotkey, name, lambda *a, **k: pytest.fail("the console must not manage the listener"))
    scripted = ScriptedConsole(["minimize", "exit"])
    run_console(read=scripted.read, write=scripted.write, focus=ScriptedFocus())


def test_main_console_runs_inside_the_hotkey_listener(monkeypatch):
    order = []
    monkeypatch.setattr(main.hotkey, "start", lambda: order.append("start") or hotkey.HotkeyStatus(hotkey.ACTIVE, "X"))
    monkeypatch.setattr(main.hotkey, "stop", lambda: order.append("stop"))
    monkeypatch.setattr(main, "run_console", lambda: order.append("console") or 0)
    assert main.main(["--console"]) == 0
    assert order == ["start", "console", "stop"]  # started before, stopped after - even though it is a daemon


def test_the_help_text_names_the_configured_hotkey(monkeypatch, capsys):
    monkeypatch.setattr(hotkey, "configured_name", lambda: "Ctrl+Alt+Backspace")
    with pytest.raises(SystemExit):
        main.main(["--help"])
    printed = capsys.readouterr().out
    assert "Ctrl+Alt+Backspace" in printed and hotkey.SETTING in printed
