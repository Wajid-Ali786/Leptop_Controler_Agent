"""
The typed-command console - where a typed command enters the assistant in Phase 1
(docs/step4 Section 4). Started with:

    python main.py --console

One line in, one action out: app/executor/commands.py turns the line into an ExecutorAction, and
handle_command() hands that action to the normal Executor pipeline (validation, safety gate,
emergency stop, the real action, the Verifier). execute_with_recovery() is the only way anything
runs from here. This module imports no adapter, sends no input and never activates a window; the
only things it asks the desktop are which window is in front and whether a modifier key is held,
both read-only.

Focus hand-over. While you type, the console itself is the active window, so a command that lands
wherever the desktop's focus or pointer is (typing, shortcuts, scrolling, refresh, window controls
and coordinate clicks - the console can sit over the coordinates) would land on the console. So for
those, the console asks YOU to switch to the window you want and waits until another window has been
in front, with no modifier key held, for console.focus_settle_seconds. Only then does the action run,
so the Executor's own checks see the window you chose. After a confirmation - which you answer here,
in the console - it waits the same way again, and the Executor's "the window changed after you
approved it" check stays the final word. The console never moves focus itself; the Phase 2/9 command
window can hand focus back on its own.

Confirmation. Medium risk and above is asked here, and only the exact answer "yes" (any capitals,
surrounding spaces ignored) allows the action; anything else, including "y", Enter, end of input and
Ctrl+C, denies it, matching the safety gate, which allows only a literal True.

Emergency stop. Every wait here is interruptible, and the flag is never reset from the console: once
it is set, every command reports it and nothing runs until the assistant is restarted. A stop that
interrupts an action comes back as STOPPED, carrying how much had already happened. The global hotkey
(app/executor/hotkey.py, started by main.py --console) is what a person presses to cause one, from any
window; this module only reports whether it is active. Ctrl+C here is NOT the emergency stop.

Privacy: the console never logs the line you typed or the confirmation prompts, and prints only
messages the Executor already made safe to show (typed text is never in them).
"""
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from app.executor import commands, emergency_stop, hotkey
from app.executor.emergency_stop import ActionInterruptedError, EmergencyStopError
from app.executor.logic import execute_with_recovery
from app.executor.models import CLICK, CLOSE_APP, OPEN_APP, REFRESH, SCROLL, SHORTCUT, TYPE_TEXT, WINDOW_CONTROL, \
    ActionResult, ExecutorAction
from app.safety.logic import ActionDeniedError
from app.verifier import logic as verifier
from config.settings import SettingsError, get_setting

YES = "yes"  # the only answer that confirms anything

# Actions that land wherever the desktop's focus or pointer is: the user hands focus over first.
HANDS_OVER = frozenset({CLICK, TYPE_TEXT, SHORTCUT, SCROLL, REFRESH, WINDOW_CONTROL})
# Actions that don't depend on which window is in front: they name the app, or close windows by name.
NO_HANDOVER = frozenset({OPEN_APP, CLOSE_APP})

WELCOME = ("AI Desktop Companion - typed commands (Phase 1). Type help for the commands, exit to leave.\n"
           f"Anything Medium risk or above asks first, and only {YES} runs it.")
STOPPED_MESSAGE = ("The emergency stop is active, so nothing ran. Restart the assistant to clear it.")
HAND_OVER_PROMPT = "Switch to the window you want (click it, or use Alt+Tab)."
HAND_BACK_PROMPT = "Now switch back to that window."
NO_FRONT_WINDOW = "I can't tell which window is in front, so I did nothing."
DIDNT_SWITCH = "You didn't switch to another window, so I did nothing."
INTERRUPTED = ("Interrupted with Ctrl+C. That is not the emergency stop and wasn't measured; if an action had "
               "started, part of it may have happened.")
HOTKEY_LOST = "The emergency-stop hotkey has stopped working: {reason} Nothing can interrupt an action by keyboard."

log = logging.getLogger(__name__)


class Status(str, Enum):
    """What happened to one typed line, beyond the message shown."""
    REFUSED = "refused"                    # not a command: the parser refused it
    RAN = "ran"                            # it went through the Executor; see `result`
    DENIED = "denied"                      # the safety gate refused it (not confirmed)
    STOPPED = "stopped"                    # the emergency stop; `result` says how much had happened
    NOT_HANDED_OVER = "not_handed_over"    # focus wasn't handed over, so nothing ran


@dataclass(frozen=True)
class CommandReply:
    """The outcome of one typed line. `message` is always safe to show: it never contains typed text."""
    status: Status
    message: str
    action: ExecutorAction | None = None
    result: ActionResult | None = None


def needs_handover(kind: str) -> bool:
    """Does this action need the user to put the window they mean in front first? Every action kind is
    classified explicitly (a rule test checks that); anything unknown fails safe by asking."""
    if kind in NO_HANDOVER:
        return False
    if kind not in HANDS_OVER:
        log.warning("Action kind %r isn't classified for focus hand-over; asking for hand-over", kind)
    return True


def handle_command(text: str, confirm=None, offer_retry=None, focus=None) -> CommandReply:
    """Run one typed line: parse it, and if it is a command, put it through the Executor pipeline.

    `focus` is the hand-over (None for callers that aren't a console window). `confirm` and
    `offer_retry` are the prompts the safety gate and the recovery loop ask.
    """
    parsed = commands.parse(text)
    if isinstance(parsed, commands.CommandRefusal):
        return CommandReply(Status.REFUSED, parsed.message)
    action = parsed
    if emergency_stop.is_stopped():
        return CommandReply(Status.STOPPED, STOPPED_MESSAGE, action)
    hands_over = focus is not None and needs_handover(action.kind)
    not_handed_over = []  # set if the hand-over after a confirmation didn't happen

    def gate(safety_action, assessment) -> bool:
        """The safety gate's confirmation, plus the hand-back it needs afterwards."""
        if confirm is None or confirm(safety_action, assessment) is not True:
            return False
        problem = focus.hand_over(HAND_BACK_PROMPT) if hands_over else None
        if problem:
            not_handed_over.append(problem)
        return problem is None

    try:
        if hands_over:
            problem = focus.hand_over(HAND_OVER_PROMPT)
            if problem:
                return CommandReply(Status.NOT_HANDED_OVER, problem, action)
        # confirm=None stays None, so the safety gate reports that no confirmation method is available.
        result = execute_with_recovery(action, confirm=gate if confirm is not None else None,
                                       offer_retry=offer_retry)
    except ActionDeniedError as exc:
        if emergency_stop.is_stopped():  # a stop while confirming denies the action; report it as a stop
            return CommandReply(Status.STOPPED, STOPPED_MESSAGE, action)
        if not_handed_over:
            return CommandReply(Status.NOT_HANDED_OVER, not_handed_over[0], action)
        return CommandReply(Status.DENIED, str(exc), action)
    except ActionInterruptedError as exc:
        return CommandReply(Status.STOPPED, f"{STOPPED_MESSAGE} {exc.result.message}", action, exc.result)
    except EmergencyStopError:
        return CommandReply(Status.STOPPED, STOPPED_MESSAGE, action)
    return CommandReply(Status.RAN, result.message, action, result)


# --- What a line WOULD be, without doing any of it (the voice console's one parser boundary) ------

@dataclass(frozen=True)
class Preview:
    """What `text` would mean if it were run - parsed and nothing else.

    It exists so another front end (app/voice_console.py) can compare two mechanical readings of one
    spoken line WITHOUT importing the Executor's parser or ever holding an ExecutorAction. Nothing
    here runs, asks, focuses a window or classifies risk.

    `equivalence_key` is what makes "the same action" a fact rather than a guess: it compares the
    parsed kind AND target, including a `type` payload, which `safe_description` deliberately hides.
    It is compared, never shown - the repr leaves it out."""
    is_command: bool
    kind: str | None            # the ExecutorAction kind, or None when it isn't a command
    free_form: bool             # the action carries text the USER wrote: never normalized or trimmed
    refusal: str | None         # commands.CommandRefusal.kind when it isn't a command
    safe_description: str       # safe to show: typed text is only ever described by its length
    equivalence_key: tuple      # (kind, target) - for comparison only

    def same_action_as(self, other: "Preview") -> bool:
        """True when both readings would do exactly the same thing to the computer."""
        return (self.is_command and other.is_command
                and self.equivalence_key == other.equivalence_key)

    def __repr__(self) -> str:
        # equivalence_key holds the raw target, which for `type` is what the user said. Not shown.
        return (f"Preview(is_command={self.is_command}, kind={self.kind!r}, "
                f"free_form={self.free_form}, refusal={self.refusal!r})")

    __str__ = __repr__


def preview(text: str) -> Preview:
    """Parse `text` and describe what it would do. NOTHING happens: no window is read or activated,
    no safety classification runs, and no action is executed."""
    parsed = commands.parse(text)
    if isinstance(parsed, commands.CommandRefusal):
        return Preview(is_command=False, kind=None, free_form=False, refusal=parsed.kind,
                       safe_description=parsed.message, equivalence_key=())
    return Preview(is_command=True, kind=parsed.kind, free_form=parsed.kind == TYPE_TEXT,
                   refusal=None, safe_description=parsed.description,
                   equivalence_key=(parsed.kind, parsed.target))


def typed_confirmation(read=input, write=print):
    """The console's own Medium-and-above confirmation, for another front end to reuse UNCHANGED.

    It reads from the KEYBOARD and allows only the exact word "yes". Voice never answers it: a
    spoken "yes" is a new utterance, not an authorization (docs/step4 Section 5)."""
    return _confirm(read, write)


def typed_retry_offer(read=input, write=print):
    """The console's own retry question, reused the same way. Also keyboard-only."""
    return _offer_retry(read, write)


def hotkey_line() -> str:
    """The emergency-stop line this console prints at startup, so another front end can print the
    same one rather than reaching for the hotkey module itself."""
    return _hotkey_line(hotkey.status())


class FocusHandover:
    """Watches which window is in front. It NEVER activates a window or sends input: the user switches
    windows themselves, and the Executor's own checks decide what may happen in the window they chose."""

    def __init__(self, write: Callable[[str], None]):
        self._write = write
        self._console = None

    def note_console_window(self) -> None:
        """Called right after a line is read: whatever is in front then is the console itself."""
        try:
            target = verifier.active_target()
        except verifier.VerifierUnavailableError:
            self._console = None
            return
        self._console = target.window.handle if target.window else None

    def hand_over(self, prompt: str) -> str | None:
        """Wait until another window has been in front long enough. None when it has; otherwise a
        message saying nothing was done. Raises EmergencyStopError if the stop is triggered."""
        if self._console is None:
            return NO_FRONT_WINDOW
        try:
            limit, settle, poll = _focus_settings()
        except SettingsError as exc:
            return str(exc)
        self._write(f"{prompt} Waiting up to {limit:g}s...")
        deadline = time.monotonic() + limit
        ready_since, ready_handle = None, None
        while True:
            try:
                front = verifier.active_target().window
                held = verifier.modifiers_held()  # a held Alt means the user is still switching
            except verifier.VerifierUnavailableError as exc:
                return f"I can't check which window is in front ({exc}), so I did nothing."
            handle = front.handle if front and front.handle != self._console else None
            if handle is None or held:
                ready_since, ready_handle = None, None
            elif handle != ready_handle:
                ready_since, ready_handle = time.monotonic(), handle
            elif time.monotonic() - ready_since >= settle:
                return None
            if time.monotonic() >= deadline:
                return DIDNT_SWITCH
            if emergency_stop.wait(poll):  # interruptible sleep
                emergency_stop.check()  # raises EmergencyStopError


def run_console(read=input, write=print, focus=None) -> int:
    """Read typed commands and run them one at a time until the user leaves. Returns an exit code."""
    write(WELCOME)
    write(_hotkey_line(hotkey.status()))
    was_active = hotkey.status().active
    focus = FocusHandover(write) if focus is None else focus
    confirm, offer_retry = _confirm(read, write), _offer_retry(read, write)
    while True:
        if was_active and not hotkey.status().active:  # said once, when it changes - not before every command
            was_active = False
            write(HOTKEY_LOST.format(reason=hotkey.status().reason or "it is no longer registered."))
        try:
            line = read("> ")
        except (EOFError, KeyboardInterrupt):
            write("")
            return 0
        word = line.strip().lower()
        if not word:
            continue
        if word == "exit":
            return 0
        if word == "help":
            write(commands.HELP)
            continue
        focus.note_console_window()
        try:
            reply = handle_command(line, confirm=confirm, offer_retry=offer_retry, focus=focus)
        except KeyboardInterrupt:
            write(INTERRUPTED)
            return 1
        except Exception as exc:  # never crash the console, and never log the line that caused it
            log.error("Typed command failed unexpectedly (%s)", type(exc).__name__)
            write(f"Something went wrong ({type(exc).__name__}); nothing else was tried.")
            continue
        write(reply.message)


def _hotkey_line(state) -> str:
    """One line so the emergency stop isn't a secret: what to press, or why there is nothing to press."""
    if state.active:
        return f"Emergency stop: press {state.hotkey} at any time - it works even when this window isn't in front."
    reason = f" ({state.reason})" if state.reason else ""
    return (f"Emergency stop hotkey {state.hotkey} is NOT active{reason}. "
            f"Nothing can interrupt an action by keyboard.")


# --- Prompts ----------------------------------------------------------------------------------

def _confirm(read, write):
    """The safety gate's confirmation: only the exact answer "yes" allows the action."""
    def confirm(action, assessment) -> bool:
        write(f"Needs your OK - {assessment.level.name} risk ({assessment.rule}):")
        write(f"  {action.description}")
        return _says_yes(read, write, f"Type {YES} to go ahead (anything else cancels): ")
    return confirm


def _offer_retry(read, write):
    """The recovery loop's retry offer: the same exact answer."""
    def offer_retry(result) -> bool:
        write(result.message)
        return _says_yes(read, write, f"Try again? Type {YES} to retry (anything else stops): ")
    return offer_retry


def _says_yes(read, write, prompt: str) -> bool:
    try:
        answer = read(prompt)
    except (EOFError, KeyboardInterrupt):  # not an Exception the safety gate would see; deny here
        write("")
        return False
    return isinstance(answer, str) and answer.strip().lower() == YES


def _focus_settings() -> tuple[float, float, float]:
    return (_positive("console.focus_handover_seconds"), _positive("console.focus_settle_seconds"),
            _positive("console.poll_interval_seconds"))


def _positive(name: str) -> float:
    value = get_setting(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise SettingsError(f"Setting '{name}' must be a positive number of seconds, got {value!r}.")
    return float(value)
