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
from app.verifier.models import WindowExpectation, WindowInfo
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
    fake = SimpleNamespace(windows=[], pending=[], polls=0, config_path=config_path, error=None)

    def list_windows():
        if fake.error:
            raise fake.error
        fake.polls += 1
        for item in list(fake.pending):
            item["polls"] -= 1
            if item["polls"] <= 0:
                fake.windows.append(item["window"])
                fake.pending.remove(item)
        return list(fake.windows)

    monkeypatch.setattr(adapter, "list_windows", list_windows)
    emergency_stop.reset("test-setup")
    yield fake
    emergency_stop.reset("test-teardown")


def open_later(fake, handle, title, polls):
    fake.pending.append({"window": WindowInfo(handle, title), "polls": polls})


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
    assert result.ok and result.window_handle == 42
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


# --- Real config + the real adapter (read-only) ---

def test_real_config_has_a_window_pattern_for_every_openable_app():
    from app.executor import logic as executor_logic
    for app_name in executor_logic._configured_apps():
        assert isinstance(logic.expect_window(app_name), WindowExpectation)


@pytest.mark.skipif(sys.platform != "win32", reason="the real desktop can only be read on Windows")
def test_real_adapter_lists_windows_without_changing_anything():
    windows = adapter.list_windows()
    assert all(isinstance(w.handle, int) and w.handle > 0 and w.title for w in windows)
