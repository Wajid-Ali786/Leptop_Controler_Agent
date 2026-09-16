"""
Verifier decision-making: did the expected result actually happen? (docs/step4 Section 4)

Phase 1 verifies "an app's window appeared": a NEW visible top-level window whose title matches
the app's configured pattern (verifier.app_windows). It deliberately does not check process
liveness - notepad.exe and calc.exe hand off to another process and exit immediately on
Windows 11. Windows that were already open before the action (snapshot_windows) never count,
so an existing Notepad window can't be mistaken for success.

wait_for_new_window() polls until the window appears or verifier.window_timeout_seconds runs out,
sleeping with the emergency stop's interruptible wait. It never reports success it didn't
observe: if the desktop can't be checked, that is a failure.
"""
import logging
import re
import time

from app.executor import emergency_stop
from app.verifier import adapter
from app.verifier.models import VerificationResult, WindowExpectation
from config.settings import SettingsError, get_setting

log = logging.getLogger(__name__)
_now = time.monotonic  # replaced in tests if a controllable clock is needed


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
        if new:
            log.info("Verifier: %s window appeared after %.2fs", name, elapsed)
            return VerificationResult(True, f"{name}'s window appeared after {elapsed:.1f}s.",
                                      elapsed_seconds=elapsed, window_handle=new[0].handle)
        remaining = timeout - elapsed
        if remaining <= 0:
            log.warning("Verifier: no new %s window within %gs", name, timeout)
            return VerificationResult(
                False, f"{name} was started, but no new window appeared within {timeout:g} seconds.",
                retryable=True, elapsed_seconds=elapsed)
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
