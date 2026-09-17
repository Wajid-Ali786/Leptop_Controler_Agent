"""
Tests for type_text (app/executor/logic.py + adapter.send_character) - typing exact text into the
active window's focused field, one character at a time.

No real keyboard input is ever sent: the active window, the focused field's text and the typing
itself are faked, and the real SendInput is blocked. The REAL safety gate and the REAL Verifier logic
decide every action. A unique secret-like string checks that typed text never leaks into logs,
repr(), results, errors or safety diagnostics.
"""
import ast
import logging
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from app.executor import adapter, emergency_stop, logic
from app.executor.emergency_stop import EmergencyStopError, TypingInterruptedError
from app.executor.logic import execute, execute_with_recovery
from app.executor.models import TYPE_TEXT, ActionResult, ExecutorAction, Outcome
from app.safety.logic import ActionDeniedError
from app.safety.models import RiskLevel
from app.verifier import adapter as verifier_adapter
from app.verifier.models import ActiveTarget, WindowInfo
from config import settings

CONFIG = (
    "safety:\n"
    '  risky_keywords: [delete, shutdown, "shut down", send, close]\n'
    "  safe_words: [sender]\n"
    "executor:\n"
    "  apps:\n"
    "    notepad: notepad.exe\n"
    "  max_attempts: 3\n"
    "  max_type_characters: 60\n"
    "  typing_interval_seconds: 0\n"
    "verifier:\n"
    "  window_timeout_seconds: 0.2\n"
    "  poll_interval_seconds: 0.01\n"
    "  text_settle_seconds: 0.05\n"
    "  max_read_characters: 1000\n"
    "  app_windows:\n"
    '    notepad: "Notepad$"\n'
)
NOTEPAD = WindowInfo(500, "Untitled - Notepad", "Notepad")
FIELD = 501
TARGET = ActiveTarget(NOTEPAD, FIELD, "Edit")
ENTER = "⏎"
SECRET = "zq7-S3CR3T-pw-4f1c9"  # unique: must never show up anywhere but the on-screen prompt


class FakeDesktop:
    """The active window/field, the field's text (an Edit control stores Enter as CRLF), and typing.
    Hooks: on_target_read[n] runs before the n-th active-window read; on_send[i] before character i."""

    def __init__(self, calls):
        self.calls = calls
        self.target = TARGET
        self.target_reads = 0
        self.on_target_read = {}
        self.on_send = {}
        self.field = ""
        self.readable = True
        self.app_shows_text = True
        self.send_error_at = {}  # index -> TypingError
        self.target_error = None

    def active_target(self):
        self.target_reads += 1
        self.on_target_read.get(self.target_reads, lambda: None)()
        if self.target_error:
            raise self.target_error
        return self.target

    def read_text(self, handle, max_characters):
        if not self.readable or handle != FIELD or len(self.field) > max_characters:
            return None
        return self.field

    def send_character(self, character):
        index = len(self.sent())
        self.on_send.get(index, lambda: None)()
        if index in self.send_error_at:
            raise self.send_error_at[index]
        self.calls.append(("send", character))
        if self.app_shows_text:
            self.field += "\r\n" if character == "\n" else character

    def sent(self):
        return [call[1] for call in self.calls if call[0] == "send"]


@pytest.fixture
def world(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_PATH", config_path)
    calls = []
    desktop = FakeDesktop(calls)
    monkeypatch.setattr(verifier_adapter, "active_target", desktop.active_target)
    monkeypatch.setattr(verifier_adapter, "read_text", desktop.read_text)
    monkeypatch.setattr(adapter, "send_character", desktop.send_character)

    def no_real_keyboard():
        raise AssertionError("real keyboard input must not be sent in this test")
    monkeypatch.setattr(adapter, "_keyboard_api", no_real_keyboard)
    monkeypatch.setitem(sys.modules, "pyautogui", None)
    emergency_stop.reset("test-setup")
    yield SimpleNamespace(calls=calls, desktop=desktop, config_path=config_path)
    emergency_stop.reset("test-teardown")


def approve(world, answer=True):
    def confirm(action, assessment):
        world.calls.append(("confirm", action, assessment))
        return answer
    return confirm


def type_text(world, text, answer=True):
    return execute(ExecutorAction(TYPE_TEXT, text), approve(world, answer))


def prompts(world):
    return [call[1].description for call in world.calls if call[0] == "confirm"]


def assessments(world):
    return [call[2] for call in world.calls if call[0] == "confirm"]


# --- Happy path: confirmed, typed character by character, confirmed in the field ---

def test_text_is_confirmed_typed_and_verified(world):
    result = type_text(world, "hello world")
    assert result.ok and result.verified and result.outcome is Outcome.DONE and not result.retryable
    assert result.progress == (11, 11)
    assert result.message == "Typed 11 characters and confirmed they appeared in the field."
    assert prompts(world) == ['type 11 characters into window "Untitled - Notepad" (field: Edit): "hello world"']
    assert (assessments(world)[0].level, assessments(world)[0].rule) == (RiskLevel.MEDIUM, logic._TYPING_RISK_REASON)
    assert "".join(world.desktop.sent()) == "hello world"
    assert world.calls[0][0] == "confirm"  # asked before anything was typed


def test_any_language_and_emoji_are_typed_exactly(world):
    text = "héllo \U0001F600 سلام 你好"
    result = type_text(world, text)
    assert result.outcome is Outcome.DONE and result.progress == (len(text), len(text))
    assert world.desktop.sent() == list(text)


def test_spaces_are_typed_not_stripped(world):
    assert type_text(world, "  two  ").outcome is Outcome.DONE
    assert "".join(world.desktop.sent()) == "  two  "


def test_windows_line_endings_become_one_enter_each(world):
    result = type_text(world, "a\r\nb")
    assert world.desktop.sent() == ["a", "\n", "b"]
    assert result.outcome is Outcome.DONE and result.progress == (3, 3)
    assert result.message == "Typed 3 characters (including 1 Enter press) and confirmed they appeared in the field."


# --- Validation: nothing asked, nothing typed ---

@pytest.mark.parametrize("text, message", [
    ("", "What should I type?"),
    ("x" * 61, "That's 61 characters; I can type at most 60 at once."),
    ("a\tb", "I can't type that: it contains Tab or another control key, which isn't text (keyboard shortcuts come later)."),
    ("bell\x07", "I can't type that: it contains Tab or another control key, which isn't text (keyboard shortcuts come later)."),
    ("lone\rreturn", "I can't type that: it contains Tab or another control key, which isn't text (keyboard shortcuts come later)."),
    ("broken\ud800", "I can't type that: it contains an invalid character."),
])
def test_invalid_text_fails_cleanly(world, text, message):
    result = type_text(world, text)
    assert not result.ok and not result.retryable and result.message == message
    assert world.calls == [] and world.desktop.target_reads == 0


def test_no_active_window_means_nothing_is_typed(world):
    world.desktop.target = ActiveTarget(window=None)
    result = type_text(world, "hello")
    assert not result.ok and result.message == "Didn't type: there's no active window to type into."
    assert world.calls == []


def test_unobservable_desktop_means_nothing_is_typed(world):
    world.desktop.target_error = verifier_adapter.VerifierAdapterError("checking windows is only supported on Windows")
    result = type_text(world, "hello")
    assert result.message == "Didn't type: I can't check the active window (checking windows is only supported on Windows)."
    assert world.calls == []


@pytest.mark.parametrize("line, setting", [
    ("  max_type_characters: 60\n", "executor.max_type_characters"),
    ("  typing_interval_seconds: 0\n", "executor.typing_interval_seconds"),
])
def test_invalid_typing_settings_fail_clearly(world, line, setting):
    world.config_path.write_text(CONFIG.replace(line, line.split(":")[0] + ": -1\n"), encoding="utf-8")
    result = type_text(world, "hello")
    assert not result.ok and setting in result.message
    assert world.calls == []


# --- Safety: always confirmed; Enter raises the risk and is spelled out ---

def test_typing_without_a_confirmation_method_is_denied(world):
    with pytest.raises(ActionDeniedError, match="typing text - always needs confirmation"):
        execute(ExecutorAction(TYPE_TEXT, "hello"))
    assert world.desktop.sent() == []


def test_typing_declined_by_the_user_types_nothing(world):
    with pytest.raises(ActionDeniedError, match="did not confirm"):
        type_text(world, "hello", answer=False)
    assert world.desktop.sent() == []


def test_line_break_is_high_risk_and_the_prompt_says_enter_will_be_pressed(world):
    type_text(world, "Hello from the AI Desktop Companion test\nline two")
    assert prompts(world) == [
        'type 49 characters into window "Untitled - Notepad" (field: Edit) AND PRESS ENTER 1 TIME - Enter can '
        'submit a form, send a message or run a command: "Hello from the AI Desktop Companion test…"']
    assert (assessments(world)[0].level, assessments(world)[0].rule) == (RiskLevel.HIGH, logic._ENTER_RISK_REASON)
    assert world.desktop.sent()[40] == "\n"  # a real Enter, sent as its own key press


def test_text_ending_with_enter_warns_it_will_submit(world):
    type_text(world, "hi\nhow are you\n")
    assert prompts(world) == [
        'type 15 characters into window "Untitled - Notepad" (field: Edit) AND PRESS ENTER 2 TIMES - Enter can '
        'submit a form, send a message or run a command (ends with Enter: it will submit as soon as typing '
        f'finishes): "hi{ENTER}how are you{ENTER}"']


@pytest.mark.parametrize("text, preview", [
    ("x" * 40, "x" * 40),
    ("x" * 41, "x" * 40 + "…"),
    ("line one\nline two", f"line one{ENTER}line two"),
])
def test_prompt_preview_is_at_most_40_characters(world, text, preview):
    type_text(world, text)
    assert prompts(world)[0].endswith(f': "{preview}"')


@pytest.mark.parametrize("target, where", [
    (ActiveTarget(WindowInfo(7, "", "X"), 8, "Edit"), 'a window with no readable title (field: Edit)'),
    (ActiveTarget(NOTEPAD, None, ""), 'window "Untitled - Notepad" (field: unknown)'),
])
def test_prompt_without_a_title_or_field(world, target, where):
    world.desktop.target = target
    type_text(world, "a")
    assert prompts(world) == [f'type 1 character into {where}: "a"']


def test_risky_words_in_the_text_keep_the_typing_rule(world):
    type_text(world, "delete everything")
    assert assessments(world)[0].rule == logic._TYPING_RISK_REASON


# --- The active window/field must still be the one approved ---

@pytest.mark.parametrize("changed", [
    ActiveTarget(WindowInfo(900, "Inbox - Mail", "Mail"), 901, "Edit"),  # another window
    ActiveTarget(NOTEPAD, 777, "Edit"),                                    # another field
    ActiveTarget(WindowInfo(NOTEPAD.handle, "Terminal", "Notepad"), FIELD, "Edit"),  # retitled
])
def test_changed_target_after_approval_types_nothing(world, changed):
    world.desktop.on_target_read[2] = lambda: setattr(world.desktop, "target", changed)
    result = type_text(world, "hello")
    assert not result.ok and not result.retryable and result.outcome is Outcome.FAILED
    assert result.message == "The active window changed after you approved, so I typed nothing."
    assert world.desktop.sent() == []


def test_title_changing_while_typing_does_not_stop_it(world):
    """Notepad adds "*" to its title as soon as the text changes."""
    world.desktop.on_send[1] = lambda: setattr(
        world.desktop, "target", ActiveTarget(WindowInfo(NOTEPAD.handle, "*Untitled - Notepad", "Notepad"), FIELD, "Edit"))
    assert type_text(world, "hello").outcome is Outcome.DONE


# --- Partial typing: honest, counted, never retryable ---

def test_focus_change_part_way_is_partial_with_progress(world):
    world.desktop.on_send[3] = lambda: None
    world.desktop.on_target_read[6] = lambda: setattr(  # reads: 1 prompt, 2 re-check, 3..: before each char
        world.desktop, "target", ActiveTarget(WindowInfo(900, "Chat", "Chat"), 901, "Edit"))
    result = type_text(world, "hello world")
    assert not result.ok and not result.verified and not result.retryable
    assert result.outcome is Outcome.PARTIAL and result.progress == (3, 11)
    assert result.message == ("Typed 3 of 11 characters, then the active window changed, so I stopped. Those 3 "
                              "characters may already be in the window. I won't retype anything.")
    assert world.desktop.sent() == list("hel")


def test_focus_change_before_the_first_character_is_a_clean_failure(world):
    world.desktop.on_target_read[3] = lambda: setattr(world.desktop, "target", ActiveTarget(None))
    result = type_text(world, "hello")
    assert result.outcome is Outcome.FAILED and result.progress is None
    assert result.message == ("I couldn't type: the active window changed before the first character, "
                              "so I typed nothing.")
    assert world.desktop.sent() == []


@pytest.mark.parametrize("index, partly, outcome, progress", [
    (0, False, Outcome.FAILED, None),
    (0, True, Outcome.PARTIAL, (1, 5)),   # the first character may have gone through
    (3, False, Outcome.PARTIAL, (3, 5)),
])
def test_windows_refusing_input_part_way(world, index, partly, outcome, progress):
    world.desktop.send_error_at[index] = adapter.TypingError("Windows didn't accept the keyboard input", partly)
    result = type_text(world, "hello")
    assert not result.ok and not result.retryable and result.outcome is outcome and result.progress == progress
    assert "Windows stopped accepting keyboard input" in result.message


# --- Emergency stop ---

def test_emergency_stop_before_anything(world):
    emergency_stop.trigger("hotkey")
    with pytest.raises(EmergencyStopError):
        type_text(world, "hello")
    assert world.calls == []


def test_stop_during_confirmation_types_nothing(world):
    def confirm_then_stop(action, assessment):
        emergency_stop.trigger("hotkey")
        return True
    with pytest.raises(EmergencyStopError) as info:
        execute(ExecutorAction(TYPE_TEXT, "hello"), confirm_then_stop)
    assert not isinstance(info.value, TypingInterruptedError)
    assert world.desktop.sent() == []


def test_stop_before_the_first_character_is_a_plain_stop(world):
    world.desktop.on_target_read[2] = lambda: emergency_stop.trigger("hotkey")  # during the re-check
    with pytest.raises(EmergencyStopError) as info:
        type_text(world, "hello")
    assert not isinstance(info.value, TypingInterruptedError)
    assert world.desktop.sent() == []


def test_stop_part_way_raises_with_how_much_was_typed(world):
    world.desktop.on_send[5] = lambda: emergency_stop.trigger("hotkey")
    with pytest.raises(TypingInterruptedError) as info:
        type_text(world, "hello world")
    assert isinstance(info.value, EmergencyStopError)  # anything that stops on a stop still stops
    result = info.value.result
    assert result.outcome is Outcome.PARTIAL and result.progress == (6, 11) and not result.retryable
    assert result.message == ("Emergency stop: typed 6 of 11 characters before stopping. Those 6 characters may "
                              "already be in the window. I won't retype anything.")
    assert str(info.value) == "Emergency stop is active (triggered by hotkey); typing stopped after 6 of 11 characters."
    assert len(world.desktop.sent()) == 6


def test_stop_is_noticed_during_the_pause_between_characters(world):
    world.config_path.write_text(CONFIG.replace("typing_interval_seconds: 0", "typing_interval_seconds: 5"),
                                 encoding="utf-8")
    threading.Timer(0.1, emergency_stop.trigger, args=("hotkey",)).start()
    started = time.monotonic()
    with pytest.raises(TypingInterruptedError) as info:
        type_text(world, "hello")
    assert time.monotonic() - started < 2  # not after the 5 s pause
    assert info.value.result.progress == (1, 5)


def test_stop_while_checking_after_everything_was_typed(world):
    world.config_path.write_text(CONFIG.replace("text_settle_seconds: 0.05", "text_settle_seconds: 5"),
                                 encoding="utf-8")
    world.desktop.app_shows_text = False  # the check keeps looking, then the stop arrives
    threading.Timer(0.1, emergency_stop.trigger, args=("hotkey",)).start()
    with pytest.raises(TypingInterruptedError) as info:
        type_text(world, "hello")
    result = info.value.result
    assert result.ok and not result.verified and result.outcome is Outcome.UNVERIFIED and result.progress == (5, 5)


# --- Verification: done only on evidence; otherwise unverified, never a failure to retry ---

def test_unreadable_field_is_unverified(world):
    world.desktop.readable = False
    result = type_text(world, "hello")
    assert result.ok and not result.verified and result.outcome is Outcome.UNVERIFIED
    assert result.message == "Typed 5 characters. I can't read that field, so I can't confirm they arrived."


def test_field_that_becomes_unreadable_is_unverified(world):
    world.desktop.on_send[4] = lambda: setattr(world.desktop, "readable", False)
    assert type_text(world, "hello").outcome is Outcome.UNVERIFIED


def test_no_focused_field_is_unverified(world):
    world.desktop.target = ActiveTarget(NOTEPAD, None, "")
    assert type_text(world, "hello").outcome is Outcome.UNVERIFIED


def test_text_that_never_shows_up_is_unverified_not_failed(world):
    world.desktop.app_shows_text = False
    result = type_text(world, "hello")
    assert result.ok and result.outcome is Outcome.UNVERIFIED and not result.retryable
    assert result.message == ("Typed 5 characters, but I couldn't find them in the field afterwards (the app may "
                              "have changed them). I won't retype anything.")


def test_text_already_in_the_field_only_counts_when_it_appears_again(world):
    world.desktop.field = "hello"
    assert type_text(world, "hello").outcome is Outcome.DONE  # now twice


def test_text_already_in_the_field_that_does_not_appear_again_is_not_success(world):
    world.desktop.field = "hello"
    world.desktop.app_shows_text = False  # e.g. it replaced a selection that said "hello"
    assert type_text(world, "hello").outcome is Outcome.UNVERIFIED


def test_slow_app_is_given_time_to_show_the_text(world):
    world.desktop.app_shows_text = False

    def show_later():
        time.sleep(0.02)
        world.desktop.field = "hello"
    world.desktop.on_send[4] = lambda: threading.Thread(target=show_later).start()
    assert type_text(world, "hello").outcome is Outcome.DONE


def test_enter_stored_as_crlf_in_the_field_still_counts(world):
    assert type_text(world, "one\ntwo").outcome is Outcome.DONE
    assert world.desktop.field == "one\r\ntwo"


# --- Recovery: typing is never retried ---

@pytest.mark.parametrize("setup", [
    lambda d: d.send_error_at.update({0: adapter.TypingError("Windows didn't accept the keyboard input")}),
    lambda d: d.send_error_at.update({2: adapter.TypingError("Windows didn't accept the keyboard input")}),
    lambda d: d.on_target_read.update({2: lambda: setattr(d, "target", ActiveTarget(None))}),
    lambda d: setattr(d, "app_shows_text", False),
    lambda d: None,
], ids=["failed", "partial", "window-changed", "unverified", "done"])
def test_typing_is_never_retried(world, setup):
    setup(world.desktop)
    offers = []
    result = execute_with_recovery(ExecutorAction(TYPE_TEXT, "hello"), approve(world),
                                   offer_retry=lambda r: offers.append(r) or True)
    assert offers == [] and not result.retryable
    assert len(prompts(world)) == 1 and len(world.desktop.sent()) <= 5


# --- Result shape ---

def test_partial_result_rules():
    action = ExecutorAction(TYPE_TEXT, "hello")
    partial = ActionResult(action, False, "Typed 2 of 5.", outcome=Outcome.PARTIAL, progress=(2, 5))
    assert not partial.ok and not partial.verified
    for bad in [dict(progress=None), dict(progress=(0, 5)), dict(progress=(6, 5)),
                dict(progress=(2, 5), retryable=True)]:
        with pytest.raises(ValueError):
            ActionResult(action, False, "x", outcome=Outcome.PARTIAL, **bad)
    with pytest.raises(ValueError):
        ActionResult(action, True, "x", outcome=Outcome.PARTIAL, progress=(2, 5))


# --- Typed text never leaks (only the on-screen prompt may preview it) ---

def _no_secret(*things):
    for thing in things:
        assert SECRET not in str(thing) and SECRET not in repr(thing), f"secret leaked into {type(thing).__name__}"


@pytest.mark.parametrize("scenario", ["done", "declined", "partial", "interrupted", "refused", "invalid", "too-long",
                                      "late-in-text"])
def test_typed_text_never_leaks(world, caplog, scenario):
    caplog.set_level(logging.DEBUG)
    text = f"{SECRET} and more"
    if scenario == "late-in-text":
        text = "x" * 41 + SECRET  # beyond the 40-character preview: not even the prompt shows it
    if scenario == "invalid":
        text = f"{SECRET}\t"
    if scenario == "too-long":
        text = SECRET * 4
    if scenario == "partial":
        world.desktop.on_target_read[6] = lambda: setattr(world.desktop, "target", ActiveTarget(None))
    if scenario == "interrupted":
        world.desktop.on_send[4] = lambda: emergency_stop.trigger("hotkey")
    if scenario == "refused":
        world.desktop.send_error_at[3] = adapter.TypingError("Windows didn't accept the keyboard input")
    action = ExecutorAction(TYPE_TEXT, text)
    outputs = [action, action.description, action.log_label]
    try:
        outputs.append(execute(action, approve(world, answer=scenario != "declined")))
    except (ActionDeniedError, TypingInterruptedError) as exc:
        outputs += [exc, exc.args, getattr(exc, "result", None), getattr(exc, "assessment", None)]
    for _, safety_action, assessment in [c for c in world.calls if c[0] == "confirm"]:
        outputs += [safety_action, assessment, assessment.rule]
        if scenario == "late-in-text":
            assert SECRET not in safety_action.description
        else:
            assert SECRET in safety_action.description  # proves the test would see a leak: the prompt has it
    _no_secret(caplog.text, *outputs, *[r.getMessage() for r in caplog.records], *[r.args for r in caplog.records])


# --- Adapter: SendInput with Unicode characters (a fake SendInput - no real input) ---

@pytest.fixture
def fake_send_input(monkeypatch):
    if sys.platform != "win32":
        pytest.skip("the keyboard structures are Windows-only")
    api = adapter._KeyboardApi()
    calls, answer = [], {"accepted": None}

    def send_input(count, inputs, size):
        calls.append([(inputs[i].type, inputs[i].ki.wVk, inputs[i].ki.wScan, inputs[i].ki.dwFlags) for i in range(count)])
        return count if answer["accepted"] is None else answer["accepted"]
    api.SendInput = send_input
    monkeypatch.setattr(adapter, "_keyboard_api", lambda: api)
    return SimpleNamespace(calls=calls, answer=answer)


def test_adapter_sends_a_unicode_character_down_and_up_in_one_call(fake_send_input):
    adapter.send_character("a")
    assert fake_send_input.calls == [[(1, 0, ord("a"), 0x4), (1, 0, ord("a"), 0x4 | 0x2)]]


def test_adapter_presses_enter_for_a_line_break(fake_send_input):
    adapter.send_character("\n")
    assert fake_send_input.calls == [[(1, 0x0D, 0, 0), (1, 0x0D, 0, 0x2)]]


def test_adapter_sends_both_halves_of_an_emoji_in_one_call(fake_send_input):
    adapter.send_character("\U0001F600")
    assert fake_send_input.calls == [[(1, 0, 0xD83D, 0x4), (1, 0, 0xD83D, 0x6), (1, 0, 0xDE00, 0x4), (1, 0, 0xDE00, 0x6)]]


@pytest.mark.parametrize("accepted, partly", [(0, False), (1, True)])
def test_adapter_reports_refused_input_without_the_character(fake_send_input, accepted, partly):
    fake_send_input.answer["accepted"] = accepted
    with pytest.raises(adapter.TypingError) as info:
        adapter.send_character("Z")
    assert info.value.partly_sent is partly and "Z" not in str(info.value)


def test_adapter_sends_exactly_one_character(monkeypatch):
    monkeypatch.setattr(adapter.sys, "platform", "win32")
    with pytest.raises(adapter.TypingError, match="exactly one character"):
        adapter.send_character("ab")


def test_adapter_refuses_off_windows(monkeypatch):
    monkeypatch.setattr(adapter.sys, "platform", "linux")
    with pytest.raises(adapter.TypingError, match="only supported on Windows"):
        adapter.send_character("a")


# --- Architecture rule: only the Executor adapter sends input to other programs ---

_INPUT_CALLS = {"SendInput", "keybd_event", "mouse_event", "SetCursorPos", "PostMessageW", "PostMessageA"}


def test_only_the_executor_adapter_sends_input():
    root = settings.PROJECT_ROOT
    allowed = root / "app" / "executor" / "adapter.py"
    offenders = sorted({
        f"{path.relative_to(root)}: {name}"
        for path in [*(root / "app").rglob("*.py"), root / "main.py"] if path != allowed
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        for name in [getattr(node, "attr", None) or getattr(node, "id", None)] if name in _INPUT_CALLS
    })
    assert offenders == [], f"Only app/executor/adapter.py may send input: {offenders}"
