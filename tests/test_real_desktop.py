"""
Opt-in REAL desktop tests. They open Notepad and Calculator through the full pipeline (safety gate,
launch, window verification, recovery loop), and close only windows the test itself opened. Windows
that were already open - your own Notepad, for example - are never touched.

Skipped unless RUN_REAL_DESKTOP_TEST=1:

    $env:RUN_REAL_DESKTOP_TEST='1'; pytest -m real_desktop -v -s

Uses the real config.yaml (safety rules, apps, verifier timeout). Makes no Claude API calls. The
close_app test opens fresh, unedited windows, so no "Save changes?" dialog is expected.
"""
import ctypes
import time

import pytest

from app.executor import emergency_stop
from app.executor.logic import execute_with_recovery
from app.executor.models import CLOSE_APP, OPEN_APP, ExecutorAction, Outcome
from app.verifier import adapter as verifier_adapter
from app.verifier import logic as verifier

WM_CLOSE = 0x0010
CLEANUP_SECONDS = 20


def _close_windows_opened_since(expectation, before, wait_for_late_windows=True):
    """Ask every matching window that wasn't open before the test to close, and wait until gone.
    If the app was slow and its window shows up late, it is still found and closed here (unless
    wait_for_late_windows is False, when nothing new is open and nothing more is expected)."""
    closed = set()
    deadline = time.monotonic() + CLEANUP_SECONDS
    while time.monotonic() < deadline:
        new = {w.handle for w in verifier_adapter.list_windows()
               if w.handle not in before and expectation.pattern.search(w.title)}
        for handle in new - closed:
            ctypes.windll.user32.PostMessageW(ctypes.c_void_p(handle), WM_CLOSE, 0, 0)
            closed.add(handle)
        if not new and (closed or not wait_for_late_windows):
            return sorted(closed)
        time.sleep(0.25)
    return sorted(closed)


def _new_windows(expectation, before):
    return [w for w in verifier_adapter.list_windows()
            if w.handle not in before and expectation.pattern.search(w.title)]


@pytest.mark.real_desktop
@pytest.mark.parametrize("app_name", ["notepad", "calculator"])
def test_open_app_really_opens_a_window_and_the_test_closes_it(app_name):
    emergency_stop.reset("real-desktop-test")
    expectation = verifier.expect_window(app_name)
    before = verifier.snapshot_windows(expectation)
    try:
        result = execute_with_recovery(ExecutorAction(OPEN_APP, app_name))
        print(f"\n{app_name}: {result.message}")
        assert result.ok, result.message
    finally:
        closed = _close_windows_opened_since(expectation, before)
        print(f"{app_name}: closed {len(closed)} window(s) this test opened")
    remaining = _new_windows(expectation, before)
    assert remaining == [], f"left {len(remaining)} {app_name} window(s) open"


@pytest.mark.real_desktop
@pytest.mark.parametrize("app_name", ["notepad", "calculator"])
def test_close_app_really_closes_the_window_it_opened_and_nothing_else(app_name):
    emergency_stop.reset("real-desktop-test")
    expectation = verifier.expect_window(app_name)
    before = verifier.snapshot_windows(expectation)  # e.g. your own Notepad: must survive untouched
    confirmations = []
    closed = None
    try:
        opened = execute_with_recovery(ExecutorAction(OPEN_APP, app_name))
        print(f"\n{app_name}: {opened.message}")
        assert opened.ok, opened.message
        for window in _new_windows(expectation, before):
            print(f"{app_name}: opened window {window.handle} class={window.class_name!r} enabled={window.enabled}")

        closed = execute_with_recovery(ExecutorAction(CLOSE_APP, app_name),
                                       confirm=lambda action, assessment: confirmations.append(action) or True)
        print(f"{app_name}: {closed.message} (outcome: {closed.outcome.value})")
        assert closed.ok and closed.outcome is Outcome.DONE, closed.message
        assert len(confirmations) == 1  # closing is Medium risk: confirmed exactly once
    finally:
        leftovers = _close_windows_opened_since(  # only closes something if close_app didn't
            expectation, before, wait_for_late_windows=not (closed is not None and closed.ok))
        print(f"{app_name}: cleanup had to close {len(leftovers)} window(s)")
    assert _new_windows(expectation, before) == []
    assert before <= verifier.snapshot_windows(expectation), "a window that was already open was closed"
