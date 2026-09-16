"""
Verifier decision-making: did the expected result actually happen? (docs/step4 Section 4)

Phase 1 verifies "an app's window appeared": a NEW visible top-level window whose title matches
the app's configured pattern (verifier.app_windows). It deliberately does not check process
liveness - notepad.exe and calc.exe hand off to another process and exit immediately on
Windows 11. Windows that were already open before the action (snapshot_windows) never count,
so an existing Notepad window can't be mistaken for success.

wait_for_new_window() polls until the window appears or verifier.window_timeout_seconds runs out,
sleeping with the emergency stop's interruptible wait. It never reports success it didn't
observe: if the desktop can't be checked, that is a failure. A window only counts as appeared once
it is actually on screen - not "cloaked". A Store app (e.g. Calculator) first creates its frame
window cloaked, then a same-titled content window that is briefly a separate top-level window, and
only then shows the frame. Every new matching window present at that moment, shown or cloaked, is
reported as one group, so the content window belongs to the app's group. An ordinary app such as
Notepad is on screen the moment its window exists, so this adds no wait.

Closing is verified the other way round: wait_for_windows_to_close() succeeds only when every
window in the group is gone - including, via hosted_windows(), titled windows living inside the
group's windows, because a Store app's content window moves out of its frame again while closing.
Measured timings, the latency cost and the pre-launched-Calculator open decision: docs/step4
Section 4, implementation notes. A window left disabled (a modal dialog such as "Save changes?" is
waiting for the user) is reported as needing the user, not as a failure to retry. A handle that
now belongs to a window whose title no longer matches counts as gone - Windows reuses handles.
"""
import logging
import re
import time

from app.executor import emergency_stop
from app.verifier import adapter
from app.verifier.models import VerificationResult, WindowExpectation, WindowInfo
from config.settings import SettingsError, get_setting

log = logging.getLogger(__name__)
_now = time.monotonic  # replaced in tests if a controllable clock is needed
_BLOCKED_POLLS = 2  # consecutive polls a window must stay disabled before it counts as waiting for the user


class VerifierUnavailableError(Exception):
    """The desktop can't be observed, so nothing can be verified."""


def expect_window(app_name: str) -> WindowExpectation:
    """What opening `app_name` must produce, from config. SettingsError if missing or invalid."""
    patterns = get_setting("verifier.app_windows")
    if not isinstance(patterns, dict):
        raise SettingsError(
            f"Setting 'verifier.app_windows' must map app names to window-title patterns, got {patterns!r}.")
    by_name = {str(name).strip().lower(): pattern for name, pattern in patterns.items()}
    pattern = by_name.get(app_name.strip().lower())
    if not isinstance(pattern, str) or not pattern.strip():
        raise SettingsError(
            f"No window title pattern for '{app_name}' in verifier.app_windows, so opening it can't be verified.")
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise SettingsError(
            f"The verifier.app_windows pattern for '{app_name}' isn't a valid regular expression ({exc}).") from None
    return WindowExpectation(
        app_name=app_name.strip().lower(),
        pattern=compiled,
        timeout_seconds=_positive_number("verifier.window_timeout_seconds"),
        poll_interval_seconds=_positive_number("verifier.poll_interval_seconds"),
    )


def snapshot_windows(expectation: WindowExpectation) -> frozenset[int]:
    """Handles of matching windows open right now. Take this BEFORE the action."""
    return frozenset(w.handle for w in _list_windows() if expectation.pattern.search(w.title))


def wait_for_new_window(expectation: WindowExpectation, before: frozenset[int]) -> VerificationResult:
    """Wait for a matching window that wasn't in `before`. Raises EmergencyStopError if stopped."""
    name, timeout = expectation.app_name, expectation.timeout_seconds
    start = _now()
    while True:
        try:
            new = [w for w in _list_windows()
                   if w.handle not in before and expectation.pattern.search(w.title)]
        except VerifierUnavailableError as exc:
            return VerificationResult(False, f"Couldn't check whether {name}'s window appeared ({exc}).")
        elapsed = _now() - start
        shown = [w for w in new if not w.cloaked]
        if shown:
            log.info("Verifier: %s window appeared after %.2fs (%d window(s) in its group)", name, elapsed, len(new))
            return VerificationResult(True, f"{name}'s window appeared after {elapsed:.1f}s.",
                                      elapsed_seconds=elapsed, window_handle=shown[0].handle,
                                      window_handles=frozenset(w.handle for w in new))
        remaining = timeout - elapsed
        if remaining <= 0:
            if new:
                log.warning("Verifier: new %s window stayed off screen (cloaked) for %gs", name, timeout)
                message = f"{name} was started, but its window didn't appear on screen within {timeout:g} seconds."
            else:
                log.warning("Verifier: no new %s window within %gs", name, timeout)
                message = f"{name} was started, but no new window appeared within {timeout:g} seconds."
            return VerificationResult(False, message, retryable=True, elapsed_seconds=elapsed)
        if emergency_stop.wait(min(expectation.poll_interval_seconds, remaining)):
            emergency_stop.check()


def find_open(expectation: WindowExpectation, handles: frozenset[int]) -> list[WindowInfo]:
    """The windows among `handles` that are still open and still match the app's title pattern.
    Raises VerifierUnavailableError if the desktop can't be observed."""
    return [w for w in _list_windows() if w.handle in handles and expectation.pattern.search(w.title)]


def hosted_windows(expectation: WindowExpectation, handles: frozenset[int]) -> frozenset[int]:
    """Handles of titled windows matching the app's pattern that live inside the windows `handles`
    (e.g. a Store app's content window inside its frame). Raises VerifierUnavailableError."""
    try:
        return frozenset(child.handle for handle in handles for child in adapter.list_child_windows(handle)
                         if expectation.pattern.search(child.title))
    except adapter.VerifierAdapterError as exc:
        raise VerifierUnavailableError(str(exc)) from None


def wait_for_windows_to_close(expectation: WindowExpectation, handles: frozenset[int]) -> VerificationResult:
    """Wait until none of `handles` is open. Raises EmergencyStopError if stopped."""
    name, timeout = expectation.app_name, expectation.timeout_seconds
    start = _now()
    blocked_polls = 0
    while True:
        try:
            still_open = find_open(expectation, handles)
        except VerifierUnavailableError as exc:
            return VerificationResult(False, f"Asked {name} to close, but couldn't check whether it closed ({exc}).")
        elapsed = _now() - start
        if not still_open:
            log.info("Verifier: %s window closed after %.2fs", name, elapsed)
            return VerificationResult(True, f"{name}'s window closed after {elapsed:.1f}s.", elapsed_seconds=elapsed)
        # Disabled on two polls in a row, so a window briefly disabled while it shuts down isn't misreported.
        blocked_polls = blocked_polls + 1 if any(not w.enabled for w in still_open) else 0
        if blocked_polls >= _BLOCKED_POLLS:
            log.warning("Verifier: %s is showing a dialog after the close request; left open", name)
            return VerificationResult(
                False, f"{name} is showing a dialog (probably asking whether to save your changes), "
                       f"so I left it open for you to decide.", elapsed_seconds=elapsed, needs_user=True)
        remaining = timeout - elapsed
        if remaining <= 0:
            log.warning("Verifier: %s window still open %gs after the close request", name, timeout)
            return VerificationResult(
                False, f"Asked {name} to close, but it's still open after {timeout:g} seconds. "
                       f"It may be waiting for you.", elapsed_seconds=elapsed)
        if emergency_stop.wait(min(expectation.poll_interval_seconds, remaining)):
            emergency_stop.check()


def _list_windows():
    try:
        return adapter.list_windows()
    except adapter.VerifierAdapterError as exc:
        raise VerifierUnavailableError(str(exc)) from None


def _positive_number(name: str) -> float:
    value = get_setting(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise SettingsError(f"Setting '{name}' must be a positive number, got {value!r}.")
    return float(value)
