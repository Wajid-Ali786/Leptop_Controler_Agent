"""
Tests for keyboard shortcuts (app/executor/shortcuts.py, logic.py, adapter.send_shortcut/release_keys).

No real keyboard input is ever sent: the active window, held modifier keys, the clipboard's change
counter and data kinds, the field's length and selection, and the key sending itself are all faked,
and the real SendInput is blocked. The REAL safety gate and the REAL Verifier logic decide every
action. Clipboard CONTENTS are never read - a rule test checks no code can.
"""
import ast
import logging
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from app.executor import adapter, emergency_stop, logic, shortcuts
from app.executor.emergency_stop import EmergencyStopError
from app.executor.logic import execute, execute_with_recovery
from app.executor.models import SHORTCUT, ExecutorAction, Outcome
from app.safety import logic as safety_logic
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
    "verifier:\n"
    "  window_timeout_seconds: 0.2\n"
    "  poll_interval_seconds: 0.01\n"
    "  shortcut_settle_seconds: 0.05\n"
    "  app_windows:\n"
    '    notepad: "Notepad$"\n'
)
NOTEPAD = WindowInfo(500, "Untitled - Notepad", "Notepad")
FIELD = 501
IN_NOTEPAD = ActiveTarget(NOTEPAD, FIELD, "Edit")
OTHER = ActiveTarget(WindowInfo(900, "Inbox - Mail", "Mail"), 901, "Edit")
DESKTOP = ActiveTarget(WindowInfo(65554, "", "Progman"), None, "")
SUPPORTED_NAMES = ["Ctrl+A", "Win+D", "Ctrl+C", "Ctrl+S", "Ctrl+Z", "Ctrl+X", "Alt+Tab", "Ctrl+V", "Alt+F4"]


class FakeDesktop:
    """Hooks: on_target_read[n] runs before the n-th active-window read. `accept` is how many key events
    Windows accepts (None: all). `stuck` modifiers read as held after sending until released."""

    def __init__(self, calls):
        self.calls = calls
        self.target = IN_NOTEPAD
        self.target_reads = 0
        self.on_target_read = {}
        self.held = []
        self.stuck_after_send = []
        self.stuck = []
        self.release_fixes_stuck = True
        self.clipboard = 7
        self.clipboard_error = None
        self.kinds = ["text"]
        self.length = 10
        self.selected = (0, 0)
        self.accept = None
        self.effects = True
        self.windows = {NOTEPAD.handle: NOTEPAD}

    def active_target(self):
        self.target_reads += 1
        self.on_target_read.get(self.target_reads, lambda: None)()
        return self.target

    def modifier_keys_down(self):
        return list(dict.fromkeys(self.held + self.stuck))

    def clipboard_sequence_number(self):
        if self.clipboard_error:
            raise self.clipboard_error
        return self.clipboard

    def clipboard_kinds(self):
        return list(self.kinds)

    def text_length(self, handle):
        return self.length if handle == FIELD else None

    def selection(self, handle):
        return self.selected if handle == FIELD else None

    def list_windows(self):
        return list(self.windows.values())

    def list_child_windows(self, handle):
        return []

    def send_shortcut(self, modifiers, key):
        name = "+".join((*modifiers, key))
        self.calls.append(("send", name))
        expected = 2 * len(modifiers) + 2
        accepted = expected if self.accept is None else self.accept
        if accepted == expected and self.effects:
            self.apply(name)
        self.stuck = list(self.stuck_after_send)
        return accepted, expected

    def release_keys(self, modifiers, key):
        self.calls.append(("release", "+".join((*modifiers, key))))
        if self.release_fixes_stuck:
            self.stuck = []
        return True

    def apply(self, name):
        if name == "Ctrl+C":
            self.clipboard += 1
        elif name == "Ctrl+X":
            self.clipboard += 1
            self.length -= 3
        elif name == "Ctrl+A":
            self.selected = (0, self.length)
        elif name == "Alt+Tab":
            self.target = OTHER
        elif name == "Win+D":
            self.target = DESKTOP
        elif name == "Alt+F4":
            self.windows.pop(self.target.window.handle, None)
            self.target = OTHER


@pytest.fixture
def world(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_PATH", config_path)
    calls = []
    desktop = FakeDesktop(calls)
    for name in ("active_target", "modifier_keys_down", "clipboard_sequence_number", "clipboard_kinds",
                 "text_length", "selection", "list_windows", "list_child_windows"):
        monkeypatch.setattr(verifier_adapter, name, getattr(desktop, name))
    monkeypatch.setattr(adapter, "send_shortcut", desktop.send_shortcut)
    monkeypatch.setattr(adapter, "release_keys", desktop.release_keys)

    def no_real_keyboard():
        raise AssertionError("real keyboard input must not be sent in this test")
    monkeypatch.setattr(adapter, "_keyboard_api", no_real_keyboard)
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


def press(world, shortcut, answer=True):
    return execute(ExecutorAction(SHORTCUT, shortcut), approve(world, answer))


def sent(world):
    return [call[1] for call in world.calls if call[0] == "send"]


def confirmations(world):
    return [call for call in world.calls if call[0] == "confirm"]


def open_in_session(world):
    logic._remember_opened("notepad", frozenset({NOTEPAD.handle}))


# --- Parsing and the allow-list ---

@pytest.mark.parametrize("text, name", [
    ("ctrl+c", "Ctrl+C"), ("CTRL + C", "Ctrl+C"), ("control+c", "Ctrl+C"), (" a + ctrl ", "Ctrl+A"),
    ("windows+d", "Win+D"), ("win+D", "Win+D"), ("tab+alt", "Alt+Tab"), ("alt+f4", "Alt+F4"),
])
def test_names_are_normalized(text, name):
    assert shortcuts.parse(text).name == name


@pytest.mark.parametrize("text, message", [
    ("", "Which shortcut should I press? For example: Ctrl+C."),
    ("ctrl++c", "I can't read the shortcut 'ctrl++c': join key names with +, e.g. Ctrl+C."),
    ("ctrl+foo", "I don't know a key called 'foo'."),
    ("ctrl+ctrl+c", "'ctrl+ctrl+c' names the same modifier twice."),
    ("ctrl+alt", "'ctrl+alt' needs exactly one key besides Ctrl, Alt, Shift or Win."),
    ("ctrl+a+b", "'ctrl+a+b' needs exactly one key besides Ctrl, Alt, Shift or Win."),
    ("shift+ctrl+z", "Ctrl+Shift+Z isn't a supported shortcut yet. Supported: Ctrl+A, Win+D, Ctrl+C, Ctrl+S, "
                     "Ctrl+Z, Ctrl+X, Alt+Tab, Ctrl+V, Alt+F4."),
    ("f5", "F5 isn't available as a shortcut. Use the Refresh action instead: it checks which app is active first, "
           "because these keys do different things in different apps."),
    ("ctrl+r", "Ctrl+R isn't available as a shortcut. Use the Refresh action instead: it checks which app is active "
               "first, because these keys do different things in different apps."),
    ("control+f5", "Ctrl+F5 isn't available as a shortcut. Use the Refresh action instead: it checks which app is "
                   "active first, because these keys do different things in different apps."),
    ("f5+shift", "Shift+F5 isn't available as a shortcut. Use the Refresh action instead: it checks which app is "
                 "active first, because these keys do different things in different apps."),
    ("shift+ctrl+r", "Ctrl+Shift+R isn't available as a shortcut. Use the Refresh action instead: it checks which app "
                     "is active first, because these keys do different things in different apps."),
    ("ctrl+alt+del", "Ctrl+Alt+Delete is a reserved Windows shortcut. This assistant will not send it."),
    ("delete+alt+control", "Ctrl+Alt+Delete is a reserved Windows shortcut. This assistant will not send it."),
    ("win+l", "Win+L is a reserved Windows shortcut. This assistant will not send it: it's intentionally "
              "unsupported in Phase 1 because it locks the Windows session."),
])
def test_malformed_unknown_unsupported_and_reserved_are_refused(text, message):
    assert shortcuts.parse(text) == shortcuts.ShortcutRefusal(message)


@pytest.mark.parametrize("name, risk", [
    ("Ctrl+A", RiskLevel.LOW), ("Win+D", RiskLevel.LOW),
    ("Ctrl+C", RiskLevel.MEDIUM), ("Ctrl+S", RiskLevel.MEDIUM), ("Ctrl+Z", RiskLevel.MEDIUM),
    ("Ctrl+X", RiskLevel.MEDIUM), ("Alt+Tab", RiskLevel.MEDIUM),
    ("Ctrl+V", RiskLevel.HIGH), ("Alt+F4", RiskLevel.HIGH),
])
def test_risk_table(name, risk):
    assert shortcuts.SUPPORTED[name].risk == risk
    assert list(shortcuts.SUPPORTED) == SUPPORTED_NAMES


@pytest.mark.parametrize("text", ["ctrl+c", "win+l", "ctrl+alt+del", "f5", "ctrl+w"])
def test_refused_or_unsupported_shortcuts_send_nothing_and_ask_nothing(world, text):
    result = press(world, text)
    assert result.ok is (text == "ctrl+c")
    if text != "ctrl+c":
        assert world.calls == [] and world.authorized == []


# --- LOW: runs without asking (but still through the gate) ---

def test_ctrl_a_runs_without_confirmation_and_is_verified_in_an_edit_field(world):
    result = press(world, "ctrl+a")
    assert result.ok and result.verified and result.outcome is Outcome.DONE
    assert result.message == "Pressed Ctrl+A; everything in the field is selected."
    assert confirmations(world) == [] and sent(world) == ["Ctrl+A"]
    assert [a.description for a in world.authorized] == ["press Ctrl+A"]  # no window title for the gate


def test_ctrl_a_in_a_field_that_cant_report_its_selection_is_unverified(world):
    world.desktop.target = ActiveTarget(NOTEPAD, 777, "Chrome_WidgetWin_1")
    result = press(world, "ctrl+a")
    assert result.outcome is Outcome.UNVERIFIED and result.message == "Pressed Ctrl+A. I can't check the selection in this field."


def test_ctrl_a_that_selects_nothing_is_unverified(world):
    world.desktop.effects = False
    assert press(world, "ctrl+a").message == "Pressed Ctrl+A, but I couldn't confirm everything is selected."


def test_win_d_runs_without_confirmation_even_with_no_active_window(world):
    world.desktop.target = ActiveTarget(None)
    result = press(world, "win+d")
    assert result.outcome is Outcome.DONE and result.message == "Pressed Win+D; the desktop is showing."
    assert confirmations(world) == []


def test_win_d_that_doesnt_show_the_desktop_is_unverified(world):
    world.desktop.effects = False
    assert press(world, "win+d").outcome is Outcome.UNVERIFIED


# --- MEDIUM and HIGH: always confirmed, with exact prompts ---

@pytest.mark.parametrize("text, prompt, risk", [
    ("ctrl+c", 'press Ctrl+C in window "Untitled - Notepad" (field: Edit) - copies the selection to the clipboard, '
               "replacing what's on it; in a terminal or console it stops the running program", RiskLevel.MEDIUM),
    ("ctrl+s", 'press Ctrl+S in window "Untitled - Notepad" (field: Edit) - saves the document - this can '
               "overwrite an existing file", RiskLevel.MEDIUM),
    ("ctrl+z", 'press Ctrl+Z in window "Untitled - Notepad" (field: Edit) - undoes the last change; some apps '
               "can't redo it", RiskLevel.MEDIUM),
    ("ctrl+x", 'press Ctrl+X in window "Untitled - Notepad" (field: Edit) - cuts the selection: removes it from '
               "the document and replaces what's on the clipboard", RiskLevel.MEDIUM),
    ("alt+tab", "press Alt+Tab - switches to another window (which one can't be known in advance); following "
                "actions go to that window", RiskLevel.MEDIUM),
    ("ctrl+v", 'press Ctrl+V in window "Untitled - Notepad" (field: Edit) - pastes the clipboard (it holds: text). '
               "I can't see what's on it; pasting into a terminal or chat can run or send it", RiskLevel.HIGH),
])
def test_confirmed_shortcuts_show_exact_prompts(world, text, prompt, risk):
    press(world, text)
    [(_, description, level, rule)] = confirmations(world)
    assert (description, level) == (prompt, risk)
    assert rule == shortcuts.parse(text).reason and "Notepad" not in rule


def test_ctrl_c_is_medium_in_every_window_no_terminal_detection(world):
    for target in (IN_NOTEPAD, ActiveTarget(WindowInfo(3, "Windows PowerShell", "ConsoleWindowClass"), 4, ""),
                   ActiveTarget(WindowInfo(5, "Chat", "Chrome_WidgetWin_1"), 6, "Chrome_RenderWidgetHostHWND")):
        world.desktop.target = target
        press(world, "ctrl+c")
    assert [c[2] for c in confirmations(world)] == [RiskLevel.MEDIUM] * 3


def test_ctrl_v_prompt_names_the_kinds_of_clipboard_data(world):
    world.desktop.kinds = ["text", "files"]
    press(world, "ctrl+v")
    assert "(it holds: text, files)" in confirmations(world)[0][1]


def test_confirmed_shortcut_declined_sends_nothing(world):
    with pytest.raises(ActionDeniedError, match="did not confirm"):
        press(world, "ctrl+z", answer=False)
    assert sent(world) == []


def test_confirmed_shortcut_without_a_confirmation_method_is_denied(world):
    with pytest.raises(ActionDeniedError, match="shortcut Ctrl\\+V"):
        execute(ExecutorAction(SHORTCUT, "ctrl+v"))
    assert sent(world) == []


# --- Validation against the desktop ---

def test_window_shortcut_needs_an_active_window(world):
    world.desktop.target = ActiveTarget(None)
    result = press(world, "ctrl+c")
    assert not result.ok and result.message == "Didn't press Ctrl+C: there's no active window."
    assert world.calls == []


@pytest.mark.parametrize("held, words", [(["Shift"], "Shift is held"), (["Ctrl", "Alt"], "Ctrl and Alt are held")])
def test_modifier_held_on_the_keyboard_is_refused(world, held, words):
    world.desktop.held = held
    result = press(world, "ctrl+z")
    assert not result.ok and words in result.message and "which would change the shortcut" in result.message
    assert world.calls == []


def test_modifier_pressed_after_approval_means_nothing_is_sent(world):
    def user_holds_shift(action, assessment):
        world.desktop.held = ["Shift"]
        return True
    result = execute(ExecutorAction(SHORTCUT, "ctrl+z"), user_holds_shift)
    assert not result.ok and "Shift is held down" in result.message
    assert sent(world) == []


def test_empty_clipboard_means_nothing_to_paste(world):
    world.desktop.kinds = []
    result = press(world, "ctrl+v")
    assert result.message == "The clipboard is empty, so there's nothing to paste."
    assert world.calls == []


@pytest.mark.parametrize("changed", [
    OTHER, ActiveTarget(NOTEPAD, 777, "Edit"), ActiveTarget(WindowInfo(500, "Terminal", "Notepad"), FIELD, "Edit"),
])
def test_changed_target_after_approval_means_nothing_is_pressed(world, changed):
    world.desktop.on_target_read[2] = lambda: setattr(world.desktop, "target", changed)
    result = press(world, "ctrl+s")
    assert result.message == "The active window changed after you approved, so I didn't press Ctrl+S."
    assert sent(world) == []


# --- Alt+F4: only on this session's windows ---

def test_alt_f4_closes_a_window_the_assistant_opened(world):
    open_in_session(world)
    result = press(world, "alt+f4")
    assert result.outcome is Outcome.DONE and result.message.startswith("Pressed Alt+F4; the window closed after")
    assert confirmations(world)[0][1:3] == (
        'press Alt+F4 on window "Untitled - Notepad" - closes it; unsaved work may be lost if the app doesn\'t ask',
        RiskLevel.HIGH)
    assert logic._session_windows["notepad"] == []  # forgotten once closed


def test_alt_f4_on_a_window_the_assistant_did_not_open_is_refused(world):
    result = press(world, "alt+f4")
    assert result.message == ("I only press Alt+F4 on windows I opened in this session, and the active window "
                              "isn't one of them, so I left it alone.")
    assert world.calls == []


def test_alt_f4_with_the_desktop_active_is_refused(world):
    world.desktop.target = DESKTOP
    result = press(world, "alt+f4")
    assert result.message == "Alt+F4 with the desktop or taskbar active opens the Shut Down dialog, so I won't press it."
    assert world.calls == []


def test_alt_f4_on_an_app_asking_to_save_needs_the_user(world):
    open_in_session(world)
    world.desktop.effects = False
    world.desktop.windows[NOTEPAD.handle] = WindowInfo(500, "Untitled - Notepad", "Notepad", enabled=False)
    result = press(world, "alt+f4")
    assert result.outcome is Outcome.NEEDS_USER and not result.retryable


def test_alt_f4_that_leaves_the_window_open_is_still_open(world):
    open_in_session(world)
    world.desktop.effects = False
    assert press(world, "alt+f4").outcome is Outcome.STILL_OPEN


# --- Sending: one batch; partial acceptance releases everything ---

def test_zero_accepted_events_is_a_failure_with_nothing_released(world):
    world.desktop.accept = 0
    result = press(world, "ctrl+z")
    assert not result.ok and result.outcome is Outcome.FAILED and not result.retryable
    assert result.message == "Windows didn't accept the keyboard input for Ctrl+Z, so nothing was pressed."
    assert [c for c in world.calls if c[0] == "release"] == []


@pytest.mark.parametrize("accepted", [1, 2, 3])
def test_partly_accepted_input_releases_every_key_and_is_unverified(world, accepted):
    world.desktop.accept = accepted
    result = press(world, "ctrl+z")
    assert result.ok and not result.verified and result.outcome is Outcome.UNVERIFIED and not result.retryable
    assert result.message == ("Windows accepted only part of the keyboard input for Ctrl+Z, so I released every key "
                              "involved. The shortcut may or may not have taken effect.")
    assert [c for c in world.calls if c[0] == "release"] == [("release", "Ctrl+Z")]


def test_modifier_still_reading_as_down_is_released_again(world):
    world.desktop.stuck_after_send = ["Ctrl"]
    result = press(world, "ctrl+c")
    assert result.outcome is Outcome.DONE and result.message == "Pressed Ctrl+C; the clipboard was updated."
    assert [c for c in world.calls if c[0] == "release"] == [("release", "Ctrl+C")]


def test_modifier_that_stays_down_gets_an_honest_note(world):
    world.desktop.stuck_after_send = ["Alt"]
    world.desktop.release_fixes_stuck = False
    result = press(world, "alt+tab")
    assert result.message == ("Pressed Alt+Tab; a different window is now active. Alt may still be held down; "
                              "press and release it once.")


# --- Emergency stop ---

def test_emergency_stop_before_anything(world):
    emergency_stop.trigger("hotkey")
    with pytest.raises(EmergencyStopError):
        press(world, "ctrl+a")
    assert world.calls == []


def test_stop_during_confirmation_sends_nothing(world):
    def confirm_then_stop(action, assessment):
        emergency_stop.trigger("hotkey")
        return True
    with pytest.raises(EmergencyStopError):
        execute(ExecutorAction(SHORTCUT, "ctrl+z"), confirm_then_stop)
    assert sent(world) == []


def test_stop_right_before_sending_sends_nothing(world):
    world.desktop.on_target_read[2] = lambda: emergency_stop.trigger("hotkey")
    with pytest.raises(EmergencyStopError):
        press(world, "ctrl+a")
    assert sent(world) == []


def test_stop_while_checking_the_effect(world, caplog):
    world.config_path.write_text(CONFIG.replace("shortcut_settle_seconds: 0.05", "shortcut_settle_seconds: 5"),
                                 encoding="utf-8")
    world.desktop.effects = False
    threading.Timer(0.1, emergency_stop.trigger, args=("hotkey",)).start()
    started = time.monotonic()
    with pytest.raises(EmergencyStopError):
        press(world, "alt+tab")
    assert time.monotonic() - started < 2 and sent(world) == ["Alt+Tab"]
    assert "the keys were already sent and released" in caplog.text


# --- Verification: done only on evidence ---

def test_ctrl_c_is_done_when_the_clipboard_counter_moves(world):
    assert press(world, "ctrl+c").outcome is Outcome.DONE


def test_ctrl_c_with_nothing_copied_is_unverified(world):
    world.desktop.effects = False
    result = press(world, "ctrl+c")
    assert result.outcome is Outcome.UNVERIFIED
    assert result.message == "Pressed Ctrl+C, but I couldn't confirm the clipboard changed (maybe nothing was selected)."


def test_ctrl_c_with_an_unreadable_clipboard_counter_is_unverified(world):
    world.desktop.clipboard_error = verifier_adapter.VerifierAdapterError("no clipboard")
    assert press(world, "ctrl+c").outcome is Outcome.UNVERIFIED


def test_ctrl_x_is_done_only_when_the_clipboard_changed_and_the_text_got_shorter(world):
    result = press(world, "ctrl+x")
    assert result.outcome is Outcome.DONE
    assert result.message == "Pressed Ctrl+X; the clipboard was updated and the field's text got shorter."


def test_ctrl_x_with_only_the_clipboard_changing_is_unverified(world, monkeypatch):
    monkeypatch.setattr(world.desktop, "apply", lambda name: setattr(world.desktop, "clipboard", 99))
    assert press(world, "ctrl+x").message == "Pressed Ctrl+X, but I couldn't confirm it cut anything."


@pytest.mark.parametrize("text, name", [("ctrl+z", "Ctrl+Z"), ("ctrl+s", "Ctrl+S"), ("ctrl+v", "Ctrl+V")])
def test_shortcuts_with_nothing_observable_are_unverified(world, text, name):
    result = press(world, text)
    assert result.ok and not result.verified and result.outcome is Outcome.UNVERIFIED
    assert result.message == f"Pressed {name}. I can't check what it did."


def test_alt_tab_is_done_when_a_different_window_is_active(world):
    assert press(world, "alt+tab").message == "Pressed Alt+Tab; a different window is now active."


def test_alt_tab_that_visibly_didnt_switch_is_a_failure(world):
    world.desktop.effects = False
    result = press(world, "alt+tab")
    assert not result.ok and result.outcome is Outcome.FAILED and not result.retryable
    assert result.message == "Pressed Alt+Tab, but the active window didn't change."


# --- Recovery: shortcuts are never retried ---

@pytest.mark.parametrize("text, setup", [
    ("ctrl+z", lambda d: setattr(d, "accept", 0)),
    ("ctrl+z", lambda d: setattr(d, "accept", 2)),
    ("alt+tab", lambda d: setattr(d, "effects", False)),
    ("ctrl+v", lambda d: None),
    ("ctrl+c", lambda d: None),
], ids=["failed", "partly-accepted", "didnt-switch", "unverified", "done"])
def test_shortcuts_are_never_retried(world, text, setup):
    setup(world.desktop)
    offers = []
    result = execute_with_recovery(ExecutorAction(SHORTCUT, text), approve(world),
                                   offer_retry=lambda r: offers.append(r) or True)
    assert offers == [] and not result.retryable and len(sent(world)) == 1


# --- Privacy: window titles and clipboard details never reach logs, results or errors ---

TITLE = "Payroll-zq88x1.xlsx - Excel"
KIND = "KIND-zq7731"


def _collect(world, caplog, text, answer=True):
    caplog.set_level(logging.DEBUG)
    world.desktop.target = ActiveTarget(WindowInfo(500, TITLE, "XLMAIN"), FIELD, "EXCEL7")
    world.desktop.kinds = [KIND]
    outputs = []
    try:
        result = press(world, text, answer)
        outputs += [result, result.message]
    except ActionDeniedError as exc:
        outputs += [exc, exc.args, exc.assessment]
    outputs += [a.minimum_reason for a in world.authorized] + [repr(a) for a in world.authorized]
    outputs += [c[3] for c in confirmations(world)]  # the logged safety rules
    return outputs + [caplog.text, *[r.getMessage() for r in caplog.records]]


def _no_leak(*things):
    for thing in things:
        for secret in (TITLE, KIND):
            assert secret not in str(thing) and secret not in repr(thing), f"{secret!r} leaked"


@pytest.mark.parametrize("text, answer", [("ctrl+v", True), ("ctrl+v", False), ("ctrl+a", True), ("alt+tab", True),
                                          ("ctrl+z", True)])
def test_titles_and_clipboard_details_never_leak(world, caplog, text, answer):
    _no_leak(*_collect(world, caplog, text, answer))
    if text == "ctrl+v":  # proves the strings were really in play: the on-screen prompt shows them
        assert TITLE in confirmations(world)[0][1] and KIND in confirmations(world)[0][1]


def test_the_leak_check_catches_a_planted_leak(world, caplog, monkeypatch):
    real_prompt = logic._shortcut_prompt
    monkeypatch.setattr(logic, "_shortcut_prompt",
                        lambda *args: logging.getLogger("app.executor.logic").info(real_prompt(*args)) or real_prompt(*args))
    with pytest.raises(AssertionError, match="leaked"):
        _no_leak(*_collect(world, caplog, "ctrl+v"))


# --- Adapter: one SendInput batch; defensive release (a fake SendInput - no real input) ---

@pytest.fixture
def fake_send_input(monkeypatch):
    if sys.platform != "win32":
        pytest.skip("the keyboard structures are Windows-only")
    api = adapter._KeyboardApi()
    batches, answer = [], {"accepted": None}

    def send_input(count, inputs, size):
        batches.append([(inputs[i].ki.wVk, inputs[i].ki.dwFlags) for i in range(count)])
        return count if answer["accepted"] is None else answer["accepted"]
    api.SendInput = send_input
    api.MapVirtualKeyW = lambda code, kind: code + 1000
    monkeypatch.setattr(adapter, "_keyboard_api", lambda: api)
    return SimpleNamespace(batches=batches, answer=answer)


DOWN, UP, EXTENDED = 0, 0x2, 0x1


def test_adapter_sends_the_whole_shortcut_in_one_batch(fake_send_input):
    assert adapter.send_shortcut(("Ctrl",), "C") == (4, 4)
    assert fake_send_input.batches == [[(0x11, DOWN), (0x43, DOWN), (0x43, UP), (0x11, UP)]]


def test_adapter_marks_the_win_key_extended_and_releases_in_reverse(fake_send_input):
    adapter.send_shortcut(("Win",), "D")
    adapter.send_shortcut(("Alt",), "Tab")
    assert fake_send_input.batches == [
        [(0x5B, DOWN | EXTENDED), (0x44, DOWN), (0x44, UP), (0x5B, UP | EXTENDED)],
        [(0x12, DOWN), (0x09, DOWN), (0x09, UP), (0x12, UP)],
    ]


def test_adapter_reports_how_many_events_windows_accepted(fake_send_input):
    fake_send_input.answer["accepted"] = 2
    assert adapter.send_shortcut(("Alt",), "F4") == (2, 4)


def test_adapter_release_taps_an_unassigned_key_before_releasing_alt_or_win(fake_send_input):
    assert adapter.release_keys(("Alt",), "F4") is True
    assert adapter.release_keys(("Ctrl",), "Z") is True
    assert fake_send_input.batches == [
        [(0x73, UP), (0xE8, DOWN), (0xE8, UP), (0x12, UP)],
        [(0x5A, UP), (0x11, UP)],
    ]


def test_adapter_includes_scan_codes(fake_send_input, monkeypatch):
    seen = []
    api = adapter._keyboard_api()
    real = api.SendInput
    api.SendInput = lambda count, inputs, size: seen.extend(inputs[i].ki.wScan for i in range(count)) or real(count, inputs, size)
    adapter.send_shortcut(("Ctrl",), "A")
    assert seen == [0x11 + 1000, 0x41 + 1000, 0x41 + 1000, 0x11 + 1000]


def test_adapter_refuses_off_windows(monkeypatch):
    monkeypatch.setattr(adapter.sys, "platform", "linux")
    with pytest.raises(adapter.ShortcutError, match="only supported on Windows"):
        adapter.send_shortcut(("Ctrl",), "C")


# --- Architecture rule: clipboard contents are never read, written or cleared ---

_CLIPBOARD_CONTENT_CALLS = {"OpenClipboard", "GetClipboardData", "SetClipboardData", "EmptyClipboard",
                            "GetClipboardOwner", "GetOpenClipboardWindow", "EnumClipboardFormats"}


def test_no_code_touches_clipboard_contents():
    root = settings.PROJECT_ROOT
    offenders = sorted({
        f"{path.relative_to(root)}: {name}"
        for path in [*(root / "app").rglob("*.py"), root / "main.py"]
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        for name in [getattr(node, "attr", None) or getattr(node, "id", None)
                     or (node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None)]
        if name in _CLIPBOARD_CONTENT_CALLS
    })
    assert offenders == [], f"Clipboard contents must never be read: {offenders}"
