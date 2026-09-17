"""
Tests for click by screen coordinates (app/executor/logic.py + adapter.click) - the Phase 1
last-resort fallback.

No real mouse input is ever sent: the screens, the window at each point, the mouse pointer and
pyautogui itself are faked, and importing the real pyautogui is blocked. The REAL safety gate and
the REAL Verifier logic decide every action.
"""
import sys
import types
from types import SimpleNamespace

import pytest

from app.executor import adapter, emergency_stop, logic
from app.executor.emergency_stop import EmergencyStopError
from app.executor.logic import execute, execute_with_recovery
from app.executor.models import CLICK, ActionResult, ExecutorAction, Outcome
from app.safety import logic as safety_logic
from app.safety.logic import ActionDeniedError
from app.safety.models import RiskLevel
from app.verifier import adapter as verifier_adapter
from app.verifier.models import Screen, WindowInfo
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
    "  app_windows:\n"
    '    notepad: "Notepad$"\n'
)
MAIN = Screen(0, 0, 1920, 1080, primary=True)
LEFT = Screen(-1280, 0, 0, 1024)        # a second monitor to the left: negative x
SHORT_RIGHT = Screen(1920, 0, 3200, 720)  # a shorter monitor to the right: (2000, 900) is in a gap
NOTEPAD = WindowInfo(500, "Untitled - Notepad", "Notepad")


class FakeDesktop:
    """Screens, a window at every point (unless `windows_at` says otherwise), and a pointer that
    moves where a click is sent. Records confirmations and clicks in `calls`."""

    def __init__(self, calls):
        self.calls = calls
        self.screens = [MAIN]
        self.window = NOTEPAD
        self.window_reads = 0
        self.on_window_read = {}  # read number -> callable run before answering that read
        self.pointer = (10, 10)
        self.pointer_after_click = None  # None: the pointer lands where the click was sent
        self.click_error = None
        self.screen_error = None
        self.cursor_error = None

    def list_screens(self):
        if self.screen_error:
            raise self.screen_error
        return list(self.screens)

    def window_at(self, x, y):
        self.window_reads += 1
        self.on_window_read.get(self.window_reads, lambda: None)()
        return self.window

    def cursor_position(self):
        if self.cursor_error:
            raise self.cursor_error
        return self.pointer

    def click(self, x, y):
        self.calls.append(("click", x, y))
        if self.click_error:
            raise self.click_error
        self.pointer = self.pointer_after_click or (x, y)


@pytest.fixture
def world(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_PATH", config_path)
    calls = []
    desktop = FakeDesktop(calls)
    monkeypatch.setattr(verifier_adapter, "list_screens", desktop.list_screens)
    monkeypatch.setattr(verifier_adapter, "window_at", desktop.window_at)
    monkeypatch.setattr(verifier_adapter, "cursor_position", desktop.cursor_position)
    monkeypatch.setattr(adapter, "click", desktop.click)
    monkeypatch.setitem(sys.modules, "pyautogui", None)  # any real import of pyautogui fails loudly
    emergency_stop.reset("test-setup")
    yield SimpleNamespace(calls=calls, desktop=desktop, config_path=config_path)
    emergency_stop.reset("test-teardown")


def approve(world, answer=True):
    def confirm(action, assessment):
        world.calls.append(("confirm", action.description, assessment.level, assessment.rule))
        return answer
    return confirm


def click(world, target="500, 300", answer=True, **kwargs):
    return execute(ExecutorAction(CLICK, target), approve(world, answer), **kwargs)


def clicks(world):
    return [call for call in world.calls if call[0] == "click"]


# --- Happy path: confirmed, sent, and honestly reported as unverified ---

def test_click_is_confirmed_then_sent_and_reported_unverified(world):
    result = click(world)
    assert result.ok and not result.verified and result.outcome is Outcome.UNVERIFIED and not result.retryable
    assert result.message == "Clicked at (500, 300). I can't check what the click did."
    assert world.calls == [
        ("confirm", 'click at (500, 300) on window "Untitled - Notepad"', RiskLevel.MEDIUM, logic._CLICK_RISK_REASON),
        ("click", 500, 300),
    ]


@pytest.mark.parametrize("target", ["500,300", "(500, 300)", "  ( 500 ,300 ) ", "500 , 300"])
def test_coordinate_formats(world, target):
    assert click(world, target).outcome is Outcome.UNVERIFIED
    assert clicks(world) == [("click", 500, 300)]


def test_click_on_a_monitor_with_negative_coordinates(world):
    world.desktop.screens = [MAIN, LEFT]
    assert click(world, "-1000, 500").ok
    assert clicks(world) == [("click", -1000, 500)]


def test_corner_of_a_secondary_monitor_is_allowed(world):
    world.desktop.screens = [MAIN, LEFT]
    assert click(world, "-1280, 0").ok  # only the MAIN screen's corners are the fail-safe


# --- Validation: nothing asked, nothing sent ---

@pytest.mark.parametrize("target", ["", "   "])
def test_missing_coordinates_ask_where(world, target):
    result = click(world, target)
    assert not result.ok and result.message == "Where should I click? Give screen coordinates as x, y (e.g. 500, 300)."
    assert world.calls == []


@pytest.mark.parametrize("target", ["abc", "500", "500, 300, 1", "1.5, 3", "500 300", "(500, 300", "500, 300)",
                                    "x=500, y=300", "٥٠٠, 300", "5e2, 300", "+500, 300"])
def test_invalid_coordinates_fail_cleanly(world, target):
    result = click(world, target)
    assert not result.ok and not result.retryable and result.outcome is Outcome.FAILED
    assert result.message == (f"I can't click at '{target.strip()}': give whole-number screen coordinates "
                              f"as x, y (e.g. 500, 300).")
    assert world.calls == []


def test_point_off_every_screen_fails_before_anything_is_sent(world):
    result = click(world, "99999, 99999")
    assert not result.ok and not result.retryable
    assert result.message == "(99999, 99999) isn't on any screen, so I didn't click. Your screens: x 0 to 1919, y 0 to 1079 (main)."
    assert world.calls == []


@pytest.mark.parametrize("target", ["1920, 500", "500, 1080", "-1, 0", "0, -1"])
def test_one_pixel_past_the_edge_is_off_screen(world, target):
    assert "isn't on any screen" in click(world, target).message
    assert world.calls == []


def test_gap_between_monitors_of_different_sizes_is_off_screen(world):
    world.desktop.screens = [MAIN, LEFT, SHORT_RIGHT]
    result = click(world, "2000, 900")  # inside the overall bounding box, but on no monitor
    assert not result.ok
    assert result.message == ("(2000, 900) isn't on any screen, so I didn't click. Your screens: x 0 to 1919, "
                              "y 0 to 1079 (main); x -1280 to -1, y 0 to 1023; x 1920 to 3199, y 0 to 719.")
    assert world.calls == []


@pytest.mark.parametrize("target", ["0, 0", "1919, 0", "0, 1079", "1919, 1079"])
def test_main_screen_corners_are_refused(world, target):
    result = click(world, target)
    assert not result.ok and "corner of the main screen" in result.message and "manual emergency stop" in result.message
    assert world.calls == []


def test_unobservable_screen_means_no_click(world):
    world.desktop.screen_error = verifier_adapter.VerifierAdapterError("checking windows is only supported on Windows")
    result = click(world)
    assert not result.ok
    assert result.message == ("Didn't click at (500, 300): I can't check the screen "
                              "(checking windows is only supported on Windows).")
    assert world.calls == []


# --- Safety: every click is confirmed, and the prompt says exactly what is being approved ---

def test_click_without_a_confirmation_method_is_denied(world):
    with pytest.raises(ActionDeniedError, match="coordinate click - always needs confirmation"):
        execute(ExecutorAction(CLICK, "500, 300"))
    assert clicks(world) == []


def test_click_declined_by_the_user_sends_nothing(world):
    with pytest.raises(ActionDeniedError, match="did not confirm"):
        click(world, answer=False)
    assert clicks(world) == []


@pytest.mark.parametrize("window, where", [
    (WindowInfo(7, "", "Shell_TrayWnd"), "a window with no readable title"),
    (None, "a spot where no window could be identified"),
])
def test_prompt_says_when_the_window_has_no_readable_title(world, window, where):
    world.desktop.window = window
    click(world)
    assert world.calls[0][1] == f"click at (500, 300) on {where}"


def test_risky_words_in_the_window_title_keep_the_click_rule_and_stay_out_of_the_log(world, caplog):
    world.desktop.window = WindowInfo(8, "Delete ALL-MY-FILES? - Cleanup", "Dialog")
    caplog.set_level("INFO")
    click(world)
    assert world.calls[0][2:] == (RiskLevel.MEDIUM, logic._CLICK_RISK_REASON)
    assert "ALL-MY-FILES" not in caplog.text  # titles are shown in the prompt, never logged


def test_every_click_passes_the_real_safety_gate(world, monkeypatch):
    seen = []
    real_authorize = safety_logic.authorize
    monkeypatch.setattr(logic, "authorize", lambda action, confirm=None: seen.append(action) or real_authorize(action, confirm))
    click(world)
    assert len(seen) == 1 and seen[0].minimum_level == RiskLevel.MEDIUM


# --- The window must still be the one approved ---

def test_different_window_after_approval_means_no_click(world):
    def user_switches_window():
        world.desktop.window = WindowInfo(900, "Inbox - Mail", "Mail")
    world.desktop.on_window_read[2] = user_switches_window  # read 1: for the prompt; read 2: the re-check
    result = click(world)
    assert not result.ok and not result.retryable and result.outcome is Outcome.FAILED
    assert result.message == "The window at (500, 300) changed after you approved the click, so I didn't click."
    assert clicks(world) == []


def test_same_window_with_a_changed_title_means_no_click(world):
    def title_changes():
        world.desktop.window = WindowInfo(NOTEPAD.handle, "Delete account? - Notepad", "Notepad")
    world.desktop.on_window_read[2] = title_changes
    assert "changed after you approved" in click(world).message
    assert clicks(world) == []


def test_window_appearing_where_there_was_none_means_no_click(world):
    world.desktop.window = None
    world.desktop.on_window_read[2] = lambda: setattr(world.desktop, "window", NOTEPAD)
    assert not click(world).ok
    assert clicks(world) == []


def test_unreadable_window_on_the_recheck_means_no_click(world, monkeypatch):
    reads = []

    def window_at(x, y):
        reads.append((x, y))
        if len(reads) == 2:
            raise verifier_adapter.VerifierAdapterError("desktop locked")
        return NOTEPAD
    monkeypatch.setattr(verifier_adapter, "window_at", window_at)
    result = click(world)
    assert not result.ok
    assert result.message == "Didn't click at (500, 300): I can't check the window there (desktop locked)."
    assert clicks(world) == []


# --- Emergency stop ---

def test_emergency_stop_before_anything(world):
    emergency_stop.trigger("hotkey")
    with pytest.raises(EmergencyStopError):
        click(world)
    assert world.calls == []


def test_stop_during_confirmation_prevents_the_click(world):
    def confirm_then_stop(action, assessment):
        emergency_stop.trigger("hotkey")
        return True
    with pytest.raises(EmergencyStopError):
        execute(ExecutorAction(CLICK, "500, 300"), confirm_then_stop)
    assert clicks(world) == []


def test_stop_right_before_the_click_prevents_it(world):
    world.desktop.on_window_read[2] = lambda: emergency_stop.trigger("hotkey")  # during the final re-check
    with pytest.raises(EmergencyStopError):
        click(world)
    assert clicks(world) == []


def test_mouse_in_a_corner_triggers_the_emergency_stop(world):
    world.desktop.click_error = adapter.MouseFailSafeError("the mouse pointer is in a corner of the main screen")
    with pytest.raises(EmergencyStopError):
        click(world)
    status = emergency_stop.status()
    assert status.stopped and status.source == "mouse-corner"
    with pytest.raises(EmergencyStopError):  # and it stays stopped for the next action
        click(world)
    assert len(clicks(world)) == 1


# --- After the click: the pointer check, and honest results ---

def test_pointer_somewhere_else_after_the_click_is_a_failure(world):
    world.desktop.pointer_after_click = (40, 60)
    result = click(world)
    assert not result.ok and not result.retryable and result.outcome is Outcome.FAILED
    assert result.message == ("I sent the click, but the mouse pointer is at (40, 60) instead of (500, 300), "
                              "so the click may have landed somewhere else.")


def test_unreadable_pointer_after_the_click_is_still_only_unverified(world):
    world.desktop.cursor_error = verifier_adapter.VerifierAdapterError("desktop locked")
    result = click(world)
    assert result.ok and not result.verified and result.outcome is Outcome.UNVERIFIED
    assert result.message == ("Clicked at (500, 300). I couldn't read where the mouse pointer ended up, "
                              "and I can't check what the click did.")


def test_click_refused_by_windows_is_a_clear_failure(world):
    world.desktop.click_error = adapter.ClickError("Windows didn't accept the click (OSError)")
    result = click(world)
    assert not result.ok and not result.retryable
    assert result.message == "I couldn't click at (500, 300): Windows didn't accept the click (OSError)."


# --- Recovery: nothing about a click is ever retried ---

@pytest.mark.parametrize("setup", [
    lambda d: setattr(d, "click_error", adapter.ClickError("Windows didn't accept the click (OSError)")),
    lambda d: setattr(d, "pointer_after_click", (1, 1)),
    lambda d: d.on_window_read.update({2: lambda: setattr(d, "window", WindowInfo(9, "Other", "X"))}),
    lambda d: None,  # a successful, unverified click
], ids=["click-refused", "pointer-elsewhere", "window-changed", "unverified"])
def test_clicks_are_never_retried(world, setup):
    setup(world.desktop)
    offers = []
    result = execute_with_recovery(ExecutorAction(CLICK, "500, 300"), approve(world),
                                   offer_retry=lambda r: offers.append(r) or True)
    assert offers == [] and not result.retryable
    assert len([c for c in world.calls if c[0] == "confirm"]) == 1 and len(clicks(world)) <= 1


# --- Result shape ---

def test_unverified_is_ok_but_never_verified():
    action = ExecutorAction(CLICK, "500, 300")
    result = ActionResult(action, True, "Clicked.", outcome=Outcome.UNVERIFIED)
    assert result.ok and not result.verified
    assert ActionResult(action, True, "Done.").verified
    with pytest.raises(ValueError):
        ActionResult(action, False, "x", outcome=Outcome.UNVERIFIED)
    with pytest.raises(ValueError):
        ActionResult(action, True, "x", retryable=True, outcome=Outcome.UNVERIFIED)


# --- Adapter: pyautogui with its fail-safe on (a fake pyautogui - no real input) ---

@pytest.fixture
def fake_pyautogui(monkeypatch):
    module = types.ModuleType("pyautogui")

    class FailSafeException(Exception):
        pass

    module.FailSafeException = FailSafeException
    module.FAILSAFE = False
    module.calls = []
    module.error = None

    def fake_click(*args, **kwargs):
        module.calls.append((args, kwargs, module.FAILSAFE))
        if module.error:
            raise module.error
    module.click = fake_click
    monkeypatch.setitem(sys.modules, "pyautogui", module)
    monkeypatch.setattr(adapter.sys, "platform", "win32")
    physical = []
    monkeypatch.setattr(adapter, "_use_physical_pixels", lambda: physical.append(True))
    module.physical = physical
    return module


def test_adapter_clicks_once_with_the_fail_safe_on_and_no_pause(fake_pyautogui):
    adapter.click(-1000, 500)
    assert fake_pyautogui.calls == [((-1000, 500), {"button": "left", "_pause": False}, True)]
    assert fake_pyautogui.physical == [True]  # DPI awareness set before pyautogui was used


def test_adapter_turns_the_fail_safe_into_its_own_error(fake_pyautogui):
    fake_pyautogui.error = fake_pyautogui.FailSafeException("corner")
    with pytest.raises(adapter.MouseFailSafeError, match="corner of the main screen"):
        adapter.click(500, 300)


def test_adapter_turns_other_failures_into_a_clear_error(fake_pyautogui):
    fake_pyautogui.error = OSError("input blocked")
    with pytest.raises(adapter.ClickError, match=r"Windows didn't accept the click \(OSError\)"):
        adapter.click(500, 300)


def test_adapter_reports_a_missing_mouse_library(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyautogui", None)
    monkeypatch.setattr(adapter.sys, "platform", "win32")
    monkeypatch.setattr(adapter, "_use_physical_pixels", lambda: None)
    with pytest.raises(adapter.ClickError, match=r"the mouse library couldn't be loaded \((ModuleNotFound|Import)Error\)"):
        adapter.click(500, 300)


def test_adapter_refuses_off_windows(monkeypatch):
    monkeypatch.setattr(adapter.sys, "platform", "linux")
    with pytest.raises(adapter.ClickError, match="only supported on Windows"):
        adapter.click(500, 300)
