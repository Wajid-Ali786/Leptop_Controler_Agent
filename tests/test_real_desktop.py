"""
Opt-in REAL desktop tests. They open Notepad and Calculator through the full pipeline (safety gate,
launch, window verification, recovery loop), and close only windows the test itself opened. Windows
that were already open - your own Notepad, for example, or a Calculator Windows started hidden in the
background - are never touched.

Skipped unless RUN_REAL_DESKTOP_TEST=1:

    $env:RUN_REAL_DESKTOP_TEST='1'; pytest -m real_desktop -v -s

Uses the real config.yaml (safety rules, apps, verifier timeout). Makes no Claude API calls. The
close_app test opens fresh, unedited windows, so no "Save changes?" dialog is expected. It FAILS if
close_app reports done while any window the test created is still open, or if the safety-net
cleanup had to close anything. To see how an app's windows behave over time, run
scripts/trace_app_windows.py.
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
WATCH_AFTER_DONE_SECONDS = 2  # after close_app says done, watch this long for a window left behind


def _describe(window):
    return f"{window.handle:#x} class={window.class_name!r} title={window.title!r}" + \
        (" cloaked" if window.cloaked else "")


def _new_windows(expectation, before):
    return [w for w in verifier_adapter.list_windows()
            if w.handle not in before and expectation.pattern.search(w.title)]


def _close_windows_opened_since(app_name, expectation, before, watch_seconds=CLEANUP_SECONDS):
    """Safety net: ask every matching window that wasn't open before the test to close, and wait until
    gone. Prints each window it closes, with its class. Until it has closed something, it keeps
    watching for `watch_seconds`, so a window that shows up late is still found and closed."""
    closed = {}
    start = time.monotonic()
    while True:
        new = {w.handle: w for w in _new_windows(expectation, before)}
        for handle, window in new.items():
            if handle not in closed:
                print(f"{app_name}: cleanup closing {_describe(window)}")
                ctypes.windll.user32.PostMessageW(ctypes.c_void_p(handle), WM_CLOSE, 0, 0)
                closed[handle] = window
        elapsed = time.monotonic() - start
        if (not new and (closed or elapsed >= watch_seconds)) or elapsed >= CLEANUP_SECONDS:
            break
        time.sleep(0.1)
    return list(closed.values())


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
        closed = _close_windows_opened_since(app_name, expectation, before)
        print(f"{app_name}: closed {len(closed)} window(s) this test opened")
    remaining = _new_windows(expectation, before)
    assert remaining == [], f"left {len(remaining)} {app_name} window(s) open"


@pytest.mark.real_desktop
@pytest.mark.parametrize("app_name", ["notepad", "calculator"])
def test_close_app_really_closes_the_window_it_opened_and_nothing_else(app_name):
    emergency_stop.reset("real-desktop-test")
    expectation = verifier.expect_window(app_name)
    before = verifier.snapshot_windows(expectation)  # e.g. your own Notepad: must survive untouched
    print(f"\n{app_name}: {len(before)} matching window(s) already open before the test")
    confirmations = []
    closed = None
    try:
        opened = execute_with_recovery(ExecutorAction(OPEN_APP, app_name))
        print(f"{app_name}: {opened.message}")
        assert opened.ok, opened.message
        for window in _new_windows(expectation, before):
            print(f"{app_name}: open now {_describe(window)}")

        closed = execute_with_recovery(ExecutorAction(CLOSE_APP, app_name),
                                       confirm=lambda action, assessment: confirmations.append(action) or True)
        left_at_done = _new_windows(expectation, before)  # checked the moment close_app returned
        print(f"{app_name}: {closed.message} (outcome: {closed.outcome.value})")
        for window in left_at_done:
            print(f"{app_name}: STILL OPEN when close_app returned: {_describe(window)}")
        assert closed.ok and closed.outcome is Outcome.DONE, closed.message
        assert len(confirmations) == 1  # closing is Medium risk: confirmed exactly once
        assert left_at_done == [], f"close_app reported done with {len(left_at_done)} {app_name} window(s) still open"
    finally:
        leftovers = _close_windows_opened_since(  # a safety net only: it should find nothing
            app_name, expectation, before,
            watch_seconds=WATCH_AFTER_DONE_SECONDS if closed is not None and closed.ok else CLEANUP_SECONDS)
        print(f"{app_name}: cleanup had to close {len(leftovers)} window(s)")
    assert leftovers == [], (f"cleanup had to close window(s) close_app should have closed: "
                             f"{[_describe(w) for w in leftovers]}")
    assert _new_windows(expectation, before) == []
    assert before <= verifier.snapshot_windows(expectation), "a window that was already open was closed"
