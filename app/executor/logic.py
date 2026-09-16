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

Phase 1 is built one action at a time: open_app, then close_app.

close_app closes ONLY windows the assistant opened in this session. open_app remembers them in
memory, so nothing carries over a restart, and windows the user opened are never touched. Closing
is a polite request (like clicking the window's X), never ending a process, and outcomes stay
distinct (models.Outcome): a window already gone is ALREADY_CLOSED, one showing a dialog such as
"Save changes?" is NEEDS_USER, and one that stays open is STILL_OPEN. Neither of the last two is
ever retryable, so the recovery loop can't retry into an app that is waiting for the user.
Measured real-desktop behavior and known open decisions: docs/step4 Section 4, implementation notes.
"""
import logging
import threading
from typing import Callable

from app.executor import adapter, emergency_stop
from app.executor.emergency_stop import EmergencyStopError
from app.executor.models import CLOSE_APP, OPEN_APP, ActionResult, ExecutorAction, Outcome
from app.safety.logic import Confirm, authorize
from app.safety.models import Action
from app.verifier import logic as verifier
from app.verifier.models import WindowExpectation, WindowInfo
from config.settings import SettingsError, get_setting

log = logging.getLogger(__name__)

OfferRetry = Callable[[ActionResult], bool]

# The outer window of a Windows Store app such as Calculator, which also has a same-titled content
# window. Asking the frame to close closes the app; the Verifier still waits for the content window.
_FRAME_WINDOW_CLASS = "ApplicationFrameWindow"

# Windows the assistant opened in this session: app name -> window groups, oldest first.
_session_windows: dict[str, list[frozenset[int]]] = {}
_session_lock = threading.Lock()


def execute(action: ExecutorAction, confirm: Confirm | None = None) -> ActionResult:
    """Carry out one action once, or explain why not. See the module docstring for the order."""
    emergency_stop.check()
    prepare = _PREPARERS.get(action.kind)
    if prepare is None:
        return _result(action, False, f"I don't know how to do '{action.kind}' yet.")
    prepared = prepare(action)  # validation only - nothing happens on the computer here
    if isinstance(prepared, ActionResult):
        return prepared
    authorize(Action(action.description), confirm)  # raises ActionDeniedError when denied
    emergency_stop.check()
    return prepared()


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
                 action.kind, action.target.strip(), attempt, max_attempts)


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


# --- Helpers ----------------------------------------------------------------------------

_PREPARERS = {OPEN_APP: _prepare_open_app, CLOSE_APP: _prepare_close_app}


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
            outcome: Outcome | None = None) -> ActionResult:
    result = ActionResult(action, ok, message, retryable, outcome)
    log.log(logging.INFO if ok else logging.WARNING, "Executor %s '%s': %s (%s) - %s",
            action.kind, action.target.strip(), "OK" if ok else "FAILED", result.outcome.value, message)
    return result
