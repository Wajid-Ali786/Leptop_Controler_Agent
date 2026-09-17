"""
Tests for app/verifier/ - Phase 1: "did the app's window actually appear?"

The desktop is faked (adapter.list_windows is replaced), so no real window is ever opened or
closed. One read-only test lists the real desktop's windows without changing anything.
"""
import re
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from app.executor import emergency_stop
from app.executor.emergency_stop import EmergencyStopError
from app.verifier import adapter, logic
from app.verifier.models import Screen, WindowExpectation, WindowInfo
from config import settings
from config.settings import SettingsError

CONFIG = (
    "verifier:\n"
    "  window_timeout_seconds: 0.3\n"
    "  poll_interval_seconds: 0.01\n"
    "  app_windows:\n"
    '    notepad: "Notepad$"\n'
    '    calculator: "^Calculator$"\n'
)


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    """A fake desktop: `windows` is what's open; `appear_after_polls` delays a pending window."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_PATH", config_path)
    fake = SimpleNamespace(windows=[], pending=[], closing=[], polls=0, config_path=config_path, error=None)

    def list_windows():
        if fake.error:
            raise fake.error
        fake.polls += 1
        for item in list(fake.pending):
            item["polls"] -= 1
            if item["polls"] <= 0:
                if item.get("change"):
                    item["change"]()
                else:
                    fake.windows.append(item["window"])
                fake.pending.remove(item)
        for item in list(fake.closing):
            item["polls"] -= 1
            if item["polls"] <= 0:
                fake.windows = [w for w in fake.windows if w.handle != item["handle"]]
                fake.closing.remove(item)
        return list(fake.windows)

    monkeypatch.setattr(adapter, "list_windows", list_windows)
    emergency_stop.reset("test-setup")
    yield fake
    emergency_stop.reset("test-teardown")


def open_later(fake, handle, title, polls):
    fake.pending.append({"window": WindowInfo(handle, title), "polls": polls})


def close_later(fake, handle, polls):
    """Remove window `handle` after `polls` more desktop reads."""
    fake.closing.append({"handle": handle, "polls": polls})


# --- Expectation from config ---

def test_expectation_comes_from_config(desktop):
    expectation = logic.expect_window("  Notepad ")
    assert expectation.app_name == "notepad"
    assert expectation.pattern.search("untitled - notepad")  # case-insensitive
    assert expectation.timeout_seconds == 0.3 and expectation.poll_interval_seconds == 0.01


@pytest.mark.parametrize("config, setting", [
    (CONFIG.replace('    calculator: "^Calculator$"\n', ""), "No window title pattern for 'calculator'"),
    (CONFIG.replace('"^Calculator$"', '"(unclosed"'), "isn't a valid regular expression"),
    (CONFIG.replace("window_timeout_seconds: 0.3", "window_timeout_seconds: 0"), "verifier.window_timeout_seconds"),
    (CONFIG.replace("poll_interval_seconds: 0.01", "poll_interval_seconds: soon"), "verifier.poll_interval_seconds"),
    ("verifier:\n  app_windows: notepad\n", "verifier.app_windows"),
])
def test_missing_or_invalid_settings_fail_clearly(desktop, config, setting):
    desktop.config_path.write_text(config, encoding="utf-8")
    with pytest.raises(SettingsError, match=re.escape(setting)):
        logic.expect_window("calculator")


# --- Waiting for a NEW window ---

def test_new_matching_window_is_success(desktop):
    expectation = logic.expect_window("notepad")
    before = logic.snapshot_windows(expectation)
    desktop.windows.append(WindowInfo(42, "Untitled - Notepad"))
    result = logic.wait_for_new_window(expectation, before)
    assert result.ok and result.window_handle == 42 and result.window_handles == {42}
    assert result.message.startswith("notepad's window appeared after")


def test_slow_starting_app_is_not_reported_as_failed(desktop):
    expectation = logic.expect_window("calculator")
    before = logic.snapshot_windows(expectation)
    open_later(desktop, 7, "Calculator", polls=8)  # appears after several polls, within the timeout
    result = logic.wait_for_new_window(expectation, before)
    assert result.ok and result.window_handle == 7
    assert desktop.polls > 2


def test_window_that_never_appears_is_a_retryable_failure(desktop):
    expectation = logic.expect_window("notepad")
    started = time.monotonic()
    result = logic.wait_for_new_window(expectation, logic.snapshot_windows(expectation))
    assert not result.ok and result.retryable
    assert result.message == "notepad was started, but no new window appeared within 0.3 seconds."
    assert 0.25 <= time.monotonic() - started < 2  # waited the timeout, then gave up


def test_window_that_was_already_open_never_counts(desktop):
    desktop.windows.append(WindowInfo(1, "Claude Code Response.txt - Notepad"))  # the user's own
    expectation = logic.expect_window("notepad")
    result = logic.wait_for_new_window(expectation, logic.snapshot_windows(expectation))
    assert not result.ok


def test_new_window_with_a_different_title_does_not_count(desktop):
    expectation = logic.expect_window("calculator")
    before = logic.snapshot_windows(expectation)
    desktop.windows.append(WindowInfo(9, "Calculator Help - Chrome"))  # doesn't match ^Calculator$
    assert not logic.wait_for_new_window(expectation, before).ok


def test_unobservable_desktop_is_a_failure_not_success(desktop):
    expectation = logic.expect_window("notepad")
    desktop.error = adapter.VerifierAdapterError("checking windows is only supported on Windows")
    result = logic.wait_for_new_window(expectation, frozenset())
    assert not result.ok and not result.retryable
    assert "Couldn't check whether notepad's window appeared" in result.message
    with pytest.raises(logic.VerifierUnavailableError):
        logic.snapshot_windows(expectation)


def test_emergency_stop_interrupts_the_wait(desktop):
    desktop.config_path.write_text(CONFIG.replace("window_timeout_seconds: 0.3", "window_timeout_seconds: 30")
                                   .replace("poll_interval_seconds: 0.01", "poll_interval_seconds: 5"),
                                   encoding="utf-8")
    expectation = logic.expect_window("notepad")
    threading.Timer(0.1, emergency_stop.trigger, args=("hotkey",)).start()
    started = time.monotonic()
    with pytest.raises(EmergencyStopError):
        logic.wait_for_new_window(expectation, frozenset())
    assert time.monotonic() - started < 2  # woke on the stop, not after the 5 s poll or 30 s timeout


def test_every_new_matching_window_is_reported_as_one_group(desktop):
    """Calculator shows an outer frame and an inner content window with the same title."""
    desktop.windows.append(WindowInfo(1, "Calculator", "ApplicationFrameWindow"))  # already open
    expectation = logic.expect_window("calculator")
    before = logic.snapshot_windows(expectation)
    desktop.windows += [WindowInfo(20, "Calculator", "ApplicationFrameWindow"),
                        WindowInfo(21, "Calculator", "Windows.UI.Core.CoreWindow")]
    result = logic.wait_for_new_window(expectation, before)
    assert result.ok and result.window_handles == {20, 21}


def test_cloaked_new_window_counts_only_once_it_is_on_screen(desktop):
    """A Store app's frame exists (cloaked) before it is shown; its content window appears meanwhile."""
    expectation = logic.expect_window("calculator")
    before = logic.snapshot_windows(expectation)
    desktop.windows.append(WindowInfo(20, "Calculator", "ApplicationFrameWindow", cloaked=True))
    desktop.pending.append({"window": WindowInfo(21, "Calculator", "Windows.UI.Core.CoreWindow", cloaked=True),
                            "polls": 2})  # content window: a separate, still cloaked, top-level window for now

    def frame_shown():
        desktop.windows[0] = WindowInfo(20, "Calculator", "ApplicationFrameWindow", cloaked=False)
    desktop.pending.append({"window": None, "polls": 4, "change": frame_shown})
    result = logic.wait_for_new_window(expectation, before)
    assert result.ok and result.window_handle == 20
    assert result.window_handles == {20, 21}  # the content window belongs to the app's group
    assert desktop.polls >= 4


def test_window_that_stays_cloaked_is_a_clear_retryable_failure(desktop):
    desktop.windows.append(WindowInfo(20, "Calculator", "ApplicationFrameWindow", cloaked=True))
    result = logic.wait_for_new_window(logic.expect_window("calculator"), frozenset())
    assert not result.ok and result.retryable
    assert result.message == "calculator was started, but its window didn't appear on screen within 0.3 seconds."


def test_hosted_windows_are_titled_matching_windows_inside_the_given_windows(desktop, monkeypatch):
    children = {20: [WindowInfo(21, "Calculator", "Windows.UI.Core.CoreWindow"), WindowInfo(22, "Help", "Popup")],
                30: [WindowInfo(31, "Calculator", "Windows.UI.Core.CoreWindow")]}
    monkeypatch.setattr(adapter, "list_child_windows", lambda handle: children.get(handle, []))
    assert logic.hosted_windows(logic.expect_window("calculator"), frozenset({20, 99})) == {21}


def test_hosted_windows_on_an_unobservable_desktop_raise(desktop, monkeypatch):
    def unavailable(handle):
        raise adapter.VerifierAdapterError("checking windows is only supported on Windows")
    monkeypatch.setattr(adapter, "list_child_windows", unavailable)
    with pytest.raises(logic.VerifierUnavailableError):
        logic.hosted_windows(logic.expect_window("calculator"), frozenset({20}))


# --- Verifying that windows closed ---

def test_find_open_returns_only_the_given_windows_that_still_match(desktop):
    desktop.windows += [WindowInfo(1, "mine - Notepad"), WindowInfo(2, "users - Notepad"),
                        WindowInfo(3, "Bank - Excel")]  # 3 was a Notepad handle, now reused
    expectation = logic.expect_window("notepad")
    assert [w.handle for w in logic.find_open(expectation, frozenset({1, 3, 99}))] == [1]


def test_windows_that_close_are_success(desktop):
    desktop.windows += [WindowInfo(20, "Calculator"), WindowInfo(21, "Calculator")]
    close_later(desktop, 20, polls=2)
    close_later(desktop, 21, polls=4)
    expectation = logic.expect_window("calculator")
    result = logic.wait_for_windows_to_close(expectation, frozenset({20, 21}))
    assert result.ok and result.message.startswith("calculator's window closed after")
    assert desktop.polls >= 4  # not done until EVERY window in the group was gone


def test_reused_handle_counts_as_closed(desktop):
    desktop.windows.append(WindowInfo(7, "Inbox - Outlook"))  # handle 7 used to be our Notepad
    result = logic.wait_for_windows_to_close(logic.expect_window("notepad"), frozenset({7}))
    assert result.ok


def test_window_that_stays_open_is_reported_after_the_timeout(desktop):
    desktop.windows.append(WindowInfo(7, "Untitled - Notepad"))
    started = time.monotonic()
    result = logic.wait_for_windows_to_close(logic.expect_window("notepad"), frozenset({7}))
    assert not result.ok and not result.retryable and not result.needs_user
    assert result.message == ("Asked notepad to close, but it's still open after 0.3 seconds. "
                              "It may be waiting for you.")
    assert 0.25 <= time.monotonic() - started < 2


def test_window_blocked_by_a_dialog_needs_the_user(desktop):
    desktop.config_path.write_text(CONFIG.replace("window_timeout_seconds: 0.3", "window_timeout_seconds: 30"),
                                   encoding="utf-8")
    desktop.windows.append(WindowInfo(7, "*Untitled - Notepad", "Notepad", enabled=False))
    started = time.monotonic()
    result = logic.wait_for_windows_to_close(logic.expect_window("notepad"), frozenset({7}))
    assert not result.ok and result.needs_user and not result.retryable
    assert "probably asking whether to save your changes" in result.message
    assert time.monotonic() - started < 2  # reported promptly, not after the 30 s timeout


def test_window_disabled_for_a_single_read_is_not_reported_as_needing_the_user(desktop):
    desktop.windows.append(WindowInfo(7, "Untitled - Notepad", "Notepad", enabled=False))
    close_later(desktop, 7, polls=2)  # disabled on the first read, gone on the second
    assert logic.wait_for_windows_to_close(logic.expect_window("notepad"), frozenset({7})).ok


def test_unobservable_desktop_while_closing_is_a_failure(desktop):
    desktop.error = adapter.VerifierAdapterError("desktop locked")
    result = logic.wait_for_windows_to_close(logic.expect_window("notepad"), frozenset({7}))
    assert not result.ok and not result.retryable and not result.needs_user
    assert result.message == "Asked notepad to close, but couldn't check whether it closed (desktop locked)."


def test_emergency_stop_interrupts_the_close_wait(desktop):
    desktop.config_path.write_text(CONFIG.replace("window_timeout_seconds: 0.3", "window_timeout_seconds: 30")
                                   .replace("poll_interval_seconds: 0.01", "poll_interval_seconds: 5"),
                                   encoding="utf-8")
    desktop.windows.append(WindowInfo(7, "Untitled - Notepad"))
    threading.Timer(0.1, emergency_stop.trigger, args=("hotkey",)).start()
    started = time.monotonic()
    with pytest.raises(EmergencyStopError):
        logic.wait_for_windows_to_close(logic.expect_window("notepad"), frozenset({7}))
    assert time.monotonic() - started < 2


# --- Real config + the real adapter (read-only) ---

def test_real_config_has_a_window_pattern_for_every_openable_app():
    from app.executor import logic as executor_logic
    for app_name in executor_logic._configured_apps():
        assert isinstance(logic.expect_window(app_name), WindowExpectation)


@pytest.mark.skipif(sys.platform != "win32", reason="the real desktop can only be read on Windows")
def test_real_adapter_lists_windows_without_changing_anything():
    windows = adapter.list_windows()
    assert all(isinstance(w.handle, int) and w.handle > 0 and w.title for w in windows)
    assert all(isinstance(w.class_name, str) and isinstance(w.enabled, bool) and isinstance(w.cloaked, bool)
               for w in windows)
    for window in windows[:5]:
        assert all(child.title for child in adapter.list_child_windows(window.handle))


# --- Screen facts for coordinate clicks (they never verify a click's effect) ---

def test_on_screen_needs_an_actual_monitor_not_just_the_bounding_box():
    screens = [Screen(0, 0, 1920, 1080, primary=True), Screen(1920, 0, 3200, 720), Screen(-1280, 0, 0, 1024)]
    assert logic.on_screen(screens, 0, 0) and logic.on_screen(screens, 1919, 1079)
    assert logic.on_screen(screens, -1280, 1023) and logic.on_screen(screens, 3199, 719)
    assert not logic.on_screen(screens, 1920, 1079)   # below the shorter right-hand monitor
    assert not logic.on_screen(screens, 3200, 0)       # right edge is exclusive
    assert not logic.on_screen(screens, 0, 1080)
    assert not logic.on_screen([], 0, 0)


@pytest.mark.parametrize("function, args, name", [
    (logic.screens, (), "list_screens"),
    (logic.window_at, (1, 2), "window_at"),
    (logic.cursor_position, (), "cursor_position"),
])
def test_screen_reads_on_an_unobservable_desktop_raise(monkeypatch, function, args, name):
    def unavailable(*a):
        raise adapter.VerifierAdapterError("checking windows is only supported on Windows")
    monkeypatch.setattr(adapter, name, unavailable)
    with pytest.raises(logic.VerifierUnavailableError, match="only supported on Windows"):
        function(*args)


@pytest.mark.skipif(sys.platform != "win32", reason="the real desktop can only be read on Windows")
def test_real_adapter_reads_screens_pointer_and_window_at_a_point_without_changing_anything():
    screens = adapter.list_screens()
    assert screens and sum(s.primary for s in screens) == 1
    assert all(s.right > s.left and s.bottom > s.top for s in screens)
    x, y = adapter.cursor_position()
    assert adapter.cursor_position() == (x, y)  # reading didn't move the pointer
    window = adapter.window_at(x, y)
    assert window is None or (isinstance(window.handle, int) and isinstance(window.title, str))
