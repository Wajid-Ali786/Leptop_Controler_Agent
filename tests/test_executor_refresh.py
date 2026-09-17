"""
Tests for refresh (app/executor/logic.py) - F5 in the active window, only after positively identifying
the app by its executable AND top-level window class.

No real input is ever sent: the active window, its owning executable, held modifier keys and the key
sending are faked, and the real SendInput is blocked. The REAL safety gate and the REAL Verifier logic
decide every action.
"""
import logging
import sys
from types import SimpleNamespace

import pytest

from app.executor import adapter, emergency_stop, logic, shortcuts
from app.executor.emergency_stop import EmergencyStopError
from app.executor.logic import execute, execute_with_recovery
from app.executor.models import REFRESH, SHORTCUT, ExecutorAction, Outcome
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
)
CHROME = ActiveTarget(WindowInfo(100, "Inbox - Google Chrome", "Chrome_WidgetWin_1"), 101, "Chrome_RenderWidgetHostHWND")
EDGE = ActiveTarget(WindowInfo(200, "News - Microsoft Edge", "Chrome_WidgetWin_1"), 201, "Chrome_RenderWidgetHostHWND")
FIREFOX = ActiveTarget(WindowInfo(300, "Mozilla Firefox", "MozillaWindowClass"), 300, "MozillaWindowClass")
EXPLORER = ActiveTarget(WindowInfo(400, "Downloads", "CabinetWClass"), 401, "DirectUIHWND")
EXPLORER_RENAMING = ActiveTarget(WindowInfo(400, "Downloads", "CabinetWClass"), 402, "Edit")
EXECUTABLES = {100: "chrome.exe", 200: "msedge.exe", 300: "firefox.exe", 400: "explorer.exe"}


class FakeDesktop:
    """on_target_read[n] runs before the n-th active-window read. `accept` is how many key events Windows
    accepts (None: all)."""

    def __init__(self, calls):
        self.calls = calls
        self.target = CHROME
        self.executables = dict(EXECUTABLES)
        self.target_reads = 0
        self.on_target_read = {}
        self.held = []
        self.accept = None

    def active_target(self):
        self.target_reads += 1
        self.on_target_read.get(self.target_reads, lambda: None)()
        return self.target

    def process_image_name(self, handle):
        return self.executables.get(handle)

    def modifier_keys_down(self):
        return list(self.held)

    def send_shortcut(self, modifiers, key):
        self.calls.append(("send", "+".join((*modifiers, key))))
        expected = 2 * len(modifiers) + 2
        return (expected if self.accept is None else self.accept), expected

    def release_keys(self, modifiers, key):
        self.calls.append(("release", "+".join((*modifiers, key))))
        return True


@pytest.fixture
def world(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_PATH", config_path)
    calls = []
    desktop = FakeDesktop(calls)
    for name in ("active_target", "process_image_name", "modifier_keys_down"):
        monkeypatch.setattr(verifier_adapter, name, getattr(desktop, name))
    monkeypatch.setattr(adapter, "send_shortcut", desktop.send_shortcut)
    monkeypatch.setattr(adapter, "release_keys", desktop.release_keys)

    def no_real_input():
        raise AssertionError("real input must not be sent in this test")
    monkeypatch.setattr(adapter, "_keyboard_api", no_real_input)
    authorized = []
    real_authorize = safety_logic.authorize
    monkeypatch.setattr(logic, "authorize",
                        lambda action, confirm=None: authorized.append(action) or real_authorize(action, confirm))
    emergency_stop.reset("test-setup")
    yield SimpleNamespace(calls=calls, desktop=desktop, authorized=authorized)
    emergency_stop.reset("test-teardown")


def approve(world, answer=True):
    def confirm(action, assessment):
        world.calls.append(("confirm", action.description, assessment.level, assessment.rule))
        return answer
    return confirm


def refresh(world, answer=True, target=""):
    return execute(ExecutorAction(REFRESH, target), approve(world, answer))


def sent(world):
    return [call[1] for call in world.calls if call[0] == "send"]


def confirmations(world):
    return [call for call in world.calls if call[0] == "confirm"]


# --- Classification: executable AND top-level window class ---

@pytest.mark.parametrize("target, app, level", [
    (CHROME, "Chrome", RiskLevel.MEDIUM), (EDGE, "Edge", RiskLevel.MEDIUM), (FIREFOX, "Firefox", RiskLevel.MEDIUM),
    (EXPLORER, "File Explorer", RiskLevel.LOW), (EXPLORER_RENAMING, "File Explorer", RiskLevel.MEDIUM),
], ids=["chrome", "edge", "firefox", "explorer", "explorer-editing"])
def test_supported_apps_and_their_risk_levels(world, target, app, level):
    world.desktop.target = target
    result = refresh(world)
    assert sent(world) == ["F5"]
    assert world.authorized[0].minimum_level == level
    assert safety_logic.assess(world.authorized[0]).level == level
    assert result.ok and result.outcome is Outcome.UNVERIFIED and not result.verified
    assert result.message == f"Pressed F5 to refresh {app}. I can't confirm the refresh happened."


@pytest.mark.parametrize("executable, class_name", [
    ("code.exe", "Chrome_WidgetWin_1"),       # VS Code (Electron): F5 starts debugging
    ("slack.exe", "Chrome_WidgetWin_1"),      # Electron
    ("claude.exe", "Chrome_WidgetWin_1"),     # Electron
    ("chrome.exe", "Chrome_RenderWidgetHostHWND"),  # right program, wrong window class
    ("firefox.exe", "Chrome_WidgetWin_1"),
    ("explorer.exe", "Progman"),              # the desktop
    ("explorer.exe", "Shell_TrayWnd"),        # the taskbar
    ("notepad.exe", "Notepad"),
    ("devenv.exe", "HwndWrapper[DefaultDomain;;x]"),  # Visual Studio (WPF)
    (None, "Chrome_WidgetWin_1"),             # executable can't be read
])
def test_everything_else_is_refused_before_the_safety_gate(world, executable, class_name):
    world.desktop.target = ActiveTarget(WindowInfo(900, "Some window", class_name), 901, "X")
    world.desktop.executables[900] = executable
    result = refresh(world)
    assert not result.ok and result.outcome is Outcome.FAILED and not result.retryable
    assert result.message == ("I can refresh only Chrome, Edge, Firefox and File Explorer windows in Phase 1, so I "
                              "didn't press anything.")
    assert world.authorized == [] and world.calls == []


def test_executable_names_are_compared_exactly_as_read(world):
    world.desktop.executables[100] = "chrome.exe.bak"
    assert not refresh(world).ok and world.authorized == []


# --- Prompts: browsers and editing Explorer ask; a plain Explorer folder view doesn't ---

def test_browser_prompt(world):
    refresh(world)
    [(_, description, level, rule)] = confirmations(world)
    assert description == ('refresh window "Inbox - Google Chrome" (Chrome) - reloads the page; anything typed into '
                           "it that isn't saved may be lost")
    assert (level, rule) == (RiskLevel.MEDIUM, "refreshing a browser page can lose unsaved input or page state")


def test_explorer_while_editing_prompt(world):
    world.desktop.target = EXPLORER_RENAMING
    refresh(world)
    [(_, description, level, rule)] = confirmations(world)
    assert description == ('refresh window "Downloads" (File Explorer) - a text box is being edited (renaming a file '
                           "or typing an address); pressing F5 now may commit or discard it")
    assert (level, rule) == (RiskLevel.MEDIUM, "refreshing File Explorer while a text box is being edited")


def test_explorer_folder_view_refreshes_without_asking(world):
    world.desktop.target = EXPLORER
    refresh(world)
    assert confirmations(world) == [] and [a.description for a in world.authorized] == ["refresh File Explorer"]


def test_browser_refresh_declined_sends_nothing(world):
    with pytest.raises(ActionDeniedError, match="did not confirm"):
        refresh(world, answer=False)
    assert sent(world) == []


def test_browser_refresh_without_a_confirmation_method_is_denied(world):
    with pytest.raises(ActionDeniedError, match="refreshing a browser page"):
        execute(ExecutorAction(REFRESH))
    assert sent(world) == []


# --- Validation ---

@pytest.mark.parametrize("target", ["chrome", "  the page ", "F5"])
def test_a_target_is_refused(world, target):
    result = refresh(world, target=target)
    assert result.message == "Refresh doesn't take a target; it refreshes the active window."
    assert world.calls == [] and world.desktop.target_reads == 0


def test_no_active_window(world):
    world.desktop.target = ActiveTarget(None)
    assert refresh(world).message == "Didn't refresh: there's no active window."
    assert world.authorized == []


@pytest.mark.parametrize("held, words", [(["Ctrl"], "Ctrl is held"), (["Shift", "Alt"], "Shift and Alt are held")])
def test_held_modifier_is_refused(world, held, words):
    world.desktop.held = held
    result = refresh(world)
    assert words in result.message and "a hard reload" in result.message
    assert world.calls == []


def test_unobservable_desktop(world, monkeypatch):
    def unavailable():
        raise verifier_adapter.VerifierAdapterError("desktop locked")
    monkeypatch.setattr(verifier_adapter, "active_target", unavailable)
    assert refresh(world).message == "Didn't refresh: I can't check the keyboard or the active window (desktop locked)."


# --- Re-check after confirmation and immediately before sending ---

@pytest.mark.parametrize("change", [
    lambda d: setattr(d, "target", EDGE),                                               # another window
    lambda d: d.executables.update({100: "code.exe"}),                                  # same handle, other program
    lambda d: setattr(d, "target", ActiveTarget(WindowInfo(100, "x", "MozillaWindowClass"), 101, "X")),  # other class
    lambda d: setattr(d, "target", ActiveTarget(CHROME.window, 555, "Edit")),           # other focused control
    lambda d: setattr(d, "target", ActiveTarget(None)),                                  # nothing active
], ids=["window", "executable", "class", "focused-control", "no-window"])
def test_identity_change_after_approval_sends_nothing(world, change):
    world.desktop.on_target_read[2] = lambda: change(world.desktop)
    result = refresh(world)
    assert not result.ok and result.outcome is Outcome.FAILED and not result.retryable
    assert result.message == "The active window changed after you approved, so I didn't refresh."
    assert sent(world) == []


def test_title_change_alone_does_not_block_the_refresh(world):
    """Browser titles change by themselves (e.g. a new message count)."""
    world.desktop.on_target_read[2] = lambda: setattr(
        world.desktop, "target", ActiveTarget(WindowInfo(100, "(3) Inbox - Google Chrome", "Chrome_WidgetWin_1"),
                                              101, "Chrome_RenderWidgetHostHWND"))
    assert refresh(world).outcome is Outcome.UNVERIFIED and sent(world) == ["F5"]


def test_modifier_pressed_after_approval_sends_nothing(world):
    def user_holds_ctrl(action, assessment):
        world.desktop.held = ["Ctrl"]
        return True
    result = execute(ExecutorAction(REFRESH), user_holds_ctrl)
    assert not result.ok and "Ctrl is held down" in result.message and sent(world) == []


# --- Emergency stop ---

def test_emergency_stop_before_anything(world):
    emergency_stop.trigger("hotkey")
    with pytest.raises(EmergencyStopError):
        refresh(world)
    assert world.calls == []


def test_stop_during_confirmation_sends_nothing(world):
    def confirm_then_stop(action, assessment):
        emergency_stop.trigger("hotkey")
        return True
    with pytest.raises(EmergencyStopError):
        execute(ExecutorAction(REFRESH), confirm_then_stop)
    assert sent(world) == []


def test_stop_right_before_sending_sends_nothing(world):
    world.desktop.target = EXPLORER  # LOW: no prompt, so the stop lands in the final re-check
    world.desktop.on_target_read[2] = lambda: emergency_stop.trigger("hotkey")
    with pytest.raises(EmergencyStopError):
        refresh(world)
    assert sent(world) == []


# --- Sending: 0 / partial / all accepted ---

def test_zero_accepted_events_is_a_failure(world):
    world.desktop.accept = 0
    result = refresh(world)
    assert not result.ok and result.outcome is Outcome.FAILED
    assert result.message == "Windows didn't accept the keyboard input for F5, so nothing was refreshed."
    assert [c for c in world.calls if c[0] == "release"] == []


def test_partly_accepted_input_releases_f5_and_is_unverified(world):
    world.desktop.accept = 1
    result = refresh(world)
    assert result.ok and result.outcome is Outcome.UNVERIFIED and not result.retryable
    assert result.message == ("Windows accepted only part of the keyboard input for F5, so I released it. The refresh "
                              "may or may not have happened.")
    assert [c for c in world.calls if c[0] == "release"] == [("release", "F5")]


@pytest.mark.parametrize("target", [CHROME, EDGE, FIREFOX, EXPLORER, EXPLORER_RENAMING])
def test_a_refresh_is_never_done_in_phase_1(world, target):
    world.desktop.target = target
    result = refresh(world)
    assert result.outcome is Outcome.UNVERIFIED and not result.verified


# --- Recovery: never retried ---

@pytest.mark.parametrize("setup", [
    lambda d: setattr(d, "accept", 0),
    lambda d: setattr(d, "accept", 1),
    lambda d: d.on_target_read.update({2: lambda: setattr(d, "target", EDGE)}),
    lambda d: None,
], ids=["failed", "partly-accepted", "window-changed", "unverified"])
def test_refresh_is_never_retried(world, setup):
    setup(world.desktop)
    offers = []
    result = execute_with_recovery(ExecutorAction(REFRESH), approve(world),
                                   offer_retry=lambda r: offers.append(r) or True)
    assert offers == [] and not result.retryable and len(sent(world)) <= 1


# --- One path only: refresh keys are refused as generic shortcuts ---

@pytest.mark.parametrize("keys", ["f5", "ctrl+r", "ctrl+f5", "shift+f5", "ctrl+shift+r"])
def test_refresh_keys_cant_bypass_refresh_through_shortcuts(world, keys):
    result = execute(ExecutorAction(SHORTCUT, keys), approve(world))
    assert not result.ok and "Use the Refresh action instead" in result.message
    assert world.calls == [] and world.authorized == []
    assert not any("F5" in name or name.endswith("+R") for name in shortcuts.SUPPORTED)


# --- Privacy: window titles never reach logs, results or errors ---

TITLE = "Bank-zq6610 statement - Google Chrome"


def _collect(world, caplog, answer=True):
    caplog.set_level(logging.DEBUG)
    world.desktop.target = ActiveTarget(WindowInfo(100, TITLE, "Chrome_WidgetWin_1"), 101, "X")
    outputs = []
    try:
        result = refresh(world, answer)
        outputs += [result, result.message]
    except ActionDeniedError as exc:
        outputs += [exc, exc.args, exc.assessment]
    outputs += [repr(a) for a in world.authorized] + [c[3] for c in confirmations(world)]
    return outputs + [caplog.text, *[r.getMessage() for r in caplog.records]]


def _no_leak(*things):
    for thing in things:
        assert TITLE not in str(thing) and TITLE not in repr(thing), "window title leaked"


@pytest.mark.parametrize("answer", [True, False])
def test_window_titles_never_leak(world, caplog, answer):
    _no_leak(*_collect(world, caplog, answer))
    assert TITLE in confirmations(world)[0][1]  # proves the title was really in play: the on-screen prompt has it


def test_the_leak_check_catches_a_planted_leak(world, caplog, monkeypatch):
    real_result = logic._result
    monkeypatch.setattr(logic, "_result", lambda action, ok, message, *a, **k:
                        real_result(action, ok, f"{message} {TITLE}", *a, **k))
    with pytest.raises(AssertionError, match="leaked"):
        _no_leak(*_collect(world, caplog))


# --- The executable lookup (read-only, real) ---

@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_real_process_name_lookup_reads_an_executable_file_name():
    from app.verifier import logic as verifier_logic
    target = verifier_adapter.active_target()
    if target.window is None:
        pytest.skip("no active window to read")
    name = verifier_logic.process_name(target.window.handle)
    assert name is None or (name == name.lower() and "\\" not in name and name.endswith(".exe"))
