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

from app.brain import interpreter
from app.brain import logic as brain
from app.brain.models import (NeedsClarification, NotACommand, NotSupported, Understood,
                              previous_action_context)
from app.executor import commands, emergency_stop, hotkey
from app.executor.emergency_stop import ActionInterruptedError, EmergencyStopError
from app.executor.logic import execute_with_recovery, resolve
from app.planner import logic as session
from app.planner.models import (TYPED_CONSOLE, FrontEnd, LifecycleRefusal, Plan, PlanRefusal,
                                PlanStepSummary, TurnContext)
from app.executor.models import CLICK, CLOSE_APP, OPEN_APP, REFRESH, SCROLL, SHORTCUT, TYPE_TEXT, WINDOW_CONTROL, \
    ActionResult, ExecutorAction
from app.safety.logic import ActionDeniedError
from app.safety.models import RiskLevel
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
    # Phase 3, for a line the Brain read. Nothing ran in any of these.
    EXPLAINED = "explained"                # the Brain answered in words: not supported, or not a command
    UNAVAILABLE = "unavailable"            # the reasoning service couldn't be reached
    NO_PLAN = "no_plan"                    # no plan could be built, or the reply couldn't be believed
    CANCELLED = "cancelled"                # a plan was shown and the user said no


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
    confirm, offer_retry = _confirm(read, write), _offer_retry(read, write)
    prompts = Prompts(read=read, write=write, confirm=confirm, offer_retry=offer_retry)
    context = TurnContext()
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
PROCEED_PROMPT = f"Proceed? Type {YES} to run it (anything else cancels): "
CANCELLED_MESSAGE = "Nothing was run."
CORRECTION_PROMPT = "Tell me what to do differently, or press Enter to leave it: "
CLARIFY_PROMPT = "Your answer: "
NO_ANSWER_MESSAGE = "No answer given, so nothing was done."
PLAN_DONE = "Done: all {count} step{plural} finished."
NOTHING_TO_DO = "There was nothing to do in that."


@dataclass(frozen=True)
class Prompts:
    """The console's own keyboard and screen, passed in so a test can drive the whole flow.

    `confirm` and `offer_retry` are the SAME prompts the deterministic path uses - the safety gate's
    confirmation is not re-implemented for Brain-planned work."""
    read: Callable[[str], str]
    write: Callable[[str], None]
    confirm: Callable | None = None
    offer_retry: Callable | None = None


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
    return _ask_the_brain(route, context, prompts, focus, interpret, frontend)


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


def _clarify(needs, text, context, prompts, focus, interpret, frontend):
    """Ask the model's question, take ONE answer, and send it back once."""
    started = session.begin_clarification(context, needs, text)
    if isinstance(started, LifecycleRefusal):
        return CommandReply(Status.NO_PLAN, started.message), context
    context = started
    prompts.write(needs.question)
    answer = _ask_text(prompts, CLARIFY_PROMPT)
    if not answer or not answer.strip():
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
    plan = session.build_plan(understood, frontend, resolve)
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
    if not _says_yes(prompts.read, prompts.write, PROCEED_PROMPT):
        rejected = session.reject_plan(context)
        context = rejected if isinstance(rejected, TurnContext) else context
        return _offer_correction(context, prompts, focus, interpret,
                                 CommandReply(Status.CANCELLED, CANCELLED_MESSAGE), frontend)
    accepted = session.accept_plan(context, pending.plan_id)
    if isinstance(accepted, LifecycleRefusal):
        return CommandReply(Status.NO_PLAN, accepted.message), context
    return _run_plan(accepted, prompts, focus, interpret, frontend)


def _run_plan(context, prompts, focus, interpret, frontend):
    """Each step through the SAME path a typed command uses, carrying the step's advisory floor."""
    steps = context.pending_plan.plan.steps
    for step in steps:
        reply = run_action(step.action, confirm=prompts.confirm, offer_retry=prompts.offer_retry,
                          focus=focus, risk_floor=step.risk_floor)
        if reply.status is Status.RAN and reply.result is not None and reply.result.ok:
            prompts.write(f"{step.number}. {reply.message}")
            done = session.complete_step(context, step.number)
            context = done if isinstance(done, TurnContext) else context
            context = _remember(reply, context)
            continue
        failed = session.fail_plan(context, step.number, reply.message)
        context = failed if isinstance(failed, TurnContext) else context
        # The step's own reply is carried through: Status.RAN with result.ok False is how this module has
        # always reported "it reached the Executor and did not work", and the result is what says so.
        return _offer_correction(context, prompts, focus, interpret, reply, frontend)
    plural = "" if len(steps) == 1 else "s"
    return CommandReply(Status.RAN, PLAN_DONE.format(count=len(steps), plural=plural)), context


def _offer_correction(context, prompts, focus, interpret, outcome: CommandReply, frontend):
    """The user may say what they wanted instead - once per root command, and only if they ask.

    This is NOT the Executor's retry, which repeats the same action when the machine got in the way.
    This asks for a DIFFERENT plan because a person said so, and it never happens on its own.
    """
    prompts.write(outcome.message)
    if not context.budget.may_replan or context.pending_plan is None:
        return outcome, context
    correction = _ask_text(prompts, CORRECTION_PROMPT)
    if not correction or not correction.strip():
        return outcome, session.cancel(context)
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
    plan = session.build_plan(outcome, frontend, resolve)
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
    for summary in session.plan_summary(context.pending_plan):
        lines.append(f"  {summary.number}. {_describe(summary)}")
    return lines


def _describe(summary: PlanStepSummary) -> str:
    """One step in plain English. `summary.target` is already the Executor's log-safe label."""
    kind, target = summary.kind, summary.target
    if kind == OPEN_APP:
        return f"Open {target}"
    if kind == CLOSE_APP:
        return f"Close {target}"
    if kind == CLICK:
        return f"Click at {target}"
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


def _ask_text(prompts: Prompts, prompt: str) -> str | None:
    """A free-text answer. None when there is no answer - end of input, or Ctrl+C."""
    try:
        answer = prompts.read(prompt)
    except (EOFError, KeyboardInterrupt):
        prompts.write("")
        return None
    return answer if isinstance(answer, str) else None


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
