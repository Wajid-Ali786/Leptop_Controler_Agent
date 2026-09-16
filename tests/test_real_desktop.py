"""
Opt-in REAL desktop test: opens Notepad and Calculator through the full pipeline (safety gate,
launch, window verification, recovery loop) and then closes ONLY the windows it verified as newly
opened. Windows that were already open - your own Notepad, for example - are never touched.

Skipped unless RUN_REAL_DESKTOP_TEST=1:

    $env:RUN_REAL_DESKTOP_TEST='1'; pytest -m real_desktop -v -s

Uses the real config.yaml (safety rules, apps, verifier timeout). Makes no Claude API calls.
"""
import ctypes
import time

import pytest

from app.executor import emergency_stop
from app.executor.logic import execute_with_recovery
from app.executor.models import OPEN_APP, ExecutorAction
from app.verifier import adapter as verifier_adapter
from app.verifier import logic as verifier

WM_CLOSE = 0x0010
CLEANUP_SECONDS = 20


def _close_windows_opened_since(expectation, before):
    """Ask every matching window that wasn't open before the test to close, and wait until gone.
    If the app was slow and its window shows up late, it is still found and closed here."""
    closed = set()
    deadline = time.monotonic() + CLEANUP_SECONDS
    while time.monotonic() < deadline:
        new = {w.handle for w in verifier_adapter.list_windows()
               if w.handle not in before and expectation.pattern.search(w.title)}
        for handle in new - closed:
            ctypes.windll.user32.PostMessageW(ctypes.c_void_p(handle), WM_CLOSE, 0, 0)
            closed.add(handle)
        if closed and not new:
            return sorted(closed)
        time.sleep(0.25)
    return sorted(closed)


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
    remaining = [w for w in verifier_adapter.list_windows()
                 if w.handle not in before and expectation.pattern.search(w.title)]
    assert remaining == [], f"left {len(remaining)} {app_name} window(s) open"
