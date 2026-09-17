"""
Tests for scroll (app/executor/logic.py + adapter.send_wheel_notch) - vertical mouse-wheel notches
sent to the active window, which must also be the window under the pointer.

No real mouse input is ever sent: the active window, the pointer, the control chain under it, held
modifier keys, scroll bar positions and the wheel itself are faked, and the real SendInput is
blocked. The REAL safety gate and the REAL Verifier logic decide every action.
"""
import logging
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from app.executor import adapter, emergency_stop, logic
from app.executor.emergency_stop import ActionInterruptedError, EmergencyStopError, TypingInterruptedError
from app.executor.logic import execute, execute_with_recovery
from app.executor.models import SCROLL, ExecutorAction, Outcome
from app.safety import logic as safety_logic
from app.safety.logic import ActionDeniedError
from app.safety.models import RiskLevel
from app.verifier import adapter as verifier_adapter
from app.verifier.models import ActiveTarget, ControlInfo, ScrollState, WindowInfo
from config import settings

CONFIG = (
    "safety:\n"
    '  risky_keywords: [delete, shutdown, "shut down", send, close]\n'
    "  safe_words: [sender]\n"
    "executor:\n"
    "  apps:\n"
    "    notepad: notepad.exe\n"
    "  max_attempts: 3\n"
    "  max_scroll_notches: 20\n"
    "  max_unclassified_notches: 3\n"
    "  scroll_interval_seconds: 0\n"
    "verifier:\n"
    "  window_timeout_seconds: 0.2\n"
    "  poll_interval_seconds: 0.01\n"
    "  scroll_settle_seconds: 0.05\n"
    "  app_windows:\n"
    '    notepad: "Notepad$"\n'
)
NOTEPAD = WindowInfo(500, "Untitled - Notepad", "Notepad")
EDIT = ControlInfo(501, "Edit")
NOTEPAD_CHAIN = [EDIT, ControlInfo(500, "Notepad")]
BROWSER = WindowInfo(700, "News - Browser", "Chrome_WidgetWin_1")
BROWSER_CHAIN = [ControlInfo(701, "Chrome_RenderWidgetHostHWND"), ControlInfo(700, "Chrome_WidgetWin_1")]
MIDDLE = ScrollState(position=50, minimum=0, maximum=199, page=30)


class FakeDesktop:
    """on_read[n] runs before the n-th pointer-chain read; on_notch[i] before notch i is sent. Each accepted
    notch moves the scroll bar of `scrolls_handle` by 3 (clamped) unless `effects` is off."""

    def __init__(self, calls):
        self.calls = calls
        self.target = ActiveTarget(NOTEPAD, EDIT.handle, "Edit")
        self.chain = list(NOTEPAD_CHAIN)
        self.reads = 0
        self.on_read = {}
        self.on_notch = {}
        self.held = []
        self.scroll = {EDIT.handle: MIDDLE}
        self.scrolls_handle = EDIT.handle
        self.effects = True
        self.accept = {}  # notch index -> False to refuse it
        self.error = None

    def active_target(self):
        return self.target

    def cursor_position(self):
        if self.error:
            raise self.error
        return (300, 200)

    def control_chain_at(self, x, y):
        self.reads += 1
        self.on_read.get(self.reads, lambda: None)()
        return list(self.chain)

    def modifier_keys_down(self):
        return list(self.held)

    def vertical_scroll(self, handle):
        return self.scroll.get(handle)

    def send_wheel_notch(self, up):
        index = len(self.notches())
        self.on_notch.get(index, lambda: None)()
        if self.accept.get(index) is False:
            return False
        self.calls.append(("notch", "up" if up else "down"))
        state = self.scroll.get(self.scrolls_handle)
        if self.effects and state:
            low, high = state.minimum, max(state.minimum, state.maximum - state.page + 1)
            position = min(high, max(low, state.position + (-3 if up else 3)))
            self.scroll[self.scrolls_handle] = ScrollState(position, state.minimum, state.maximum, state.page)
        return True

    def notches(self):
        return [call[1] for call in self.calls if call[0] == "notch"]


@pytest.fixture
def world(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_PATH", config_path)
    calls = []
    desktop = FakeDesktop(calls)
    for name in ("active_target", "cursor_position", "control_chain_at", "modifier_keys_down", "vertical_scroll"):
        monkeypatch.setattr(verifier_adapter, name, getattr(desktop, name))
    monkeypatch.setattr(adapter, "send_wheel_notch", desktop.send_wheel_notch)

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


def scroll(world, target, answer=True):
    return execute(ExecutorAction(SCROLL, target), approve(world, answer))


def confirmations(world):
    return [call for call in world.calls if call[0] == "confirm"]


def in_browser(world):
    world.desktop.target = ActiveTarget(BROWSER, 701, "Chrome_RenderWidgetHostHWND")
    world.desktop.chain = list(BROWSER_CHAIN)
    world.desktop.scroll = {}


# --- Input format ---

@pytest.mark.parametrize("target, message", [
    ("", "Which way and how far should I scroll? For example: down 3."),
    ("+3", "Say up or down instead of + or -. For example: down 3."),
    ("-3", "Say up or down instead of + or -. For example: down 3."),
    ("left 3", "Horizontal scrolling isn't supported yet."),
    ("Right 2", "Horizontal scrolling isn't supported yet."),
    ("down", "How many notches? For example: down 3."),
    ("down 0", "Scroll at least 1 notch."),
    ("down 21", "That's 21 notches; I scroll at most 20 at once."),
    ("down 999", "That's 999 notches; I scroll at most 20 at once."),
    ("down 3.5", "I can't read 'down 3.5': say up or down and a number of notches. For example: down 3."),
    ("down -3", "I can't read 'down -3': say up or down and a number of notches. For example: down 3."),
    ("sideways 2", "I can't read 'sideways 2': say up or down and a number of notches. For example: down 3."),
])
def test_invalid_input_fails_cleanly_and_asks_nothing(world, target, message):
    result = scroll(world, target)
    assert not result.ok and not result.retryable and result.message == message
    assert world.calls == [] and world.authorized == [] and world.desktop.reads == 0


@pytest.mark.parametrize("target, direction", [("down 5", "down"), ("UP 5", "up"), ("  down   5 ", "down")])
def test_directions_and_spacing(world, target, direction):
    result = scroll(world, target)
    assert world.desktop.notches() == [direction] * 5 and result.progress == (5, 5)


# --- LOW: no prompt; done only when the scroll bar moved ---

def test_scroll_runs_without_confirmation_and_is_verified(world):
    result = scroll(world, "down 5")
    assert result.ok and result.verified and result.outcome is Outcome.DONE and not result.retryable
    assert result.message == "Scrolled down 5 notches; the scroll position moved down."
    assert confirmations(world) == []
    assert [a.description for a in world.authorized] == ["scroll down 5 notches"]  # the gate sees no title


def test_scroll_bar_on_a_parent_counts(world):
    world.desktop.chain = [ControlInfo(9, "Pane"), EDIT, ControlInfo(500, "Notepad")]
    world.desktop.target = ActiveTarget(NOTEPAD, 9, "Pane")
    assert scroll(world, "up 2").outcome is Outcome.DONE


# --- MEDIUM only over value-changing controls, found on the control or its parents ---

@pytest.mark.parametrize("chain, label", [
    ([ControlInfo(10, "ComboBox"), ControlInfo(500, "Notepad")], "a drop-down list"),
    ([ControlInfo(11, "Edit"), ControlInfo(10, "ComboBox"), ControlInfo(500, "Notepad")], "a drop-down list"),
    ([ControlInfo(11, "Edit"), ControlInfo(10, "ComboBox"), ControlInfo(12, "ComboBoxEx32"), ControlInfo(500, "Notepad")],
     "a drop-down list"),
    ([ControlInfo(13, "msctls_trackbar32"), ControlInfo(500, "Notepad")], "a slider"),
    ([ControlInfo(14, "MSCTLS_UPDOWN32"), ControlInfo(500, "Notepad")], "a spin box"),
    ([ControlInfo(15, "SysDateTimePick32"), ControlInfo(16, "Group"), ControlInfo(500, "Notepad")], "a date picker"),
], ids=["combo", "edit-inside-combo", "inside-comboboxex", "slider", "spin-case-insensitive", "date-in-group"])
def test_value_changing_controls_need_confirmation(world, chain, label):
    world.desktop.chain = chain
    scroll(world, "down 2")
    [(_, description, level, rule)] = confirmations(world)
    assert description == (f'scroll down 2 notches over {label} in window "Untitled - Notepad" - scrolling over it '
                           f"changes its value")
    assert (level, rule) == (RiskLevel.MEDIUM, "scrolling over a control whose value the wheel changes")


def test_value_changing_control_declined_scrolls_nothing(world):
    world.desktop.chain = [ControlInfo(10, "ComboBox"), ControlInfo(500, "Notepad")]
    with pytest.raises(ActionDeniedError, match="did not confirm"):
        scroll(world, "down 2", answer=False)
    assert world.desktop.notches() == []


def test_prompt_for_a_window_without_a_title(world):
    world.desktop.target = ActiveTarget(WindowInfo(500, "", "Notepad"), 10, "ComboBox")
    world.desktop.chain = [ControlInfo(10, "ComboBox"), ControlInfo(500, "Notepad")]
    scroll(world, "up 1")
    assert confirmations(world)[0][1] == ("scroll up 1 notch over a drop-down list in a window with no readable "
                                          "title - scrolling over it changes its value")


# --- Unclassified surfaces (no standard scroll bar): MEDIUM - always asked - AND capped at 3 notches ---

UNCLASSIFIED_RULE = "scrolling a surface whose scroll area can't be identified"


def test_unclassified_target_is_medium_and_needs_confirmation(world):
    in_browser(world)
    result = scroll(world, "down 10")
    [(_, description, level, rule)] = confirmations(world)
    assert description == ('scroll down 3 notches in window "News - Browser" - I couldn\'t confidently identify this '
                           "window's scroll area, so for safety I'll send at most 3 notches (you asked for 10)")
    assert (level, rule) == (RiskLevel.MEDIUM, UNCLASSIFIED_RULE)
    assert world.desktop.notches() == ["down"] * 3  # approved down 10: at most 3 sent
    assert result.ok and result.outcome is Outcome.UNVERIFIED and result.progress == (3, 10)
    assert result.message == ("Scrolled down 3 notches. I can't read this window's scroll position, so I can't confirm "
                              "it moved. I couldn't identify this window's scroll area, so I scrolled at most 3 notches "
                              "(you asked for 10).")


def test_the_safety_gate_receives_the_medium_classification(world):
    in_browser(world)
    scroll(world, "down 10")
    [gate_action] = world.authorized
    assert (gate_action.minimum_level, gate_action.minimum_reason) == (RiskLevel.MEDIUM, UNCLASSIFIED_RULE)
    assert safety_logic.assess(gate_action).level == RiskLevel.MEDIUM


def test_declining_an_unclassified_scroll_sends_nothing(world):
    in_browser(world)
    with pytest.raises(ActionDeniedError, match="did not confirm"):
        scroll(world, "down 10", answer=False)
    assert world.desktop.notches() == []


def test_unclassified_scroll_without_a_confirmation_method_is_denied(world):
    in_browser(world)
    with pytest.raises(ActionDeniedError, match="scroll area can't be identified"):
        execute(ExecutorAction(SCROLL, "down 2"))
    assert world.desktop.notches() == []


def test_unclassified_surface_within_the_cap_still_asks(world):
    in_browser(world)
    result = scroll(world, "up 2")
    assert confirmations(world)[0][1] == ('scroll up 2 notches in window "News - Browser" - I couldn\'t confidently '
                                          "identify this window's scroll area, so for safety I'll send at most 3 notches")
    assert world.desktop.notches() == ["up", "up"] and "at most" not in result.message


@pytest.mark.parametrize("classified, asked", [(True, False), (False, True)], ids=["standard-scroll-bar", "unclassified"])
def test_only_a_positively_identified_scroll_area_is_low(world, classified, asked):
    if not classified:
        world.desktop.scroll = {}  # same Notepad chain, but no readable standard scroll bar
    scroll(world, "down 2")
    assert bool(confirmations(world)) is asked
    assert world.authorized[0].minimum_level == (RiskLevel.LOW if classified else RiskLevel.MEDIUM)


# --- Validation against the desktop ---

def test_no_active_window(world):
    world.desktop.target = ActiveTarget(None)
    assert scroll(world, "down 1").message == "Didn't scroll: there's no active window."
    assert world.desktop.notches() == []


@pytest.mark.parametrize("chain", [[ControlInfo(701, "X"), ControlInfo(700, "Chrome_WidgetWin_1")], []])
def test_pointer_not_over_the_active_window(world, chain):
    world.desktop.chain = chain
    result = scroll(world, "down 1")
    assert result.message == ("The mouse pointer isn't over the active window, so I can't be sure which window would "
                              "scroll. Move the pointer over the window you want to scroll.")
    assert world.calls == []


@pytest.mark.parametrize("held, words", [(["Ctrl"], "Ctrl is held"), (["Ctrl", "Shift"], "Ctrl and Shift are held")])
def test_held_modifier_is_refused(world, held, words):
    world.desktop.held = held
    result = scroll(world, "down 1")
    assert words in result.message and "e.g. zoom" in result.message
    assert world.calls == []


def test_unobservable_desktop(world):
    world.desktop.error = verifier_adapter.VerifierAdapterError("desktop locked")
    assert scroll(world, "down 1").message == "Didn't scroll: I can't check the pointer or the active window (desktop locked)."


# --- Changes part-way: partial, never retried ---

@pytest.mark.parametrize("change, read, reason", [
    (lambda d: setattr(d, "chain", [ControlInfo(88, "Other"), ControlInfo(500, "Notepad")]), 4,
     "the mouse pointer moved off what it was over"),
    # the active window is read before the pointer chain, so a change during chain read 3 shows at notch 2
    (lambda d: setattr(d, "target", ActiveTarget(BROWSER, 701, "X")), 3, "the active window changed"),
    (lambda d: setattr(d, "held", ["Ctrl"]), 4, "Ctrl was pressed"),
])
def test_change_part_way_is_partial(world, change, read, reason):
    world.desktop.on_read[read] = lambda: change(world.desktop)  # chain read 1: validation; 2, 3, 4: notches 0, 1, 2
    result = scroll(world, "down 5")
    assert not result.ok and not result.retryable and result.outcome is Outcome.PARTIAL and result.progress == (2, 5)
    assert result.message == f"Scrolled 2 of 5 notches, then {reason}, so I stopped."
    assert world.desktop.notches() == ["down", "down"]


def test_change_before_the_first_notch_is_a_failure(world):
    world.desktop.on_read[2] = lambda: setattr(world.desktop, "held", ["Alt"])
    result = scroll(world, "down 5")
    assert result.outcome is Outcome.FAILED and result.progress is None
    assert result.message == "Didn't scroll: Alt was pressed before the first notch, so nothing scrolled."
    assert world.desktop.notches() == []


@pytest.mark.parametrize("index, outcome, progress", [(0, Outcome.FAILED, None), (3, Outcome.PARTIAL, (3, 5))])
def test_windows_refusing_input(world, index, outcome, progress):
    world.desktop.accept[index] = False
    result = scroll(world, "down 5")
    assert result.outcome is outcome and result.progress == progress
    assert "Windows stopped accepting mouse input" in result.message


# --- Emergency stop ---

def test_emergency_stop_before_anything(world):
    emergency_stop.trigger("hotkey")
    with pytest.raises(EmergencyStopError):
        scroll(world, "down 3")
    assert world.calls == []


def test_stop_before_the_first_notch_is_a_plain_stop(world):
    world.desktop.on_read[2] = lambda: emergency_stop.trigger("hotkey")  # during the first per-notch check
    with pytest.raises(EmergencyStopError) as info:
        scroll(world, "down 3")
    assert not isinstance(info.value, ActionInterruptedError)
    assert world.desktop.notches() == []


def test_stop_part_way_raises_with_how_far_it_scrolled(world):
    world.desktop.on_read[4] = lambda: emergency_stop.trigger("hotkey")  # checked right before notch 2
    with pytest.raises(ActionInterruptedError) as info:
        scroll(world, "down 5")
    assert isinstance(info.value, EmergencyStopError) and not isinstance(info.value, TypingInterruptedError)
    result = info.value.result
    assert result.outcome is Outcome.PARTIAL and result.progress == (2, 5) and not result.retryable
    assert result.message == "Emergency stop: scrolled 2 of 5 notches before stopping."
    assert str(info.value) == "Emergency stop is active (triggered by hotkey); scrolling stopped after 2 notches of 5."
    assert world.desktop.notches() == ["down", "down"]


def test_stop_is_noticed_during_the_pause_between_notches(world):
    world.config_path.write_text(CONFIG.replace("scroll_interval_seconds: 0", "scroll_interval_seconds: 5"),
                                 encoding="utf-8")
    threading.Timer(0.1, emergency_stop.trigger, args=("hotkey",)).start()
    started = time.monotonic()
    with pytest.raises(ActionInterruptedError) as info:
        scroll(world, "down 5")
    assert time.monotonic() - started < 2 and info.value.result.progress == (1, 5)


def test_stop_while_checking_after_every_notch_was_sent(world):
    world.config_path.write_text(CONFIG.replace("scroll_settle_seconds: 0.05", "scroll_settle_seconds: 5"),
                                 encoding="utf-8")
    world.desktop.effects = False
    threading.Timer(0.1, emergency_stop.trigger, args=("hotkey",)).start()
    with pytest.raises(ActionInterruptedError) as info:
        scroll(world, "down 2")
    result = info.value.result
    assert result.ok and result.outcome is Outcome.UNVERIFIED and result.progress == (2, 2)


def test_typing_interrupted_error_is_still_an_action_interrupted_error():
    assert issubclass(TypingInterruptedError, ActionInterruptedError)
    assert issubclass(ActionInterruptedError, EmergencyStopError)
    error = TypingInterruptedError("stopped", "result")
    assert error.result == "result" and str(error) == "stopped"


# --- Verification: done only when the scroll bar visibly moved ---

@pytest.mark.parametrize("target, state, message", [
    ("down 2", ScrollState(170, 0, 199, 30), "Scrolled down 2 notches, but it was already at the bottom, so nothing moved."),
    ("up 1", ScrollState(0, 0, 199, 30), "Scrolled up 1 notch, but it was already at the top, so nothing moved."),
])
def test_already_at_the_end_is_unverified_and_says_so(world, target, state, message):
    world.desktop.scroll = {EDIT.handle: state}
    result = scroll(world, target)
    assert result.ok and result.outcome is Outcome.UNVERIFIED and result.message == message


def test_scroll_bar_that_doesnt_move_is_unverified(world):
    world.desktop.effects = False
    result = scroll(world, "down 2")
    assert result.outcome is Outcome.UNVERIFIED
    assert result.message == "Scrolled down 2 notches, but the scroll position didn't move down."


def test_scroll_bar_moving_the_other_way_is_not_success(world, monkeypatch):
    def wrong_way(up):
        world.calls.append(("notch", "up" if up else "down"))
        world.desktop.scroll[EDIT.handle] = ScrollState(40, 0, 199, 30)
        return True
    monkeypatch.setattr(adapter, "send_wheel_notch", wrong_way)
    assert scroll(world, "down 1").outcome is Outcome.UNVERIFIED


def test_scroll_bar_unreadable_afterwards_is_unverified(world):
    world.desktop.on_notch[1] = lambda: world.desktop.scroll.clear()
    result = scroll(world, "down 2")
    assert result.message == "Scrolled down 2 notches, but I couldn't read the scroll position afterwards."


def test_slow_app_is_given_time_to_move(world):
    world.desktop.effects = False

    def move_later():
        time.sleep(0.02)
        world.desktop.scroll[EDIT.handle] = ScrollState(80, 0, 199, 30)
    world.desktop.on_notch[1] = lambda: threading.Thread(target=move_later).start()
    assert scroll(world, "down 2").outcome is Outcome.DONE


# --- Recovery: scrolling is never retried ---

@pytest.mark.parametrize("setup", [
    lambda d: d.accept.update({0: False}),
    lambda d: d.on_read.update({3: lambda: setattr(d, "held", ["Ctrl"])}),
    lambda d: setattr(d, "effects", False),
    lambda d: None,
], ids=["failed", "partial", "unverified", "done"])
def test_scrolling_is_never_retried(world, setup):
    setup(world.desktop)
    offers = []
    result = execute_with_recovery(ExecutorAction(SCROLL, "down 3"), approve(world),
                                   offer_retry=lambda r: offers.append(r) or True)
    assert offers == [] and not result.retryable and len(world.desktop.notches()) <= 3


# --- Privacy: titles and control classes never reach logs, results or errors ---

TITLE = "Salary-zq5521.xlsx - Excel"
CLASS = "CLASS-zq9083"


def _collect(world, caplog, answer=True):
    caplog.set_level(logging.DEBUG)
    world.desktop.target = ActiveTarget(WindowInfo(500, TITLE, CLASS), 10, CLASS)
    world.desktop.chain = [ControlInfo(10, CLASS), ControlInfo(11, "ComboBox"), ControlInfo(500, CLASS)]
    outputs = []
    try:
        result = scroll(world, "down 2", answer)
        outputs += [result, result.message]
    except ActionDeniedError as exc:
        outputs += [exc, exc.args, exc.assessment]
    outputs += [repr(a) for a in world.authorized] + [c[3] for c in confirmations(world)]
    return outputs + [caplog.text, *[r.getMessage() for r in caplog.records]]


def _no_leak(*things):
    for thing in things:
        for secret in (TITLE, CLASS):
            assert secret not in str(thing) and secret not in repr(thing), f"{secret!r} leaked"


@pytest.mark.parametrize("answer", [True, False])
def test_titles_and_classes_never_leak(world, caplog, answer):
    _no_leak(*_collect(world, caplog, answer))
    assert TITLE in confirmations(world)[0][1]  # proves the title was really in play: the on-screen prompt has it


def test_the_leak_check_catches_a_planted_leak(world, caplog, monkeypatch):
    real_result = logic._result
    monkeypatch.setattr(logic, "_result", lambda action, ok, message, *a, **k:
                        real_result(action, ok, f"{message} {TITLE}", *a, **k))
    with pytest.raises(AssertionError, match="leaked"):
        _no_leak(*_collect(world, caplog))


# --- Adapter: one wheel notch per SendInput call (a fake SendInput - no real input) ---

@pytest.fixture
def fake_send_input(monkeypatch):
    if sys.platform != "win32":
        pytest.skip("the input structures are Windows-only")
    api = adapter._KeyboardApi()
    events, answer = [], {"accepted": None}

    def send_input(count, inputs, size):
        events.append([(inputs[i].type, inputs[i].mi.dwFlags, inputs[i].mi.mouseData) for i in range(count)])
        return count if answer["accepted"] is None else answer["accepted"]
    api.SendInput = send_input
    monkeypatch.setattr(adapter, "_keyboard_api", lambda: api)
    return SimpleNamespace(events=events, answer=answer)


def test_adapter_sends_one_wheel_notch_per_call(fake_send_input):
    assert adapter.send_wheel_notch(up=True) is True
    assert adapter.send_wheel_notch(up=False) is True
    assert fake_send_input.events == [[(0, 0x0800, 120)], [(0, 0x0800, 0xFFFFFF88)]]  # +120 / -120


def test_adapter_reports_a_refused_notch(fake_send_input):
    fake_send_input.answer["accepted"] = 0
    assert adapter.send_wheel_notch(up=False) is False


def test_adapter_refuses_off_windows(monkeypatch):
    monkeypatch.setattr(adapter.sys, "platform", "linux")
    with pytest.raises(adapter.ShortcutError, match="scrolling is only supported on Windows"):
        adapter.send_wheel_notch(up=True)
