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

Prompts. Every question this console asks other than "> " is a NESTED prompt, and they all share one
vocabulary - classify_answer() decides what a line means, and nothing else interprets one:

  exit / help           always work, at any prompt, and end whatever was pending first.
  cancel / no / stop    abandon that question and say what was abandoned.
  a command             is never consumed as an answer. It goes in the loop's one slot UNCHANGED and
                        runs on the next turn, exactly as if it had been typed at "> " - one
                        interpretation, with a fresh Brain allowance. The abandoned question spends
                        nothing. At a yes-or-no prompt "a command" means anything that is not one of
                        the words above, because such a prompt has exactly one accepting answer; at the
                        correction prompt, where prose IS the answer, only a line the deterministic
                        parser recognises is handed back.
  a mistyped "yes"      at the plan prompt only, is asked again ONCE instead of killing the plan.
  a short slip          at a yes-or-no prompt, one short word the parser does not recognise ("yas") is
                        a mistyped answer, not a request: it denies and is NOT queued. Real short
                        commands are safe because the parser is asked first, not because of the length.
  yes                   approves, and at a Medium-or-above confirmation it is still the only thing that
                        does. There is no near-miss there and no handoff that defers the question: the
                        action is denied first, and the queued line is a new root command with its own
                        gate, so a command can never become a way to approve or resume one.

A clarification is the one prompt that can ask again on its own: an answer that cannot be the missing
value ("yes" to "which browser?") is re-asked once, locally, without spending the single Brain round -
and the question names the applications this console already knows about.

Reporting. run_console writes every reply's message, and it is the ONLY thing that does. Anything that
happens mid-plan - each step, failed ones included - is reported where it happens, numbered, and the
plan's own reply then says where it stopped rather than repeating the step. Two layers reporting the
same outcome is what made every abandonment print twice.

After the user has DECIDED - an explicit cancel, or declining a Medium-or-above confirmation
(NO_CORRECTION_AFTER) - nothing asks what they would have preferred instead. The correction prompt is
for a plan that went wrong, not for one they stopped on purpose.

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
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable

from app.brain import interpreter
from app.brain import logic as brain
from app.brain.models import (NeedsClarification, NotACommand, NotSupported, Understood,
                              previous_action_context)
from app.executor import commands, emergency_stop, hotkey
from app.executor.emergency_stop import ActionInterruptedError, EmergencyStopError
from app.executor.logic import configured_app_names, execute_with_recovery, resolve
from app.planner import logic as session
from app.planner.models import (TYPED_CONSOLE, FrontEnd, LifecycleRefusal, Plan, PlanRefusal,
                                PlanStepSummary, TurnContext)
from app.executor.models import CLICK, CLICK_TARGET, CLOSE_APP, CLOSE_BROWSER, NAVIGATE, \
    OPEN_APP, \
    OPEN_BROWSER, REFRESH, SCROLL, SHORTCUT, \
    TYPE_TEXT, WINDOW_CONTROL, \
    ActionResult, ExecutorAction, RESOLVE_UNKNOWN_APP, Resolved, Unresolved
from app.brain import personal_memory
from app.memory import logic as memory_logic
from app.memory import queries as memory_queries
from app.memory.models import (Found as MemoryFound, MemoryDatabase, PersistenceDecision,
                               TABLE_NAMES as MEMORY_STRUCTURES, normalize)
from app.safety.logic import ActionDeniedError
from app.safety.models import RiskLevel
from app.verifier import logic as verifier
from config.settings import SettingsError, get_setting

YES = "yes"  # the only answer that confirms anything

# --- The words every nested prompt understands (Slice 1) ----------------------------------------------
#
# Before this slice each mini-prompt had its own idea of what a line meant, so the same typed line was
# an answer in one place, a command in another and silently discarded in a third. One vocabulary, used
# by every nested prompt, is what makes the console predictable: see classify_answer().
EXIT_WORD = "exit"
HELP_WORD = "help"
# The console's own administrative command, in the console's own vocabulary beside exit and help - it is
# not a desktop action, it never becomes an ExecutorAction, and it asks the Brain nothing.
#
# THE WORDING IS NOT A FREE CHOICE. app/memory/logic.MISSING_RECOVERY tells the user to type
# "start fresh memory", so that exact phrase has to be the one that works; the longer form is accepted
# because it is how the sentence reads. A test pins the offer and the command to each other.
START_MEMORY_WORDS = frozenset({"start fresh memory", "start a fresh memory"})
# "abandon this question" - said in the three ways a person actually says it. At a yes-or-no prompt
# these already meant "not yes"; what changes is that the console now SAYS what it abandoned instead of
# falling silently through a "not yes" branch.
ABANDON_WORDS = frozenset({"cancel", "no", "stop"})
# A near-miss of YES, at the plan prompt only. Derived from YES itself so the two cannot drift apart:
# nothing longer than YES plus two characters, built only from YES's own letters. "y", "yees", "yse"
# and "e" are in; a command is not, and is checked for first anyway (see classify_answer).
_YES_LETTERS = frozenset(YES)
NEAR_MISS_LIMIT = len(YES) + 2
# A slip of the finger that is not even a near-miss. The owner typed "yas" at a confirmation: it has an
# "a", so it was not a mistyped yes, and it fell through to "this must be a command", which queued it
# and spent a turn reporting that no request could be found in "yas".
#
# At a yes-or-no prompt, ONE short word the deterministic parser does not recognise is a mistyped answer
# rather than a request, so it simply denies. What actually protects a real short command is that
# is_fresh_command() is asked FIRST, so "help", "exit", "refresh", "minimize" and anything else the
# parser knows never reach this rule, however short they are.
#
# FOUR, not five. A bare "close" is five characters and the parser calls it AMBIGUOUS rather than a
# command, so at five it was swallowed as noise and the user lost the existing "Close what? Say close
# <app>..." guidance. Keeping that guidance is worth more than treating "close" as a typo, and "yas" -
# the slip this rule exists for - is three. The word is not special-cased anywhere: the behaviour falls
# out of the parser, this limit, and the ordinary handoff.
SHORT_WORD_LIMIT = 4
# A clarification asks for a VALUE that is missing - an app, a recipient, which of several. A bare
# confirmation or refusal cannot be that value, which is knowable here, for free, without asking the
# model. The owner's real session spent its one clarification round sending "yes" to the provider as
# the answer to "which browser?".
UNUSABLE_ANSWERS = frozenset({"yes", "no", "y", "n", "ok", "okay", "sure", "yeah", "yep", "yup",
                              "nope", "nah"})

# What a question asked for. The shape decides what an unrecognised line MEANS, and that is the whole
# difference between the prompts:
#   CLOSED_QUESTION - one accepting answer ("yes"). Anything else is not an answer to the question that
#                     was asked, so it is handed back to the console loop rather than swallowed.
#   FREE_TEXT       - prose IS the answer (the correction prompt). Only a line the deterministic parser
#                     recognises as a command is handed back; "use the other Ali" stays a correction.
#   CLARIFICATION   - free text, plus the unusable-answer rule above.
CLOSED_QUESTION = "closed_question"
FREE_TEXT = "free_text"
CLARIFICATION = "clarification"

# Actions that land wherever the desktop's focus or pointer is: the user hands focus over first.
HANDS_OVER = frozenset({CLICK, TYPE_TEXT, SHORTCUT, SCROLL, REFRESH, WINDOW_CONTROL})
# Actions that don't depend on which window is in front: they name the app, or close windows by name.
#
# click_target is here even though it clicks. It is the one action that already KNOWS its window - the
# ownership token proved it before anything was read - so it does not need the user to choose one by
# putting it in front. It brings that window forward itself, after the confirmation, and refuses if
# Windows will not allow it. Every other clicking or typing action still lands wherever focus is, so
# those keep the hand-over: without it they would act on the console being typed into.
NO_HANDOVER = frozenset({OPEN_APP, CLOSE_APP, CLICK_TARGET, OPEN_BROWSER, CLOSE_BROWSER,
                         NAVIGATE})

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
    # Phase 3, for a line the Brain read. Nothing ran in any of these.
    EXPLAINED = "explained"                # the Brain answered in words: not supported, or not a command
    UNAVAILABLE = "unavailable"            # the reasoning service couldn't be reached
    NO_PLAN = "no_plan"                    # no plan could be built, or the reply couldn't be believed
    CANCELLED = "cancelled"                # a plan was shown and the user said no
    # Phase 4 wiring. Handled from local memory: nothing reached the Executor, nothing was planned, and
    # nothing left this machine - the line never reached the Brain at all. It covers both a recall
    # answer and the explicit "start fresh memory", which writes a database and no action.
    ANSWERED = "answered"


class Answer(str, Enum):
    """What one line typed at a NESTED prompt means. Decided by classify_answer(), which is pure.

    This is the console's whole answer vocabulary. A prompt reads a line, asks what it means, and acts
    on the meaning - no prompt interprets a line for itself any more."""
    NONE = "none"            # nothing was typed: blank, end of input, or Ctrl+C
    YES = "yes"              # the exact word, and the only thing that ever approves anything
    EXIT = "exit"            # leave the console, now
    HELP = "help"            # show the commands, now
    ABANDON = "abandon"      # cancel / no / stop: drop this question and go back to ">"
    COMMAND = "command"      # a new thing to do: queued for the loop, never consumed here
    NEAR_MISS = "near_miss"  # a mistyped "yes" at the plan prompt, which is re-asked once
    UNUSABLE = "unusable"    # a clarification answer that cannot be the value that was asked for
    ANSWER = "answer"        # genuine free text. At a CLOSED_QUESTION it simply means "not yes".


# The meanings that end the current interaction on the user's say-so. Each one has to be REPORTED -
# saying nothing is how the old console lost a command without admitting it.
ABANDONMENTS = (Answer.EXIT, Answer.HELP, Answer.COMMAND, Answer.ABANDON)
# The ones the console loop has to be told about, because they are not answers at all.
HANDED_BACK = (Answer.EXIT, Answer.HELP, Answer.COMMAND)


@dataclass(frozen=True)
class CommandReply:
    """The outcome of one typed line. `message` is safe to SHOW the user, and never contains typed text.

    ONE EXCEPTION, AND IT IS THE POINT OF IT: a Status.ANSWERED reply to a recall question carries the
    value the user asked for, because showing it back is the whole answer. That message is built at the
    seam in handle_typed_line() - ahead of every provider call - and the reply is returned from there,
    so it is not a plan step, not an ActionResult, not an action target and not logged."""
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

    Parsing is all this adds: everything after it is run_action(), which the Brain path in this module
    uses with an action the Planner built. There is one post-parse execution path, not two.
    """
    parsed = commands.parse(text)
    if isinstance(parsed, commands.CommandRefusal):
        return CommandReply(Status.REFUSED, parsed.message)
    return run_action(parsed, confirm=confirm, offer_retry=offer_retry, focus=focus)


def run_action(action: ExecutorAction, confirm=None, offer_retry=None, focus=None, *,
               risk_floor: RiskLevel = RiskLevel.LOW) -> CommandReply:
    """Put one already-built ExecutorAction through the Executor pipeline.

    The order is unchanged from Phase 1: emergency-stop check, focus hand-over if this kind needs it,
    then execute_with_recovery(), which owns the safety gate. `risk_floor` is the Brain's advisory
    opinion when a Planner step asked for extra care; it is passed straight through to the Executor,
    which may raise its own floor with it and can never be lowered by it. This module makes no risk
    decision of its own and never calls authorize() itself.
    """
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
                                       offer_retry=offer_retry, risk_floor=risk_floor)
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
    pending = PendingCommand()
    confirm, offer_retry = _confirm(read, write, pending), _offer_retry(read, write, pending)
    prompts = Prompts(read=read, write=write, confirm=confirm, offer_retry=offer_retry, pending=pending)
    context = TurnContext()
    while True:
        if was_active and not hotkey.status().active:  # said once, when it changes - not before every command
            was_active = False
            write(HOTKEY_LOST.format(reason=hotkey.status().reason or "it is no longer registered."))
        queued = pending.take()      # a command typed at a mini-prompt, taken exactly once
        if queued is not None:
            line = queued
        else:
            try:
                line = read("> ")
            except (EOFError, KeyboardInterrupt):
                write("")
                return 0
        # A new line is a new root command, so the previous one's abandonment is spent. Done here, in
        # one place, rather than by whichever prompt happened to set it.
        pending.begin_root_command()
        word = line.strip().lower()
        if not word:
            continue
        if word == EXIT_WORD:
            return 0
        if word == HELP_WORD:
            write(commands.HELP)
            continue
        focus.note_console_window()
        try:
            reply, context = handle_typed_line(line, context, prompts, focus=focus)
        except KeyboardInterrupt:
            write(INTERRUPTED)
            return 1
        except Exception as exc:  # never crash the console, and never log the line that caused it
            log.error("Typed command failed unexpectedly (%s)", type(exc).__name__)
            write(f"Something went wrong ({type(exc).__name__}); nothing else was tried.")
            continue
        write(reply.message)


# ======================================================================================================
# THE BRAIN PATH (Phase 3 Slice 3A) - typed console only
#
# One typed line goes through brain.route(): an exact command whose target resolves runs locally exactly
# as it did in Phase 1, with no model call and no cost. Only a line that route() calls BrainEligible is
# sent anywhere, and even then nothing happens to the computer until the user has read a numbered plan
# and typed yes.
#
# There is no second state machine here. The lifecycle is app/planner/logic.py's - propose, accept,
# complete, fail, correct - and this module only asks the questions and reports the answers. The one
# piece of state it holds is the TurnContext it passes back to the caller.
#
# The Brain-call allowance is one interpretation, one clarification and one correction per ROOT command.
# That works because a clarification answer and a correction are collected inside the handling of the
# same typed line, as sub-prompts - never as a new line at the ">" prompt. Every new line at that prompt
# is a new root command and resets the allowance, which is exactly what begin_root_command() means.
# ======================================================================================================

PLAN_HEADER = "Plan:"
PROCEED_PROMPT = f"Proceed? Type {YES} to run it, or cancel: "
CANCELLED_MESSAGE = "Nothing was run."
CORRECTION_PROMPT = "Tell me what to do differently, or press Enter to leave it: "
CLARIFY_PROMPT = "Your answer: "
NO_ANSWER_MESSAGE = "No answer given, so nothing was done."

# --- Saying what was abandoned (Slice 1, Rule 3) ------------------------------------------------------
# Every one of these names the thing that is no longer happening. "Nothing was run" on its own left the
# owner guessing which of the plan, the action and the question had gone.
PLAN_ABANDONED = "Cancelled the plan. Nothing was run."
PLAN_ABANDONED_FOR_COMMAND = "Cancelled the plan; I'll do what you just typed instead."
NEAR_MISS_MESSAGE = f"I didn't catch that. The plan is still waiting - type {YES} to run it, or cancel."
CONFIRM_ABANDONED = "Cancelled: I did not do that."
CONFIRM_ABANDONED_FOR_COMMAND = "Cancelled that; I'll do what you just typed instead."
RETRY_ABANDONED = "Stopped. I won't try that again."
CORRECTION_ABANDONED = "Left the plan alone."
# Said after the correction prompt was offered and declined. It must NOT repeat the failure reason:
# _offer_correction has just printed that immediately above the prompt, and repeating it here is the
# duplicate the "printed twice" fix removed.
CORRECTION_DECLINED = "Left it there."
CLARIFY_ABANDONED = "Dropped the question, so nothing was done."
CLARIFY_UNUSABLE = ("That doesn't answer the question - I need the missing detail itself, not yes or no. "
                    "Asking once more:")
CLARIFY_GIVEN_UP = ("I still don't have what I need, so I've left that alone. Tell me the whole thing "
                    "again when you want to.")
# Candidates the console knows WITHOUT asking anyone: the configured application names. Naming them
# turns "which browser?" from a guessing game into a choice.
CLARIFY_CANDIDATES = "I know about: {names}."
# Which `missing` fields are an application choice. NeedsClarification.missing is the model's own word
# for the absent field ("app", "recipient", "which_of", ...).
_APP_MISSING_FIELDS = frozenset({"app", "application", "apps", "app_name", "program", "which_app"})
# A plan of SEVERAL steps reports the failing step where it happened, numbered like the others, and then
# says where it stopped. A ONE-step plan does neither: its own message is the whole story and goes back
# as the reply, so reply.message stays the authoritative outcome for anything that only reads it.
PLAN_STOPPED_AT = "Stopped at step {number} of {total}."
PLAN_STOPPED_EARLY = "Stopped at step {number} of {total}; nothing after it was run."
# Outcomes after which the correction prompt is NOT offered. Three cases, one reason: rewording the
# request cannot address any of them.
#
#   DENIED            - they were shown a Medium-or-above action and said no. A decision, not a failed
#                       attempt. This is the case the owner's smoke found.
#   STOPPED           - the emergency stop. Nothing may follow it but a report, and asking a question
#                       on top of a stop would be the console talking over it.
#   NOT_HANDED_OVER   - focus never arrived, so no wording of the command would have helped; the window
#                       is what has to change.
#
# Status.RAN with a failed result is deliberately NOT here - that is the machine getting in the way,
# which is exactly what a correction is for. Suppressing the PROMPT is all this does: the status, the
# result and the original reason are untouched, and the reason is still reported exactly once.
NO_CORRECTION_AFTER = (Status.DENIED, Status.STOPPED, Status.NOT_HANDED_OVER)
PLAN_DONE = "Done: all {count} step{plural} finished."
# The same sentence would be a lie about a step whose effect nobody can see. A click is SENT, never
# observed: Outcome.UNVERIFIED is the Executor saying so, and the plan's closing line has to say it
# too rather than reporting "done" over the top of it. The narrowest possible change - each step's
# own message is already truthful and is printed unchanged above this line.
PLAN_DONE_UNVERIFIED = ("All {count} step{plural} finished, but I couldn't check what {unverified} of "
                        "them actually did - so this isn't confirmation that it worked.")
NOTHING_TO_DO = "There was nothing to do in that."

# Phase 4 wiring. REMEMBERED deliberately does NOT echo the address: the write path applies no
# disclosure rule, so it is not a place that may disclose. RECALLED is the one message in this module
# that carries a stored value, because showing it back IS the answer the user asked for.
REMEMBERED = "Remembered: {name}'s {channel}. It stays on this machine - I didn't send it anywhere."
RECALLED = "{name}'s {channel}: {address}"
KEYBOARD_ONLY = ("I only store things like that when you type them - a misheard digit stored quietly is "
                 "worse than typing it again. So I haven't stored that, and I haven't sent it anywhere "
                 "either.")
MEMORY_STARTED = ("Memory is ready: a new, empty database with all {count} structures, at {path}. It "
                  "lives on this machine, and nothing in it is ever sent to the reasoning service.")


def is_fresh_command(line: str) -> bool:
    """Is this line a NEW top-level command rather than an answer to the question just asked?

    The mini-prompts - retry, correction, clarification - ask for information, and a line typed there
    used to be consumed as the answer and thrown away. Worse, at the correction prompt it became a paid
    model call about a plan the user had already abandoned.

    This invents no second parser. It reuses the existing boundaries, and the discriminator is whether
    the DETERMINISTIC PARSER recognised a verb:

      * LocalAction                 "open chrome"       - parsed and resolvable
      * BrainEligible with parsed   "close powershell"  - a real verb whose target did not resolve
      * the console's own words      help, exit

    Everything else is an answer, which is what keeps these prompts usable: a correction like "use the
    other Ali", a clarification answer, and yes/no/cancel all have no parsed action, so they are still
    consumed as answers. A bare "close" is ambiguous rather than parsed, so it stays an answer too."""
    if not isinstance(line, str) or not line.strip():
        return False
    if line.strip().lower() in ("exit", "help"):
        return True
    if _is_start_memory(line):
        # The console's own command, so it behaves like exit and help do: handed back to the loop rather
        # than consumed as correction or clarification text. Without it, typing the one command the
        # missing-database message names, at the prompt that message was printed above, would send it to
        # the provider as prose and create nothing.
        return True
    if personal_memory.is_covered(line):
        # A "remember this" is a new command wherever it is typed, and here that is not a convenience.
        # Consuming it as a correction is what sent it: the correction text goes into replan_request(),
        # and a clarification answer goes into clarification_request(), both of which are provider
        # calls. Handing it back instead means the loop runs it through handle_typed_line(), where the
        # local guard answers or refuses it. One addition closes the correction, clarification and
        # retry prompts at once, because all three ask this question.
        return True
    route = brain.route(line, resolve)
    if isinstance(route, brain.LocalAction):
        return True
    return isinstance(route, brain.BrainEligible) and getattr(route, "parsed", None) is not None


def _is_near_miss(word: str) -> bool:
    """A mistyped YES, and nothing that could be anything else.

    Deliberately narrow: only YES's own letters, and never longer than YES plus two. That admits
    "y", "e", "yees", "yse" and "yesss" and excludes every abandon word ("no", "stop", "cancel" all
    contain letters YES does not), every blank line, and anything with a space in it. It cannot swallow
    a command for a second reason as well: classify_answer asks is_fresh_command FIRST, so a line the
    parser recognises is handed back before this is ever consulted."""
    return 0 < len(word) <= NEAR_MISS_LIMIT and word != YES and set(word) <= _YES_LETTERS


def _is_short_slip(word: str) -> bool:
    """One short word, no space in it. Reached only after is_fresh_command() has said no, so it can
    never catch a command the parser recognises - and a phrase is never a slip, however short."""
    return len(word) <= SHORT_WORD_LIMIT and len(word.split()) == 1


def classify_answer(line, shape: str = CLOSED_QUESTION, *, handoff: bool = True) -> Answer:
    """What one line typed at a nested prompt means. PURE: nothing runs, nothing is spent, no model
    call is made - is_fresh_command() routes through the deterministic parser only.

    `shape` is what the question asked for: CLOSED_QUESTION, FREE_TEXT or CLARIFICATION.

    `handoff` is whether the caller owns a console loop that can be handed a line back. Without one -
    which is what app/voice_console.py and every caller that is not run_console passes - there is
    nowhere to put a command, nothing to exit from, and no second read to make, so the answer space
    collapses to exactly what it was before this slice: the exact word yes, or not. That single `if` is
    why voice behaviour is unchanged.

    THE ORDER IS THE RULE SET, and two steps of it are load-bearing:

      * is_fresh_command() is asked BEFORE _is_near_miss(), so a command can never be read as a
        mistyped yes (and so Rule 4 cannot swallow one).
      * at a CLARIFICATION the unusable-answer check comes BEFORE the abandon words, because "yes" and
        "no" there are the exact case the local re-ask exists for. Treating them as "abandon" would
        make that rule dead code. "cancel" and "stop" still abandon, and nothing else changes.
    """
    if not isinstance(line, str) or not line.strip():
        return Answer.NONE
    word = line.strip().lower()
    if shape == CLOSED_QUESTION and word == YES:
        # The approval, and the ONE place it is decided. Scoped to the closed question because "yes" is
        # not an approval at the other two prompts: at a correction it is prose, and at a clarification
        # it is the unusable answer that cost the owner their one round.
        return Answer.YES
    if not handoff and personal_memory.is_covered(line):
        # THE ONE THING THAT IS NOT AN ANSWER EVEN WITHOUT A LOOP. Everywhere else, no loop means the
        # answer space collapses to "yes, or not" and a line is consumed as free text - which for a
        # "remember this" would put it in replan_request() or clarification_request(). There is nowhere
        # to hand it back to here (app/voice_console.py owns no loop), so the prompt is abandoned: the
        # user is told the question was dropped, and nothing is sent. Fail closed, at the cost of a
        # retype.
        return Answer.ABANDON
    if not handoff:
        return Answer.ANSWER
    if word == EXIT_WORD:
        return Answer.EXIT
    if word == HELP_WORD:
        return Answer.HELP
    if shape == CLARIFICATION and word in UNUSABLE_ANSWERS:
        return Answer.UNUSABLE
    if word in ABANDON_WORDS:
        return Answer.ABANDON
    if is_fresh_command(line):
        return Answer.COMMAND
    if shape == CLOSED_QUESTION:
        if _is_near_miss(word):
            return Answer.NEAR_MISS
        if _is_short_slip(word):
            return Answer.ANSWER      # a mistyped answer: deny, and do NOT queue it as a request
        return Answer.COMMAND
    return Answer.ANSWER


@dataclass
class PendingCommand:
    """One slot for a line typed at a mini-prompt that turned out to be a fresh command.

    Owned by run_console. A prompt puts the line here INSTEAD of consuming it, and the loop takes it and
    runs it exactly once: take() empties the slot, so the line can neither run twice nor be left behind.
    There is no nesting and no queue - one slot, taken before each read.

    `abandoned` is the other half: once a nested prompt has been abandoned, NOTHING may ask another
    question about this root command. Without it, "exit" at the retry prompt was queued correctly and
    the user was then shown the correction prompt instead of leaving - which is what the owner saw."""
    line: str | None = None
    abandoned: bool = False

    def put(self, line: str) -> None:
        self.line = line

    def take(self) -> str | None:
        line, self.line = self.line, None
        return line

    def abandon(self) -> None:
        """The user ended this interaction. No further prompt for this root command."""
        self.abandoned = True

    def begin_root_command(self) -> None:
        """Called by the loop once per line it is about to handle: the abandonment is spent."""
        self.abandoned = False


@dataclass(frozen=True)
class Prompts:
    """The console's own keyboard and screen, passed in so a test can drive the whole flow.

    `confirm` and `offer_retry` are the SAME prompts the deterministic path uses - the safety gate's
    confirmation is not re-implemented for Brain-planned work."""
    read: Callable[[str], str]
    write: Callable[[str], None]
    confirm: Callable | None = None
    offer_retry: Callable | None = None
    # Where a mini-prompt hands a fresh command back to the console loop. None - which is what the voice
    # console and every caller that does not own a loop pass - keeps the old behaviour exactly.
    pending: "PendingCommand | None" = None


def is_brain_eligible(text: str) -> bool:
    """Would this line need the Brain at all? PURE routing and nothing else.

    It parses and asks whether the target resolves, exactly as handle_typed_line() will. It spends no
    Brain allowance, starts no lifecycle, makes no provider call and changes no context.

    It exists for app/voice_console.py, which is tested never to import anything from app.executor: the
    voice console reaches the computer only through this module, and that invariant is worth keeping, so
    the question is asked here on its behalf."""
    return isinstance(brain.route(text, resolve), brain.BrainEligible)


def handle_typed_line(text: str, context: TurnContext, prompts: Prompts, focus=None,
                      interpret=None, *,
                      frontend: FrontEnd = TYPED_CONSOLE) -> tuple[CommandReply, TurnContext]:
    """Handle one accepted line, with the Brain available. Returns the reply and the new session state.

    `interpret` is app/brain/interpreter.interpret, injected so tests need no provider. This module
    never imports an adapter of any kind.

    `frontend` is WHICH CALLER this is, and it is the Brain's capability guard - not a cosmetic label.
    It reaches app/planner/logic.build_plan() at every planning site, and VOICE_CONSOLE is what stops a
    Brain-planned click or type_text from ever becoming a step, because voice mode has no reliable focus
    hand-over for those kinds. It defaults to TYPED_CONSOLE so the typed console is unchanged; voice
    passes VOICE_CONSOLE explicitly. A hardcoded front end inside this chain would be a silent way past
    that guard, so it is threaded rather than assumed.
    """
    interpret = interpreter.interpret if interpret is None else interpret
    if emergency_stop.is_stopped():
        # Nothing pending may survive a stop, so it can never be accepted or executed afterwards. Done
        # here rather than in a front end's loop so the typed and spoken consoles behave identically.
        context = session.emergency_stop(context)
    route = brain.route(text, resolve)
    if isinstance(route, brain.LocalRefusal):
        return CommandReply(Status.REFUSED, route.refusal.message), context
    context = session.begin_root_command(context)          # a new line is a new root command
    if isinstance(route, brain.LocalAction):
        return _run_local(route.action, context, prompts, focus)
    if _is_start_memory(text):
        return _start_memory(), context
    personal = _personal_memory(text, frontend)
    if personal is not None:
        return personal, context
    remembered = _remembered_app(route)
    if remembered is not None:
        return _run_local(remembered, context, prompts, focus)
    return _ask_the_brain(route, context, prompts, focus, interpret, frontend)


def _is_start_memory(text) -> bool:
    """Is this line the explicit request to create a memory database? An EXACT phrase, nothing fuzzy.

    Exact on purpose: this is the only command in the project that brings a database into existence, and
    a loose match is how "don't start a fresh memory" would create one. normalize() is the project's one
    comparison form, so capitals and extra spaces are all that is forgiven."""
    return normalize(text) in START_MEMORY_WORDS


def _start_memory() -> CommandReply:
    """Accept the offer the missing-database message makes, and report precisely what happened.

    THE USER TYPED IT: nothing here runs on anyone's behalf, nothing creates a database during a
    remember, a recall or application start-up, and there is no path from the Brain to this function -
    it is reached only by the exact phrase above, before any provider call. It costs nothing.

    Memory decides whether creating is allowed; this function only turns the three answers into words.
    A refusal is reported with Memory's own message, which says what is there and that it was left
    alone - so "I won't replace it" is said by the layer that did not replace it."""
    started = memory_logic.start_fresh_memory()
    if isinstance(started, MemoryDatabase):
        return CommandReply(Status.ANSWERED, MEMORY_STARTED.format(count=len(MEMORY_STRUCTURES),
                                                                   path=started.path))
    return CommandReply(Status.REFUSED, started.message)


def _personal_memory(text: str, frontend: FrontEnd) -> CommandReply | None:
    """Answer a "remember this" or a "what is their number" from LOCAL memory, or None to carry on.

    WHERE THIS SITS IS THE WHOLE PRIVACY GUARANTEE. It is called from handle_typed_line() before
    _ask_the_brain(), so a line it answers reaches no provider request of any kind: not the
    interpretation, not a clarification, not a replan. There is no second place in this module where the
    same line could be handled, and a None from here is the only way past it.

    Spending nothing is the other half: a line answered here costs no model call, no Brain allowance and
    no money, exactly like the remembered-alias path below.

    VOICE CANNOT STORE THIS SLICE. A spoken "remember Ali ka whatsapp ..." is refused rather than sent:
    Whisper is not reliable enough to be trusted with a phone number (Phase 2's closeout records that
    unclear speech can come back as other words entirely), and a wrong digit stored silently is worse
    than a retype. A spoken QUESTION falls through to the Brain unchanged, because the words the user
    said are all it contains - nothing stored is in it. Answering it aloud would be a new disclosure
    surface, a room instead of a screen, and that decision is not in this slice."""
    request = personal_memory.recognise(text)
    if request is None:
        return None
    if isinstance(request, personal_memory.KeepLocal):
        # The fail-closed case: a keep marker this grammar could not parse. REFUSED, never forwarded.
        return CommandReply(Status.REFUSED, request.message)
    if isinstance(request, personal_memory.RememberContact):
        if frontend != TYPED_CONSOLE:
            return CommandReply(Status.REFUSED, KEYBOARD_ONLY)
        return _remember_contact(request)
    if frontend != TYPED_CONSOLE:
        return None
    return _recall_contact(request)


def _remember_contact(request) -> CommandReply:
    """Store one contact, and say so only if the whole write succeeded.

    WHY THE PERSISTENCE DECISION IS ALLOW HERE, STATED RATHER THAN ASSUMED. PersistenceDecision exists
    because Memory cannot tell a phone number from a password by looking at it, so the caller has to
    decide. This caller can: the line reached this function only by matching a grammar whose pivot is a
    channel word from a closed vocabulary, so "remember my password is ..." has no channel in it, is
    never parsed as a contact and never gets here - it is refused upstream as KeepLocal. The user also
    asked for exactly this, in the same sentence. That is the decision, and it is why it is made at the
    call site instead of inside Memory.

    The reply NAMES NOTHING STORED. "Remembered: Ali's whatsapp" says what happened without echoing the
    number, so the write path - which applies no disclosure rule - never discloses."""
    stored = memory_logic.remember_contact(request.name, request.channel, request.address,
                                           PersistenceDecision.ALLOW)
    if isinstance(stored, MemoryFound):
        return CommandReply(Status.ANSWERED,
                            REMEMBERED.format(name=request.name, channel=request.channel))
    # Ambiguous, NotFound, Redacted and MemoryUnavailable all carry their own safe message, and every
    # one of them means nothing was stored. None of them is ever reported as "remembered".
    return CommandReply(Status.REFUSED, stored.message)


def _recall_contact(request) -> CommandReply:
    """Answer "what is their number" from local memory.

    Two lookups, both through app/memory/queries.py, which is the module that guarantees a lookup cannot
    produce an action. The read is made under the USER disclosure context: a rule written against BRAIN
    withholds a value from the model without blinding the person who stored it, which is the whole
    reason the two contexts are separate names.

    Every failure is an ANSWER rather than a refusal - "I don't know who that is." is what the user
    asked for, truthfully - and a Redacted result keeps its own message, so a withheld address stays
    withheld here instead of being recovered."""
    who = memory_queries.person(request.name)
    if not isinstance(who, MemoryFound):
        return CommandReply(Status.ANSWERED, who.message)
    found = memory_queries.contact_for_user(who.value.id, request.channel)
    if not isinstance(found, MemoryFound):
        return CommandReply(Status.ANSWERED, found.message)
    return CommandReply(Status.ANSWERED, RECALLED.format(name=who.value.name,
                                                         channel=found.value.channel,
                                                         address=found.value.address))


def _remembered_app(route) -> ExecutorAction | None:
    """The action a remembered APP ALIAS makes runnable, or None to carry on to the Brain.

    THE ORDER MATTERS AND IS NOT NEGOTIABLE. Configuration is asked first, always: this is reached only
    after brain.route() has already put the line through the Phase 1 resolver and that resolver refused
    the name for being an unknown app. So a configured name never gets here, and "open notepad" behaves
    exactly as it did before Memory existed.

    What Memory may then supply is one thing: a key configuration ALREADY HAS. It cannot return an
    executable, a path or a command - the applications table has no column that could hold one - and the
    existing resolver has the final word on whatever comes back. A remembered alias therefore cannot add
    capability, and a stale one pointing at an app that is no longer configured resolves to nothing.

    Memory missing, empty or broken returns None, which is today's behaviour unchanged: the line goes to
    the Brain exactly as it would have. A Memory failure never changes what a deterministic command does,
    and never spends a provider request of its own - a line resolved here makes no model call at all."""
    parsed = getattr(route, "parsed", None)
    if parsed is None or parsed.kind not in (OPEN_APP, CLOSE_APP):
        return None
    if route.reason != brain.UNRESOLVED_TARGET:
        return None
    # brain.route() reports EVERY resolver refusal as UNRESOLVED_TARGET, so the resolver is asked which
    # refusal it actually was. Memory answers one question - "I don't know that app name" - and must not
    # be consulted for a missing target ("open" on its own) or for a broken configuration.
    resolution = resolve(parsed)
    if not isinstance(resolution, Unresolved) or resolution.reason != RESOLVE_UNKNOWN_APP:
        return None
    try:
        configured = configured_app_names()
    except SettingsError:
        return None
    remembered = memory_queries.application(parsed.target, configured)
    if not isinstance(remembered, MemoryFound):
        return None
    action = replace(parsed, target=remembered.value)
    return action if isinstance(resolve(action), Resolved) else None


def _run_local(action, context, prompts, focus):
    """The Phase 1 path, unchanged: no model call, no advisory floor, no lifecycle."""
    reply = run_action(action, confirm=prompts.confirm, offer_retry=prompts.offer_retry, focus=focus)
    return reply, _remember(reply, context)


def _remember(reply: CommandReply, context: TurnContext) -> TurnContext:
    """Keep the privacy-safe summary of an action that actually happened, for a follow-up like "close it".

    THE CRITERION IS ActionResult.verified, NOT ok, and not Status.RAN. Status.RAN only means the action
    reached the Executor - the result says what became of it - and `ok` includes Outcome.UNVERIFIED,
    which app/executor/models.py defines as "carried out without error, but nothing confirms it achieved
    anything", with the instruction never to treat it as confirmed success. A click is always UNVERIFIED,
    so "the last thing I did" must not claim it happened.

    Nothing is lost by being strict: the only kinds previous_action_context() keeps a TARGET for -
    open_app, close_app, window_control - all report DONE or ALREADY_CLOSED when they genuinely succeed,
    and those are exactly the verified outcomes.

    The privacy decision is not made here. app/brain/models.previous_action_context() decides per kind
    what may be kept, and drops a typed payload; this module never builds a PreviousActionContext."""
    if reply.status is not Status.RAN or reply.result is None or not reply.result.verified:
        return context
    return session.remember_action(context, reply.action.kind, reply.action.target)


def _ask_the_brain(route, context, prompts, focus, interpret, frontend):
    """One BrainEligible line: interpret it, then act on which of the four answers came back."""
    spent = session.spend_interpretation(context)
    if isinstance(spent, LifecycleRefusal):
        return CommandReply(Status.NO_PLAN, spent.message), context
    context = spent
    outcome = interpret(brain.interpretation_request(route.text, context.previous_action_context))
    return _on_interpretation(outcome, route.text, context, prompts, focus, interpret, frontend,
                              clarified=False)


def _on_interpretation(outcome, text, context, prompts, focus, interpret, frontend, *,
                       clarified: bool):
    """The four answers, plus the two ways there is no answer. Nothing here executes anything."""
    if isinstance(outcome, interpreter.Unavailable):
        # The frozen sentence and nothing else: no prefix, no suffix, no second line. The parser's own
        # explanation is deliberately NOT appended - BrainEligible.local_message stays part of the route
        # contract for other callers, but this message is shown exactly as agreed.
        return CommandReply(Status.UNAVAILABLE, brain.UNAVAILABLE_MESSAGE), context
    if isinstance(outcome, brain.InterpretationError):
        return CommandReply(Status.NO_PLAN, _cannot_use_that_answer()), context
    if isinstance(outcome, NotSupported):
        return CommandReply(Status.EXPLAINED, _supported_message(outcome)), context
    if isinstance(outcome, NotACommand):
        return CommandReply(Status.EXPLAINED, outcome.message or "That doesn't look like something to do."), context
    if isinstance(outcome, NeedsClarification):
        if clarified:
            # The one allowed round is spent. begin_clarification refuses it; say so and stop.
            refused = session.begin_clarification(context, outcome, text)
            message = refused.message if isinstance(refused, LifecycleRefusal) else _cannot_use_that_answer()
            return CommandReply(Status.NO_PLAN, message), context
        return _clarify(outcome, text, context, prompts, focus, interpret, frontend)
    return _plan_it(outcome, text, context, prompts, focus, interpret, frontend)


def _clarification_question(needs) -> list[str]:
    """The question to show, and the candidates the console already knows.

    A choice question the user cannot answer without guessing is the thing that burned the owner's one
    clarification round ("which browser?" -> "yes"). When the missing field is an APPLICATION, the
    configured names are local knowledge: no model call, no desktop read, no Memory lookup. A broken or
    unreadable configuration simply adds nothing, which is today's behaviour."""
    lines = [needs.question]
    if (needs.missing or "").strip().lower() not in _APP_MISSING_FIELDS:
        return lines
    try:
        names = configured_app_names()
    except SettingsError:
        return lines
    if names:
        lines.append(CLARIFY_CANDIDATES.format(names=", ".join(names)))
    return lines


def _clarify(needs, text, context, prompts, focus, interpret, frontend):
    """Ask the model's question, take ONE USABLE answer, and send it back once.

    The one Brain round is spent on the question (app/planner/logic.begin_clarification), and this is
    where it is CASHED - at interpret(). So an answer that cannot possibly be the missing value never
    reaches that line: the question is asked again, locally, at most once, and the round is still there
    for the real answer. A second unusable answer abandons the root command rather than guessing."""
    started = session.begin_clarification(context, needs, text)
    if isinstance(started, LifecycleRefusal):
        return CommandReply(Status.NO_PLAN, started.message), context
    context = started
    for line in _clarification_question(needs):
        prompts.write(line)
    meaning, answer = _read_answer(prompts, CLARIFY_PROMPT, CLARIFICATION)
    if meaning is Answer.UNUSABLE:
        # ONE local re-ask. No Brain call, no new question, and no loop: straight-line code with a
        # second read, after which an unusable answer is the end of it.
        prompts.write(CLARIFY_UNUSABLE)
        for line in _clarification_question(needs):
            prompts.write(line)
        meaning, answer = _read_answer(prompts, CLARIFY_PROMPT, CLARIFICATION)
        if meaning is Answer.UNUSABLE:
            return (CommandReply(Status.NO_PLAN, CLARIFY_GIVEN_UP),
                    session.cancel_clarification(context))
    if meaning is Answer.ABANDON:
        return CommandReply(Status.NO_PLAN, CLARIFY_ABANDONED), session.cancel_clarification(context)
    if meaning is not Answer.ANSWER or not answer.strip():
        return CommandReply(Status.NO_PLAN, NO_ANSWER_MESSAGE), session.cancel_clarification(context)
    continued = session.answer_clarification(context, answer)
    if isinstance(continued, LifecycleRefusal):
        return CommandReply(Status.NO_PLAN, continued.message), context
    continuation, context = continued
    outcome = interpret(brain.clarification_request(continuation))
    return _on_interpretation(outcome, text, context, prompts, focus, interpret, frontend,
                              clarified=True)


def _plan_it(understood, text, context, prompts, focus, interpret, frontend):
    """Understood -> the pure Planner -> a proposal nobody has accepted yet.

    PLANNING SITE 1 of 2. `frontend` decides which kinds may become steps at all."""
    plan = session.build_plan(understood, frontend, resolve, text)
    if isinstance(plan, PlanRefusal):
        return CommandReply(Status.NO_PLAN, plan.message), context
    proposed = session.propose_plan(context, plan, text)
    if isinstance(proposed, LifecycleRefusal):
        return CommandReply(Status.NO_PLAN, proposed.message), context
    return _offer(proposed, prompts, focus, interpret, frontend)


def _offer(context, prompts, focus, interpret, frontend):
    """Show the plan, and run it only if the user says yes to THIS proposal."""
    pending = context.pending_plan
    for line in _preview(context):
        prompts.write(line)
    meaning = _plan_answer(prompts)
    if meaning is not Answer.YES:
        rejected = session.reject_plan(context)
        context = rejected if isinstance(rejected, TurnContext) else context
        # The plan is dead either way; what differs is what the user is told, and whether there is any
        # point asking them anything else. _offer_correction asks nothing once the slot is abandoned, so
        # exit, help, a command and an explicit cancel all go straight back to ">".
        if meaning is Answer.COMMAND:
            message = PLAN_ABANDONED_FOR_COMMAND
        elif meaning in ABANDONMENTS:
            message = PLAN_ABANDONED
        else:
            message = CANCELLED_MESSAGE
        return _offer_correction(context, prompts, focus, interpret,
                                 CommandReply(Status.CANCELLED, message), frontend)
    accepted = session.accept_plan(context, pending.plan_id)
    if isinstance(accepted, LifecycleRefusal):
        return CommandReply(Status.NO_PLAN, accepted.message), context
    return _run_plan(accepted, prompts, focus, interpret, frontend)


def _run_plan(context, prompts, focus, interpret, frontend):
    """Each step through the SAME path a typed command uses, carrying the step's advisory floor."""
    steps = context.pending_plan.plan.steps
    unverified = 0
    for step in steps:
        reply = run_action(step.action, confirm=prompts.confirm, offer_retry=prompts.offer_retry,
                          focus=focus, risk_floor=step.risk_floor)
        if reply.status is Status.RAN and reply.result is not None and reply.result.ok:
            prompts.write(f"{step.number}. {reply.message}")
            unverified += 0 if reply.result.verified else 1
            done = session.complete_step(context, step.number)
            context = done if isinstance(done, TurnContext) else context
            context = _remember(reply, context)
            continue
        failed = session.fail_plan(context, step.number, reply.message)
        context = failed if isinstance(failed, TurnContext) else context
        # The failing step is printed HERE, numbered and in sequence exactly like a successful one, and
        # the reply that goes back carries a PLAN-LEVEL message instead of a copy of it.
        #
        # This is the fix for "every abandonment printed twice". The cause was not a stray print: two
        # layers each reported the same outcome. _offer_correction wrote outcome.message, and
        # run_console writes every reply's message, so the step's message appeared once above the
        # correction prompt and once below it. run_console is now the only thing that reports a reply;
        # anything that happens mid-plan is reported where it happens.
        #
        # Status, action and result are carried through unchanged - Status.DENIED is what tells
        # _offer_correction that the user said no, and the result is still what says what became of it.
        if len(steps) == 1:
            # ONE step IS the plan, so its own message is the whole story. It goes back as the reply
            # and run_console reports it - once. Nothing is written here, which matters beyond tidiness:
            # reply.message has to stay the authoritative outcome for a caller that only reads it, and
            # app/voice_console.py SPEAKS it. Replacing it with "stopped there" would have left the real
            # reason - an ownership refusal, for instance - printed but never said.
            return _offer_correction(context, prompts, focus, interpret, reply, frontend)
        prompts.write(f"{step.number}. {reply.message}")
        stopped = CommandReply(
            reply.status,
            (PLAN_STOPPED_EARLY if step.number < len(steps) else PLAN_STOPPED_AT).format(
                number=step.number, total=len(steps)),
            reply.action, reply.result)
        return _offer_correction(context, prompts, focus, interpret, stopped, frontend)
    plural = "" if len(steps) == 1 else "s"
    if unverified:
        return CommandReply(Status.RAN, PLAN_DONE_UNVERIFIED.format(
            count=len(steps), plural=plural, unverified=unverified)), context
    return CommandReply(Status.RAN, PLAN_DONE.format(count=len(steps), plural=plural)), context


def _offer_correction(context, prompts, focus, interpret, outcome: CommandReply, frontend):
    """The user may say what they wanted instead - once per root command, and only if they ask.

    This is NOT the Executor's retry, which repeats the same action when the machine got in the way.
    This asks for a DIFFERENT plan because a person said so, and it never happens on its own.
    """
    if _abandoned(prompts) or outcome.status in NO_CORRECTION_AFTER:
        # Two reasons not to ask, and they are the same reason underneath: the USER has already decided.
        #
        # _abandoned - they left, asked for help, cancelled, or typed something else to do. Asking
        # "what should I have done instead?" on top of that is what made `exit` at the retry prompt
        # produce another question.
        #
        # NO_CORRECTION_AFTER - they were shown a Medium-or-above action and declined it. A denial is a
        # decision, not a failed attempt; this prompt exists for a plan that went wrong, not for one the
        # user stopped on purpose. Checked on the status here, in one place, so the deterministic and
        # Brain paths cannot disagree about it.
        #
        # This function no longer writes outcome.message: run_console reports every reply exactly once.
        return outcome, session.cancel(context)
    if not context.budget.may_replan or context.pending_plan is None:
        return outcome, context
    # THE REASON FIRST. A question about a failure is unanswerable until the failure has been
    # reported, and the "printed twice" fix accidentally removed the only report that came before it:
    # run_console became the single owner of reply reporting, and a ONE-STEP plan returns its step's
    # reply unchanged without printing it, so nothing was said before this prompt. (A multi-step plan
    # was never affected - it prints the failing step in sequence.)
    #
    # This is the only place the message is written, and it is written only when a question follows
    # it, so there is still exactly one report either way.
    prompts.write(outcome.message)
    correction = _ask_text(prompts, CORRECTION_PROMPT)
    if not correction or not correction.strip():
        # The reason is on screen directly above; the closing line must not say it again.
        return (CommandReply(outcome.status, CORRECTION_DECLINED, outcome.action, outcome.result),
                session.cancel(context))
    corrected = session.correct_plan(context, correction)
    if isinstance(corrected, LifecycleRefusal):
        return CommandReply(outcome.status, corrected.message, outcome.action, outcome.result), context
    request, context = corrected
    outcome = interpret(brain.replan_request(request))
    if isinstance(outcome, interpreter.Unavailable):
        return CommandReply(Status.UNAVAILABLE, brain.UNAVAILABLE_MESSAGE), context
    if isinstance(outcome, brain.InterpretationError) or isinstance(outcome, NeedsClarification):
        return CommandReply(Status.NO_PLAN, _cannot_use_that_answer()), context
    if isinstance(outcome, NotSupported):
        return CommandReply(Status.EXPLAINED, _supported_message(outcome)), context
    if isinstance(outcome, NotACommand):
        return CommandReply(Status.EXPLAINED, outcome.message or NOTHING_TO_DO), context
    # PLANNING SITE 2 of 2 - the same front end, so a correction cannot widen what voice may do.
    # The correction is the user's words for THIS exchange; the root text is what they said
    # before it. A navigation address must appear in one of them.
    plan = session.build_plan(outcome, frontend, resolve,
                              f"{request.original_text} {correction}")
    if isinstance(plan, PlanRefusal):
        return CommandReply(Status.NO_PLAN, plan.message), context
    installed = session.install_replacement(context, plan)
    if isinstance(installed, LifecycleRefusal):
        return CommandReply(Status.NO_PLAN, installed.message), context
    return _offer(installed, prompts, focus, interpret, frontend)


# --- Showing a plan, in words ---------------------------------------------------------------------------

def _preview(context: TurnContext) -> list[str]:
    """A numbered plan a person can check before accepting it.

    Built from app/planner/logic.plan_summary(), which already describes a typed payload by its length,
    so nothing here can print the user's own words. No repr of a model object is ever shown."""
    lines = [PLAN_HEADER]
    controls = {step.number: step.action.control for step in context.pending_plan.plan.steps}
    urls = {step.number: step.action.url for step in context.pending_plan.plan.steps}
    for summary in session.plan_summary(context.pending_plan):
        lines.append(f"  {summary.number}. "
                     f"{_describe(summary, controls.get(summary.number, ''), urls.get(summary.number, ''))}")
    return lines


def _describe(summary: PlanStepSummary, control: str = "", url: str = "") -> str:
    """One step in plain English. `summary.target` is already the Executor's log-safe label.

    `control` is passed in separately rather than read from the summary, because a PlanStepSummary
    is the shape that may be SENT TO THE MODEL inside a ReplanRequest. The user's own word for a
    control does not need to go there for re-planning, so it does not: it is taken straight from
    the step and used only for this line, which the user reads."""
    kind, target = summary.kind, summary.target
    if kind == OPEN_APP:
        return f"Open {target}"
    if kind == CLOSE_APP:
        return f"Close {target}"
    if kind == CLICK:
        return f"Click at {target}"
    if kind == OPEN_BROWSER:
        return "Open the assistant's own browser"
    if kind == CLOSE_BROWSER:
        return "Close the assistant's own browser"
    if kind == NAVIGATE:
        # The URL comes from the STEP, not from the summary, for the same reason `control`
        # does: a PlanStepSummary may travel to the model, and log_label must stay empty.
        return f"Open {url} in the assistant's own browser" if url else "Open a web page"
    if kind == CLICK_TARGET:
        # The user's own words for the control, which is the point of a named click: the plan they
        # check says what they asked for, not a coordinate nobody can verify by reading it.
        where = f" in {target}" if target else ""
        return f'Click "{control}"{where}' if control else f"Click the control you named{where}"
    if kind == TYPE_TEXT:
        return f"Type <{target}>"
    if kind == SHORTCUT:
        return f"Press {target}"
    if kind == SCROLL:
        return f"Scroll {target}"
    if kind == REFRESH:
        return "Refresh the window in front"
    if kind == WINDOW_CONTROL:
        return f"{target.capitalize()} the window in front"
    return f"{kind.replace('_', ' ')} {target}".strip()


def _supported_message(outcome: NotSupported) -> str:
    return outcome.message or f"I can't {outcome.what} yet."


def _cannot_use_that_answer() -> str:
    """One wording for every unusable answer. It never quotes the reply or names a schema field."""
    return "I couldn't make sense of that well enough to act on it, so I did nothing."


def _read_answer(prompts: Prompts, prompt: str, shape: str) -> tuple[Answer, str]:
    """Read one line at a nested prompt and say what it means. The ONLY place a nested prompt reads
    from the keyboard, so the rule set has exactly one implementation.

    Returns (meaning, the line exactly as it was typed). A line that is not an answer at all - exit,
    help, or a command - is put in the loop's slot UNCHANGED and the interaction is marked abandoned,
    so nothing asks another question about this root command. Nothing here spends a Brain call: the
    queued line is interpreted once, later, by the outer loop, exactly as if it had been typed at ">".
    """
    try:
        line = prompts.read(prompt)
    except (EOFError, KeyboardInterrupt):
        prompts.write("")
        return Answer.NONE, ""
    line = line if isinstance(line, str) else ""
    meaning = classify_answer(line, shape, handoff=prompts.pending is not None)
    if meaning in HANDED_BACK:
        prompts.pending.put(line)        # unchanged: the loop sees what the user actually typed
    if meaning in ABANDONMENTS:
        prompts.pending.abandon()
    return meaning, line


def _plan_answer(prompts: Prompts) -> Answer:
    """The plan prompt, including Rule 4's single re-ask.

    A mistyped yes is not a decision, and the plan is still sitting there unaccepted, so asking again
    costs nothing - no Brain call, no action, no state change. It happens AT MOST ONCE and that is
    structural: this is straight-line code with two reads and no loop, and a second near-miss is
    returned as a plain "not yes"."""
    meaning, _line = _read_answer(prompts, PROCEED_PROMPT, CLOSED_QUESTION)
    if meaning is not Answer.NEAR_MISS:
        return meaning
    prompts.write(NEAR_MISS_MESSAGE)
    meaning, _line = _read_answer(prompts, PROCEED_PROMPT, CLOSED_QUESTION)
    return Answer.ANSWER if meaning is Answer.NEAR_MISS else meaning


def _abandoned(prompts: Prompts) -> bool:
    """Has a nested prompt already been ended by the user? Then nothing may ask them anything else."""
    return prompts.pending is not None and prompts.pending.abandoned


def _ask_text(prompts: Prompts, prompt: str) -> str | None:
    """A free-text answer for the CORRECTION prompt. None when there is no answer to use.

    Prose is what this prompt is for, so only a line the deterministic parser recognises as a command
    is handed back - "use the other Ali" is still a correction. cancel/no/stop now end it explicitly
    instead of becoming a paid model call about the word "no"."""
    meaning, line = _read_answer(prompts, prompt, FREE_TEXT)
    if meaning is Answer.ABANDON:
        prompts.write(CORRECTION_ABANDONED)
    return line if meaning is Answer.ANSWER else None


def _hotkey_line(state) -> str:
    """One line so the emergency stop isn't a secret: what to press, or why there is nothing to press."""
    if state.active:
        return f"Emergency stop: press {state.hotkey} at any time - it works even when this window isn't in front."
    reason = f" ({state.reason})" if state.reason else ""
    return (f"Emergency stop hotkey {state.hotkey} is NOT active{reason}. "
            f"Nothing can interrupt an action by keyboard.")


# --- Prompts ----------------------------------------------------------------------------------

def _confirm(read, write, pending=None):
    """The safety gate's confirmation. THE EXCEPTION, and it does not move: the only value that ever
    returns True is the exact word "yes" (capitals and surrounding spaces ignored, as it has always
    been). There is no near-miss re-ask here and no second chance - Rule 4 is the plan prompt only.

    What this slice adds is the OTHER half of "anything else cancels". A command, exit or help typed
    here used to be read as "not yes" and thrown away; now the action is still denied, exactly as
    before, and the line is handed to the console loop for the next turn. The denial comes first and
    does not depend on the handoff: the gate sees False before anything is queued, the cancelled action
    never resumes (the slot is marked abandoned, so nothing asks a follow-up), and the queued line
    starts a brand-new root command with its own safety gate. A command can therefore never be a way to
    approve, defer or resume a Medium-or-above action.
    """
    def confirm(action, assessment) -> bool:
        write(f"Needs your OK - {assessment.level.name} risk ({assessment.rule}):")
        write(f"  {action.description}")
        prompts = Prompts(read=read, write=write, pending=pending)
        meaning, _line = _read_answer(prompts, f"Type {YES} to go ahead (anything else cancels): ",
                                      CLOSED_QUESTION)
        if meaning is Answer.YES:
            return True
        if meaning is Answer.COMMAND:
            write(CONFIRM_ABANDONED_FOR_COMMAND)
        elif meaning in ABANDONMENTS:
            write(CONFIRM_ABANDONED)
        return False
    return confirm


def _offer_retry(read, write, pending=None):
    """The recovery loop's retry offer: the same exact answer.

    A fresh command typed here is handed to the console loop instead of being read as "not yes" and
    thrown away. The answer semantics are unchanged: only YES retries, anything else stops."""
    def offer_retry(result) -> bool:
        write(result.message)
        prompts = Prompts(read=read, write=write, pending=pending)
        meaning, _line = _read_answer(prompts, f"Try again? Type {YES} to retry (anything else stops): ",
                                      CLOSED_QUESTION)
        if meaning is Answer.ABANDON:
            write(RETRY_ABANDONED)
        return meaning is Answer.YES
    return offer_retry


def _focus_settings() -> tuple[float, float, float]:
    return (_positive("console.focus_handover_seconds"), _positive("console.focus_settle_seconds"),
            _positive("console.poll_interval_seconds"))


def _positive(name: str) -> float:
    value = get_setting(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise SettingsError(f"Setting '{name}' must be a positive number of seconds, got {value!r}.")
    return float(value)
