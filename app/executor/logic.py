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

Phase 1 is built one action at a time: open_app, close_app, click, then type_text.

close_app closes ONLY windows the assistant opened in this session. open_app remembers them in
memory, so nothing carries over a restart, and windows the user opened are never touched. Closing
is a polite request (like clicking the window's X), never ending a process, and outcomes stay
distinct (models.Outcome): a window already gone is ALREADY_CLOSED, one showing a dialog such as
"Save changes?" is NEEDS_USER, and one that stays open is STILL_OPEN. Neither of the last two is
ever retryable, so the recovery loop can't retry into an app that is waiting for the user.

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
    count, the window, the field, the number of Enter presses (and whether the text ends with one),
    and a preview of at most the first 40 characters with Enter shown as a return symbol
  - right after confirmation: if the active window or focused field changed, nothing is typed
  - one character at a time; before EVERY character the emergency stop and the active window/field
    are checked, and the pause between characters is interruptible
  - outcomes: DONE only when the field's text, read before and after, shows the exact text appearing
    one more time; UNVERIFIED when the field can't be read or the text isn't found; FAILED when
    nothing was typed; PARTIAL (progress=(sent, total)) when typing stopped part-way. An emergency
    stop after something was typed raises TypingInterruptedError carrying that result
  - nothing about typing is ever retryable: a retry could type the text twice
  - the text itself never appears in logs, result messages, repr() or errors - only its length. It
    exists in memory while typing and checking, and (at most 40 characters of it) in the prompt,
    which is shown on screen and never logged.

Measured real-desktop behavior and known open decisions: docs/step4 Section 4, implementation notes.
"""
import logging
import re
import threading
import unicodedata
from dataclasses import dataclass
from typing import Callable

from app.executor import adapter, emergency_stop
from app.executor.emergency_stop import EmergencyStopError, TypingInterruptedError
from app.executor.models import CLICK, CLOSE_APP, OPEN_APP, TYPE_TEXT, ActionResult, ExecutorAction, Outcome
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
PREVIEW_CHARACTERS = 40
_ENTER_SYMBOL = "\u23ce"  # the return symbol that marks each Enter in the prompt's preview

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

    def run() -> ActionResult:
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
        failure = _request_close(name, still_open)
        if failure:
            return _result(action, False, failure)
        try:
            check = verifier.wait_for_windows_to_close(expectation, relevant)
        except EmergencyStopError:
            log.warning("Emergency stop while verifying close_app '%s': the close request was already sent "
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

    return run


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
    safety_action = Action(_typing_prompt(text, approved, enters),
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


def _typing_prompt(text: str, target: ActiveTarget, enters: int) -> str:
    """What the user approves: count, window, field, Enter presses and a short preview. Shown on screen
    only - never logged."""
    window = f'window "{target.window.title}"' if target.window.title else "a window with no readable title"
    field = target.control_class or "unknown"
    prompt = f"type {len(text)} character{'s' if len(text) != 1 else ''} into {window} (field: {field})"
    if enters:
        prompt += (f" AND PRESS ENTER {enters} TIME{'S' if enters != 1 else ''} - Enter can submit a form, "
                   f"send a message or run a command")
        if text.endswith("\n"):
            prompt += " (ends with Enter: it will submit as soon as typing finishes)"
    preview = text[:PREVIEW_CHARACTERS].replace("\n", _ENTER_SYMBOL)
    ellipsis = "\u2026" if len(text) > PREVIEW_CHARACTERS else ""
    return f'{prompt}: "{preview}{ellipsis}"'


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


# --- Helpers ----------------------------------------------------------------------------

_PREPARERS = {OPEN_APP: _prepare_open_app, CLOSE_APP: _prepare_close_app, CLICK: _prepare_click,
              TYPE_TEXT: _prepare_type_text}


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
