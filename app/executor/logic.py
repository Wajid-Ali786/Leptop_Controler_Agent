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
memory, so nothing carries over a restart, and windows the user opened are never touched. Each group
is remembered as its handles PLUS an ownership token attached to those windows (a Windows window
property, app/executor/adapter.py), because a handle number alone is not an identity: Windows gives
handle numbers to new windows, so a window we never opened could otherwise be mistaken for ours. A
window the token can't be attached to is never recorded, and so is never closable. Closing
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
import urllib.parse
import time
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Callable

from app.executor import adapter, emergency_stop, shortcuts
from app.executor.emergency_stop import ActionInterruptedError, EmergencyStopError, TypingInterruptedError
from app.executor.models import (ASSISTANT_BROWSER, CLICK, CLICK_TARGET, CLOSE_APP, CLOSE_BROWSER,
                                 NAVIGATE,
                                 OPEN_APP, OPEN_BROWSER, REFRESH, RESOLVE_BAD_FORMAT,
                                 RESOLVE_NO_TARGET, RESOLVE_OUT_OF_RANGE, RESOLVE_SETTINGS,
                                 RESOLVE_UNKNOWN_APP, RESOLVE_UNKNOWN_KIND, RESOLVE_UNWANTED_TARGET,
                                 SCROLL, SHORTCUT, TYPE_TEXT, WINDOW_CONTROL, ActionResult,
                                 ExecutorAction, Outcome, Resolved, Unresolved)
from app.safety.logic import Confirm, authorize
from app.safety.models import Action, RiskLevel
from app.verifier import logic as verifier
from app.verifier import observation
from app.verifier.models import (ActionTarget, ActiveTarget, Ambiguous, DomTarget, Found,
                                 FramesNotSupported, NotEligible, NotFound, Observed, Screen, Stale,
                                 Target, Unavailable, WindowExpectation, WindowInfo, normalize_name)
from config.settings import SettingsError, get_setting

log = logging.getLogger(__name__)

_clock = time.monotonic   # replaced in tests that need the activation deadline to pass instantly

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
_session_windows: dict[str, list["_OwnedWindowGroup"]] = {}
_session_lock = threading.Lock()

# WINDOWS THE ASSISTANT FOUND, which is NOT the same thing and must never be confused with it.
#
# _session_windows above means OWNED: every entry carries an ownership token that was attached and read
# back, and _ours_now re-checks that token before anything may act. This registry is the opposite - a
# window that was already on the desktop when "open <app>" was asked for, which open_app selected and
# brought to the front. The assistant did not create it, so it has NO token and never gets one.
#
# What a record here is worth, exactly: a handle number plus the app it matched. That is NOT an
# identity. Windows reuses handle numbers, so a record here can only ever be re-checked by asking
# whether a window with that number is still open AND still matches the app's title pattern - which a
# different window could also satisfy. It is deliberately the weakest honest representation, and
# NOTHING in this slice grants it any power: no ownership check reads it, close_app cannot reach it,
# and a named click still refuses an app that only appears here. Slice 3 decides what, if anything,
# non-destructive actions may do with it.
_found_windows: dict[str, "_FoundWindow"] = {}


@dataclass(frozen=True)
class _FoundWindow:
    """One window open_app selected but did not create. There is deliberately no token field: a
    _FoundWindow cannot be mistaken for an _OwnedWindowGroup, by construction rather than by care.

    `process` and `class_name` are a STRUCTURAL FINGERPRINT taken when the window was recorded, so
    that a handle number alone never decides where a click goes. The three fields together are the
    same evidence _window_control_identity and the refresh path already use - handle, executable,
    top-level window class - and the title is deliberately absent for the same reason it is absent
    there: titles change by themselves, and Chrome's changes with every page."""
    app: str
    handle: int
    process: str | None
    class_name: str


@dataclass(frozen=True)
class _OwnedWindowGroup:
    """Windows the assistant opened in this session, and the token that proves it.

    `handles` holds ONLY the windows whose ownership token was attached and read back, so a window that
    could not be proved ours is never closable. The token is what makes ownership refer to the window
    OBJECTS rather than to their numbers: a window property dies with its window, so a different window
    that is later given one of these numbers carries no token and can never be ours.

    The token is never logged, never shown to the user and never persisted - it is left out of the repr
    (but NOT out of equality) so that logging a group can't leak it."""
    handles: frozenset[int]
    token: int = field(repr=False)


@dataclass(frozen=True)
class _Prepared:
    """A validated action, ready to run once the safety gate allows it."""
    run: Callable[[], ActionResult]
    safety_action: Action | None = None  # what the gate classifies and the user approves; default: the description


# --- Target resolution: the one place that answers "is this target usable?" --------------------------
# Every preparer used to open with its own target check. Those checks are now here, unchanged, so the
# same question has one answer whether it is asked by a preparer about to act or by the Phase 3 router
# deciding whether a line needs the Brain.
#
# SIDE-EFFECT FREE, and it must stay that way: no verifier call, no active window, no snapshot, no OS
# adapter, nothing executed. Config reads are allowed, because some targets are only valid relative to
# configuration (which apps exist, how many notches are allowed, how long typed text may be).
#
# It is NOT a promise that the action will succeed. It answers only the config-and-grammar half. The
# desktop half - is a window there, is the right one in front - still happens in the preparer, where
# reading the screen belongs.

def resolve(action: ExecutorAction) -> Resolved | Unresolved:
    """Can this action's target be used? No side effects; see the note above."""
    resolver = _RESOLVERS.get(action.kind)
    if resolver is None:
        return Unresolved(RESOLVE_UNKNOWN_KIND, f"I don't know how to do '{action.kind}' yet.")
    return resolver(action)


def _resolve_open_app(action: ExecutorAction) -> Resolved | Unresolved:
    return _resolve_app(action, "open")


def _resolve_close_app(action: ExecutorAction) -> Resolved | Unresolved:
    return _resolve_app(action, "close")


def _resolve_app(action: ExecutorAction, verb: str) -> Resolved | Unresolved:
    """The app-name rule for open and close. The two verbs word their messages differently, and both
    wordings are preserved exactly."""
    name = action.target.strip().lower() if isinstance(action.target, str) else ""
    if not name:
        return Unresolved(RESOLVE_NO_TARGET, f"Which app should I {verb}?")
    try:
        apps = _configured_apps()
    except SettingsError as exc:
        return Unresolved(RESOLVE_SETTINGS, str(exc))
    if name not in apps:
        return Unresolved(RESOLVE_UNKNOWN_APP,
                          f"I don't know an app called '{action.target.strip()}'. "
                          f"Apps I can {verb}: {', '.join(sorted(apps))}.")
    return Resolved(name)


def _resolve_click(action: ExecutorAction) -> Resolved | Unresolved:
    target = action.target.strip() if isinstance(action.target, str) else ""
    if not target:
        return Unresolved(RESOLVE_NO_TARGET,
                          "Where should I click? Give screen coordinates as x, y (e.g. 500, 300).")
    inner = target[1:-1].strip() if target.startswith("(") and target.endswith(")") else target
    match = _POINT.fullmatch(inner)
    if not match:
        return Unresolved(RESOLVE_BAD_FORMAT,
                          f"I can't click at '{target}': give whole-number screen coordinates "
                          f"as x, y (e.g. 500, 300).")
    return Resolved((int(match.group(1)), int(match.group(2))))


def _resolve_click_target(action: ExecutorAction) -> Resolved | Unresolved:
    """The grammar-and-config half of a named click: is there a control name, and is the app one we
    have? Side-effect free like every other resolver - no window is looked for here, because which
    window is open is a fact about the desktop and belongs in the preparer."""
    control = action.control.strip() if isinstance(action.control, str) else ""
    if not control:
        return Unresolved(RESOLVE_NO_TARGET, "What should I click? Name the button or field.")
    app = action.target.strip().lower() if isinstance(action.target, str) else ""
    if not app:
        return Resolved((control, ""))      # which context, is answered from this session's records
    if app == ASSISTANT_BROWSER:
        # RESERVED, and checked FIRST - before configuration and before Memory's aliases get a say.
        # It names runtime context, so no configured app and no remembered alias can redefine it.
        return Resolved((control, ASSISTANT_BROWSER))
    try:
        apps = _configured_apps()
    except SettingsError as exc:
        return Unresolved(RESOLVE_SETTINGS, str(exc))
    if app not in apps:
        return Unresolved(RESOLVE_UNKNOWN_APP,
                          f"I don't know an app called '{action.target.strip()}'. "
                          f"Apps I can click in: {', '.join(sorted(apps))}.")
    return Resolved((control, app))


def _resolve_scroll(action: ExecutorAction) -> Resolved | Unresolved:
    target = " ".join(action.target.split()) if isinstance(action.target, str) else ""
    parsed = _parse_scroll(target)
    if isinstance(parsed, str):
        return Unresolved(RESOLVE_BAD_FORMAT, parsed)
    direction, requested = parsed
    try:
        limit, _unclassified_limit, _interval = _scroll_settings()
    except SettingsError as exc:
        return Unresolved(RESOLVE_SETTINGS, str(exc))
    if requested > limit:
        return Unresolved(RESOLVE_OUT_OF_RANGE,
                          f"That's {requested} notches; I scroll at most {limit} at once.")
    return Resolved((direction, requested))


def _resolve_shortcut(action: ExecutorAction) -> Resolved | Unresolved:
    parsed = shortcuts.parse(action.target)
    if isinstance(parsed, shortcuts.ShortcutRefusal):
        return Unresolved(RESOLVE_BAD_FORMAT, parsed.message)
    return Resolved(parsed)


def _resolve_window_control(action: ExecutorAction) -> Resolved | Unresolved:
    target = " ".join(action.target.lower().split()) if isinstance(action.target, str) else ""
    if not target:
        return Unresolved(RESOLVE_NO_TARGET,
                          "Which window control? minimize, maximize, restore or close.")
    if target not in _WINDOW_OPERATIONS:
        return Unresolved(RESOLVE_BAD_FORMAT,
                          f"I can't do '{action.target.strip()}' to a window. Window controls: minimize, "
                          f"maximize, restore or close.")
    return Resolved(target)


def _resolve_type_text(action: ExecutorAction) -> Resolved | Unresolved:
    raw = action.target if isinstance(action.target, str) else ""
    if not raw:
        return Unresolved(RESOLVE_NO_TARGET, "What should I type?")
    text = raw.replace("\r\n", "\n")
    try:
        limit = _max_type_characters()
    except SettingsError as exc:
        return Unresolved(RESOLVE_SETTINGS, str(exc))
    if len(text) > limit:
        return Unresolved(RESOLVE_OUT_OF_RANGE,
                          f"That's {len(text)} characters; I can type at most {limit} at once.")
    categories = {unicodedata.category(c) for c in text if c != "\n"}
    if "Cs" in categories:
        return Unresolved(RESOLVE_BAD_FORMAT, "I can't type that: it contains an invalid character.")
    if "Cc" in categories:
        return Unresolved(RESOLVE_BAD_FORMAT,
                          "I can't type that: it contains Tab or another control key, which isn't "
                          "text (keyboard shortcuts come later).")
    return Resolved(text)


def _resolve_refresh(action: ExecutorAction) -> Resolved | Unresolved:
    if isinstance(action.target, str) and action.target.strip():
        return Unresolved(RESOLVE_UNWANTED_TARGET,
                          "Refresh doesn't take a target; it refreshes the active window.")
    return Resolved(None)


ADVISORY_FLOOR_REASON = "the reasoning step asked for extra care"


def execute(action: ExecutorAction, confirm: Confirm | None = None, *,
            risk_floor: RiskLevel = RiskLevel.LOW) -> ActionResult:
    """Carry out one action once, or explain why not. See the module docstring for the order.

    `risk_floor` is an ADVISORY minimum from the caller - Phase 3 passes the Brain's opinion about how
    careful to be. It can only ever RAISE the floor this module's own preparer already chose: see
    _with_advisory_floor(). It is deliberately not part of ExecutorAction, because it is not something
    the assistant does, only something somebody thinks about it.
    """
    emergency_stop.check()
    prepare = _PREPARERS.get(action.kind)
    if prepare is None:
        return _result(action, False, f"I don't know how to do '{action.kind}' yet.")
    prepared = prepare(action)  # validation only - nothing happens on the computer here
    return _authorize_and_run(action, prepared, confirm, risk_floor)


def _authorize_and_run(action: ExecutorAction, prepared, confirm: Confirm | None,
                       risk_floor: RiskLevel) -> ActionResult:
    """The gate, and then the action. Extracted so every entry point - a typed command and a
    re-identified screen target alike - goes through the SAME confirmation and the same checkpoints.
    There is deliberately no second copy of this sequence anywhere."""
    if isinstance(prepared, ActionResult):
        return prepared
    if not isinstance(prepared, _Prepared):
        prepared = _Prepared(prepared)
    safety_action = prepared.safety_action or Action(action.description)
    authorize(_with_advisory_floor(safety_action, risk_floor), confirm)  # raises ActionDeniedError
    emergency_stop.check()
    return prepared.run()


def _with_advisory_floor(safety_action: Action, risk_floor: RiskLevel) -> Action:
    """Combine the preparer's own floor with an advisory one: the higher of the two wins.

    RiskLevel is an IntEnum, so the comparison is the same one app/safety/logic.assess() already uses
    to decide between the words and the floor. An advisory floor that is not higher changes NOTHING -
    not the level, and not the preparer's own minimum_reason, which says something true about why this
    particular action is risky and must not be replaced by a generic sentence.
    """
    try:
        advisory = RiskLevel(risk_floor)
    except ValueError:
        advisory = RiskLevel.CRITICAL      # an unreadable floor fails closed, like assess() does
    if advisory <= RiskLevel(safety_action.minimum_level):
        return safety_action
    return replace(safety_action, minimum_level=advisory, minimum_reason=ADVISORY_FLOOR_REASON)


def execute_with_recovery(action: ExecutorAction, confirm: Confirm | None = None,
                          offer_retry: OfferRetry | None = None, *,
                          risk_floor: RiskLevel = RiskLevel.LOW) -> ActionResult:
    """Action -> Result -> Recovery. A retryable failure is offered via offer_retry(result), which
    must return exactly True to retry; without it, nothing is retried. Stops after
    executor.max_attempts attempts.

    `risk_floor` is passed to EVERY attempt: a retry runs the whole pipeline again, safety gate
    included, so it must be asked for with the same care as the first try.
    """
    try:
        max_attempts = _max_attempts()
    except SettingsError as exc:
        return _result(action, False, str(exc))
    attempt = 1
    while True:
        result = execute(action, confirm, risk_floor=risk_floor)
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

# --- open_app's four outcomes, in words -----------------------------------------------------------
# "open <app>" means ENSURE THE APP IS AVAILABLE AND IN FRONT, so the four things that can happen have
# to be told apart. The old single message claimed a launch had failed whenever no NEW window appeared,
# which is what reported "chrome was started, but no new window appeared within 15 seconds" while
# Chrome was open the whole time.
#
#   we created it            -> "Opened {name}; its window appeared after ...s."   (unchanged, below)
#   it was already there     -> _ALREADY_OPEN_IN_FRONT
#   there but not frontable  -> _AVAILABLE_BUT_MINIMIZED / _AVAILABLE_BUT_NOT_FRONTED
#   genuinely nothing        -> the Verifier's own "no new window appeared" message (unchanged)
_ALREADY_OPEN_IN_FRONT = "{name} was already open, so I brought it to the front instead of opening another."
# A launch DID happen and a single usable window is now there, but nothing proves we made it - a
# launcher that hands off to a running process ends up here. Reporting it as created would be a lie,
# and it is the sentence close_app's later refusal has to be consistent with.
_OPENED_BUT_NOT_PROVABLY_MINE = ("{name} is open and in front. I can't prove I'm the one who opened "
                                 "that window, so I won't close it automatically.")
_AVAILABLE_BUT_MINIMIZED = ("{name} is already open but minimized, so I couldn't bring it to the "
                            "front. Bring it back up and say that again.")
_AVAILABLE_BUT_NOT_FRONTED = ("{name} is already open, but Windows wouldn't bring it to the front{why}. "
                              "It's running - put it in front yourself if you need it there.")
# Deferred, not solved: see _prepare_open_app. Says the count, activates none, launches nothing, asks
# nothing - choosing between windows the user already had open is Slice 3's problem at the earliest.
_MANY_ALREADY_OPEN = ("{name} already has {count} windows open and I didn't open any of them, so I "
                      "don't know which one you mean. I've left them all alone and opened nothing.")


def _prepare_open_app(action: ExecutorAction):
    resolution = resolve(action)          # the one target rule; see the note above resolve()
    if isinstance(resolution, Unresolved):
        return _result(action, False, resolution.message)
    name = resolution.value
    executable = _configured_apps()[name]
    try:
        expectation = verifier.expect_window(name)
    except SettingsError as exc:
        return _result(action, False, str(exc))

    def run() -> ActionResult:
        try:
            before, existing = _app_windows_now(expectation)
        except verifier.VerifierUnavailableError as exc:
            return _result(action, False, f"Didn't open {name}: I can't check whether its window appears ({exc}).")
        if len(existing) > 1:
            # AMBIGUITY, deferred rather than solved. Nothing is activated and nothing is launched:
            # choosing between windows the user already had open needs selection machinery that does
            # not exist, and guessing is how an assistant acts in the wrong window.
            return _result(action, False, _MANY_ALREADY_OPEN.format(name=name, count=len(existing)),
                           log_message=f"open_app '{name}': {len(existing)} pre-existing windows, refused")
        if existing:
            # ALREADY OPEN. No launch and NO 15-second wait: there is nothing to wait for.
            return _found_and_fronted(action, name, existing[0], launched=False)
        return _launch_then_look(action, name, executable, expectation, before)

    return run


def _app_windows_now(expectation: WindowExpectation) -> tuple[frozenset[int], list[WindowInfo]]:
    """(every matching handle, the ones a person could actually act in).

    USABLE means matching the app's title pattern and not cloaked - the same "shown" test
    verifier.wait_for_new_window already applies to a window that has just appeared, so "is this app
    available?" gets one answer whether the window is new or was already there.

    Both halves come from one listing, so the handle set and the window list cannot disagree. The
    handle set is what wait_for_new_window needs as its `before`, and it deliberately includes cloaked
    windows: a window that is starting up is not usable yet, but it is not new either."""
    handles = verifier.snapshot_windows(expectation)
    return handles, [w for w in verifier.find_open(expectation, handles) if not w.cloaked]


def _launch_then_look(action: ExecutorAction, name: str, executable: str,
                      expectation: WindowExpectation, before: frozenset[int]) -> ActionResult:
    """Nothing usable was open, so launch - and then be honest about what actually appeared."""
    try:
        adapter.launch_app(executable)
    except adapter.ExecutorAdapterError as exc:
        return _result(action, False, str(exc))
    check = verifier.wait_for_new_window(expectation, before)
    if check.ok:
        opened = f"Opened {name}; its window appeared after {check.elapsed_seconds:.1f}s"
        if _remember_opened(name, check.window_handles):
            return _result(action, True, f"{opened}.")
        # The launch really happened, so it is reported as success - but nothing was proved ours, and
        # saying so now is better than refusing without explanation when a close is asked for later.
        return _result(action, True, f"{opened}, but I won't be able to close it automatically.")

    # No NEW window was proved. That is not the same as "the app isn't there": a launcher that hands
    # off to a running process, or a window that was created before the snapshot could see it, both
    # end up here. So look at what IS there before calling it a failure - this is the exact case that
    # reported "no new window appeared within 15 seconds" while Chrome was open all along.
    try:
        _before_again, usable = _app_windows_now(expectation)
    except verifier.VerifierUnavailableError:
        return _result(action, False, check.message, retryable=check.retryable)
    if not usable:
        return _result(action, False, check.message, retryable=check.retryable)   # a genuine failure
    if len(usable) > 1:
        return _result(action, False, _MANY_ALREADY_OPEN.format(name=name, count=len(usable)),
                       log_message=f"open_app '{name}': {len(usable)} windows after launch, refused")
    return _found_and_fronted(action, name, usable[0], launched=True)


def _found_and_fronted(action: ExecutorAction, name: str, window: WindowInfo, *,
                       launched: bool) -> ActionResult:
    """A window we did NOT create: bring it to the front and say exactly what can be proved.

    THE OWNERSHIP BOUNDARY, and the whole point of the slice. This window existed before the command
    (or appeared without being provably ours), so it gets NO ownership token and never enters
    _session_windows. _remember_found records only that open_app selected it, in a separate registry
    that no ownership check reads - so close_app still refuses it, exactly as it refuses any window
    the user opened themselves."""
    outcome = _activate_to_front(window.handle)
    state, detail = outcome.state, outcome.detail
    if state == _FRONT_IN_FRONT:
        _remember_found(name, window)
        return _result(action, True, _ALREADY_OPEN_IN_FRONT.format(name=name) if not launched
                       else _OPENED_BUT_NOT_PROVABLY_MINE.format(name=name),
                       log_message=f"open_app '{name}': found an existing window and fronted it "
                                   f"(launched={launched})")
    # Available, but not in front. NOT a failure and NOT a full success: the app is there, so saying
    # it is absent would be false and relaunching it would be wrong.
    #
    # Outcome.NEEDS_USER is the closest EXISTING semantics and no new status is invented: the action
    # cannot finish until the person does something - bring the window up, or click it - which is what
    # NEEDS_USER has always meant. It also carries the right consequence for free: the model forbids
    # retryable on it, so nothing loops on an activation Windows has already refused. (Outcome.PARTIAL
    # would have been the other candidate and is wrong: it requires progress=(sent, total) counting
    # units of work, which an activation does not have.)
    _remember_found(name, window)
    if state == _FRONT_MINIMIZED:
        reason = _AVAILABLE_BUT_MINIMIZED.format(name=name)
    elif state == _FRONT_GONE_BEFORE or state == _FRONT_GONE_DURING:
        # It closed between being listed and being activated. Nothing is available any more, and
        # relaunching on its own initiative is not this command's job.
        _forget_found(name)
        return _result(action, False, f"{name}'s window closed before I could bring it to the front.",
                       retryable=True, log_message=f"open_app '{name}': the found window closed")
    elif state == _FRONT_SETTINGS:
        reason = detail
    elif state == _FRONT_ERROR:
        reason = _AVAILABLE_BUT_NOT_FRONTED.format(name=name, why=f" ({detail})")
    elif state == _FRONT_UNREADABLE_WINDOW or state == _FRONT_UNREADABLE_FOREGROUND:
        reason = _AVAILABLE_BUT_NOT_FRONTED.format(name=name, why=f" ({detail})")
    else:
        reason = _AVAILABLE_BUT_NOT_FRONTED.format(name=name, why="")
    return _result(action, False, reason, outcome=Outcome.NEEDS_USER,
                   log_message=f"open_app '{name}': available but not fronted ({state})")


def configured_app_names() -> list[str]:
    """The app names configuration allows, sorted. Read-only, and the single source of that answer.

    Published so a caller that must check a name against the configured set does not have to read or
    re-validate executor.apps for itself. It grants nothing: knowing a name is configured is not
    permission to open it, and every open still goes through resolve() and the safety gate."""
    return sorted(_configured_apps())


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
    group: _OwnedWindowGroup
    expectation: WindowExpectation


def _prepare_close_app(action: ExecutorAction):
    resolution = resolve(action)
    if isinstance(resolution, Unresolved):
        return _result(action, False, resolution.message)
    name = resolution.value
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
        still_open = _ours_now(group, expectation)  # it may have closed during confirmation
    except verifier.VerifierUnavailableError as exc:
        return _cant_check(action, name, exc)
    if not still_open:
        _forget(name, group)
        return _result(action, True, f"{name} is already closed.", outcome=Outcome.ALREADY_CLOSED)
    ours = frozenset(window.handle for window in still_open)
    try:
        # Titled windows inside the group (a Store app's content window) must be gone too: they move
        # out of the frame as a separate window while the app closes. Only windows that are still OURS
        # count - waiting for a handle number that now belongs to someone else would never finish.
        relevant = ours | verifier.hosted_windows(expectation, ours)
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


def _remember_opened(name: str, handles: frozenset[int]) -> bool:
    """Take ownership of the windows the Verifier just saw appear, and say whether any could be taken.

    Only handles whose ownership token was attached AND read back are recorded: a sibling that couldn't
    be tagged is left out rather than kept on its number alone, so it can never receive a close request.
    If none could be tagged there is no ownership record at all - the open still happened, but close_app
    will refuse these windows."""
    if not handles:
        return False
    token = adapter.new_window_token()
    tagged = frozenset(handle for handle in handles if adapter.tag_window(handle, token))
    if not tagged:
        log.warning("Executor: couldn't prove ownership of the new %s window(s), so I won't close them", name)
        return False
    if tagged != handles:
        log.info("Executor: %d of %d new %s window(s) can be closed later", len(tagged), len(handles), name)
    with _session_lock:
        _session_windows.setdefault(name, []).append(_OwnedWindowGroup(tagged, token))
    return True


def _ours_now(group: _OwnedWindowGroup, expectation: WindowExpectation) -> list[WindowInfo]:
    """THE ownership test, and the only one: the windows of `group` that are still open, still match the
    app, and still carry this group's ownership token.

    Both halves are required. A recorded handle NUMBER is not ownership, because Windows gives handle
    numbers to new windows; the token is what the original window object carried. Raises
    VerifierUnavailableError."""
    return [window for window in verifier.find_open(expectation, group.handles)
            if adapter.window_token(window.handle) == group.token]


def _forget(name: str, group: _OwnedWindowGroup) -> None:
    with _session_lock:
        groups = _session_windows.get(name, [])
        if group in groups:
            groups.remove(group)


def _open_session_group(name: str, expectation: WindowExpectation) -> _OwnedWindowGroup | None:
    """The most recently opened window group of `name` from this session that is still ours, or None.
    Groups with no token-carrying window left are forgotten - whether their windows closed or their
    handle numbers now belong to windows we never opened. Raises VerifierUnavailableError."""
    with _session_lock:
        groups = list(_session_windows.get(name, []))
    for group in reversed(groups):
        if _ours_now(group, expectation):
            return group
        _forget(name, group)
    return None


def _remember_found(name: str, window: WindowInfo) -> None:
    """Record that open_app selected a window it did NOT create, with its structural fingerprint.

    One per app, replacing any earlier one: this answers "which window did open_app last put in
    front?", which is a single answer by definition. The fingerprint is taken HERE, at the moment the
    window was observed and fronted, so the check before a later click compares against what was
    actually seen rather than against whatever happens to hold that handle number by then."""
    with _session_lock:
        _found_windows[name] = _FoundWindow(app=name, handle=window.handle,
                                            process=verifier.process_name(window.handle),
                                            class_name=window.class_name)


def _found_now(name: str, expectation: WindowExpectation) -> WindowInfo | None:
    """The window open_app found for `name`, IF every piece of its structural evidence still holds -
    otherwise None. Raises VerifierUnavailableError.

    WHAT THIS PROVES: a window with that handle is open, it still matches the app's configured title
    pattern, it is still owned by the same executable, and it is still the same top-level window class.
    Four independent facts, none of which is the window's title text.

    WHAT IT CANNOT PROVE: that it is the SAME WINDOW OBJECT. Windows reuses handle numbers, so a
    second Chrome window created after the first closed could take the number and satisfy all four -
    it is the same program, the same class, and matches the same pattern. Only the ownership token
    rules that out, and a found window has none by definition. This is why a found window may be
    clicked - behind a confirmation that names it - and may never be closed."""
    with _session_lock:
        found = _found_windows.get(name)
    if found is None:
        return None
    windows = verifier.find_open(expectation, frozenset({found.handle}))
    if len(windows) != 1:
        return None                                   # gone, or no longer this app's kind of window
    window = windows[0]
    if window.class_name != found.class_name:
        return None                                   # the same number, a different kind of window
    # Fail closed: an executable that could not be read when the window was recorded leaves nothing
    # to compare, so the window is simply not clickable rather than clickable on weaker evidence.
    if not found.process or verifier.process_name(window.handle) != found.process:
        return None
    return window


def _forget_found(name: str) -> None:
    with _session_lock:
        _found_windows.pop(name, None)


def found_window_handle(name: str) -> int | None:
    """The handle open_app last selected for `name` without owning it, or None.

    Published for TESTS and for the slice that will decide what may be done with such a window. It is
    a number, not a permission and not an identity: a caller must still re-check that a window with
    that number is open and matches the app, and even then it is weaker than ownership. Nothing in the
    Executor acts on it today."""
    with _session_lock:
        found = _found_windows.get(name)
    return None if found is None else found.handle


def forget_session_windows() -> None:
    """Forget every window opened in this session, so close_app will close none of them.

    Also drops the found-window records: they are session state too, and a stale one would outlive the
    reason it was taken. It never granted anything, so clearing it takes nothing away."""
    with _session_lock:
        _session_windows.clear()
        _found_windows.clear()


# --- click ------------------------------------------------------------------------------

def _prepare_click(action: ExecutorAction):
    resolution = resolve(action)
    if isinstance(resolution, Unresolved):
        return _result(action, False, resolution.message)
    x, y = resolution.value
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
        return _send_click(action, x, y, f"at ({x}, {y})")

    return _Prepared(run, safety_action)


def _send_click(action: ExecutorAction, x: int, y: int, what: str,
                log_what: str | None = None) -> ActionResult:
    """Send the one click and check where the pointer ended up. The ONLY place this module clicks.

    `what` is how the click is described back to the user - "at (500, 300)" for a coordinate click,
    the user's own word for the control for a screen target. It changes the wording and nothing else:
    both kinds of click pass the same emergency-stop checkpoint, the same fail-safe handling and the
    same after-the-fact pointer check, and both are UNVERIFIED, because what a click DID still cannot
    be observed - a re-identified target proves where the click landed, never what it achieved.

    `log_what` is how the same click is described in the log when `what` is the user's own words, which
    are not written to disk. Left out, the message is logged as it stands, as every other action's is."""
    emergency_stop.check()  # last checkpoint before the input is sent
    logged = what if log_what is None else log_what
    try:
        adapter.click(x, y)
    except adapter.MouseFailSafeError:
        emergency_stop.trigger("mouse-corner")
        emergency_stop.check()  # raises EmergencyStopError
    except adapter.ExecutorAdapterError as exc:
        return _result(action, False, f"I couldn't click {what}: {exc}.",
                       log_message=f"couldn't click {logged}: {exc}")
    try:
        pointer = verifier.cursor_position()
    except verifier.VerifierUnavailableError:
        return _result(action, True, f"Clicked {what}. I couldn't read where the mouse pointer ended "
                                     f"up, and I can't check what the click did.", outcome=Outcome.UNVERIFIED,
                       log_message=f"clicked {logged} at ({x}, {y}); the pointer could not be read back")
    if pointer != (x, y):
        return _result(action, False, f"I sent the click, but the mouse pointer is at {pointer} instead of "
                                      f"({x}, {y}), so the click may have landed somewhere else.")
    return _result(action, True, f"Clicked {what}. I can't check what the click did.",
                   outcome=Outcome.UNVERIFIED, log_message=f"clicked {logged} at ({x}, {y})")


# --- click on a screen target the user named (Phase 5 Slice 2) ---------------------------------------
# A UIA target does not get its own click. It gets its own PREPARER, and then joins the existing path:
# the same safety gate, the same confirmation, the same emergency-stop checkpoints, the same single
# adapter.click() and the same honest UNVERIFIED result.
#
# THE ORDER IS THE WHOLE POINT. Observing, confirming and acting happen at three different moments, and
# the screen is free to change between them. So the bounds shown to the user are never the bounds that
# are clicked: the control is found AGAIN after the confirmation, and if anything about it has changed -
# it moved out of view, it was disabled, it became two controls, it went away - nothing is clicked. The
# pre-confirmation observation is used for exactly one thing: knowing what to look for again.
#
# Precision does not buy permission. Knowing exactly which button is under the pointer says nothing
# about what that button DOES, so a UIA click is confirmed at MEDIUM exactly like a coordinate click.

# How a click on a screen target is described in LOGS. The user's own word for the control is command
# text and never goes to disk, so the log says which KIND of click it was and what became of it.
_TARGET = "screen target"

_TARGET_CLICK_RISK = RiskLevel.MEDIUM
_TARGET_CLICK_RISK_REASON = ("click on a screen target - always needs confirmation (what the control does "
                             "can't be known from its name)")


def click_target(target: Target, observed: Observed, confirm: Confirm | None = None, *,
                 risk_floor: RiskLevel = RiskLevel.LOW) -> ActionResult:
    """Click the control the user named, having found it again first.

    `target` is the user's own words and the window they meant; `observed` is what app/verifier
    observation found for it. Phase 5 Slice 2 deliberately takes both as typed arguments: wiring this to
    a spoken or typed sentence is a later slice, and the local primitive is built and proved first."""
    emergency_stop.check()
    action = ExecutorAction(CLICK)  # a click, with no coordinate yet: it is not known until after the gate
    return _authorize_and_run(action, _prepare_target_click(action, target, observed), confirm, risk_floor)


def _describe_window(window: WindowInfo, context: "_AppWindowContext | None") -> str:
    """How a window is named to the USER, in the confirmation and in the result, so the two agree.

    THE CONFIRMATION IS NOW THE AUTHORIZATION. With ownership no longer the click gate, this sentence
    is the only thing between a wrong resolution and a wrong click, so it always names the window and
    - when a context established provenance - says plainly whether the assistant opened it or merely
    found it. "did NOT open" is deliberately blunt and deliberately not a synonym of "opened".

    The title is shown because it is what lets a person RECOGNISE the window. It is not what proves
    identity (that is _found_now's fingerprint) and it is never written to the log.

    `context=None` is the bare click_target() primitive, which has no provenance to report, and keeps
    exactly the wording it had before this slice."""
    titled = f'"{window.title}"' if window.title else "no readable title"
    if context is None:
        return f'window "{window.title}"' if window.title else "a window with no readable title"
    if context.owned:
        return f'the {context.app} window I opened ({titled})'
    return f'a {context.app} window I did NOT open ({titled})'


def _prepare_target_click(action: ExecutorAction, target: Target, observed: Observed,
                          context: "_AppWindowContext | None" = None):
    if not isinstance(target, Target) or not isinstance(target.name, str) or not target.name.strip():
        return _result(action, False, "I need the name of something to click.")
    if not isinstance(observed, Observed):
        return _result(action, False, f"I haven't found '{target.name.strip()}' on screen yet, "
                                      f"so there's nothing to click.",
                       log_message=f"{_TARGET} not observed yet")
    named = target.name.strip()
    try:
        window = verifier.window_by_handle(target.window_handle)
    except verifier.VerifierUnavailableError as exc:
        return _result(action, False, f"Didn't click '{named}': I can't check the screen ({exc}).",
                       log_message=f"{_TARGET}: the screen could not be read ({exc})")
    if window is None:
        return _result(action, False, f"Didn't click '{named}': that window isn't open any more.",
                       log_message=f"{_TARGET}: the window is gone")
    where = _describe_window(window, context)
    # The prompt names the user's own word for the control and the window it is in. Nothing read off the
    # screen goes in here: the accessible labels that made the match never left the verifier's adapter.
    safety_action = Action(f'click "{named}" in {where}',
                           minimum_level=_TARGET_CLICK_RISK, minimum_reason=_TARGET_CLICK_RISK_REASON)

    def run() -> ActionResult:
        emergency_stop.check()  # before the re-identification, which is allowed to take a moment
        found = observation.reidentify(target, observed)
        if not isinstance(found, ActionTarget):
            message, why = _refused(named, found)
            return _result(action, False, message, log_message=f"{_TARGET}: {why}")
        x, y = found.point
        try:
            all_screens = verifier.screens()
            if not verifier.on_screen(all_screens, x, y):
                return _result(action, False, f"'{named}' is at ({x}, {y}), which isn't on any screen, "
                                              f"so I didn't click it.",
                               log_message=f"{_TARGET}: ({x}, {y}) is on no screen")
            if (x, y) in _fail_safe_corners(all_screens):
                return _result(action, False, f"'{named}' is in a corner of the main screen, which is the "
                                              f"manual emergency stop, so I won't click there.",
                               log_message=f"{_TARGET}: ({x}, {y}) is a fail-safe corner")
            # UIA reports where a control IS, not whether anything is in front of it. An overlapping
            # window would take the click instead, so the window that is actually on top at that point
            # has to be the one the target belongs to.
            on_top = verifier.window_at(x, y)
        except verifier.VerifierUnavailableError as exc:
            return _result(action, False, f"Didn't click '{named}': I can't check the screen ({exc}).",
                           log_message=f"{_TARGET}: the screen could not be read ({exc})")
        if on_top is None or on_top.handle != found.window_handle:
            return _result(action, False, f"Something else is in front of '{named}' now, so I didn't click "
                                          f"it - the click would have gone to the wrong window.",
                           log_message=f"{_TARGET}: another window is in front of it")
        # The result names the window it acted in, so provenance stays VISIBLE even now that it is no
        # longer a gate. `log_what` is still _TARGET, so neither the control name nor the window title
        # reaches the log.
        return _send_click(action, x, y, f'"{named}" in {where}', _TARGET)

    return _Prepared(run, safety_action)


# --- click a control the user named (Phase 5 Slice 3) -------------------------------------------------
# This is the wiring, not a new capability. It answers one question the Brain is deliberately not
# allowed to answer - WHICH WINDOW - and then hands Slice 2's bridge exactly what it already takes.
#
# WHERE THE WINDOW COMES FROM, and why it is not the one in front. A window being visible says nothing
# about whether the user meant it, so the only windows considered are the ones this session OPENED and
# can still prove it owns: the same _session_windows records and the same ownership token that close_app
# requires. That record is only ever written after the Verifier confirmed the window appeared and the
# token was read back, which is why an open that failed, or one that could not be proved, cannot put a
# window within reach of a click.
#
# It refuses rather than choosing whenever that leaves more than one answer. Two Notepads open and no
# app named is not a 50/50 guess worth taking.

# --- the assistant's own browser: open and close ------------------------------------------------------
# Lifecycle only. Opening mirrors open_app (LOW - starting something changes no data), and closing
# mirrors close_app (a MEDIUM code constant), so no new risk policy is invented here.

def _resolve_open_browser(action: ExecutorAction) -> Resolved | Unresolved:
    return _no_target(action, "open_browser")


def _resolve_close_browser(action: ExecutorAction) -> Resolved | Unresolved:
    return _no_target(action, "close_browser")


def _resolve_navigate(action: ExecutorAction) -> Resolved | Unresolved:
    """The address lives in `url`, not in `target`, so `target` must be empty like the other
    browser kinds. The scheme is judged in the preparer, not here: resolve() is side-effect free
    and is also asked by the Phase 3 router, which must not need an opinion about a URL."""
    return _no_target(action, "navigate")


def _no_target(action: ExecutorAction, kind: str) -> Resolved | Unresolved:
    """These kinds take nothing: there is one assistant browser, or there is none."""
    if isinstance(action.target, str) and action.target.strip():
        return Unresolved(RESOLVE_UNWANTED_TARGET,
                          f"'{kind.replace('_', ' ')}' doesn't take anything after it.")
    return Resolved(None)


def _prepare_open_browser(action: ExecutorAction):
    resolution = resolve(action)
    if isinstance(resolution, Unresolved):
        return _result(action, False, resolution.message)
    if adapter.browser_sessions():
        return _result(action, False, "The assistant browser is already open, so I didn't open "
                                      "another one.")
    try:
        channel, launch_timeout = _browser_launch_settings()
    except SettingsError as exc:
        return _result(action, False, str(exc))

    def run() -> ActionResult:
        emergency_stop.check()
        try:
            # headed: the user is the one who will put a page in it, so they have to be able to see it.
            adapter.browser_open_session(channel, launch_timeout, True)
        except adapter.BrowserError as exc:
            return _result(action, False, f"I couldn't open the assistant browser ({exc}).")
        return _result(action, True, "Opened the assistant browser. It's mine, not your usual Chrome: "
                                     "no profile, no tabs, no saved logins. Put a page in it and tell "
                                     "me what to click.")

    return _Prepared(run)


# --- navigate the assistant's own browser (usability Slice 5) -----------------------------------------
# WHAT IS NEW HERE IS EGRESS. The provider path has used the network since Phase 3; this is the first
# way for the assistant to load an ARBITRARY website the user asked for. So the address is checked
# twice before anything can reach it, by two different owners:
#
#   app/planner/logic._url_provenance  - did this address come from the USER, or did the model make it
#                                        up? Checked against what the user typed, before a plan exists.
#   _navigable_url (below)             - is the scheme one we will touch at all? Checked HERE, in the
#                                        preparer, so an unacceptable address never reaches the
#                                        adapter function that can open a socket.
#
# WHAT A PAGE NEVER GAINS. Loading it grants it no authority: no page text, title, address or cookie is
# read back, none of it reaches the Brain, and the DOM click path still resolves only the control name
# the user themselves gave.

# http and https only, and the refusals are named rather than lumped together: a user who typed a
# file:// path deserves to know that is why, not "bad URL".
_ALLOWED_SCHEMES = ("http", "https")
_NAVIGATE_RISK = RiskLevel.MEDIUM
# Equally true of any address, including one the user typed perfectly. It is NOT about the URL being
# suspect - it is about what has not been seen yet, and about what the next action would act on.
_NAVIGATE_RISK_REASON = ("opening a web page - the page hasn't been seen yet, it may make further "
                         "requests of its own, and the next click would act on whatever loaded")


def _navigable_url(url) -> str | None:
    """The address if this assistant may open it, otherwise None. PURE: no network, no browser.

    Rejects anything that is not http or https - file:// reads the disk, javascript: and data: execute
    in the page, about: and the rest are browser-internal. A URL is parsed rather than string-matched
    so that "HTTPS://x" and "  https://x  " are the same answer, and so a scheme cannot be smuggled
    past a prefix check."""
    if not isinstance(url, str) or not url.strip():
        return None
    candidate = url.strip()
    try:
        parts = urllib.parse.urlsplit(candidate)
    except ValueError:
        return None
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        return None
    if not parts.netloc:                     # "https:///x" or "http://" - no host to go to
        return None
    return candidate


def _prepare_navigate(action: ExecutorAction):
    resolution = resolve(action)
    if isinstance(resolution, Unresolved):
        return _result(action, False, resolution.message)
    url = _navigable_url(action.url)
    if url is None:
        # Checked BEFORE the session lookup and before any adapter call, so a refused address never
        # reaches a function that could touch the network.
        return _result(action, False,
                       "I only open http and https web addresses, so I didn't open that one.",
                       log_message="navigate: refused the address's scheme")
    sessions = adapter.browser_sessions()
    if not sessions:
        return _result(action, False, "The assistant browser isn't open, so there's nothing to "
                                      "navigate. Say 'open assistant browser' first.")
    if len(sessions) > 1:
        return _result(action, False, "I have more than one assistant browser page open, so I don't "
                                      "know which to navigate.")
    session_id, page_id = sessions[0]
    try:
        timeout = _navigate_timeout()
    except SettingsError as exc:
        return _result(action, False, str(exc))
    # The URL IS shown, in full, because reading the address before it loads is the whole point of
    # asking. It is not logged: log_message below carries no address, and action.target is empty.
    safety_action = Action(f"navigate the assistant browser to {url}", minimum_level=_NAVIGATE_RISK,
                           minimum_reason=_NAVIGATE_RISK_REASON)

    def run() -> ActionResult:
        emergency_stop.check()
        try:
            outcome = adapter.browser_navigate(session_id, page_id, url, timeout)
        except adapter.BrowserError as exc:
            return _result(action, False, f"I couldn't navigate the assistant browser ({exc}).",
                           log_message="navigate: the browser refused")
        emergency_stop.check()
        if outcome == adapter.NAVIGATE_TIMEOUT:
            # NEVER "the page loaded". goto() may have navigated and then run out of time waiting for
            # the load event, so the page may be partly there - and the next click would act on it.
            # Outcome.UNVERIFIED is the existing way to say "it was sent, nothing confirms what it
            # achieved"; the session is left open because it is still usable.
            return _result(action, True,
                           f"I sent the assistant browser to that address, but it didn't finish "
                           f"loading within {timeout:g} seconds. Part of the page may be there and "
                           f"part may not, so I can't tell you it loaded. Look at the browser before "
                           f"clicking in it.", outcome=Outcome.UNVERIFIED,
                           log_message="navigate: timed out; the load was not confirmed")
        return _result(action, True,
                       "Sent the assistant browser to that address. I can't check what the page "
                       "contains, so look at it before clicking in it.", outcome=Outcome.UNVERIFIED,
                       log_message="navigate: the browser reported the navigation complete")

    return _Prepared(run, safety_action)


def _navigate_timeout() -> float:
    value = get_setting("browser.navigate_timeout_seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise SettingsError(f"Setting 'browser.navigate_timeout_seconds' must be a positive number of "
                            f"seconds, got {value!r}.")
    return float(value)


def _prepare_close_browser(action: ExecutorAction):
    resolution = resolve(action)
    if isinstance(resolution, Unresolved):
        return _result(action, False, resolution.message)
    sessions = adapter.browser_sessions()
    if not sessions:
        return _result(action, False, "The assistant browser isn't open, so there was nothing to "
                                      "close.")
    safety_action = Action(action.description, minimum_level=_CLOSE_RISK,
                           minimum_reason=_CLOSE_RISK_REASON)

    def run() -> ActionResult:
        emergency_stop.check()
        # Only sessions in the adapter's own registry - the configured personal Chrome is a launched
        # application with a window token, is in no part of this registry, and cannot be reached here.
        closed = sum(1 for session_id, _page in sessions if adapter.browser_close_session(session_id))
        if not closed:
            return _result(action, False, "The assistant browser was already gone.")
        return _result(action, True, "Closed the assistant browser.")

    return _Prepared(run, safety_action)


def _browser_launch_settings() -> tuple[str, float]:
    channel = get_setting("browser.channel")
    if not isinstance(channel, str) or not channel.strip():
        raise SettingsError(f"Setting 'browser.channel' must be a browser name, got {channel!r}.")
    timeout = get_setting("browser.launch_timeout_seconds")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise SettingsError(f"Setting 'browser.launch_timeout_seconds' must be a positive number, "
                            f"got {timeout!r}.")
    return channel.strip(), float(timeout)


def _prepare_named_click(action: ExecutorAction):
    resolution = resolve(action)
    if isinstance(resolution, Unresolved):
        return _result(action, False, resolution.message, log_message=f"{_TARGET}: {resolution.reason}")
    control, app = resolution.value
    context = _context_for_named_click(app)
    if isinstance(context, str):
        return _result(action, False, context, log_message=f"{_TARGET}: no single context to use")
    if isinstance(context, _BrowserPageContext):
        # Layer 2. The SAME preparer the DOM entry point uses - there is one DOM path, not a second.
        return _prepare_dom_click(action, DomTarget(control, context.session_id, context.page_id))

    window = context.window
    target = Target(control, window.handle)
    found = observation.resolve_target(target)
    if not isinstance(found, Found):
        message, why = _refused(control, found)
        return _result(action, False, message, log_message=f"{_TARGET}: {why}")
    # Found. From here it is Slice 2's path, unchanged: it builds the confirmation, re-identifies the
    # control after the user answers, and sends the one click. The only thing added is one step in
    # front of that run - bringing the owned window forward - which is why Slice 2's preparer is
    # COMPOSED rather than edited: the coordinate click's run must stay exactly as it was.
    prepared = _prepare_target_click(action, target, found.observed, context)
    if isinstance(prepared, ActionResult):
        return prepared
    return _Prepared(_activate_then(action, context, prepared.run), prepared.safety_action)


def _found_still_valid(action: ExecutorAction, context: "_AppWindowContext") -> ActionResult | None:
    """None if the found window's structural evidence STILL holds; otherwise the refusal.

    Run after the confirmation and before anything is activated or clicked, because that is the window
    in which the user authorized a click. An owned window does not need this: its token was checked
    when the context was chosen and the token cannot be inherited by another window."""
    try:
        expectation = verifier.expect_window(context.app)
        window = _found_now(context.app, expectation)
    except SettingsError as exc:
        return _result(action, False, str(exc), log_message=f"{_TARGET}: {exc}")
    except verifier.VerifierUnavailableError as exc:
        return _result(action, False, f"Didn't click: I can't check {context.app}'s window ({exc}).",
                       log_message=f"{_TARGET}: the found window could not be re-checked ({exc})")
    if window is None or window.handle != context.window.handle:
        return _result(action, False,
                       f"The {context.app} window I was going to click in isn't the same one any "
                       f"more, so I didn't click. Say that again and I'll look afresh.",
                       retryable=True,
                       log_message=f"{_TARGET}: the found window no longer matches its fingerprint")
    return None


def _activate_then(action: ExecutorAction, context: "_AppWindowContext", run):
    """Run `run` only once the window is genuinely in front.

    WHY THIS IS AFTER THE CONFIRMATION, and must be. The user answers the confirmation in the console,
    so the console has to stay in front until they have typed it - activating the target first would
    take the keyboard away from the very prompt being answered. Afterwards is also the one moment
    Windows is most likely to allow the change at all: our process is the foreground process and it
    just received the last input event, which are two of the documented conditions under which
    SetForegroundWindow is permitted.

    For a window the assistant only FOUND, the structural fingerprint is re-checked first - before any
    window is activated - because the confirmation the user just answered named THAT window."""
    def activate_then_run() -> ActionResult:
        emergency_stop.check()
        if not context.owned:
            refusal = _found_still_valid(action, context)
            if refusal is not None:
                return refusal             # nothing activated and nothing clicked
        refusal = _bring_window_forward(action, context.window)
        if refusal is not None:
            return refusal                 # nothing was clicked, and the result says to try again
        return run()
    return activate_then_run


# What happened when ONE window was asked to come to the front. The mechanism's answer, with no
# message in it, because the two callers say different things about the same outcome: a named click
# refuses, and open_app reports the app as available but not in front.
#
# The two UNREADABLE and the two GONE states are separate only so each caller can keep the exact log
# line it had before this was extracted - the user-facing text for each pair is identical.
_FRONT_IN_FRONT = "in_front"
_FRONT_GONE_BEFORE = "gone_before"              # window_state says it is no longer there
_FRONT_GONE_DURING = "gone_during"              # it closed during the activation call
_FRONT_MINIMIZED = "minimized"                  # deliberately NOT restored; see below
_FRONT_UNREADABLE_WINDOW = "unreadable_window"  # the window's own state could not be read
_FRONT_UNREADABLE_FOREGROUND = "unreadable_foreground"   # the foreground could not be read
_FRONT_SETTINGS = "settings"                    # the activation settings are invalid
_FRONT_ERROR = "error"                          # the activation call itself failed
_FRONT_REFUSED = "refused"                      # Windows would not give it the foreground


@dataclass(frozen=True)
class _Activation:
    """The result of _activate_to_front. `detail` carries an exception's text and NEVER a window
    title; `accepted` is what SetForegroundWindow itself claimed, for the log only."""
    state: str
    detail: str = ""
    accepted: bool | None = None


def _activate_to_front(handle: int) -> _Activation:
    """Bring ONE window to the front and verify it got there. THE activation path - there is no other.

    SetForegroundWindow only, exactly as the auto-focus slice decided. A minimized window is REPORTED
    and never restored: un-minimising someone's window is a change to their desktop that nobody asked
    for, and the mechanism for it would have to reach a background window, which window_control does
    not do. Asking is the honest option.

    SetForegroundWindow's own answer is not proof - it can report success and the window still not be
    in front - so the foreground is read back BY HANDLE until it is ours or the time runs out.

    It returns a state rather than an ActionResult so that one mechanism can serve two intents. The
    caller owns the wording."""
    try:
        state = verifier.window_state(handle)
    except verifier.VerifierUnavailableError as exc:
        return _Activation(_FRONT_UNREADABLE_WINDOW, str(exc))
    if state is None:
        return _Activation(_FRONT_GONE_BEFORE)
    if state.minimized:
        return _Activation(_FRONT_MINIMIZED)
    try:
        accepted = adapter.activate_window(handle)
    except adapter.WindowGoneError:
        return _Activation(_FRONT_GONE_DURING)
    except adapter.ExecutorAdapterError as exc:
        return _Activation(_FRONT_ERROR, str(exc))
    try:
        settle, poll = _activation_settings()
    except SettingsError as exc:
        return _Activation(_FRONT_SETTINGS, str(exc))
    deadline = _clock() + settle
    while True:
        try:
            front = verifier.active_target().window
        except verifier.VerifierUnavailableError as exc:
            return _Activation(_FRONT_UNREADABLE_FOREGROUND, str(exc))
        if front is not None and front.handle == handle:
            return _Activation(_FRONT_IN_FRONT, accepted=accepted)
        if _clock() >= deadline:
            return _Activation(_FRONT_REFUSED, accepted=accepted)
        if emergency_stop.wait(poll):      # interruptible: never a raw sleep
            emergency_stop.check()         # raises EmergencyStopError


def _bring_window_forward(action: ExecutorAction, window: WindowInfo) -> ActionResult | None:
    """None once `window` is in front; otherwise the result explaining why nothing was clicked.

    `window` is the EXACT one the caller's context already established - by the ownership token, or by
    the found window's structural fingerprint re-checked a moment ago - so no title is matched here and
    no other window can be brought forward by this path. (Renamed from _bring_owned_window_forward in
    Slice 3: the mechanism never cared about ownership, and the caller now may have either kind.)
    A refusal here is RETRYABLE on purpose: putting a window in front is something the user can do in
    a second, and the existing retry offer then asks them to.

    The mechanism is _activate_to_front; this function is only the click path's wording for it, and
    every message and log line below is unchanged from the auto-focus slice."""
    name = action.target.strip() or "that app"
    outcome = _activate_to_front(window.handle)
    state, detail = outcome.state, outcome.detail
    if state == _FRONT_IN_FRONT:
        return None
    if state == _FRONT_UNREADABLE_WINDOW:
        return _result(action, False, f"Didn't click: I can't check {name}'s window ({detail}).",
                       log_message=f"{_TARGET}: the window state could not be read ({detail})")
    if state == _FRONT_GONE_BEFORE:
        return _result(action, False, f"Didn't click: {name}'s window isn't open any more.",
                       log_message=f"{_TARGET}: the window closed before it could be brought forward")
    if state == _FRONT_MINIMIZED:
        return _result(action, False,
                       f"{name} is minimized, so I can't click in it. Bring it back up and say that "
                       f"again.", retryable=True,
                       log_message=f"{_TARGET}: the owned window is minimized")
    if state == _FRONT_GONE_DURING:
        return _result(action, False, f"Didn't click: {name}'s window isn't open any more.",
                       log_message=f"{_TARGET}: the window closed during activation")
    if state == _FRONT_ERROR:
        return _result(action, False, f"I couldn't bring {name} to the front ({detail}), so I didn't "
                                      f"click. Put it in front and say that again.", retryable=True,
                       log_message=f"{_TARGET}: activation failed ({detail})")
    if state == _FRONT_SETTINGS:
        return _result(action, False, detail, log_message=f"{_TARGET}: {detail}")
    if state == _FRONT_UNREADABLE_FOREGROUND:
        return _result(action, False, f"Didn't click: I can't check which window is in front "
                                      f"({detail}).",
                       log_message=f"{_TARGET}: the foreground could not be read ({detail})")
    return _result(action, False,
                   f"I couldn't bring {name} to the front, so I didn't click. Windows can "
                   f"refuse that while another window has it. Put {name} in front and say "
                   f"that again.", retryable=True,
                   log_message=f"{_TARGET}: foreground not acquired (accepted={outcome.accepted})")


def _activation_settings() -> tuple[float, float]:
    """How long to wait for the foreground, and how often to look.

    The poll interval is the Verifier's existing one rather than a second new setting: "how often to
    re-read the desktop" is already answered there, and answering it twice is how two numbers drift."""
    settle = get_setting("executor.activation_settle_seconds")
    if isinstance(settle, bool) or not isinstance(settle, (int, float)) or settle <= 0:
        raise SettingsError(f"Setting 'executor.activation_settle_seconds' must be a positive number, "
                            f"got {settle!r}.")
    poll = get_setting("verifier.poll_interval_seconds")
    if isinstance(poll, bool) or not isinstance(poll, (int, float)) or poll <= 0:
        raise SettingsError(f"Setting 'verifier.poll_interval_seconds' must be a positive number, "
                            f"got {poll!r}.")
    return float(settle), float(poll)


# Two DIFFERENT kinds of place a named click can happen, as two different types - so an opaque
# browser session id can never be handled as though it were a window handle, or the reverse.
#
# HWND JUSTIFICATION. For this first vertical, an explicitly selected assistant-browser page routes
# page-content targets directly to DOM. HWND mapping is not required for that scoped workflow.
# Browser chrome and native window controls remain UIA territory.


@dataclass(frozen=True)
class _AppWindowContext:
    """A window a named click may act in. Resolved through UI Automation.

    `owned` is the PROVENANCE and it is not a permission: True when the ownership token proves this
    session created the window, False when open_app only found it already on the desktop. From Slice 3
    both may be clicked; only an owned one may be closed. The flag exists so the confirmation can say
    which it is, because that sentence is now what authorizes the click."""
    app: str
    window: WindowInfo
    owned: bool = True


@dataclass(frozen=True)
class _BrowserPageContext:
    """A page in the assistant's own browser. Resolved through the DOM.

    Opaque ids only - there is no window handle here, because a page is not a window and this slice
    maps neither to the other."""
    session_id: str
    page_id: str


def _context_for_named_click(app: str) -> "_AppWindowContext | _BrowserPageContext | str":
    """WHERE a named click should happen, or a message saying why there is no single answer.

    The one rule: when the user did not say, there has to be exactly ONE candidate. Two is not a
    preference to apply quietly - it is a question to ask, which is why a live assistant browser
    beside an owned app window refuses rather than choosing either."""
    sessions = adapter.browser_sessions()
    if app == ASSISTANT_BROWSER:
        if not sessions:
            return ("I don't have an assistant browser open, so there's no page to click in. Say "
                    "'open assistant browser' first.")
        if len(sessions) > 1:
            return ("I have more than one assistant browser page open, so I don't know which you "
                    "mean.")
        session_id, page_id = sessions[0]
        return _BrowserPageContext(session_id=session_id, page_id=page_id)

    with _session_lock:
        opened = {name: list(groups) for name, groups in _session_windows.items() if groups}
        found_apps = sorted(_found_windows)
    # A window open_app FOUND is a candidate here from Slice 3 on, and it is a candidate on exactly
    # the same terms as any other - it does not get its own shortcut and it cannot skip the ambiguity
    # rule below. What it does NOT become is ownership proof: see the two branches further down, where
    # an owned window is preferred and a found one is re-checked structurally before it is used.
    available = sorted(set(opened) | set(found_apps))
    if app and app not in available:
        return (f"I don't have a window of {app} to click in - I haven't opened it in this session "
                f"and open_app hasn't put one in front. Open it first.")
    if not app:
        candidates = available + ([ASSISTANT_BROWSER] if sessions else [])
        if not candidates:
            return ("I haven't opened anything yet in this session, so I don't know which window you "
                    "mean. Open the app first, or say which app to click in.")
        if len(candidates) > 1:
            if sessions:
                # The mixed case: an app window AND a page. Naming the reserved selector matters,
                # because without it the user has no words for the browser.
                return (f"I've opened more than one thing in this session "
                        f"({', '.join(candidates)}), so I don't know which one you mean. Say which - "
                        f"for example 'in {ASSISTANT_BROWSER}', or the app's name.")
            if set(candidates) <= set(opened):
                return (f"I've opened more than one app in this session ({', '.join(candidates)}), so "
                        f"I don't know which one you mean. Say which app to click in.")
            # At least one candidate is a window we only found, so "opened" would be untrue.
            return (f"More than one app is available to click in ({', '.join(candidates)}), so I "
                    f"don't know which one you mean. Say which app to click in.")
        if candidates == [ASSISTANT_BROWSER]:
            session_id, page_id = sessions[0]
            return _BrowserPageContext(session_id=session_id, page_id=page_id)
        app = candidates[0]

    try:
        expectation = verifier.expect_window(app)
    except SettingsError as exc:
        return str(exc)

    # OWNED FIRST, always. A window the ownership token proves is preferred over one we merely found,
    # so "in chrome" can never quietly pick a stranger while a window we opened is sitting there.
    if app in opened:
        try:
            windows = [window for group in opened[app] for window in _ours_now(group, expectation)]
        except verifier.VerifierUnavailableError as exc:
            return f"I can't check {app}'s windows right now ({exc})."
        if len(windows) > 1:
            return (f"I have {len(windows)} {app} windows open from this session, so I don't know "
                    f"which one you mean.")
        if windows:
            return _AppWindowContext(app=app, window=windows[0], owned=True)

    # Then the ONE window open_app recorded, if its structural evidence still holds. _found_now looks
    # only at that recorded handle - it never scans for "a chrome window", so this cannot silently
    # select a different one.
    try:
        window = _found_now(app, expectation)
    except verifier.VerifierUnavailableError as exc:
        return f"I can't check {app}'s windows right now ({exc})."
    if window is not None:
        return _AppWindowContext(app=app, window=window, owned=False)

    if app in opened:
        return (f"I opened {app} earlier, but I can't find a window of it that I can still prove is "
                f"mine, so I won't click in it.")
    return (f"The {app} window I had isn't there any more - it closed, or it isn't the same window. "
            f"Say 'open {app}' again.")


# --- click a control the user named, in the assistant's own browser (Phase 5 DOM Slice 2) -------------
# Layer 2 of the frozen hierarchy gets an action, and it reuses everything layer 1 already proved: the
# same prepare -> authorize -> run seam, the same yes-only MEDIUM confirmation, the same emergency-stop
# checkpoints, the same honest UNVERIFIED result.
#
# NO NEW ACTION KIND, deliberately. Adding one would mean adding it to the Brain's vocabulary, which
# this slice must not do - and two tests already hold ARGS_FOR_KIND and _PREPARERS to the same set. So
# this is an entry point rather than a dispatch entry, and the result's ExecutorAction carries the
# user's word in `control` with an EMPTY target, so log_label stays empty and nothing leaks.
#
# WHAT IS NOT HERE. No Playwright: the library lives in the adapter, and this module only sequences.
# No URL, no selector, no accessible name read back off the page - the only name in play is the one
# the caller was given by the user, which is why the confirmation may say it.

_DOM_CLICK_RISK = RiskLevel.MEDIUM
_DOM_CLICK_RISK_REASON = ("click on a page control - always needs confirmation (what the control does "
                          "can't be known from its name)")
_DOM = "dom target"          # how this path is described in LOGS: a layer, never a name or an address


def click_dom_target(target: DomTarget, confirm: Confirm | None = None, *,
                     risk_floor: RiskLevel = RiskLevel.LOW) -> ActionResult:
    """Click the control the user named, in an assistant-owned browser page.

    `target` carries the user's own words and the opaque session/page the caller already opened. There
    is no command for this yet and no Brain wiring: the local primitive is built and proved first."""
    emergency_stop.check()
    # An empty target keeps log_label empty; `control` is the user's own word, shown but never logged.
    action = ExecutorAction(CLICK_TARGET, "", target.name.strip() if isinstance(target, DomTarget)
                            and isinstance(target.name, str) else "")
    return _authorize_and_run(action, _prepare_dom_click(action, target), confirm, risk_floor)


def _prepare_dom_click(action: ExecutorAction, target: DomTarget):
    if not isinstance(target, DomTarget) or not isinstance(target.name, str) or not target.name.strip():
        return _result(action, False, "I need the name of something to click.")
    named = target.name.strip()
    try:
        query_timeout = _browser_query_timeout()
    except SettingsError as exc:
        return _result(action, False, str(exc), log_message=f"{_DOM}: {exc}")

    try:
        elements = adapter.dom_query(target.session_id, target.page_id,
                                     normalize_name(target.name), query_timeout)
        has_frames = adapter.dom_page_has_frames(target.session_id, target.page_id)
    except adapter.BrowserError as exc:
        return _result(action, False, f"Didn't click '{named}': {exc}.",
                       log_message=f"{_DOM}: the page could not be read ({exc})")

    found = observation.resolve_dom_target(target, elements, has_frames)
    if not isinstance(found, Found):
        message, why = _dom_refused(named, found)
        return _result(action, False, message, log_message=f"{_DOM}: {why}")

    token = found.observed.runtime_id          # the opaque element token; meaningless outside the adapter
    safety_action = Action(f'click "{named}" in the assistant browser',
                           minimum_level=_DOM_CLICK_RISK, minimum_reason=_DOM_CLICK_RISK_REASON)

    def run() -> ActionResult:
        emergency_stop.check()                 # last checkpoint before the input is sent
        try:
            outcome = adapter.dom_click(target.session_id, target.page_id, token, query_timeout)
        except adapter.BrowserError as exc:
            return _result(action, False, f"I couldn't click '{named}': {exc}.",
                           log_message=f"{_DOM}: the click failed ({exc})")
        finally:
            # Playwright's click is a blocking call into its driver and cannot be interrupted part
            # way. The honest guarantee is therefore before and after, not during - and checking
            # afterwards is what stops a plan continuing past a stop pressed while it ran.
            emergency_stop.check()
        if outcome != adapter.DOM_CLICKED:
            message, why = _dom_stale(named, outcome)
            return _result(action, False, message, log_message=f"{_DOM}: {why}")
        return _result(action, True, f"Clicked \"{named}\" in the assistant browser. I can't check "
                                     f"what the click did.", outcome=Outcome.UNVERIFIED,
                       log_message=f"{_DOM}: clicked")

    return _Prepared(run, safety_action)


def _dom_refused(named: str, found) -> tuple[str, str]:
    """Why a DOM target was not clicked, before the confirmation: what the USER is told, and what a LOG
    may keep. Two sentences because they have different audiences - the log never gets the name."""
    if isinstance(found, Ambiguous):
        return found.message, f"ambiguous ({len(found.candidates)} matches)"
    if isinstance(found, FramesNotSupported):
        return found.reason, "not found, and the page has frames that are not read"
    if isinstance(found, NotFound):
        return found.message, "no such control on that page"
    if isinstance(found, Unavailable):
        return f"Didn't click '{named}': {found.reason}", "the page could not be observed"
    return f"Didn't click '{named}': I couldn't find it.", "unrecognised resolution"


def _dom_stale(named: str, outcome: str) -> tuple[str, str]:
    """Why a re-resolution after the confirmation refused. Every branch clicked NOTHING."""
    if outcome == adapter.DOM_PAGE_CHANGED:
        return (f"That page changed while you were answering, so I didn't click '{named}'. Ask again "
                f"if you still want it.", "the page changed after the confirmation")
    if outcome == adapter.DOM_AMBIGUOUS:
        return (f"There is more than one '{named}' on that page now, so I'm not going to guess which "
                f"one you meant.", "ambiguous after the confirmation")
    if outcome == adapter.DOM_ROLE_CHANGED:
        return (f"The '{named}' on that page isn't the same kind of control any more, so I didn't "
                f"click it.", "the control's role changed after the confirmation")
    if outcome == adapter.DOM_FRAMES_UNREAD:
        return (f"'{named}' isn't in the main part of that page any more, and the rest of it is "
                f"frames I can't read yet - so I didn't click.", "frames unread after the confirmation")
    return (f"'{named}' wasn't there any more by the time you answered, so I didn't click.",
            "gone after the confirmation")


def _browser_query_timeout() -> float:
    """One timeout for reading a page and for clicking in it.

    Deliberately not a second setting. A click waits for actionability where a query only reads, so
    these are not quite the same concept - but two numbers that drift apart are worse than one that is
    slightly generous, and if a real click needs longer than a real query, a separate
    browser.action_timeout_seconds is the first thing to add."""
    value = get_setting("browser.query_timeout_seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise SettingsError(f"Setting 'browser.query_timeout_seconds' must be a positive number, "
                            f"got {value!r}.")
    return float(value)


def _refused(named: str, found) -> tuple[str, str]:
    """Why a re-identified target was not clicked: what the USER is told, and what a LOG may keep.

    Two sentences rather than one because they have different audiences. The user's needs their own word
    for the control in it to make sense; the log must not have it, because that word is command text.

    Every branch refuses. The Executor never falls back to the coordinates it was shown before the
    confirmation, because those describe where the control WAS."""
    if isinstance(found, NotEligible):
        return found.reason, "not eligible to be clicked"
    if isinstance(found, Stale):
        return found.reason, "a different control now answers to that name"
    if isinstance(found, Ambiguous):
        return found.message, f"ambiguous now ({len(found.candidates)} matches)"
    if isinstance(found, NotFound):
        return found.message, "no longer in that window"
    if isinstance(found, Unavailable):
        return f"Didn't click '{named}': {found.reason}", "the window could not be read again"
    return f"Didn't click '{named}': I couldn't find it again.", "unrecognised re-identification result"


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
    resolution = resolve(action)
    if isinstance(resolution, Unresolved):
        return _result(action, False, resolution.message)
    text = resolution.value
    try:
        interval = _typing_interval()
    except SettingsError as exc:
        return _result(action, False, str(exc))
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
    resolution = resolve(action)
    if isinstance(resolution, Unresolved):
        return _result(action, False, resolution.message)
    shortcut = resolution.value
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
    resolution = resolve(action)
    if isinstance(resolution, Unresolved):
        return _result(action, False, resolution.message)
    direction, requested = resolution.value
    limit, unclassified_limit, interval = _scroll_settings()
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
    resolution = resolve(action)
    if isinstance(resolution, Unresolved):
        return _result(action, False, resolution.message)
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
    resolution = resolve(action)
    if isinstance(resolution, Unresolved):
        return _result(action, False, resolution.message)
    operation = resolution.value
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
        # The active window must itself still carry the token: its handle being one we recorded proves
        # nothing on its own, because Windows reuses handle numbers for new windows.
        if handle in group.handles and adapter.window_token(handle) == group.token:
            expectation = verifier.expect_window(app)
            if _ours_now(group, expectation):
                return _SessionGroup(app, group, expectation)
    return None


# --- Helpers ----------------------------------------------------------------------------

_RESOLVERS = {OPEN_APP: _resolve_open_app, CLOSE_APP: _resolve_close_app, CLICK: _resolve_click,
              CLICK_TARGET: _resolve_click_target,
              OPEN_BROWSER: _resolve_open_browser, CLOSE_BROWSER: _resolve_close_browser,
              SCROLL: _resolve_scroll, SHORTCUT: _resolve_shortcut, REFRESH: _resolve_refresh,
              WINDOW_CONTROL: _resolve_window_control, TYPE_TEXT: _resolve_type_text,
              NAVIGATE: _resolve_navigate}

_PREPARERS = {OPEN_APP: _prepare_open_app, CLOSE_APP: _prepare_close_app, CLICK: _prepare_click,
              CLICK_TARGET: _prepare_named_click,
              OPEN_BROWSER: _prepare_open_browser, CLOSE_BROWSER: _prepare_close_browser,
              TYPE_TEXT: _prepare_type_text, SHORTCUT: _prepare_shortcut, SCROLL: _prepare_scroll,
              REFRESH: _prepare_refresh, WINDOW_CONTROL: _prepare_window_control,
              NAVIGATE: _prepare_navigate}


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
            outcome: Outcome | None = None, progress: tuple[int, int] | None = None, *,
            log_message: str | None = None) -> ActionResult:
    """Build the result, and log it.

    Every message passed here is LOGGED, which is why no message in this module contains typed text.
    `log_message` is for the one case where what the user should read and what a log file may keep are
    not the same sentence: a click on a screen target says the user's own word for the control back to
    them, and that word is command text, which this project does not write to disk."""
    result = ActionResult(action, ok, message, retryable, outcome, progress)
    log.log(logging.INFO if ok else logging.WARNING, "Executor %s '%s': %s (%s) - %s",
            action.kind, action.log_label, "OK" if ok else "FAILED", result.outcome.value,
            message if log_message is None else log_message)
    return result
