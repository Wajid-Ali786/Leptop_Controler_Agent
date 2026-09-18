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
not ask), Ctrl+Z (MEDIUM, approved), empties the Notepad as test code, and closes it with window control
close (MEDIUM, approved - Alt+F4 is no longer a shortcut). It never touches the clipboard, other
windows, Alt+Tab or Win+D. The clipboard test additionally needs RUN_REAL_CLIPBOARD_TEST=1, because it
REPLACES what is on your clipboard (Ctrl+C, then Ctrl+V into its own Notepad).

The scroll test records where your mouse pointer is, opens its own Notepad, fills it with 200 short
lines and moves the pointer over it (both as test code), scrolls down and up (LOW - no prompts), puts
that Notepad at the top (test code) and checks "up 1" reports "already at the top", checks refusals,
and ALWAYS - even if an assertion fails - puts your pointer back and closes only its own Notepad.

The refresh test creates a temporary folder and opens it in a NEW File Explorer window (both as test
code), refreshes it (File Explorer folder view: LOW - it must not ask; result unverified), and ALWAYS
closes only that Explorer window and deletes only that temporary folder. There is no browser test: it
would open your real browser profile.

The window-control test uses only Notepads it started: A (opened by the assistant) is maximized, restored
and minimized (LOW, each verified by reading the window state) and closed with close_app while minimized;
B (opened by the assistant) is closed with window control close (MEDIUM, approved); C is started by the
TEST, not the assistant, so window control close must refuse it (nothing sent) and the test closes it.
Alt+F4 is checked to be refused as a shortcut. Everything the test started is closed in finally.

The typed-command test is Phase 1's acceptance run: ten different commands typed into the REAL console
(app/console.py), back to back, each going through the normal Executor pipeline. As TEST setup it starts
its own Notepad as a scroll fixture (filled with 200 lines, put at the top - it is what the click and
scroll commands act on), and opens a temporary folder in a NEW File Explorer window. The commands open
and close Calculator, open a Notepad and maximize, type into, select in and minimize it. The TEST, never
the console, switches windows: the console only watches which window is in front. Confirmations are
answered "yes" only when the console asked exactly what that command should ask. In finally it puts the
pointer back and closes only the windows it started, leaving no Notepad, Calculator or Explorer window
behind and never touching the clipboard.
"""
import ctypes
import time
from types import SimpleNamespace

import pytest

from app.executor import emergency_stop
from app.executor.logic import execute_with_recovery
from app.executor.models import (CLICK, CLOSE_APP, OPEN_APP, REFRESH, SCROLL, SHORTCUT, TYPE_TEXT, WINDOW_CONTROL,
                                 ExecutorAction, Outcome)
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


def _window_control(operation, label, expect_prompt_start=None):
    """Window control through the full pipeline. With expect_prompt_start=None it must NOT ask (LOW); otherwise
    only a prompt starting with that text is approved."""
    prompts = []

    def confirm(action, assessment):
        prompts.append((action.description, assessment.level.name))
        return expect_prompt_start is not None and action.description.startswith(expect_prompt_start)
    result = execute_with_recovery(ExecutorAction(WINDOW_CONTROL, operation), confirm=confirm)
    print(f"{label}: window control {operation} -> {result.outcome.value}: {result.message} (prompts: {prompts})")
    assert (prompts == []) is (expect_prompt_start is None), prompts
    return result


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
        closed = _window_control("close", "shortcuts", expect_prompt_start="close window ")
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
        closed = _window_control("close", "clipboard", expect_prompt_start="close window ")
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


def _explorer_windows_for(folder_name, exclude):
    return [w for w in verifier_adapter.list_windows()
            if w.class_name == "CabinetWClass" and w.handle not in exclude and folder_name in w.title]


def _wait_for(check, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.2)
    return check()


@pytest.mark.real_desktop
def test_refresh_a_file_explorer_window_the_test_opened():
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    emergency_stop.reset("real-desktop-test")
    folder = Path(tempfile.mkdtemp(prefix="companion-refresh-test-"))  # created by the test
    existing = {w.handle for w in verifier_adapter.list_windows() if w.class_name == "CabinetWClass"}
    print(f"\nrefresh: temporary folder {folder.name}; {len(existing)} File Explorer window(s) already open")
    closed_handles, folder_removed = [], False
    try:
        subprocess.Popen(["explorer.exe", str(folder)])  # test setup: a NEW Explorer window on the test's folder
        windows = _wait_for(lambda: _explorer_windows_for(folder.name, existing), 15)
        assert windows, "the test's File Explorer window didn't appear"
        window = windows[0]
        active = _wait_for(lambda: verifier_adapter.active_target().window
                           and verifier_adapter.active_target().window.handle == window.handle, 5)
        target = verifier_adapter.active_target()
        print(f"refresh: test window {window.handle:#x}; active: {_describe(target.window) if target.window else None}, "
              f"field class {target.control_class!r}")
        assert active, "the test's File Explorer window isn't the active window - not refreshing"

        prompts = []
        started = time.monotonic()
        result = execute_with_recovery(ExecutorAction(REFRESH),
                                       confirm=lambda action, assessment: prompts.append(action.description) or False)
        print(f"refresh: {result.outcome.value}: {result.message} ({time.monotonic() - started:.2f}s, prompts {prompts})")
        assert prompts == [], "a File Explorer folder view must refresh without asking"
        assert result.ok and result.outcome is Outcome.UNVERIFIED and not result.verified, result.message
        assert result.message == "Pressed F5 to refresh File Explorer. I can't confirm the refresh happened."
    finally:
        # Close ONLY the Explorer window(s) showing the test's folder that weren't open before, then delete ONLY the
        # test's folder.
        for w in _explorer_windows_for(folder.name, existing):
            ctypes.windll.user32.PostMessageW(ctypes.c_void_p(w.handle), WM_CLOSE, 0, 0)
            closed_handles.append(w.handle)
        _wait_for(lambda: not _explorer_windows_for(folder.name, existing), CLEANUP_SECONDS)
        print(f"refresh: closed {len(closed_handles)} test Explorer window(s)")
        try:
            shutil.rmtree(folder)
            folder_removed = True
        except OSError as exc:
            print(f"refresh: couldn't delete the temporary folder yet: {exc!r}")
    assert _explorer_windows_for(folder.name, existing) == [], "the test's Explorer window is still open"
    assert len(closed_handles) == 1, f"expected to close exactly the test's window, closed {len(closed_handles)}"
    assert folder_removed and not folder.exists()
    still_open = {w.handle for w in verifier_adapter.list_windows() if w.class_name == "CabinetWClass"}
    assert existing <= still_open, "a File Explorer window that was already open was closed"


def _new_notepads(before):
    return [w for w in verifier_adapter.list_windows() if w.class_name == "Notepad" and w.handle not in before]


def _wait_active(handle, seconds=5):
    return _wait_for(lambda: (verifier_adapter.active_target().window or SimpleWindow).handle == handle, seconds)


class SimpleWindow:
    handle = None


@pytest.mark.real_desktop
def test_window_controls_on_notepads_the_test_started():
    import subprocess

    emergency_stop.reset("real-desktop-test")
    expectation = verifier.expect_window("notepad")
    before = {w.handle for w in verifier_adapter.list_windows() if w.class_name == "Notepad"}
    before_matching = verifier.snapshot_windows(expectation)
    try:
        # --- A: opened by the assistant; maximize, restore, minimize - each read back - then close_app ---
        a, _ = _open_test_notepad(expectation, before_matching, "window-control A")
        state = verifier_adapter.window_state(a.handle)
        print(f"window-control A: start state {state}")
        assert state and not state.minimized and not state.maximized
        started = time.monotonic()
        assert _window_control("maximize", "window-control A").outcome is Outcome.DONE
        assert verifier_adapter.window_state(a.handle).maximized
        assert _window_control("restore", "window-control A").outcome is Outcome.DONE
        restored = verifier_adapter.window_state(a.handle)
        assert not restored.maximized and not restored.minimized
        assert _window_control("minimize", "window-control A").outcome is Outcome.DONE
        assert verifier_adapter.window_state(a.handle).minimized
        print(f"window-control A: maximize + restore + minimize took {time.monotonic() - started:.2f}s")
        closed_a = execute_with_recovery(ExecutorAction(CLOSE_APP, "notepad"), confirm=lambda action, assessment: True)
        print(f"window-control A: close_app on the minimized window -> {closed_a.outcome.value}: {closed_a.message}")
        assert closed_a.outcome is Outcome.DONE and verifier_adapter.window_state(a.handle) is None

        # --- B: opened by the assistant; window control close (MEDIUM, one confirmation) ---
        b, _ = _open_test_notepad(expectation, before_matching, "window-control B")
        closed_b = _window_control("close", "window-control B",
                                   expect_prompt_start=f'close window "{b.title}" (notepad, opened by the assistant')
        assert closed_b.outcome is Outcome.DONE and verifier_adapter.window_state(b.handle) is None

        # --- C: started by the TEST, not the assistant: window control close must refuse it ---
        subprocess.Popen(["notepad.exe"])  # test setup - deliberately not a session window
        c = _wait_for(lambda: _new_notepads(before), 15)
        assert c, "the test's Notepad C didn't appear"
        c = c[0]
        assert _wait_active(c.handle), "Notepad C isn't the active window - not testing the refusal"
        refused = _window_control("close", "window-control C")  # refused before the gate: no prompt at all
        assert not refused.ok and "I only close windows I opened in this session" in refused.message
        assert verifier_adapter.window_state(c.handle) is not None, "Notepad C must still be open"

        # --- Alt+F4 is not a second close path ---
        alt_f4 = execute_with_recovery(ExecutorAction(SHORTCUT, "alt+f4"), confirm=lambda action, assessment: True)
        print(f"window-control: shortcut alt+f4 -> {alt_f4.outcome.value}: {alt_f4.message}")
        assert not alt_f4.ok and "use close_app or window control close" in alt_f4.message
        assert verifier_adapter.window_state(c.handle) is not None, "Alt+F4 must not have closed Notepad C"
    finally:
        # Close every Notepad the test started (A, B, C or any left behind), and only those.
        leftovers = []
        for w in _new_notepads(before):
            ctypes.windll.user32.PostMessageW(ctypes.c_void_p(w.handle), WM_CLOSE, 0, 0)
            leftovers.append(w)
        _wait_for(lambda: not _new_notepads(before), CLEANUP_SECONDS)
        print(f"window-control: cleanup closed {len(leftovers)} window(s): {[_describe(w) for w in leftovers]}")
    assert _new_notepads(before) == [], "a Notepad the test started is still open"
    assert [w.handle for w in leftovers] == [c.handle], "cleanup should only have had to close Notepad C"
    still_open = {w.handle for w in verifier_adapter.list_windows() if w.class_name == "Notepad"}
    assert before <= still_open, "a Notepad that was already open was closed"


# --- Typed commands: the Phase 1 acceptance run ---------------------------------------------------

TYPED_TEXT = "Hello from the typed-command test"  # 33 characters, no line break


def _step(line, target=None, expect_prompt=None):
    """One typed command: the line, the window the person would switch to first (None if the command
    doesn't depend on which window is in front), and the confirmation it must ask (None: must not ask)."""
    return SimpleNamespace(line=line, target=target, expect_prompt=expect_prompt)


def _bring_to_front(handle, seconds=5):
    """TEST CODE standing in for the person switching windows. app/console.py never does this: it only
    watches which window is in front."""
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32")
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SwitchToThisWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
    user32.SetForegroundWindow(handle)
    if not _wait_active(handle, 1):
        user32.SwitchToThisWindow(handle, True)
    return _wait_active(handle, seconds)


class TypedSession:
    """Types commands into the REAL console (app/console.py) and answers its prompts, the way a person
    would. Nothing reaches the Executor any other way."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.index = -1
        self.current = None
        self.written = []
        self.since_command = []
        self.confirmations = []
        self.replies = []

    def read(self, prompt=""):
        if prompt.startswith(">"):
            self.index += 1
            if self.index >= len(self.steps):
                return "exit"
            self.current = self.steps[self.index]
            self.since_command = []
            print(f"\ntyped [{self.index + 1}]> {self.current.line}")
            return self.current.line
        asked = "\n".join(self.since_command)  # everything the console said before it asked
        expected = self.current.expect_prompt
        answer = "yes" if expected and expected in asked else "no"
        self.confirmations.append((self.index + 1, asked, answer))
        print(f"  console asked: {asked!r} -> {answer}")
        return answer

    def write(self, text=""):
        self.written.append(str(text))
        self.since_command.append(str(text))
        print(f"  {text}")


class TypedFocus:
    """The focus hand-over, done by the TEST: it puts the window the current command is for in front, and
    refuses - so nothing runs - if that window isn't in front afterwards."""

    def __init__(self, session):
        self.session = session
        self.calls = []

    def note_console_window(self):
        self.calls.append((self.session.index + 1, "noted"))

    def hand_over(self, prompt):
        step = self.session.current
        self.calls.append((self.session.index + 1, prompt))
        assert step.target, f"command {step.line!r} asked for a hand-over but the test names no window"
        if not _bring_to_front(step.target):
            return f"the test couldn't put window {step.target:#x} in front"
        return None


@pytest.mark.real_desktop
def test_ten_typed_commands_back_to_back_through_the_console():
    """Phase 1's done-when: ten different typed commands, through the real typed-command entry point and
    the normal Executor pipeline, back to back with no code changes between them."""
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    from app import console

    emergency_stop.reset("real-desktop-test")
    expectation = verifier.expect_window("notepad")
    notepads_before = {w.handle for w in verifier_adapter.list_windows() if w.class_name == "Notepad"}
    matching_before = verifier.snapshot_windows(expectation)
    calculator = verifier.expect_window("calculator")
    calculators_before = verifier.snapshot_windows(calculator)
    explorers_before = {w.handle for w in verifier_adapter.list_windows() if w.class_name == "CabinetWClass"}
    original_pointer = verifier_adapter.cursor_position()
    clipboard_before = verifier.clipboard_sequence()
    folder = Path(tempfile.mkdtemp(prefix="companion-typed-test-"))
    print(f"\ntyped: temporary folder {folder.name}; pointer at {original_pointer}")

    fixture = fixture_field = session_notepad = session_field = None
    leftovers, calc_left, explorer_left, folder_removed = [], [], [], False
    try:
        # --- TEST SETUP (not typed commands): the scroll fixture Notepad and an Explorer window ---
        subprocess.Popen(["notepad.exe"])  # started by the TEST, deliberately NOT a session window
        fixture = _wait_for(lambda: _new_notepads(notepads_before), 15)
        assert fixture, "the test's scroll-fixture Notepad didn't appear"
        fixture = fixture[0]
        assert _wait_active(fixture.handle, 2) or _bring_to_front(fixture.handle), \
            "the scroll fixture isn't in front - not filling it"
        fixture_field = verifier_adapter.active_target().control_handle
        _fill_with_lines(fixture_field, 200)
        _scroll_to_top(fixture_field)
        start_scroll = verifier_adapter.vertical_scroll(fixture_field)
        click_x, click_y = _text_area_centre(fixture_field)
        print(f"typed: scroll fixture {fixture.handle:#x}, scroll bar {start_scroll}, "
              f"click point ({click_x}, {click_y})")
        assert start_scroll is not None and start_scroll.at_top and not start_scroll.at_bottom

        subprocess.Popen(["explorer.exe", str(folder)])  # test setup: a NEW Explorer window on the test's folder
        explorer = _wait_for(lambda: _explorer_windows_for(folder.name, explorers_before), 15)
        assert explorer, "the test's File Explorer window didn't appear"
        explorer = explorer[0]

        # --- THE TEN TYPED COMMANDS ---
        steps = [
            _step("refresh", explorer.handle),
            _step("open calculator"),
            _step("close calculator", expect_prompt="close app calculator"),
            _step("open notepad"),
            _step("maximize"),                      # target filled in once the assistant's Notepad exists
            _step(f"type {TYPED_TEXT}", expect_prompt="type 33 characters into window"),
            _step("shortcut ctrl+a"),
            _step(f"click {click_x}, {click_y}", fixture.handle,
                  expect_prompt=f"click at ({click_x}, {click_y}) on window"),
            _step("scroll down 3", fixture.handle),
            _step("minimize"),
        ]
        session = TypedSession(steps)
        focus = TypedFocus(session)
        real_handle_command = console.handle_command

        observed = []  # what the desktop looked like right AFTER each command (before the next one)

        def recording(text, **kwargs):  # the real entry point; the test only records what came back
            nonlocal session_field, session_notepad
            reply = real_handle_command(text, **kwargs)
            session.replies.append(reply)
            if reply.action is not None and reply.action.kind == OPEN_APP and reply.action.target == "notepad":
                found = [w for w in _new_notepads(notepads_before) if w.handle != fixture.handle]
                assert found, "the Notepad the assistant opened wasn't found"
                session_notepad = found[0]
                for step in (steps[4], steps[5], steps[6], steps[9]):  # the commands that act on it
                    step.target = session_notepad.handle
                session_field = verifier_adapter.active_target().control_handle  # for cleanup only
            observed.append(SimpleNamespace(
                state=verifier_adapter.window_state(session_notepad.handle) if session_notepad else None,
                scroll=verifier_adapter.vertical_scroll(fixture_field),
                pointer=verifier_adapter.cursor_position()))
            return reply
        console.handle_command = recording
        try:
            started = time.monotonic()
            exit_code = console.run_console(read=session.read, write=session.write, focus=focus)
        finally:
            console.handle_command = real_handle_command
        elapsed = time.monotonic() - started
        session_notepad = next(w for w in _new_notepads(notepads_before) if w.handle != fixture.handle)
        print(f"\ntyped: ten commands took {elapsed:.1f}s; exit code {exit_code}; "
              f"{len(session.confirmations)} confirmation(s)")

        # --- What each command must have done ---
        assert exit_code == 0 and len(session.replies) == 10, [r.status for r in session.replies]
        for step, reply in zip(steps, session.replies):
            print(f"typed: {step.line!r} -> {reply.status.value}/"
                  f"{reply.result.outcome.value if reply.result else None}: {reply.message}")
        assert all(r.status.value == "ran" and r.result.ok for r in session.replies), \
            [(r.status.value, r.message) for r in session.replies]

        refresh, calc_open, calc_close, np_open, maximize, typing, select, click, scroll, minimize = session.replies
        assert refresh.result.outcome is Outcome.UNVERIFIED
        assert refresh.message == "Pressed F5 to refresh File Explorer. I can't confirm the refresh happened."
        assert calc_open.result.outcome is Outcome.DONE and calc_open.message.startswith("Opened calculator")
        assert calc_close.result.outcome is Outcome.DONE and calc_close.message.startswith("Closed calculator")
        assert np_open.result.outcome is Outcome.DONE and np_open.message.startswith("Opened notepad")
        assert maximize.result.outcome is Outcome.DONE and maximize.message == "Maximized the window."
        assert observed[4].state.maximized, "the window wasn't maximized after the maximize command"
        assert typing.result.outcome is Outcome.DONE and typing.result.verified
        assert typing.result.progress == (33, 33) and TYPED_TEXT not in typing.message
        assert select.result.outcome is Outcome.DONE, select.message
        assert click.result.outcome is Outcome.UNVERIFIED
        assert click.message == f"Clicked at ({click_x}, {click_y}). I can't check what the click did."
        assert observed[7].pointer == (click_x, click_y)
        assert scroll.result.outcome is Outcome.DONE and scroll.result.progress == (3, 3)
        print(f"typed: scroll fixture position {start_scroll.position} -> {observed[8].scroll}")
        assert observed[8].scroll.position > start_scroll.position, "the fixture Notepad didn't actually scroll"
        assert minimize.result.outcome is Outcome.DONE and minimize.message == "Minimized the window."
        assert observed[9].state.minimized, "the window wasn't minimized after the minimize command"

        # --- Exactly three confirmations, and the hand-over for every command that lands on the desktop ---
        assert [number for number, _, _ in session.confirmations] == [3, 6, 8], session.confirmations
        assert all(answer == "yes" for _, _, answer in session.confirmations), session.confirmations
        assert not any(TYPED_TEXT in asked for _, asked, _ in session.confirmations)  # never the text itself
        handed = [number for number, prompt in focus.calls if prompt != "noted"]
        assert handed == [1, 5, 6, 6, 7, 8, 8, 9, 10], focus.calls  # twice where a confirmation came between
        assert TYPED_TEXT not in "\n".join(session.written)
    finally:
        _test_user32().SetCursorPos(*original_pointer)  # always put the user's pointer back
        for field in (session_field, fixture_field):
            if field:
                _discard_test_text(field)  # test cleanup: its own Notepads then close without a save dialog
        for w in _new_notepads(notepads_before):
            ctypes.windll.user32.PostMessageW(ctypes.c_void_p(w.handle), WM_CLOSE, 0, 0)
            leftovers.append(w)
        _wait_for(lambda: not _new_notepads(notepads_before), CLEANUP_SECONDS)
        calc_left = _close_windows_opened_since("calculator", calculator, calculators_before,
                                                watch_seconds=WATCH_AFTER_DONE_SECONDS)
        explorer_left = _explorer_windows_for(folder.name, explorers_before)
        for w in explorer_left:
            ctypes.windll.user32.PostMessageW(ctypes.c_void_p(w.handle), WM_CLOSE, 0, 0)
        _wait_for(lambda: not _explorer_windows_for(folder.name, explorers_before), CLEANUP_SECONDS)
        try:
            shutil.rmtree(folder)
            folder_removed = True
        except OSError as exc:
            print(f"typed: couldn't delete the temporary folder yet: {exc!r}")
        print(f"typed: cleanup closed {len(leftovers)} Notepad(s), {len(calc_left)} Calculator window(s), "
              f"{len(explorer_left)} Explorer window(s)")
    assert _new_notepads(notepads_before) == [], "a Notepad the test started is still open"
    assert sorted(w.handle for w in leftovers) == sorted([fixture.handle, session_notepad.handle]), \
        "cleanup should have closed exactly the scroll fixture and the Notepad the assistant opened"
    assert calc_left == [], "the typed close command should have closed Calculator"
    assert len(explorer_left) == 1 and _explorer_windows_for(folder.name, explorers_before) == []
    assert folder_removed and not folder.exists()
    assert verifier_adapter.cursor_position() == original_pointer
    assert verifier_adapter.modifier_keys_down() == [], "a modifier key reads as still held down"
    assert verifier.clipboard_sequence() == clipboard_before, "the clipboard changed"
    assert matching_before <= verifier.snapshot_windows(expectation), "a Notepad that was already open was closed"
    assert calculators_before <= verifier.snapshot_windows(calculator), "a Calculator that was already open was closed"
    still_open = {w.handle for w in verifier_adapter.list_windows() if w.class_name == "CabinetWClass"}
    assert explorers_before <= still_open, "a File Explorer window that was already open was closed"
