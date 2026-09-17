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

The shortcut test stays inside a Notepad the test opened: it types "abc", presses Ctrl+A (LOW - it must
not ask), Ctrl+Z (MEDIUM, approved), empties the Notepad as test code, and closes it with Alt+F4 (HIGH,
approved, allowed because the assistant opened that window). It never touches the clipboard, other
windows, Alt+Tab or Win+D. The clipboard test additionally needs RUN_REAL_CLIPBOARD_TEST=1, because it
REPLACES what is on your clipboard (Ctrl+C, then Ctrl+V into its own Notepad).

The scroll test records where your mouse pointer is, opens its own Notepad, fills it with 200 short
lines and moves the pointer over it (both as test code), scrolls down and up (LOW - no prompts), puts
that Notepad at the top (test code) and checks "up 1" reports "already at the top", checks refusals,
and ALWAYS - even if an assertion fails - puts your pointer back and closes only its own Notepad.
"""
import ctypes
import time

import pytest

from app.executor import emergency_stop
from app.executor.logic import execute_with_recovery
from app.executor.models import CLICK, CLOSE_APP, OPEN_APP, SCROLL, SHORTCUT, TYPE_TEXT, ExecutorAction, Outcome
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
                           ' - Enter can submit a form, send a message or run a command']
        assert not any(word in prompts[0] for word in ("Hello", "Companion", "line two"))  # no text in the prompt

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


def _open_test_notepad(expectation, before, label):
    opened = execute_with_recovery(ExecutorAction(OPEN_APP, "notepad"))
    print(f"\n{label}: {opened.message}")
    assert opened.ok, opened.message
    notepad = next(w for w in _new_windows(expectation, before) if w.class_name == "Notepad")
    active = verifier_adapter.active_target()
    assert active.window is not None and active.window.handle == notepad.handle and active.control_handle, \
        "the test's Notepad isn't the active window - not pressing anything"
    return notepad, active.control_handle


def _press(shortcut, label, expect_prompt_start=None):
    """Press through the full pipeline. With expect_prompt_start=None the shortcut must NOT ask (LOW);
    otherwise only a prompt starting with that text is approved."""
    prompts = []

    def confirm(action, assessment):
        prompts.append((action.description, assessment.level.name))
        return expect_prompt_start is not None and action.description.startswith(expect_prompt_start)
    result = execute_with_recovery(ExecutorAction(SHORTCUT, shortcut), confirm=confirm)
    print(f"{label}: {shortcut} -> {result.outcome.value}: {result.message} (prompts: {prompts})")
    assert (prompts == []) is (expect_prompt_start is None), prompts
    return result


@pytest.mark.real_desktop
def test_shortcuts_in_a_notepad_the_test_opened():
    emergency_stop.reset("real-desktop-test")
    expectation = verifier.expect_window("notepad")
    before = verifier.snapshot_windows(expectation)
    closed = None
    try:
        notepad, field = _open_test_notepad(expectation, before, "shortcuts")
        typed = execute_with_recovery(ExecutorAction(TYPE_TEXT, "abc"), confirm=lambda action, assessment: True)
        assert typed.outcome is Outcome.DONE, typed.message

        started = time.monotonic()
        select_all = _press("ctrl+a", "shortcuts")
        print(f"shortcuts: Ctrl+A took {time.monotonic() - started:.2f}s")
        assert select_all.outcome is Outcome.DONE, select_all.message

        undo = _press("ctrl+z", "shortcuts", expect_prompt_start='press Ctrl+Z in window "')
        assert undo.ok and undo.outcome is Outcome.UNVERIFIED, undo.message

        _discard_test_text(field)
        closed = _press("alt+f4", "shortcuts", expect_prompt_start=f'press Alt+F4 on window "{notepad.title}"')
        if closed.outcome is Outcome.NEEDS_USER:
            _answer_dont_save(notepad.handle)
        assert closed.outcome is Outcome.DONE, closed.message
        assert verifier_adapter.modifier_keys_down() == [], "a modifier key reads as still held down"
    finally:
        leftovers = _close_windows_opened_since(
            "notepad", expectation, before,
            watch_seconds=WATCH_AFTER_DONE_SECONDS if closed is not None and closed.ok else CLEANUP_SECONDS)
        print(f"notepad: cleanup had to close {len(leftovers)} window(s)")
    assert leftovers == [], f"cleanup had to close: {[_describe(w) for w in leftovers]}"
    assert before <= verifier.snapshot_windows(expectation), "a window that was already open was closed"


@pytest.mark.real_desktop
@pytest.mark.real_clipboard
def test_copy_and_paste_in_a_notepad_the_test_opened():
    """REPLACES your clipboard's contents. Needs RUN_REAL_CLIPBOARD_TEST=1 as well."""
    emergency_stop.reset("real-desktop-test")
    expectation = verifier.expect_window("notepad")
    before = verifier.snapshot_windows(expectation)
    closed = None
    try:
        notepad, field = _open_test_notepad(expectation, before, "clipboard")
        yes = lambda action, assessment: True  # noqa: E731
        assert execute_with_recovery(ExecutorAction(TYPE_TEXT, "clipboard test"), confirm=yes).outcome is Outcome.DONE
        assert _press("ctrl+a", "clipboard").outcome is Outcome.DONE
        copied = _press("ctrl+c", "clipboard", expect_prompt_start='press Ctrl+C in window "')
        assert copied.outcome is Outcome.DONE, copied.message
        assert execute_with_recovery(ExecutorAction(TYPE_TEXT, " "), confirm=yes).ok  # replaces the selection
        pasted = _press("ctrl+v", "clipboard", expect_prompt_start='press Ctrl+V in window "')
        assert pasted.ok and pasted.outcome is Outcome.UNVERIFIED, pasted.message
        assert verifier.wait_until(lambda: verifier.count_text(field, "clipboard test") == 1), \
            "the pasted text didn't show up in the test's Notepad"  # checked by the TEST, not the assistant
        _discard_test_text(field)
        closed = _press("alt+f4", "clipboard", expect_prompt_start="press Alt+F4 on window")
        if closed.outcome is Outcome.NEEDS_USER:
            _answer_dont_save(notepad.handle)
        assert closed.outcome is Outcome.DONE, closed.message
    finally:
        leftovers = _close_windows_opened_since(
            "notepad", expectation, before,
            watch_seconds=WATCH_AFTER_DONE_SECONDS if closed is not None and closed.ok else CLEANUP_SECONDS)
    assert leftovers == []


def _test_user32():
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32")
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = wintypes.LPARAM
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    return user32


def _fill_with_lines(field_handle, count):
    """Test setup, not an assistant action: put `count` short lines into the test's own Notepad."""
    text = "\r\n".join(f"scroll test line {n}" for n in range(1, count + 1))
    _test_user32().SendMessageW(field_handle, 0x000C, 0, ctypes.cast(ctypes.c_wchar_p(text), ctypes.c_void_p).value)


def _scroll_to_top(field_handle):
    """Test setup: scroll the test's own Notepad to the very top (WM_VSCROLL, SB_TOP)."""
    _test_user32().SendMessageW(field_handle, 0x0115, 6, 0)


def _scroll(target, label):
    prompts = []
    result = execute_with_recovery(ExecutorAction(SCROLL, target),
                                   confirm=lambda action, assessment: prompts.append(action.description) or False)
    print(f"{label}: scroll {target!r} -> {result.outcome.value}: {result.message} (progress {result.progress}, "
          f"prompts {prompts})")
    return result, prompts


@pytest.mark.real_desktop
def test_scroll_in_a_notepad_the_test_opened():
    emergency_stop.reset("real-desktop-test")
    expectation = verifier.expect_window("notepad")
    before = verifier.snapshot_windows(expectation)
    original_pointer = verifier_adapter.cursor_position()  # recorded before anything moves the pointer
    print(f"\nscroll: original pointer position {original_pointer}")
    closed, field = None, None
    try:
        notepad, field = _open_test_notepad(expectation, before, "scroll")
        _fill_with_lines(field, 200)
        _scroll_to_top(field)
        x, y = _text_area_centre(field)
        _test_user32().SetCursorPos(x, y)
        start = verifier_adapter.vertical_scroll(field)
        print(f"scroll: pointer at ({x}, {y}); scroll bar before: {start}")
        assert start is not None and start.at_top

        down, prompts = _scroll("down 5", "scroll")
        assert prompts == [] and down.outcome is Outcome.DONE and down.progress == (5, 5), down.message
        middle = verifier_adapter.vertical_scroll(field)
        assert middle.position > start.position

        up, prompts = _scroll("up 5", "scroll")
        assert prompts == [] and up.outcome is Outcome.DONE, up.message

        _scroll_to_top(field)  # explicitly at the top - not relying on down 5 + up 5 cancelling out
        assert verifier_adapter.vertical_scroll(field).at_top
        at_top, prompts = _scroll("up 1", "scroll")
        assert prompts == [] and at_top.ok and at_top.outcome is Outcome.UNVERIFIED
        assert at_top.message == "Scrolled up 1 notch, but it was already at the top, so nothing moved."

        for target, message in [("down 999", "That's 999 notches; I scroll at most 20 at once."),
                                ("left 3", "Horizontal scrolling isn't supported yet.")]:
            refused, prompts = _scroll(target, "scroll")
            assert not refused.ok and refused.message == message and prompts == []
        assert verifier_adapter.vertical_scroll(field).at_top  # the refusals scrolled nothing
    finally:
        _test_user32().SetCursorPos(*original_pointer)  # always put the user's pointer back
        print(f"scroll: pointer restored to {verifier_adapter.cursor_position()}")
        if field:
            _discard_test_text(field)
        try:
            closed = execute_with_recovery(ExecutorAction(CLOSE_APP, "notepad"), confirm=lambda action, assessment: True)
            print(f"scroll: {closed.message} (outcome: {closed.outcome.value})")
        except Exception as exc:  # cleanup must continue to the safety net below
            print(f"scroll: close_app failed during cleanup: {exc!r}")
        leftovers = _close_windows_opened_since(
            "notepad", expectation, before,
            watch_seconds=WATCH_AFTER_DONE_SECONDS if closed is not None and closed.ok else CLEANUP_SECONDS)
        print(f"notepad: cleanup had to close {len(leftovers)} window(s)")
    assert verifier_adapter.cursor_position() == original_pointer
    assert closed is not None and closed.outcome is Outcome.DONE, closed and closed.message
    assert leftovers == [], f"cleanup had to close: {[_describe(w) for w in leftovers]}"
    assert before <= verifier.snapshot_windows(expectation), "a window that was already open was closed"
