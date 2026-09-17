"""
Executor decision-making - the ONLY path by which the assistant acts on the computer.

execute(action, confirm) always runs in this order:
  1. emergency-stop checkpoint
  2. validate the action - no side effects (an unknown action, a missing or unknown app, or an
     app whose result can't be verified fails cleanly without asking the user anything)
  3. Permission & Safety gate: app/safety authorize() - every action, no exceptions (CLAUDE.md rule 5)
  4. emergency-stop checkpoint again (a confirmation prompt can take a while)
  5. the real action, through app/executor/adapter.py
  6. the Verifier confirms the expected result actually happened - success is never assumed

A stop or a safety denial RAISES (EmergencyStopError / ActionDeniedError) so it can't be
silently ignored. An action that simply didn't work returns ActionResult(ok=False) with a clear
message, marked retryable when trying again could help.

execute_with_recovery() adds the Action -> Result -> Recovery loop (docs/build-plan Section 2):
a retryable failure is OFFERED for retry - never silently continued - and every retry runs the
whole pipeline again, safety gate and verification included.

Phase 1 is built one action at a time: open_app, close_app, click, type_text, shortcut, scroll, refresh,
then window_control.

close_app closes ONLY windows the assistant opened in this session. open_app remembers them in
memory, so nothing carries over a restart, and windows the user opened are never touched. Closing
is a polite request (like clicking the window's X), never ending a process, and outcomes stay
distinct (models.Outcome): a window already gone is ALREADY_CLOSED, one showing a dialog such as
"Save changes?" is NEEDS_USER, and one that stays open is STILL_OPEN. Neither of the last two is
ever retryable, so the recovery loop can't retry into an app that is waiting for the user.
Closing is always at least MEDIUM (a code constant). close_app and window_control close both resolve a window
group from this session's records, go through execute() (one confirmation), and then run the ONE close
mechanism, _close_session_group() - which also re-checks that the group belongs to this session.

click (by screen coordinates) is the Phase 1 last-resort fallback - no screen understanding yet:
  - validation: whole-number "x, y" that lies on an actual monitor (gaps between monitors don't
    count) and isn't a corner of the main screen (pyautogui's fail-safe corners)
  - safety: ALWAYS Medium risk, whatever the words say, so every click is confirmed. The prompt
    names the exact coordinates and the title of the window at that point.
  - right after confirmation: if a different window is now at the point, nothing is clicked
  - emergency stop is checked again immediately before the click; pyautogui's fail-safe (pointer
    in a main-screen corner) triggers the emergency stop with source "mouse-corner"
  - result: a click's effect can't be observed in Phase 1, so a sent click is Outcome.UNVERIFIED
    (ok=True, verified=False), never DONE. The only check afterwards is that the pointer really is
    at (x, y); if not, the click may have landed elsewhere and the result is FAILED
  - nothing about a click is ever retryable
The click itself is one instant input event: there is no "mid-click" to interrupt.

type_text types exact text into the ACTIVE window's focused field:
  - validation: not empty, at most executor.max_type_characters, no Tab or other control characters
    (line breaks are allowed), no broken Unicode, and there must be an active window
  - safety: always at least Medium risk; any line break makes it HIGH, because each one presses
    Enter, which can submit a form, send a message or run a command. The prompt names the character
    count, the window, the field and the number of Enter presses (and whether the text ends with
    one) - and none of the text itself
  - right after confirmation: if the active window or focused field changed, nothing is typed
  - one character at a time; before EVERY character the emergency stop and the active window/field
    are checked, and the pause between characters is interruptible
  - outcomes: DONE only when the field's text, read before and after, shows the exact text appearing
    one more time; UNVERIFIED when the field can't be read or the text isn't found; FAILED when
    nothing was typed; PARTIAL (progress=(sent, total)) when typing stopped part-way. An emergency
    stop after something was typed raises TypingInterruptedError carrying that result
  - nothing about typing is ever retryable: a retry could type the text twice
  - the text itself never appears in the confirmation prompt, logs, result messages, repr() or
    errors - only its length. It exists transiently in memory, only to validate it, type it and
    check it was typed.

shortcut presses one keyboard shortcut from the fixed allow-list in app/executor/shortcuts.py, whose
risk levels decide confirmation (LOW runs without asking; MEDIUM and HIGH always ask):
  - validation: a supported name; an active window where the shortcut acts on one; no modifier held
    down on the keyboard; something on the clipboard for Ctrl+V (Alt+F4 is refused: see close)
  - after confirmation: if the active window, its title or its field changed, nothing is pressed
  - the whole shortcut is ONE SendInput call (modifiers down, key down, key up, modifiers up), right
    after the last emergency-stop check - so a stop can't leave keys down
  - Windows accepted 0 events -> FAILED; some but not all -> every key involved is released at once
    and the result is UNVERIFIED; all -> the shortcut's own check. Afterwards the modifiers must read as
    released (released again if not; an honest note if still down)
  - DONE only on evidence (clipboard change counter, full selection in an edit field, a different
    active window, the desktop active); otherwise UNVERIFIED; Alt+Tab that visibly didn't switch is FAILED
  - never retryable; clipboard contents are never read; window titles and clipboard details appear only
    in the on-screen prompt, never in logs or results

scroll sends vertical mouse-wheel notches ("up N" / "down N") to the ACTIVE window:
  - validation: explicit direction word and 1..executor.max_scroll_notches notches; the window under the
    mouse pointer must BE the active window (so the wheel lands there whatever the "scroll inactive
    windows" setting is); no modifier held down. The pointer is never moved.
  - risk: LOW - no prompt - only where a standard scroll bar is positively identified (on the control
    under the pointer or a parent). MEDIUM over a standard control whose value the wheel changes
    (drop-down list, slider, spin box, date picker), found on the control or any of its parents
  - MEDIUM too where no standard scroll bar can be identified (browsers, WPF, Store apps, Electron):
    the prompt says so, and at most executor.max_unclassified_notches notches are sent - the cap is an
    extra bound, not a substitute for confirmation - and the result says so
  - one notch per SendInput call, with an interruptible pause; before EVERY notch the emergency stop,
    the active window, the control under the pointer and held modifiers are checked
  - DONE only when a standard scroll bar's position moved in the requested direction; otherwise
    UNVERIFIED (unreadable, already at the end, didn't move); FAILED if nothing scrolled; PARTIAL if it
    stopped part-way; an emergency stop part-way raises ActionInterruptedError. Never retryable.

refresh presses F5 in the ACTIVE window, but only after positively identifying the app by its executable
AND top-level window class: Chrome, Edge, Firefox or a File Explorer folder window. Everything else is
refused before the safety gate (Electron apps share Chrome's window class, and F5 debugs there).
  - risk: browsers MEDIUM (a reload can lose unsaved input); File Explorer LOW, or MEDIUM while a text
    box has focus (a rename or typed address)
  - no modifier may be held (Ctrl+F5 / Shift+F5 would hard-reload)
  - after confirmation and immediately before sending: the same window handle, executable, window class
    and focused control (not the title - browser titles change by themselves); then the emergency stop
  - F5 is one SendInput batch (the shortcut machinery): 0 accepted -> FAILED; part -> released,
    UNVERIFIED; all -> UNVERIFIED. A refresh is never DONE in Phase 1: nothing reliable proves it happened
  - never retryable; logs name the app, never the window title
F5, Ctrl+R, Ctrl+F5, Shift+F5 and Ctrl+Shift+R are refused as keyboard shortcuts, so this is the only way to
send a refresh key.

window_control minimizes, maximizes, restores or closes the ACTIVE window:
  - refused before the safety gate: no active window, the desktop or taskbar, a tool window, a window that
    isn't responding; minimize only if the window offers it, maximize only if it offers it
  - minimize / maximize / restore are LOW: a WM_SYSCOMMAND request (what the title-bar buttons send), with
    the window's identity (handle + executable + top-level class, not the title) re-checked and the
    emergency stop checked immediately before sending. Already in the requested state -> DONE, nothing
    sent; state read back as requested -> DONE; unreadable afterwards -> UNVERIFIED; readable but not changed
    within verifier.window_state_settle_seconds, or the window vanished -> FAILED. "restore" means a normal
    (neither minimized nor maximized) window
  - close delegates to close_app's mechanism: only a window group this session opened, MEDIUM, one
    confirmation, then _close_session_group (DONE / NEEDS_USER / STILL_OPEN / FAILED)
  - never retryable; logs never contain window titles (only the close prompt shows one, on screen)
Alt+F4 is refused as a keyboard shortcut, so closing has exactly one mechanism.

Measured real-desktop behavior and known open decisions: docs/step4 Section 4, implementation notes.
"""
import logging
import re
import threading
import unicodedata
from dataclasses import dataclass
from typing import Callable

from app.executor import adapter, emergency_stop, shortcuts
from app.executor.emergency_stop import ActionInterruptedError, EmergencyStopError, TypingInterruptedError
from app.executor.models import (CLICK, CLOSE_APP, OPEN_APP, REFRESH, SCROLL, SHORTCUT, TYPE_TEXT, WINDOW_CONTROL,
                                 ActionResult, ExecutorAction, Outcome)
from app.safety.logic import Confirm, authorize
from app.safety.models import Action, RiskLevel
from app.verifier import logic as verifier
from app.verifier.models import ActiveTarget, Screen, WindowExpectation, WindowInfo
from config.settings import SettingsError, get_setting

log = logging.getLogger(__name__)

OfferRetry = Callable[[ActionResult], bool]

# The outer window of a Windows Store app such as Calculator, which also has a same-titled content
# window. Asking the frame to close closes the app; the Verifier still waits for the content window.
_FRAME_WINDOW_CLASS = "ApplicationFrameWindow"

# Every coordinate click is at least this risky - a constant, so it can't be configured off. The words
# "click at (500, 300)" say nothing about what is there: it could be Delete, Send or Confirm.
_CLICK_RISK = RiskLevel.MEDIUM
_CLICK_RISK_REASON = "coordinate click - always needs confirmation (the target can't be checked in Phase 1)"
_POINT = re.compile(r"(-?[0-9]+)\s*,\s*(-?[0-9]+)")  # "x, y"; optionally wrapped in one pair of brackets

# Typing is at least Medium risk (the words can't tell what the active window will do with the text);
# a line break (Enter) makes it High. Constants, so they can't be configured off.
_TYPING_RISK = RiskLevel.MEDIUM
_TYPING_RISK_REASON = "typing text - always needs confirmation (the active window can't be checked in Phase 1)"
_ENTER_RISK = RiskLevel.HIGH
_ENTER_RISK_REASON = ("text contains line breaks - each presses Enter, which can submit a form, send a message "
                      "or run a command")

# Windows the assistant opened in this session: app name -> window groups, oldest first.
_session_windows: dict[str, list[frozenset[int]]] = {}
_session_lock = threading.Lock()


@dataclass(frozen=True)
class _Prepared:
    """A validated action, ready to run once the safety gate allows it."""
    run: Callable[[], ActionResult]
    safety_action: Action | None = None  # what the gate classifies and the user approves; default: the description


def execute(action: ExecutorAction, confirm: Confirm | None = None) -> ActionResult:
    """Carry out one action once, or explain why not. See the module docstring for the order."""
    emergency_stop.check()
    prepare = _PREPARERS.get(action.kind)
    if prepare is None:
        return _result(action, False, f"I don't know how to do '{action.kind}' yet.")
    prepared = prepare(action)  # validation only - nothing happens on the computer here
    if isinstance(prepared, ActionResult):
        return prepared
    if not isinstance(prepared, _Prepared):
        prepared = _Prepared(prepared)
    authorize(prepared.safety_action or Action(action.description), confirm)  # raises ActionDeniedError
    emergency_stop.check()
    return prepared.run()


def execute_with_recovery(action: ExecutorAction, confirm: Confirm | None = None,
                          offer_retry: OfferRetry | None = None) -> ActionResult:
    """Action -> Result -> Recovery. A retryable failure is offered via offer_retry(result), which
    must return exactly True to retry; without it, nothing is retried. Stops after
    executor.max_attempts attempts."""
    try:
        max_attempts = _max_attempts()
    except SettingsError as exc:
        return _result(action, False, str(exc))
    attempt = 1
    while True:
        result = execute(action, confirm)
        if result.ok or not result.retryable:
            return result
        if attempt >= max_attempts:
            return _result(action, False,
                           f"{result.message} Gave up after {attempt} attempt{'s' if attempt != 1 else ''}.")
        if not _retry_accepted(result, offer_retry):
            return _result(action, False, f"{result.message} Not retried.", retryable=True)
        attempt += 1
        log.info("Executor retrying %s '%s' (attempt %d of %d)",
                 action.kind, action.log_label, attempt, max_attempts)


# --- open_app ---------------------------------------------------------------------------

def _prepare_open_app(action: ExecutorAction):
    name = action.target.strip().lower()
    if not name:
        return _result(action, False, "Which app should I open?")
    try:
        apps = _configured_apps()
    except SettingsError as exc:
        return _result(action, False, str(exc))
    executable = apps.get(name)
    if executable is None:
        return _result(action, False, f"I don't know an app called '{action.target.strip()}'. "
                                      f"Apps I can open: {', '.join(sorted(apps))}.")
    try:
        expectation = verifier.expect_window(name)
    except SettingsError as exc:
        return _result(action, False, str(exc))

    def run() -> ActionResult:
        try:
            before = verifier.snapshot_windows(expectation)
        except verifier.VerifierUnavailableError as exc:
            return _result(action, False, f"Didn't open {name}: I can't check whether its window appears ({exc}).")
        try:
            adapter.launch_app(executable)
        except adapter.ExecutorAdapterError as exc:
            return _result(action, False, str(exc))
        check = verifier.wait_for_new_window(expectation, before)
        if not check.ok:
            return _result(action, False, check.message, retryable=check.retryable)
        _remember_opened(name, check.window_handles)
        return _result(action, True, f"Opened {name}; its window appeared after {check.elapsed_seconds:.1f}s.")

    return run


def _configured_apps() -> dict[str, str]:
    apps = get_setting("executor.apps")
    valid = isinstance(apps, dict) and apps and all(
        isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip() for k, v in apps.items())
    if not valid:
        raise SettingsError(f"Setting 'executor.apps' must map app names to executables, got {apps!r}.")
    return {k.strip().lower(): v.strip() for k, v in apps.items()}


# --- close_app --------------------------------------------------------------------------

# Closing can lose unsaved work: always at least MEDIUM - a code constant, so configuration can't lower it. Other
# safety rules may still raise it (the gate takes the higher of the two).
_CLOSE_RISK = RiskLevel.MEDIUM
_CLOSE_RISK_REASON = "closing a window can lose unsaved work"


@dataclass(frozen=True)
class _SessionGroup:
    """A window group the assistant opened in this session. Created only by _open_session_group and
    _session_group_containing, which resolve it from the session's own records - never from a raw handle."""
    app: str
    group: frozenset[int]
    expectation: WindowExpectation


def _prepare_close_app(action: ExecutorAction):
    name = action.target.strip().lower()
    if not name:
        return _result(action, False, "Which app should I close?")
    try:
        apps = _configured_apps()
    except SettingsError as exc:
        return _result(action, False, str(exc))
    if name not in apps:
        return _result(action, False, f"I don't know an app called '{action.target.strip()}'. "
                                      f"Apps I can close: {', '.join(sorted(apps))}.")
    try:
        expectation = verifier.expect_window(name)
    except SettingsError as exc:
        return _result(action, False, str(exc))
    try:
        group = _open_session_group(name, expectation)
        if group is None:
            return _nothing_of_mine_to_close(action, name, expectation)
    except verifier.VerifierUnavailableError as exc:
        return _cant_check(action, name, exc)
    session = _SessionGroup(name, group, expectation)
    safety_action = Action(action.description, minimum_level=_CLOSE_RISK, minimum_reason=_CLOSE_RISK_REASON)
    return _Prepared(lambda: _close_session_group(action, session), safety_action)


def _close_session_group(action: ExecutorAction, session: _SessionGroup) -> ActionResult:
    """THE close mechanism - the only code that sends a close request. Call it only from the run() of a close
    action that went through execute() (so it was validated and confirmed there, exactly once), with a group
    resolved from this session's records. It asks nothing itself. As a defensive check it refuses any group
    that isn't (still) one this session opened, so it can never close an unrelated window."""
    name, group, expectation = session.app, session.group, session.expectation
    if not _owned_by_session(session):
        log.warning("Close refused: the window group isn't one this session opened")
        return _result(action, False, "I only close windows I opened in this session, so I left it alone.")
    try:
        still_open = verifier.find_open(expectation, group)  # it may have closed during confirmation
    except verifier.VerifierUnavailableError as exc:
        return _cant_check(action, name, exc)
    if not still_open:
        _forget(name, group)
        return _result(action, True, f"{name} is already closed.", outcome=Outcome.ALREADY_CLOSED)
    try:
        # Titled windows inside the group (a Store app's content window) must be gone too: they move
        # out of the frame as a separate window while the app closes.
        relevant = group | verifier.hosted_windows(expectation, frozenset(w.handle for w in still_open))
    except verifier.VerifierUnavailableError as exc:
        return _cant_check(action, name, exc)
    emergency_stop.check()  # last checkpoint before the close request is sent
    failure = _request_close(name, still_open)
    if failure:
        return _result(action, False, failure)
    try:
        check = verifier.wait_for_windows_to_close(expectation, relevant)
    except EmergencyStopError:
        log.warning("Emergency stop while verifying a close of '%s': the close request was already sent "
                    "and can't be taken back; stopped waiting to verify it", name)
        raise
    if check.ok:
        _forget(name, group)
        return _result(action, True, f"Closed {name} after {check.elapsed_seconds:.1f}s.")
    if check.needs_user:
        return _result(action, False, check.message, outcome=Outcome.NEEDS_USER)
    if check.elapsed_seconds is None:  # the desktop couldn't be observed, so nothing is known
        return _result(action, False, check.message)
    return _result(action, False, check.message, outcome=Outcome.STILL_OPEN)


def _owned_by_session(session: _SessionGroup) -> bool:
    with _session_lock:
        return session.group in _session_windows.get(session.app, [])


def _request_close(name: str, windows: list[WindowInfo]) -> str | None:
    """Ask the app's window group to close: the frame window if there is one, otherwise every window.
    Returns a failure message, or None once requested."""
    frames = [w for w in windows if w.class_name == _FRAME_WINDOW_CLASS]
    targets = frames or windows
    for window in targets:
        try:
            adapter.request_close(window.handle)
        except adapter.WindowGoneError:
            continue  # already closing - the Verifier decides the outcome
        except adapter.WindowCloseError as exc:
            return f"I couldn't ask {name} to close: {exc}."
    log.info("Executor: close requested for %d %s window(s)", len(targets), name)
    return None


def _nothing_of_mine_to_close(action: ExecutorAction, name: str, expectation: WindowExpectation) -> ActionResult:
    """No window opened in this session is still open. Close nothing; say whether other windows are."""
    others = len(verifier.snapshot_windows(expectation))
    if not others:
        return _result(action, True, f"{name} is already closed.", outcome=Outcome.ALREADY_CLOSED)
    them = "them" if others != 1 else "it"
    return _result(action, False,
                   f"I only close windows I opened in this session. {others} {name} "
                   f"{'windows are' if others != 1 else 'window is'} open, but I didn't open {them}, "
                   f"so I left {them} alone.")


def _cant_check(action: ExecutorAction, name: str, exc: Exception) -> ActionResult:
    return _result(action, False, f"Didn't close {name}: I can't check its windows ({exc}).")


def _remember_opened(name: str, handles: frozenset[int]) -> None:
    if handles:
        with _session_lock:
            _session_windows.setdefault(name, []).append(frozenset(handles))


def _forget(name: str, group: frozenset[int]) -> None:
    with _session_lock:
        groups = _session_windows.get(name, [])
        if group in groups:
            groups.remove(group)


def _open_session_group(name: str, expectation: WindowExpectation) -> frozenset[int] | None:
    """The most recently opened window group of `name` from this session that is still open, or None.
    Groups whose windows are all gone are forgotten. Raises VerifierUnavailableError."""
    with _session_lock:
        groups = list(_session_windows.get(name, []))
    for group in reversed(groups):
        if verifier.find_open(expectation, group):
            return group
        _forget(name, group)
    return None


def forget_session_windows() -> None:
    """Forget every window opened in this session, so close_app will close none of them."""
    with _session_lock:
        _session_windows.clear()


# --- click ------------------------------------------------------------------------------

def _prepare_click(action: ExecutorAction):
    target = action.target.strip()
    if not target:
        return _result(action, False, "Where should I click? Give screen coordinates as x, y (e.g. 500, 300).")
    inner = target[1:-1].strip() if target.startswith("(") and target.endswith(")") else target
    match = _POINT.fullmatch(inner)
    if not match:
        return _result(action, False, f"I can't click at '{target}': give whole-number screen coordinates "
                                      f"as x, y (e.g. 500, 300).")
    x, y = int(match.group(1)), int(match.group(2))
    try:
        all_screens = verifier.screens()
        if not verifier.on_screen(all_screens, x, y):
            return _result(action, False, f"({x}, {y}) isn't on any screen, so I didn't click. "
                                          f"Your screens: {_describe_screens(all_screens)}.")
        if (x, y) in _fail_safe_corners(all_screens):
            return _result(action, False, f"({x}, {y}) is a corner of the main screen. Those corners are the "
                                          f"manual emergency stop (moving the mouse there halts actions), so I "
                                          f"won't click there.")
        approved = verifier.window_at(x, y)
    except verifier.VerifierUnavailableError as exc:
        return _result(action, False, f"Didn't click at ({x}, {y}): I can't check the screen ({exc}).")
    where = f'window "{approved.title}"' if approved and approved.title else \
        "a window with no readable title" if approved else "a spot where no window could be identified"
    safety_action = Action(f"click at ({x}, {y}) on {where}",
                           minimum_level=_CLICK_RISK, minimum_reason=_CLICK_RISK_REASON)

    def run() -> ActionResult:
        try:
            now = verifier.window_at(x, y)  # the user may have switched windows while approving
        except verifier.VerifierUnavailableError as exc:
            return _result(action, False, f"Didn't click at ({x}, {y}): I can't check the window there ({exc}).")
        if _window_identity(now) != _window_identity(approved):
            return _result(action, False, f"The window at ({x}, {y}) changed after you approved the click, "
                                          f"so I didn't click.")
        emergency_stop.check()  # last checkpoint before the input is sent
        try:
            adapter.click(x, y)
        except adapter.MouseFailSafeError:
            emergency_stop.trigger("mouse-corner")
            emergency_stop.check()  # raises EmergencyStopError
        except adapter.ExecutorAdapterError as exc:
            return _result(action, False, f"I couldn't click at ({x}, {y}): {exc}.")
        try:
            pointer = verifier.cursor_position()
        except verifier.VerifierUnavailableError:
            return _result(action, True, f"Clicked at ({x}, {y}). I couldn't read where the mouse pointer ended "
                                         f"up, and I can't check what the click did.", outcome=Outcome.UNVERIFIED)
        if pointer != (x, y):
            return _result(action, False, f"I sent the click, but the mouse pointer is at {pointer} instead of "
                                          f"({x}, {y}), so the click may have landed somewhere else.")
        return _result(action, True, f"Clicked at ({x}, {y}). I can't check what the click did.",
                       outcome=Outcome.UNVERIFIED)

    return _Prepared(run, safety_action)


def _window_identity(window: WindowInfo | None) -> tuple | None:
    return None if window is None else (window.handle, window.title)


def _fail_safe_corners(all_screens: list[Screen]) -> set[tuple[int, int]]:
    """The four corner pixels of the main screen, where pyautogui's fail-safe stops every action."""
    corners = set()
    for s in all_screens:
        if s.primary:
            corners |= {(s.left, s.top), (s.right - 1, s.top), (s.left, s.bottom - 1), (s.right - 1, s.bottom - 1)}
    return corners


def _describe_screens(all_screens: list[Screen]) -> str:
    return "; ".join(f"x {s.left} to {s.right - 1}, y {s.top} to {s.bottom - 1}{' (main)' if s.primary else ''}"
                     for s in all_screens)


# --- type_text --------------------------------------------------------------------------

def _prepare_type_text(action: ExecutorAction):
    raw = action.target if isinstance(action.target, str) else ""
    if not raw:
        return _result(action, False, "What should I type?")
    text = raw.replace("\r\n", "\n")
    try:
        limit, interval = _max_type_characters(), _typing_interval()
    except SettingsError as exc:
        return _result(action, False, str(exc))
    if len(text) > limit:
        return _result(action, False, f"That's {len(text)} characters; I can type at most {limit} at once.")
    categories = {unicodedata.category(c) for c in text if c != "\n"}
    if "Cs" in categories:
        return _result(action, False, "I can't type that: it contains an invalid character.")
    if "Cc" in categories:
        return _result(action, False, "I can't type that: it contains Tab or another control key, which isn't "
                                      "text (keyboard shortcuts come later).")
    try:
        approved = verifier.active_target()
    except verifier.VerifierUnavailableError as exc:
        return _result(action, False, f"Didn't type: I can't check the active window ({exc}).")
    if approved.window is None:
        return _result(action, False, "Didn't type: there's no active window to type into.")
    total, enters = len(text), text.count("\n")
    safety_action = Action(_typing_prompt(total, enters, text.endswith("\n"), approved),
                           minimum_level=_ENTER_RISK if enters else _TYPING_RISK,
                           minimum_reason=_ENTER_RISK_REASON if enters else _TYPING_RISK_REASON)

    def run() -> ActionResult:
        try:
            now = verifier.active_target()
        except verifier.VerifierUnavailableError as exc:
            return _result(action, False, f"Didn't type: I can't check the active window ({exc}).")
        if _focus_identity(now, with_title=True) != _focus_identity(approved, with_title=True):
            return _result(action, False, "The active window changed after you approved, so I typed nothing.")
        count_before = verifier.count_text(approved.control_handle, text)
        sent = 0
        for character in text:
            if emergency_stop.is_stopped():
                _stop_typing(action, sent, total)
            try:
                now = verifier.active_target()
            except verifier.VerifierUnavailableError:
                now = None
            # the title may change while typing (e.g. Notepad adds "*"), so only the handles must match
            if now is None or _focus_identity(now) != _focus_identity(approved):
                return _typing_stopped(action, sent, total, "the active window changed")
            try:
                adapter.send_character(character)
            except adapter.TypingError as exc:
                return _typing_stopped(action, sent + (1 if exc.partly_sent else 0), total,
                                       "Windows stopped accepting keyboard input")
            sent += 1
            if sent < total and emergency_stop.wait(interval):
                _stop_typing(action, sent, total)
        try:
            check = verifier.wait_for_typed_text(approved.control_handle, text, count_before)
        except EmergencyStopError:
            _stop_typing(action, sent, total)
        typed = _typed_summary(total, enters)
        if check == verifier.TEXT_CONFIRMED:
            return _result(action, True, f"Typed {typed} and confirmed they appeared in the field.",
                           progress=(sent, total))
        if check == verifier.TEXT_UNREADABLE:
            message = f"Typed {typed}. I can't read that field, so I can't confirm they arrived."
        else:
            message = (f"Typed {typed}, but I couldn't find them in the field afterwards (the app may have "
                       f"changed them). I won't retype anything.")
        return _result(action, True, message, outcome=Outcome.UNVERIFIED, progress=(sent, total))

    return _Prepared(run, safety_action)


def _typing_prompt(total: int, enters: int, ends_with_enter: bool, target: ActiveTarget) -> str:
    """What the user approves: character count, window, field and Enter presses. It is deliberately
    built without the text itself, so none of the text can ever appear in the prompt."""
    window = f'window "{target.window.title}"' if target.window.title else "a window with no readable title"
    field = target.control_class or "unknown"
    prompt = f"type {total} character{'s' if total != 1 else ''} into {window} (field: {field})"
    if enters:
        prompt += (f" AND PRESS ENTER {enters} TIME{'S' if enters != 1 else ''} - Enter can submit a form, "
                   f"send a message or run a command")
        if ends_with_enter:
            prompt += " (ends with Enter: it will submit as soon as typing finishes)"
    return prompt


def _typed_summary(total: int, enters: int) -> str:
    summary = f"{total} character{'s' if total != 1 else ''}"
    return summary + (f" (including {enters} Enter press{'es' if enters != 1 else ''})" if enters else "")


def _focus_identity(target: ActiveTarget, with_title: bool = False) -> tuple | None:
    if target.window is None:
        return None
    identity = (target.window.handle, target.control_handle)
    return identity + (target.window.title,) if with_title else identity


def _typing_stopped(action: ExecutorAction, sent: int, total: int, reason: str) -> ActionResult:
    """Typing ended early, not by the emergency stop. Never retryable."""
    if sent == 0:
        return _result(action, False, f"I couldn't type: {reason} before the first character, so I typed nothing.")
    return _result(action, False, f"Typed {sent} of {total} characters, then {reason}, so I stopped. Those {sent} "
                                  f"characters may already be in the window. I won't retype anything.",
                   outcome=Outcome.PARTIAL, progress=(sent, total))


def _stop_typing(action: ExecutorAction, sent: int, total: int):
    """The emergency stop fired while typing. Nothing sent yet: the usual EmergencyStopError. Otherwise
    TypingInterruptedError carrying how much was typed."""
    if sent == 0:
        emergency_stop.check()
    status = emergency_stop.status()
    if sent < total:
        result = _result(action, False, f"Emergency stop: typed {sent} of {total} characters before stopping. "
                                        f"Those {sent} characters may already be in the window. I won't retype "
                                        f"anything.", outcome=Outcome.PARTIAL, progress=(sent, total))
    else:
        result = _result(action, True, f"Emergency stop: typed all {total} characters, then stopped before "
                                       f"checking them.", outcome=Outcome.UNVERIFIED, progress=(sent, total))
    raise TypingInterruptedError(f"Emergency stop is active (triggered by {status.source}); typing stopped after "
                                 f"{sent} of {total} characters.", result)


def _max_type_characters() -> int:
    value = get_setting("executor.max_type_characters")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SettingsError(f"Setting 'executor.max_type_characters' must be a whole number of at least 1, got {value!r}.")
    return value


def _typing_interval() -> float:
    value = get_setting("executor.typing_interval_seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise SettingsError(f"Setting 'executor.typing_interval_seconds' must be a number of seconds (0 or more), "
                            f"got {value!r}.")
    return float(value)


# --- shortcut ---------------------------------------------------------------------------

_SHELL_CLASSES = ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd")  # desktop and taskbar
_DESKTOP_CLASSES = ("Progman", "WorkerW")


def _prepare_shortcut(action: ExecutorAction):
    parsed = shortcuts.parse(action.target)
    if isinstance(parsed, shortcuts.ShortcutRefusal):
        return _result(action, False, parsed.message)
    shortcut = parsed
    name = shortcut.name
    try:
        approved = verifier.active_target()
        held = verifier.modifiers_held()
        kinds = verifier.clipboard_kinds() if name == "Ctrl+V" else []
    except verifier.VerifierUnavailableError as exc:
        return _result(action, False, f"Didn't press {name}: I can't check the keyboard or the active window ({exc}).")
    if shortcut.needs_active_window and approved.window is None:
        return _result(action, False, f"Didn't press {name}: there's no active window.")
    if held:
        return _result(action, False, _held_message(name, held))
    if name == "Ctrl+V" and not kinds:
        return _result(action, False, "The clipboard is empty, so there's nothing to paste.")
    if shortcut.risk > RiskLevel.LOW:
        description = _shortcut_prompt(shortcut, approved, kinds)
    else:  # LOW runs without a prompt, so the gate sees no window title at all
        description = f"press {name}"
    safety_action = Action(description, minimum_level=shortcut.risk, minimum_reason=shortcut.reason)

    def run() -> ActionResult:
        try:
            now = verifier.active_target()
            held_now = verifier.modifiers_held()
        except verifier.VerifierUnavailableError as exc:
            return _result(action, False, f"Didn't press {name}: I can't check the keyboard or the active window ({exc}).")
        if shortcut.needs_active_window and _focus_identity(now, with_title=True) != _focus_identity(approved, with_title=True):
            return _result(action, False, f"The active window changed after you approved, so I didn't press {name}.")
        if held_now:
            return _result(action, False, _held_message(name, held_now))
        baseline = _shortcut_baseline(shortcut, now)
        emergency_stop.check()  # last checkpoint before the keys are sent
        try:
            accepted, expected = adapter.send_shortcut(shortcut.modifiers, shortcut.key)
        except adapter.ExecutorAdapterError as exc:
            return _result(action, False, f"I couldn't press {name}: {exc}.")
        if accepted == 0:
            return _result(action, False, f"Windows didn't accept the keyboard input for {name}, so nothing was pressed.")
        if accepted < expected:
            adapter.release_keys(shortcut.modifiers, shortcut.key)  # defensive: every key involved, at once
            return _result(action, True, f"Windows accepted only part of the keyboard input for {name}, so I released "
                                         f"every key involved. The shortcut may or may not have taken effect."
                                         f"{_release_note(shortcut)}", outcome=Outcome.UNVERIFIED)
        try:
            note = _release_note(shortcut)
            ok, outcome, message = _verify_shortcut(shortcut, now, baseline)
        except EmergencyStopError:
            log.warning("Emergency stop while checking shortcut %s: the keys were already sent and released", name)
            raise
        return _result(action, ok, message + note, outcome=outcome)

    return _Prepared(run, safety_action)


def _shortcut_prompt(shortcut, target: ActiveTarget, kinds: list[str]) -> str:
    """What the user approves. Shown on screen only - never logged."""
    effect = shortcut.effect.format(clipboard=f"it holds: {', '.join(kinds)}")
    if not shortcut.needs_active_window:
        return f"press {shortcut.name} - {effect}"
    window = f'window "{target.window.title}"' if target.window.title else "a window with no readable title"
    return f"press {shortcut.name} in {window} (field: {target.control_class or 'unknown'}) - {effect}"


def _held_message(name: str, held: list[str]) -> str:
    keys = " and ".join(held)
    return (f"Didn't press {name}: {keys} {'is' if len(held) == 1 else 'are'} held down on the keyboard, which "
            f"would change the shortcut. Let go and try again.")


def _shortcut_baseline(shortcut, target: ActiveTarget) -> dict:
    """What the check afterwards compares against, read just before the keys are sent."""
    if shortcut.check in (shortcuts.CHECK_CLIPBOARD, shortcuts.CHECK_CUT):
        return {"clipboard": verifier.clipboard_sequence(), "length": verifier.field_text_length(target.control_handle)}
    if shortcut.check == shortcuts.CHECK_ACTIVE_CHANGED:
        return {"active": target.window.handle if target.window else None}
    return {}


def _verify_shortcut(shortcut, target: ActiveTarget, baseline: dict):
    """(ok, outcome, message) - done only on evidence. Raises EmergencyStopError if stopped while waiting."""
    name, check = shortcut.name, shortcut.check
    if check == shortcuts.CHECK_CLIPBOARD:
        if baseline["clipboard"] is not None and verifier.wait_until(
                lambda: _changed(verifier.clipboard_sequence(), baseline["clipboard"])):
            return True, Outcome.DONE, f"Pressed {name}; the clipboard was updated."
        return True, Outcome.UNVERIFIED, f"Pressed {name}, but I couldn't confirm the clipboard changed (maybe nothing was selected)."
    if check == shortcuts.CHECK_CUT:
        def cut_happened():
            length = verifier.field_text_length(target.control_handle)
            changed = _changed(verifier.clipboard_sequence(), baseline["clipboard"])
            if changed is None or length is None or baseline["length"] is None:
                return None
            return changed and length < baseline["length"]
        if verifier.wait_until(cut_happened):
            return True, Outcome.DONE, f"Pressed {name}; the clipboard was updated and the field's text got shorter."
        return True, Outcome.UNVERIFIED, f"Pressed {name}, but I couldn't confirm it cut anything."
    if check == shortcuts.CHECK_SELECT_ALL:
        selected = verifier.wait_until(lambda: verifier.everything_selected(target))
        if selected:
            return True, Outcome.DONE, f"Pressed {name}; everything in the field is selected."
        if selected is None:
            return True, Outcome.UNVERIFIED, f"Pressed {name}. I can't check the selection in this field."
        return True, Outcome.UNVERIFIED, f"Pressed {name}, but I couldn't confirm everything is selected."
    if check == shortcuts.CHECK_ACTIVE_CHANGED:
        switched = verifier.wait_until(lambda: _active_changed(baseline["active"]))
        if switched:
            return True, Outcome.DONE, f"Pressed {name}; a different window is now active."
        if switched is None:
            return True, Outcome.UNVERIFIED, f"Pressed {name}, but I couldn't check which window is active."
        return False, Outcome.FAILED, f"Pressed {name}, but the active window didn't change."
    if check == shortcuts.CHECK_DESKTOP:
        if verifier.wait_until(_desktop_active):
            return True, Outcome.DONE, f"Pressed {name}; the desktop is showing."
        return True, Outcome.UNVERIFIED, f"Pressed {name}, but I couldn't confirm the desktop is showing."
    return True, Outcome.UNVERIFIED, f"Pressed {name}. I can't check what it did."


def _changed(now: int | None, before: int | None) -> bool | None:
    return None if now is None or before is None else now != before


def _active_changed(before: int | None) -> bool | None:
    try:
        now = verifier.active_target()
    except verifier.VerifierUnavailableError:
        return None
    return (now.window.handle if now.window else None) != before


def _desktop_active() -> bool | None:
    try:
        now = verifier.active_target()
    except verifier.VerifierUnavailableError:
        return None
    return bool(now.window and now.window.class_name in _DESKTOP_CLASSES)


def _release_note(shortcut) -> str:
    """Make sure the shortcut's modifiers are released: check, release again if needed, check again.
    Returns "" when released, otherwise an honest note for the user."""
    if not shortcut.modifiers:
        return ""

    def released():
        try:
            return not any(m in shortcut.modifiers for m in verifier.modifiers_held())
        except verifier.VerifierUnavailableError:
            return None
    if verifier.wait_until(released):
        return ""
    adapter.release_keys(shortcut.modifiers, shortcut.key)
    if verifier.wait_until(released):
        return ""
    keys = " and ".join(m for m in shortcut.modifiers)
    return f" {keys} may still be held down; press and release {'it' if len(shortcut.modifiers) == 1 else 'them'} once."


# --- scroll -----------------------------------------------------------------------------

_SCROLL = re.compile(r"(up|down)\s+([0-9]+)", re.IGNORECASE)
_SCROLL_RISK_REASON = "scrolling over a control whose value the wheel changes"
_UNCLASSIFIED_RISK_REASON = "scrolling a surface whose scroll area can't be identified"


def _prepare_scroll(action: ExecutorAction):
    target = " ".join(action.target.split()) if isinstance(action.target, str) else ""
    parsed = _parse_scroll(target)
    if isinstance(parsed, str):
        return _result(action, False, parsed)
    direction, requested = parsed
    try:
        limit, unclassified_limit, interval = _scroll_settings()
    except SettingsError as exc:
        return _result(action, False, str(exc))
    if requested > limit:
        return _result(action, False, f"That's {requested} notches; I scroll at most {limit} at once.")
    try:
        approved = verifier.active_target()
        _, chain = verifier.control_chain_at_pointer()
        held = verifier.modifiers_held()
    except verifier.VerifierUnavailableError as exc:
        return _result(action, False, f"Didn't scroll: I can't check the pointer or the active window ({exc}).")
    if approved.window is None:
        return _result(action, False, "Didn't scroll: there's no active window.")
    if not chain or chain[-1].handle != approved.window.handle:
        return _result(action, False, "The mouse pointer isn't over the active window, so I can't be sure which "
                                      "window would scroll. Move the pointer over the window you want to scroll.")
    if held:
        return _result(action, False, _scroll_held_message(held))
    region = verifier.scroll_region(chain)
    planned = requested if region else min(requested, unclassified_limit)
    value_control = verifier.value_changing_control(chain)
    notches = _notches(planned)
    title = f'window "{approved.window.title}"' if approved.window.title else "a window with no readable title"
    if value_control:
        description = (f"scroll {direction} {notches} over {value_control} in {title} - scrolling over it changes "
                       f"its value")
        safety_action = Action(description, minimum_level=RiskLevel.MEDIUM, minimum_reason=_SCROLL_RISK_REASON)
    elif region is None:
        asked = f" (you asked for {requested})" if planned < requested else ""
        description = (f"scroll {direction} {notches} in {title} - I couldn't confidently identify this window's "
                       f"scroll area, so for safety I'll send at most {unclassified_limit} notches{asked}")
        safety_action = Action(description, minimum_level=RiskLevel.MEDIUM, minimum_reason=_UNCLASSIFIED_RISK_REASON)
    else:  # a standard scroll bar positively identified: LOW, no prompt
        safety_action = Action(f"scroll {direction} {notches}")
    pane = chain[0].handle

    def run() -> ActionResult:
        before = verifier.scroll_state(region[0]) if region else None
        sent = 0
        for _ in range(planned):
            reason = _scroll_target_changed(approved, pane)
            if reason:
                return _scrolling_stopped(action, sent, planned, reason)
            if emergency_stop.is_stopped():  # checked last, immediately before the notch is sent
                _stop_scrolling(action, sent, planned)
            try:
                accepted = adapter.send_wheel_notch(direction == "up")
            except adapter.ExecutorAdapterError as exc:
                return _scrolling_stopped(action, sent, planned, str(exc))
            if not accepted:
                return _scrolling_stopped(action, sent, planned, "Windows stopped accepting mouse input")
            sent += 1
            if sent < planned and emergency_stop.wait(interval):
                _stop_scrolling(action, sent, planned)
        cap_note = (f" I couldn't identify this window's scroll area, so I scrolled at most {unclassified_limit} "
                    f"notches (you asked for {requested})." if planned < requested else "")
        scrolled = f"Scrolled {direction} {_notches(sent)}"
        if region is None or before is None:
            return _result(action, True, f"{scrolled}. I can't read this window's scroll position, so I can't confirm "
                                         f"it moved.{cap_note}", outcome=Outcome.UNVERIFIED, progress=(sent, requested))
        try:
            moved = verifier.wait_until(lambda: _scrolled_toward(region[0], before, direction),
                                        "verifier.scroll_settle_seconds")
        except EmergencyStopError:
            _stop_scrolling(action, sent, planned)
        if moved:
            return _result(action, True, f"{scrolled}; the scroll position moved {direction}.{cap_note}",
                           progress=(sent, requested))
        at_end = before.at_top if direction == "up" else before.at_bottom
        if at_end:
            message = f"{scrolled}, but it was already at the {'top' if direction == 'up' else 'bottom'}, so nothing moved."
        elif moved is None:
            message = f"{scrolled}, but I couldn't read the scroll position afterwards."
        else:
            message = f"{scrolled}, but the scroll position didn't move {direction}."
        return _result(action, True, message + cap_note, outcome=Outcome.UNVERIFIED, progress=(sent, requested))

    return _Prepared(run, safety_action)


def _parse_scroll(target: str) -> tuple[str, int] | str:
    """("up"|"down", notches) or a message saying what's wrong."""
    example = "For example: down 3."
    if not target:
        return f"Which way and how far should I scroll? {example}"
    if target[0] in "+-" or target.lstrip("+-").isdigit():
        return f"Say up or down instead of + or -. {example}"
    words = target.lower().split()
    if words[0] in ("left", "right"):
        return "Horizontal scrolling isn't supported yet."
    if words in (["up"], ["down"]):
        return f"How many notches? {example}"
    match = _SCROLL.fullmatch(target)
    if not match:
        return f"I can't read '{target}': say up or down and a number of notches. {example}"
    count = int(match.group(2))
    if count < 1:
        return "Scroll at least 1 notch."
    return match.group(1).lower(), count


def _notches(count: int) -> str:
    return f"{count} notch{'es' if count != 1 else ''}"


def _scroll_settings() -> tuple[int, int, float]:
    values = []
    for name in ("executor.max_scroll_notches", "executor.max_unclassified_notches"):
        value = get_setting(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise SettingsError(f"Setting '{name}' must be a whole number of at least 1, got {value!r}.")
        values.append(value)
    interval = get_setting("executor.scroll_interval_seconds")
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval < 0:
        raise SettingsError(f"Setting 'executor.scroll_interval_seconds' must be a number of seconds (0 or more), "
                            f"got {interval!r}.")
    return values[0], values[1], float(interval)


def _scroll_held_message(held: list[str]) -> str:
    keys = " and ".join(held)
    return (f"Didn't scroll: {keys} {'is' if len(held) == 1 else 'are'} held down on the keyboard, which would "
            f"change what the wheel does (e.g. zoom). Let go and try again.")


def _scroll_target_changed(approved: ActiveTarget, pane: int) -> str | None:
    """Why scrolling must stop now, or None: the active window, the control under the pointer, or a held
    modifier changed since validation."""
    try:
        active = verifier.active_target()
        _, chain = verifier.control_chain_at_pointer()
        held = verifier.modifiers_held()
    except verifier.VerifierUnavailableError:
        return "I couldn't check the pointer or the active window"
    if active.window is None or active.window.handle != approved.window.handle:
        return "the active window changed"
    if not chain or chain[0].handle != pane:
        return "the mouse pointer moved off what it was over"
    if held:
        return f"{' and '.join(held)} {'was' if len(held) == 1 else 'were'} pressed"
    return None


def _scrolled_toward(handle: int, before, direction: str) -> bool | None:
    now = verifier.scroll_state(handle)
    if now is None:
        return None
    return now.position < before.position if direction == "up" else now.position > before.position


def _scrolling_stopped(action: ExecutorAction, sent: int, total: int, reason: str) -> ActionResult:
    """Scrolling ended early, not by the emergency stop. Never retryable."""
    if sent == 0:
        return _result(action, False, f"Didn't scroll: {reason} before the first notch, so nothing scrolled.")
    return _result(action, False, f"Scrolled {sent} of {_notches(total)}, then {reason}, so I stopped.",
                   outcome=Outcome.PARTIAL, progress=(sent, total))


def _stop_scrolling(action: ExecutorAction, sent: int, total: int):
    """The emergency stop fired while scrolling: the plain EmergencyStopError before the first notch,
    otherwise ActionInterruptedError carrying how far it scrolled."""
    if sent == 0:
        emergency_stop.check()
    status = emergency_stop.status()
    if sent < total:
        result = _result(action, False, f"Emergency stop: scrolled {sent} of {_notches(total)} before stopping.",
                         outcome=Outcome.PARTIAL, progress=(sent, total))
    else:
        result = _result(action, True, f"Emergency stop: scrolled all {_notches(total)}, then stopped before "
                                       f"checking the scroll position.", outcome=Outcome.UNVERIFIED,
                         progress=(sent, total))
    raise ActionInterruptedError(f"Emergency stop is active (triggered by {status.source}); scrolling stopped after "
                                 f"{_notches(sent)} of {total}.", result)


# --- refresh ----------------------------------------------------------------------------

# The only windows Refresh supports in Phase 1: (executable, top-level window class) -> (app name, kind).
# Code, not configuration. Everything else - including Electron apps that share Chrome's window class,
# where F5 means "start debugging" - is refused before the safety gate.
_REFRESH_TARGETS = {
    ("chrome.exe", "Chrome_WidgetWin_1"): ("Chrome", "browser"),
    ("msedge.exe", "Chrome_WidgetWin_1"): ("Edge", "browser"),
    ("firefox.exe", "MozillaWindowClass"): ("Firefox", "browser"),
    ("explorer.exe", "CabinetWClass"): ("File Explorer", "explorer"),
}
_BROWSER_REFRESH_REASON = "refreshing a browser page can lose unsaved input or page state"
_EDITING_REFRESH_REASON = "refreshing File Explorer while a text box is being edited"


def _prepare_refresh(action: ExecutorAction):
    if isinstance(action.target, str) and action.target.strip():
        return _result(action, False, "Refresh doesn't take a target; it refreshes the active window.")
    try:
        approved = verifier.active_target()
        held = verifier.modifiers_held()
    except verifier.VerifierUnavailableError as exc:
        return _result(action, False, f"Didn't refresh: I can't check the keyboard or the active window ({exc}).")
    if approved.window is None:
        return _result(action, False, "Didn't refresh: there's no active window.")
    executable = verifier.process_name(approved.window.handle)
    app = _REFRESH_TARGETS.get((executable, approved.window.class_name))
    if app is None:
        return _result(action, False, "I can refresh only Chrome, Edge, Firefox and File Explorer windows in Phase 1, "
                                      "so I didn't press anything.")
    if held:
        return _result(action, False, _refresh_held_message(held))
    app_name, kind = app
    title = f'window "{approved.window.title}"' if approved.window.title else "a window with no readable title"
    if kind == "browser":
        safety_action = Action(f"refresh {title} ({app_name}) - reloads the page; anything typed into it that isn't "
                               f"saved may be lost", minimum_level=RiskLevel.MEDIUM, minimum_reason=_BROWSER_REFRESH_REASON)
    elif approved.control_class.lower().startswith("edit"):
        safety_action = Action(f"refresh {title} (File Explorer) - a text box is being edited (renaming a file or "
                               f"typing an address); pressing F5 now may commit or discard it",
                               minimum_level=RiskLevel.MEDIUM, minimum_reason=_EDITING_REFRESH_REASON)
    else:  # a File Explorer folder view: re-reads the listing, changes no data
        safety_action = Action("refresh File Explorer")
    identity = (approved.window.handle, executable, approved.window.class_name, approved.control_handle)

    def run() -> ActionResult:
        # Re-read immediately before sending. The title is deliberately not part of the identity: browser
        # titles change on their own; the same window, program, class and focused control must remain.
        try:
            now = verifier.active_target()
            held_now = verifier.modifiers_held()
        except verifier.VerifierUnavailableError as exc:
            return _result(action, False, f"Didn't refresh: I can't check the keyboard or the active window ({exc}).")
        now_identity = None if now.window is None else (
            now.window.handle, verifier.process_name(now.window.handle), now.window.class_name, now.control_handle)
        if now_identity != identity:
            return _result(action, False, "The active window changed after you approved, so I didn't refresh.")
        if held_now:
            return _result(action, False, _refresh_held_message(held_now))
        emergency_stop.check()  # last checkpoint before F5 is sent
        try:
            accepted, expected = adapter.send_shortcut((), "F5")
        except adapter.ExecutorAdapterError as exc:
            return _result(action, False, f"I couldn't refresh {app_name}: {exc}.")
        if accepted == 0:
            return _result(action, False, "Windows didn't accept the keyboard input for F5, so nothing was refreshed.")
        if accepted < expected:
            adapter.release_keys((), "F5")  # defensive: release the key at once
            return _result(action, True, "Windows accepted only part of the keyboard input for F5, so I released it. "
                                         "The refresh may or may not have happened.", outcome=Outcome.UNVERIFIED)
        return _result(action, True, f"Pressed F5 to refresh {app_name}. I can't confirm the refresh happened.",
                       outcome=Outcome.UNVERIFIED)

    return _Prepared(run, safety_action)


def _refresh_held_message(held: list[str]) -> str:
    keys = " and ".join(held)
    return (f"Didn't refresh: {keys} {'is' if len(held) == 1 else 'are'} held down on the keyboard, which would change "
            f"what F5 does (e.g. a hard reload). Let go and try again.")


# --- window_control ---------------------------------------------------------------------

_WINDOW_OPERATIONS = ("minimize", "maximize", "restore", "close")
_PAST = {"minimize": "minimized", "maximize": "maximized", "restore": "restored"}


def _prepare_window_control(action: ExecutorAction):
    target = " ".join(action.target.lower().split()) if isinstance(action.target, str) else ""
    if not target:
        return _result(action, False, "Which window control? minimize, maximize, restore or close.")
    if target not in _WINDOW_OPERATIONS:
        return _result(action, False, f"I can't do '{action.target.strip()}' to a window. Window controls: minimize, "
                                      f"maximize, restore or close.")
    operation = target
    try:
        approved = verifier.active_target()
        state = verifier.window_state(approved.window.handle) if approved.window else None
    except verifier.VerifierUnavailableError as exc:
        return _result(action, False, f"Didn't {operation} anything: I can't check the active window ({exc}).")
    if approved.window is None:
        return _result(action, False, f"Didn't {operation} anything: there's no active window.")
    if approved.window.class_name in _SHELL_CLASSES:
        return _result(action, False, f"The desktop or taskbar is active, so I didn't {operation} anything.")
    if state is None:
        return _result(action, False, f"The active window closed before I could {operation} it.")
    if state.tool_window:
        return _result(action, False, f"The active window is a tool window, so I didn't {operation} it.")
    if state.hung:
        return _result(action, False, f"The active window isn't responding, so I didn't {operation} it.")
    identity = _window_control_identity(approved)

    if operation == "close":
        try:
            session = _session_group_containing(approved.window.handle)
        except verifier.VerifierUnavailableError as exc:
            return _result(action, False, f"Didn't close anything: I can't check the active window ({exc}).")
        except SettingsError as exc:
            return _result(action, False, str(exc))
        if session is None:
            return _result(action, False, "I only close windows I opened in this session, and the active window isn't "
                                          "one of them, so I left it alone.")
        title = f'window "{approved.window.title}"' if approved.window.title else "a window with no readable title"
        safety_action = Action(f"close {title} ({session.app}, opened by the assistant this session) - closing can "
                               f"lose unsaved work", minimum_level=_CLOSE_RISK, minimum_reason=_CLOSE_RISK_REASON)

        def run_close() -> ActionResult:
            changed = _window_control_changed(action, identity, "close")
            return changed or _close_session_group(action, session)

        return _Prepared(run_close, safety_action)

    if (operation == "minimize" and not state.has_minimize_box) or (operation == "maximize" and not state.has_maximize_box):
        return _result(action, False, f"This window doesn't offer {operation}, so I didn't change it.")
    handle = approved.window.handle

    def run() -> ActionResult:
        changed = _window_control_changed(action, identity, operation)
        if changed:
            return changed
        try:
            now = verifier.window_state(handle)
        except verifier.VerifierUnavailableError as exc:
            return _result(action, False, f"Didn't {operation} the window: I can't check it ({exc}).")
        if now is None:
            return _result(action, False, f"The window closed before I could {operation} it.")
        if _in_requested_state(now, operation):
            return _result(action, True, f"The window is already {_PAST[operation]}, so I didn't change anything.")
        emergency_stop.check()  # last checkpoint before the request is sent
        try:
            adapter.request_window_state(handle, operation)
        except adapter.WindowGoneError:
            return _result(action, False, f"The window closed before I could {operation} it.")
        except adapter.ExecutorAdapterError as exc:
            return _result(action, False, f"I couldn't {operation} the window: {exc}.")
        try:
            verifier.wait_until(lambda: _state_settled(handle, operation), "verifier.window_state_settle_seconds")
            final = verifier.window_state(handle)
        except EmergencyStopError:
            log.warning("Emergency stop while checking window_control %s: the request was already sent and can't be "
                        "taken back", operation)
            raise
        except verifier.VerifierUnavailableError:
            final = _UNREADABLE
        if final is _UNREADABLE:
            return _result(action, True, f"I asked the window to {operation}, but I couldn't read its state afterwards.",
                           outcome=Outcome.UNVERIFIED)
        if final is None:
            return _result(action, False, "The window closed during the action.")
        if _in_requested_state(final, operation):
            return _result(action, True, f"{_PAST[operation].capitalize()} the window.")
        return _result(action, False, f"I asked the window to {operation}, but it didn't {operation} within "
                                      f"{_window_state_settle_seconds():g} seconds.")

    return _Prepared(run, Action(f"{operation} the active window"))


_UNREADABLE = object()


def _window_control_identity(target: ActiveTarget) -> tuple | None:
    """Handle + executable + top-level class. Deliberately not the title: titles change by themselves."""
    if target.window is None:
        return None
    return target.window.handle, verifier.process_name(target.window.handle), target.window.class_name


def _window_control_changed(action: ExecutorAction, identity: tuple, operation: str) -> ActionResult | None:
    """A FAILED result if the active window is no longer the one captured at validation, else None."""
    try:
        now = verifier.active_target()
    except verifier.VerifierUnavailableError as exc:
        return _result(action, False, f"Didn't {operation} the window: I can't check the active window ({exc}).")
    if _window_control_identity(now) != identity:
        return _result(action, False, f"The active window changed, so I didn't {operation} it.")
    return None


def _in_requested_state(state, operation: str) -> bool:
    if operation == "minimize":
        return state.minimized
    if operation == "maximize":
        return state.maximized
    return not state.minimized and not state.maximized  # restore: a normal window


def _state_settled(handle: int, operation: str) -> bool | None:
    """True once the window reached the requested state or is gone; None if it can't be read."""
    try:
        state = verifier.window_state(handle)
    except verifier.VerifierUnavailableError:
        return None
    return state is None or _in_requested_state(state, operation)


def _window_state_settle_seconds() -> float:
    value = get_setting("verifier.window_state_settle_seconds")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _session_group_containing(handle: int) -> _SessionGroup | None:
    """The session window group containing `handle` that is still open, or None. Raises
    VerifierUnavailableError or SettingsError."""
    with _session_lock:
        groups = [(app, group) for app, app_groups in _session_windows.items() for group in app_groups]
    for app, group in groups:
        if handle in group:
            expectation = verifier.expect_window(app)
            if verifier.find_open(expectation, group):
                return _SessionGroup(app, group, expectation)
    return None


# --- Helpers ----------------------------------------------------------------------------

_PREPARERS = {OPEN_APP: _prepare_open_app, CLOSE_APP: _prepare_close_app, CLICK: _prepare_click,
              TYPE_TEXT: _prepare_type_text, SHORTCUT: _prepare_shortcut, SCROLL: _prepare_scroll,
              REFRESH: _prepare_refresh, WINDOW_CONTROL: _prepare_window_control}


def _max_attempts() -> int:
    value = get_setting("executor.max_attempts")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SettingsError(f"Setting 'executor.max_attempts' must be a whole number of at least 1, got {value!r}.")
    return value


def _retry_accepted(result: ActionResult, offer_retry: OfferRetry | None) -> bool:
    if offer_retry is None:
        return False
    try:
        return offer_retry(result) is True
    except Exception as exc:  # a broken prompt must not crash the assistant or retry blindly
        log.warning("Retry prompt failed (%s); not retrying", type(exc).__name__)
        return False


def _result(action: ExecutorAction, ok: bool, message: str, retryable: bool = False,
            outcome: Outcome | None = None, progress: tuple[int, int] | None = None) -> ActionResult:
    result = ActionResult(action, ok, message, retryable, outcome, progress)
    log.log(logging.INFO if ok else logging.WARNING, "Executor %s '%s': %s (%s) - %s",
            action.kind, action.log_label, "OK" if ok else "FAILED", result.outcome.value, message)
    return result
