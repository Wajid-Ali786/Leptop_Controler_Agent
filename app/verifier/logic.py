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
A window left disabled (a modal dialog such as "Save changes?" is waiting for the user) is reported
as needing the user, not as a failure to retry. A handle that now belongs to a window whose title no
longer matches counts as gone - Windows reuses handles.

Coordinate clicks are NOT verified: their effect can't be observed in Phase 1. For them the Verifier
only answers facts - which screens exist, which window is at a point, where the pointer is - that
the Executor uses to refuse a click or to catch one that went to the wrong place.

Typed text is checked as far as Phase 1 honestly can: the focused field's text is read before and
after typing, and the typing counts as confirmed only when the exact text (line endings normalised)
now appears MORE times than before. A field that can't be read, or text that doesn't show up (apps
auto-correct, auto-indent or replace a selection), is "unverified" - never a failure to retry, since
retyping would duplicate text. Field text is compared in memory only and never logged.

Measured timings, the latency cost and the pre-launched-Calculator open decision: docs/step4
Section 4, implementation notes.
"""
import logging
import re
import time

from app.executor import emergency_stop
from app.verifier import adapter
from app.verifier.models import (ActiveTarget, ControlInfo, Screen, ScrollState, VerificationResult,
                                 WindowExpectation, WindowInfo)
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


# --- Screen points (for coordinate clicks) -----------------------------------------------------
# A coordinate click's EFFECT can't be observed in Phase 1, so nothing here confirms a click worked.
# These only answer factual questions the Executor uses to refuse or to catch a misdelivered click.

def screens() -> list[Screen]:
    """Every monitor's area. Raises VerifierUnavailableError."""
    return _observe(adapter.list_screens)


def on_screen(all_screens: list[Screen], x: int, y: int) -> bool:
    """Is (x, y) on an actual monitor? Gaps between monitors of different sizes don't count."""
    return any(screen.contains(x, y) for screen in all_screens)


def window_at(x: int, y: int) -> WindowInfo | None:
    """The top-level window at (x, y), or None. Raises VerifierUnavailableError."""
    return _observe(adapter.window_at, x, y)


def cursor_position() -> tuple[int, int]:
    """Where the mouse pointer is. Raises VerifierUnavailableError."""
    return _observe(adapter.cursor_position)


def active_target() -> ActiveTarget:
    """The active window and its focused control. Raises VerifierUnavailableError."""
    return _observe(adapter.active_target)


# Typed-text check results
TEXT_CONFIRMED, TEXT_UNREADABLE, TEXT_NOT_FOUND = "confirmed", "unreadable", "not_found"


def count_text(control_handle: int | None, text: str) -> int | None:
    """How many times `text` appears in the control's text right now, or None if it can't be read."""
    if not control_handle:
        return None
    try:
        limit = get_setting("verifier.max_read_characters")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            return None
        content = adapter.read_text(control_handle, limit)
    except (adapter.VerifierAdapterError, SettingsError):
        return None
    return None if content is None else _normalise(content).count(_normalise(text))


def wait_for_typed_text(control_handle: int | None, text: str, count_before: int | None) -> str:
    """After typing: TEXT_CONFIRMED once `text` appears more often than `count_before`, TEXT_UNREADABLE
    if the field can't be read (before or now), TEXT_NOT_FOUND if it didn't appear within
    verifier.text_settle_seconds. Raises EmergencyStopError if stopped while waiting."""
    if count_before is None:
        return TEXT_UNREADABLE
    settle = _positive_number("verifier.text_settle_seconds")
    poll = _positive_number("verifier.poll_interval_seconds")
    start = _now()
    while True:
        count = count_text(control_handle, text)
        if count is None:
            return TEXT_UNREADABLE
        if count > count_before:
            return TEXT_CONFIRMED
        remaining = settle - (_now() - start)
        if remaining <= 0:
            return TEXT_NOT_FOUND
        if emergency_stop.wait(min(poll, remaining)):
            emergency_stop.check()


# --- Keyboard shortcuts: facts only; clipboard CONTENTS are never read ----------------------

_EDIT_CLASSES = ("edit", "richedit")  # controls that answer EM_GETSEL


def modifiers_held() -> list[str]:
    """Which of Ctrl, Alt, Shift and Win are held down. Raises VerifierUnavailableError."""
    return _observe(adapter.modifier_keys_down)


def clipboard_sequence() -> int | None:
    """The clipboard's change counter, or None if it can't be read. Reads no content."""
    try:
        return adapter.clipboard_sequence_number()
    except adapter.VerifierAdapterError:
        return None


def clipboard_kinds() -> list[str]:
    """Kinds of data on the clipboard ("text", "image", "files", "other data"). Raises
    VerifierUnavailableError. Reads no content."""
    return _observe(adapter.clipboard_kinds)


def field_text_length(control_handle: int | None) -> int | None:
    """Length of the field's text, or None if it can't be read."""
    if not control_handle:
        return None
    try:
        return adapter.text_length(control_handle)
    except adapter.VerifierAdapterError:
        return None


def everything_selected(target: ActiveTarget) -> bool | None:
    """True when a standard edit field has all its text selected, False when not, None when this
    field can't report its selection."""
    if not target.control_handle or not target.control_class.lower().startswith(_EDIT_CLASSES):
        return None
    try:
        length = adapter.text_length(target.control_handle)
        selected = adapter.selection(target.control_handle)
    except adapter.VerifierAdapterError:
        return None
    if length is None or selected is None or length > 0xFFFF:  # EM_GETSEL can only report 65535
        return None
    return selected == (0, length)


def wait_until(check, settle_setting: str = "verifier.shortcut_settle_seconds") -> bool | None:
    """Poll check() - returning True, False, or None for "can't tell" - until it is True or the settle
    time runs out. Returns True, or None if the last answer was "can't tell", else False. Raises
    EmergencyStopError if stopped while waiting."""
    settle = _positive_number(settle_setting)
    poll = _positive_number("verifier.poll_interval_seconds")
    start = _now()
    while True:
        answer = check()
        if answer is True:
            return True
        remaining = settle - (_now() - start)
        if remaining <= 0:
            return answer
        if emergency_stop.wait(min(poll, remaining)):
            emergency_stop.check()


# --- Scrolling: what is under the pointer, and a standard scroll bar's position ----------------

# Standard Windows controls whose VALUE the mouse wheel changes, with how a prompt names them. Checked on
# the control under the pointer AND its parents, so e.g. the Edit inside a ComboBox counts too.
VALUE_CHANGING_CONTROLS = {
    "combobox": "a drop-down list", "comboboxex32": "a drop-down list", "msctls_trackbar32": "a slider",
    "msctls_updown32": "a spin box", "sysdatetimepick32": "a date picker",
}


def control_chain_at_pointer() -> tuple[tuple[int, int], list[ControlInfo]]:
    """The pointer position and the control chain under it (control first, top-level window last).
    Raises VerifierUnavailableError."""
    position = _observe(adapter.cursor_position)
    return position, _observe(adapter.control_chain_at, *position)


def value_changing_control(chain: list[ControlInfo]) -> str | None:
    """How a prompt names the first value-changing control in the chain, or None."""
    for control in chain:
        label = VALUE_CHANGING_CONTROLS.get(control.class_name.lower())
        if label:
            return label
    return None


def scroll_region(chain: list[ControlInfo]) -> tuple[int, ScrollState] | None:
    """The first control in the chain with a readable standard vertical scroll bar, and its state -
    a positively identified scrollable region - or None."""
    for control in chain:
        state = scroll_state(control.handle)
        if state is not None:
            return control.handle, state
    return None


def scroll_state(handle: int) -> ScrollState | None:
    try:
        return adapter.vertical_scroll(handle)
    except adapter.VerifierAdapterError:
        return None


def process_name(window_handle: int | None) -> str | None:
    """The lower-case executable file name owning a window (e.g. "chrome.exe"), or None if unknown."""
    if not window_handle:
        return None
    try:
        return adapter.process_image_name(window_handle)
    except adapter.VerifierAdapterError:
        return None


def _normalise(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _observe(read, *args):
    try:
        return read(*args)
    except adapter.VerifierAdapterError as exc:
        raise VerifierUnavailableError(str(exc)) from None


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
