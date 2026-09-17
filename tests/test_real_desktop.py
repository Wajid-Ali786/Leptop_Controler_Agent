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

The click test is harmless by construction: it clicks only inside the empty text area of a Notepad
window the test opened itself (which just places the text cursor), approves the click only if the
confirmation names those exact coordinates and a Notepad window, and closes that Notepad afterwards.
It moves the mouse pointer. It also checks that a click at (99999, 99999) is refused before any
input is sent.

The typing test is harmless too: it types two lines of test text into a Notepad window the test
opened itself (and only if that Notepad is the active window), approves only a prompt naming Notepad,
49 characters and one Enter press, then - as test code, not the assistant - empties that Notepad and
marks it unmodified before closing it with close_app. If a "Save changes?" dialog appears anyway, the
test answers Don't Save on its own Notepad.
"""
import ctypes
import time

import pytest

from app.executor import emergency_stop
from app.executor.logic import execute_with_recovery
from app.executor.models import CLICK, CLOSE_APP, OPEN_APP, TYPE_TEXT, ExecutorAction, Outcome
from app.safety.models import RiskLevel
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


def _text_area_centre(handle):
    """Screen coordinates of the centre of a window's client area (Notepad: its empty text area)."""
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32")
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    rect = wintypes.RECT()
    assert user32.GetClientRect(handle, ctypes.byref(rect)), "couldn't read Notepad's client area"
    point = wintypes.POINT((rect.right - rect.left) // 2, (rect.bottom - rect.top) // 2)
    assert user32.ClientToScreen(handle, ctypes.byref(point))
    return point.x, point.y


@pytest.mark.real_desktop
def test_click_inside_the_text_area_of_a_notepad_the_test_opened():
    emergency_stop.reset("real-desktop-test")
    expectation = verifier.expect_window("notepad")
    before = verifier.snapshot_windows(expectation)

    # Off every screen: refused before anything is sent, so the pointer doesn't move.
    pointer_before = verifier_adapter.cursor_position()
    refused = execute_with_recovery(ExecutorAction(CLICK, "99999, 99999"))  # no confirm: asking would be a bug
    print(f"\nclick: {refused.message}")
    assert not refused.ok and "isn't on any screen" in refused.message
    assert verifier_adapter.cursor_position() == pointer_before

    prompts, clicked, closed = [], None, None
    try:
        opened = execute_with_recovery(ExecutorAction(OPEN_APP, "notepad"))
        print(f"click: {opened.message}")
        assert opened.ok, opened.message
        notepad = next(w for w in _new_windows(expectation, before) if w.class_name == "Notepad")
        x, y = _text_area_centre(notepad.handle)
        at_point = verifier_adapter.window_at(x, y)
        print(f"click: target ({x}, {y}); window there: {_describe(at_point) if at_point else None}")
        assert at_point is not None and at_point.handle == notepad.handle, \
            "the test's Notepad isn't the window at the click point - not clicking"

        def confirm(action, assessment):
            prompts.append(action.description)
            print(f"click: confirmation asked: {action.description!r} ({assessment.level.name}: {assessment.rule})")
            return (assessment.level >= RiskLevel.MEDIUM and f"({x}, {y})" in action.description
                    and "Notepad" in action.description)

        started = time.monotonic()
        clicked = execute_with_recovery(ExecutorAction(CLICK, f"{x}, {y}"), confirm=confirm)
        print(f"click: {clicked.message} (outcome: {clicked.outcome.value}, verified: {clicked.verified}, "
              f"{time.monotonic() - started:.2f}s including confirmation)")
        assert clicked.ok and clicked.outcome is Outcome.UNVERIFIED and not clicked.verified, clicked.message
        assert prompts == [f'click at ({x}, {y}) on window "{notepad.title}"']
        assert verifier_adapter.cursor_position() == (x, y)
        after = verifier_adapter.window_at(x, y)
        assert after is not None and after.handle == notepad.handle and after.title == notepad.title  # nothing typed

        closed = execute_with_recovery(ExecutorAction(CLOSE_APP, "notepad"), confirm=lambda action, assessment: True)
        print(f"click: {closed.message} (outcome: {closed.outcome.value})")
        assert closed.outcome is Outcome.DONE, closed.message
    finally:
        leftovers = _close_windows_opened_since(
            "notepad", expectation, before,
            watch_seconds=WATCH_AFTER_DONE_SECONDS if closed is not None and closed.ok else CLEANUP_SECONDS)
        print(f"notepad: cleanup had to close {len(leftovers)} window(s)")
    assert leftovers == [], f"cleanup had to close: {[_describe(w) for w in leftovers]}"
    assert before <= verifier.snapshot_windows(expectation), "a window that was already open was closed"


def _discard_test_text(field_handle):
    """Test cleanup, not an assistant action: empty the test's own Notepad and mark it unmodified."""
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32")
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = wintypes.LPARAM
    user32.SendMessageW(field_handle, 0x000C, 0, ctypes.cast(ctypes.c_wchar_p(""), ctypes.c_void_p).value)  # WM_SETTEXT
    user32.SendMessageW(field_handle, 0x00B9, 0, 0)  # EM_SETMODIFY: unmodified


def _answer_dont_save(notepad_handle):
    """Fallback cleanup for the test's own Notepad: answer its save dialog with Don't Save."""
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32")
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    for window in verifier_adapter.list_windows():
        if window.class_name == "#32770" and user32.GetWindow(window.handle, 4) == notepad_handle:  # GW_OWNER
            print(f"typing: answering Don't Save in {_describe(window)}")
            user32.SendMessageW(window.handle, 0x0400 + 102, 7, 0)  # TDM_CLICK_BUTTON, IDNO


@pytest.mark.real_desktop
def test_type_text_into_a_notepad_the_test_opened():
    emergency_stop.reset("real-desktop-test")
    expectation = verifier.expect_window("notepad")
    before = verifier.snapshot_windows(expectation)
    text = "Hello from the AI Desktop Companion test\nline two"
    prompts, typed, closed, field = [], None, None, None
    try:
        opened = execute_with_recovery(ExecutorAction(OPEN_APP, "notepad"))
        print(f"\ntyping: {opened.message}")
        assert opened.ok, opened.message
        notepad = next(w for w in _new_windows(expectation, before) if w.class_name == "Notepad")
        active = verifier_adapter.active_target()
        print(f"typing: active window {_describe(active.window) if active.window else None}, "
              f"field class {active.control_class!r}")
        assert active.window is not None and active.window.handle == notepad.handle and active.control_handle, \
            "the test's Notepad isn't the active window - not typing"
        field = active.control_handle

        def confirm(action, assessment):
            prompts.append(action.description)
            print(f"typing: confirmation asked: {action.description!r} ({assessment.level.name})")
            return (assessment.level.name == "HIGH" and "Notepad" in action.description
                    and action.description.startswith("type 49 characters") and "AND PRESS ENTER 1 TIME" in action.description)

        started = time.monotonic()
        typed = execute_with_recovery(ExecutorAction(TYPE_TEXT, text), confirm=confirm)
        print(f"typing: {typed.message} (outcome: {typed.outcome.value}, verified: {typed.verified}, "
              f"progress: {typed.progress}, {time.monotonic() - started:.2f}s including confirmation and check)")
        assert typed.outcome is Outcome.DONE and typed.verified and typed.progress == (49, 49), typed.message
        assert prompts == ['type 49 characters into window "Untitled - Notepad" (field: Edit) AND PRESS ENTER 1 TIME'
                           ' - Enter can submit a form, send a message or run a command: '
                           '"Hello from the AI Desktop Companion test\u2026"']

        _discard_test_text(field)
        closed = execute_with_recovery(ExecutorAction(CLOSE_APP, "notepad"), confirm=lambda action, assessment: True)
        print(f"typing: {closed.message} (outcome: {closed.outcome.value})")
        if closed.outcome is Outcome.NEEDS_USER:
            _answer_dont_save(notepad.handle)
        assert closed.outcome is Outcome.DONE, closed.message
    finally:
        leftovers = _close_windows_opened_since(
            "notepad", expectation, before,
            watch_seconds=WATCH_AFTER_DONE_SECONDS if closed is not None and closed.ok else CLEANUP_SECONDS)
        print(f"notepad: cleanup had to close {len(leftovers)} window(s)")
    assert leftovers == [], f"cleanup had to close: {[_describe(w) for w in leftovers]}"
    assert before <= verifier.snapshot_windows(expectation), "a window that was already open was closed"
