"""
Planner decision-making: intent -> ordered plan steps.
Built in Phase 3 (docs/step4 Section 6).

Phase 3 Slice 1A: building a plan, and nothing else. This module is PURE and has no side effects at
all - it does not authorize, execute, verify, read a window or touch a device. It turns an Understood
interpretation into a numbered plan, or refuses to.

It is also the place where the model stops being trusted. Four checks stand between an Intent and a
PlanStep, and all four are ours, not the model's:

  1. the kind must be one the Executor implements - the vocabulary never widens here;
  2. the args must be the typed shape that belongs to that kind;
  3. WE build the ExecutorAction, formatting the canonical target ourselves, so the model never writes
     a target string that would be re-parsed;
  4. the constructed action's target must resolve, by the Executor's own rule.

Then the front end's capability set is applied, so a kind that cannot be executed safely from the
caller it came from never reaches the safety gate.

`resolve` is injected for the same reason as in app/brain/logic.py: the Planner must be able to plan
without importing the Executor's OS adapter, and so without the ability to act.

Slice 1B adds the session LIFECYCLE below plan construction: one bounded clarification round, the
PROPOSED -> RUNNING -> TERMINAL plan states, and the user-driven correction that turns a failed plan
into a request for a new one. All of it is pure, and all of it is transitions between the frozen shapes
in app/planner/models.py - every function here returns new state and changes nothing.

WHAT THIS IS NOT. It is not the Executor's recovery. app/executor/logic.execute_with_recovery() owns
LOW-LEVEL retry: the window wasn't ready, the keystroke didn't land, try that same action again. This
module owns the SEMANTIC layer above it: the plan was wrong, and the USER says so in their own words.
The two never mix - nothing here retries an action, and nothing here is triggered by a failure alone.
A correction happens only because a person asked for one, at most once per request.
"""
from uuid import uuid4

from app.brain.models import (ARGS_FOR_KIND, SCROLL_DIRECTIONS, WINDOW_OPERATIONS, ClickArgs, CloseBrowserArgs, OpenBrowserArgs, ClickTargetArgs,
                              CloseAppArgs, Intent, NeedsClarification, OpenAppArgs, RefreshArgs,
                              ScrollArgs, ShortcutArgs, TypeTextArgs, Understood, WindowControlArgs,
                              previous_action_context)
from app.executor.models import (CLICK, CLOSE_APP, OPEN_APP, REFRESH, SCROLL, SHORTCUT, TYPE_TEXT,
                                 WINDOW_CONTROL, ExecutorAction, Unresolved)
from app.planner.models import (CLARIFICATION_SPENT, MAX_PLAN_STEPS, NO_CLARIFICATION_PENDING,
                                NO_PLAN_PENDING, NOT_CORRECTABLE, NOT_PROPOSED, NOT_RUNNING,
                                PLAN_FINISHED, PLAN_NOT_HERE, PLAN_NO_STEPS, PLAN_PROPOSED,
                                PLAN_RUNNING, PLAN_TERMINAL, PLAN_TOO_MANY_STEPS, PLAN_UNKNOWN_KIND,
                                PLAN_UNRESOLVED, PLAN_WRONG_ARGS, REPLAN_SPENT, REUSED_PLAN_ID,
                                INTERPRETATION_SPENT, MISSING_CORRECTION, PLAN_ALREADY_RUNNING,
                                PLAN_STILL_LIVE, STALE_PLAN_ID, STEP_ALREADY_DONE, UNSAFE_FAILURE,
                                UNKNOWN_STEP, BrainBudget,
                                ClarificationContinuation, FailureContext, LifecycleRefusal, Plan,
                                PlannerOutcome, PlanRefusal, PlanStep, PlanStepSummary, PendingPlan,
                                PendingClarification, ReplanRequest, TurnContext)


def build_plan(understood: Understood, frontend, resolve) -> PlannerOutcome:
    """Turn an Understood interpretation into a numbered Plan, or refuse with a safe message.

    Pure: no safety gate, no Executor, no Verifier, no window, no device. `resolve` is
    app/executor/logic.resolve - see the module docstring.
    """
    intents = tuple(understood.intents)
    if not intents:
        return PlanRefusal(PLAN_NO_STEPS, "There was nothing to do in that.")
    if len(intents) > MAX_PLAN_STEPS:
        return PlanRefusal(PLAN_TOO_MANY_STEPS,
                           f"That would take {len(intents)} steps; I do at most {MAX_PLAN_STEPS} "
                           f"at a time. Ask for part of it.")
    steps = []
    for number, intent in enumerate(intents, start=1):
        step = _step(number, intent, frontend, resolve)
        if isinstance(step, PlanRefusal):
            return step
        steps.append(step)
    return Plan(steps=tuple(steps))


def _step(number: int, intent: Intent, frontend, resolve) -> PlanStep | PlanRefusal:
    """One validated, numbered step - or the reason there isn't one."""
    expected = ARGS_FOR_KIND.get(intent.kind)
    if expected is None:
        return PlanRefusal(PLAN_UNKNOWN_KIND, f"I can't do '{intent.kind}' - that isn't something I know how to do.")
    if not isinstance(intent.args, expected):
        return PlanRefusal(PLAN_WRONG_ARGS, "I understood that, but not well enough to act on it safely.")
    if not frontend.allows(intent.kind):
        return PlanRefusal(PLAN_NOT_HERE, _not_here(intent.kind, frontend))
    target = _target(intent.args)
    if isinstance(target, PlanRefusal):
        return target
    # WE build it; the model never does. `control` is the one field a named click adds, and it carries
    # the user's own words through unchanged - the model chose them, from what the user typed.
    control = intent.args.control if isinstance(intent.args, ClickTargetArgs) else ""
    action = ExecutorAction(intent.kind, target, control.strip() if isinstance(control, str) else "")
    resolution = resolve(action)
    if isinstance(resolution, Unresolved):
        return PlanRefusal(PLAN_UNRESOLVED, resolution.message)   # the Executor's own wording
    return PlanStep(number=number, action=action, why=intent.why, risk_floor=intent.risk_floor)


def _target(args) -> str | PlanRefusal:
    """Format the canonical ExecutorAction target for one typed args shape.

    This is the only place a target string is written, so the format the Executor parses has exactly
    one author. Anything the type system cannot guarantee is checked here."""
    if isinstance(args, (OpenAppArgs, CloseAppArgs)):
        return args.app.strip() if isinstance(args.app, str) else ""
    if isinstance(args, (OpenBrowserArgs, CloseBrowserArgs)):
        return ""                      # args-free, like refresh: there is one assistant browser
    if isinstance(args, ClickTargetArgs):
        # The app, which may be empty: the Executor then uses the one app this session opened, if
        # there is exactly one. The control name is NOT part of the target - see ExecutorAction.
        return args.app.strip().lower() if isinstance(args.app, str) else ""
    if isinstance(args, ClickArgs):
        if isinstance(args.x, bool) or isinstance(args.y, bool):
            return PlanRefusal(PLAN_WRONG_ARGS, "I need whole-number screen coordinates to click.")
        if not isinstance(args.x, int) or not isinstance(args.y, int):
            return PlanRefusal(PLAN_WRONG_ARGS, "I need whole-number screen coordinates to click.")
        return f"{args.x}, {args.y}"
    if isinstance(args, TypeTextArgs):
        return args.text if isinstance(args.text, str) else ""   # verbatim: never trimmed or re-spaced
    if isinstance(args, ShortcutArgs):
        return args.keys.strip() if isinstance(args.keys, str) else ""
    if isinstance(args, ScrollArgs):
        if args.direction not in SCROLL_DIRECTIONS:
            return PlanRefusal(PLAN_WRONG_ARGS, "I can only scroll up or down.")
        if isinstance(args.notches, bool) or not isinstance(args.notches, int):
            return PlanRefusal(PLAN_WRONG_ARGS, "I need a whole number of notches to scroll.")
        return f"{args.direction} {args.notches}"
    if isinstance(args, RefreshArgs):
        return ""
    if isinstance(args, WindowControlArgs):
        if args.operation not in WINDOW_OPERATIONS:
            return PlanRefusal(PLAN_WRONG_ARGS,
                               "Window controls: minimize, maximize, restore or close.")
        return args.operation
    return PlanRefusal(PLAN_WRONG_ARGS, "I understood that, but not well enough to act on it safely.")


def _not_here(kind: str, frontend) -> str:
    """Why this front end cannot carry out this kind yet. Plain, and it says what does work."""
    readable = kind.replace("_", " ")
    return (f"I can't {readable} from the {frontend.name} yet: it would act on this window rather than "
            f"the one you mean. Use the typed console for that.")


# ======================================================================================================
# SESSION LIFECYCLE (Slice 1B)
#
# Every function below takes a TurnContext and returns either a NEW TurnContext or a LifecycleRefusal.
# Nothing is mutated, nothing is executed, nothing is persisted, and no model is called - the two
# functions that produce something for the Brain (answer_clarification, correct_plan) return the data
# for the caller to send, and sending it is not this module's job.
#
# A refusal is returned rather than raised because a stale click, a repeated answer or a second
# correction is ordinary user behaviour, not a program error.
# ======================================================================================================

def new_plan_id() -> str:
    """An opaque id for one proposal.

    Never derived from the request text and never taken from model output, so it carries nothing
    private and says nothing about what was asked. Callers may supply their own id instead, which is
    what the tests do, so the lifecycle itself stays deterministic."""
    return uuid4().hex


# --- The root request, and its bounded number of Brain calls ------------------------------------------

def begin_root_command(context: TurnContext) -> TurnContext:
    """A new thing was asked for. Stale pending interaction goes; short-term memory stays.

    The budget resets here, and only here: that is what makes "at most three Brain calls" a bound on
    one REQUEST rather than on the whole session. A plan that is already RUNNING is left alone - this is
    pure state and cannot stop a run; the caller stops it (emergency_stop) before starting something
    new."""
    pending = context.pending_plan
    if pending is not None and pending.state != PLAN_RUNNING:
        pending = None                      # a proposal nobody accepted is dead, not carried forward
    return TurnContext(previous_action_context=context.previous_action_context,
                       pending_clarification=None,
                       pending_plan=pending,
                       budget=BrainBudget(),
                       used_plan_ids=context.used_plan_ids)   # ids stay spent across requests


def spend_interpretation(context: TurnContext) -> TurnContext | LifecycleRefusal:
    """Record the one interpretation call this request is allowed, before the caller makes it."""
    if not context.budget.may_interpret:
        return LifecycleRefusal(INTERPRETATION_SPENT,
                                "I've already worked out what that means. Ask me again if it was wrong.")
    return _with_budget(context, interpretations=1)


def _with_budget(context: TurnContext, *, interpretations: int = 0, clarifications: int = 0,
                 replans: int = 0) -> TurnContext:
    budget = context.budget
    return _replace(context, budget=BrainBudget(interpretations=budget.interpretations + interpretations,
                                                clarifications=budget.clarifications + clarifications,
                                                replans=budget.replans + replans))


def _replace(context: TurnContext, **changes) -> TurnContext:
    """dataclasses.replace by hand, so this module needs no import beyond the shapes it names."""
    fields = {"previous_action_context": context.previous_action_context,
              "pending_clarification": context.pending_clarification,
              "pending_plan": context.pending_plan,
              "budget": context.budget,
              "used_plan_ids": context.used_plan_ids}
    fields.update(changes)
    return TurnContext(**fields)


# --- Clarification: exactly one round, then it is terminal --------------------------------------------

def begin_clarification(context: TurnContext, needs: NeedsClarification,
                        original_text: str) -> TurnContext | LifecycleRefusal:
    """Remember a question the assistant asked, so the answer can be understood beside the request.

    The allowance is spent HERE, when the question is asked, not when it is answered. Asking commits
    this request to a second Brain call, and spending it at the question is what makes a second round
    impossible by any route - answered, cancelled or abandoned. One question per request, then the
    answer is final."""
    if not context.budget.may_clarify:
        return LifecycleRefusal(CLARIFICATION_SPENT,
                                "I've already asked about that one. Tell me the whole thing again and "
                                "I'll start fresh.")
    pending = PendingClarification(original_text=original_text, question=needs.question,
                                  missing=needs.missing)
    return _with_budget(_replace(context, pending_clarification=pending), clarifications=1)


def answer_clarification(context: TurnContext,
                         answer: str) -> tuple[ClarificationContinuation, TurnContext] | LifecycleRefusal:
    """The user answered. Produce what the future Brain adapter needs, and consume the question.

    Returns (continuation, context). Building the continuation is NOT a model call: the caller sends
    it. The pending clarification is consumed either way, so the same answer cannot be sent twice."""
    pending = context.pending_clarification
    if pending is None:
        return LifecycleRefusal(NO_CLARIFICATION_PENDING, "I wasn't waiting on an answer to anything.")
    continuation = ClarificationContinuation(original_text=pending.original_text,
                                             question=pending.question, missing=pending.missing,
                                             answer=answer)
    return continuation, _replace(context, pending_clarification=None)


def cancel_clarification(context: TurnContext) -> TurnContext:
    """Drop the question. Idempotent, and it does not give the allowance back."""
    return _replace(context, pending_clarification=None)


# --- The plan: PROPOSED -> RUNNING -> TERMINAL, and nothing goes backwards ----------------------------

def propose_plan(context: TurnContext, plan: Plan, original_text: str,
                 plan_id: str | None = None) -> TurnContext | LifecycleRefusal:
    """Show a plan and wait. Nothing has run, and nothing can run until it is accepted by its own id.

    An id that has already named a plan in this session is refused. Without that, proposing again with
    the same id after a rejection would make a dead id live, and the "yes" the user gave to the FIRST
    plan would accept the second one."""
    if not plan.steps:
        return LifecycleRefusal(PLAN_NO_STEPS, "There was nothing to do in that.")
    live = context.pending_plan
    if live is not None and live.state == PLAN_RUNNING:
        return LifecycleRefusal(PLAN_ALREADY_RUNNING,
                                "I'm still working on the last one. Stop it first if you want "
                                "something else.")
    identifier = plan_id if plan_id is not None else new_plan_id()
    if identifier in context.used_plan_ids:
        return LifecycleRefusal(REUSED_PLAN_ID, "That plan number has been used already.")
    pending = PendingPlan(plan_id=identifier, original_text=original_text, plan=plan,
                          state=PLAN_PROPOSED, completed_steps=())
    return _replace(context, pending_plan=pending,
                    used_plan_ids=context.used_plan_ids | {identifier})


def accept_plan(context: TurnContext, plan_id: str) -> TurnContext | LifecycleRefusal:
    """The user said yes to THIS plan. It becomes RUNNING before any step could be carried out.

    The id is required and must match: a stale "yes" - to a plan that was replaced, cancelled or has
    already finished - is refused, so an old proposal can never be brought back to life."""
    pending = context.pending_plan
    if pending is None:
        return LifecycleRefusal(NO_PLAN_PENDING, "There's no plan waiting for an answer.")
    if pending.plan_id != plan_id:
        return LifecycleRefusal(STALE_PLAN_ID, "That was a different plan; it isn't the current one.")
    if pending.state == PLAN_TERMINAL:
        return LifecycleRefusal(PLAN_FINISHED, "That plan is finished; it can't be run again.")
    if pending.state == PLAN_RUNNING:
        return LifecycleRefusal(NOT_PROPOSED, "I'm already working on that one.")
    return _replace(context, pending_plan=_state(pending, PLAN_RUNNING))


def complete_step(context: TurnContext, number: int) -> TurnContext | LifecycleRefusal:
    """Record that one step was carried out. The last one turns the plan TERMINAL.

    This is the guard that stops finished work being done twice: a number already in completed_steps is
    refused, and no transition anywhere ever removes a number from that tuple."""
    pending = context.pending_plan
    if pending is None:
        return LifecycleRefusal(NO_PLAN_PENDING, "There's no plan being run.")
    if pending.state == PLAN_TERMINAL:
        return LifecycleRefusal(PLAN_FINISHED, "That plan is finished; there's nothing left to do.")
    if pending.state != PLAN_RUNNING:
        return LifecycleRefusal(NOT_RUNNING, "That plan hasn't been accepted yet, so nothing has run.")
    numbers = _step_numbers(pending)
    if number not in numbers:
        return LifecycleRefusal(UNKNOWN_STEP, f"There's no step {number} in that plan.")
    if number in pending.completed_steps:
        return LifecycleRefusal(STEP_ALREADY_DONE, f"Step {number} is already done; I won't repeat it.")
    completed = pending.completed_steps + (number,)
    finished = set(completed) == set(numbers)
    return _replace(context, pending_plan=_state(pending, PLAN_TERMINAL if finished else PLAN_RUNNING,
                                                 completed_steps=completed))


def fail_plan(context: TurnContext, step_number: int,
              safe_message: str) -> TurnContext | LifecycleRefusal:
    """A step failed. The plan stops for good, keeping which steps had already been done.

    `safe_message` must be the Executor's own user-facing sentence. An exception is not a string, so
    passing one is refused here rather than being stored and shown to somebody later."""
    pending = context.pending_plan
    if pending is None:
        return LifecycleRefusal(NO_PLAN_PENDING, "There's no plan being run.")
    if pending.state != PLAN_RUNNING:
        return LifecycleRefusal(NOT_RUNNING, "That plan isn't running, so it can't fail.")
    if step_number not in _step_numbers(pending):
        return LifecycleRefusal(UNKNOWN_STEP, f"There's no step {step_number} in that plan.")
    if not isinstance(safe_message, str) or not safe_message.strip():
        return LifecycleRefusal(UNSAFE_FAILURE,
                                "A failure needs the message the user was shown, not an error object.")
    failure = FailureContext(step_number=step_number, safe_message=safe_message)
    return _replace(context, pending_plan=_state(pending, PLAN_TERMINAL, failure=failure))


def reject_plan(context: TurnContext) -> TurnContext | LifecycleRefusal:
    """The user looked at the plan and said no. It dies, but stays available to be corrected."""
    pending = context.pending_plan
    if pending is None:
        return LifecycleRefusal(NO_PLAN_PENDING, "There's no plan waiting for an answer.")
    if pending.state == PLAN_TERMINAL:
        return LifecycleRefusal(PLAN_FINISHED, "That plan is already finished.")
    if pending.state == PLAN_RUNNING:
        return LifecycleRefusal(PLAN_ALREADY_RUNNING,
                                "I've already started that one. Stop it if you want it to stop.")
    return _replace(context, pending_plan=_state(pending, PLAN_TERMINAL))


def cancel(context: TurnContext) -> TurnContext:
    """The user is done with this request. Every pending interaction goes, and the plan cannot run.

    Idempotent, and it never gives an allowance back - a cancelled request is over, not rewound."""
    pending = context.pending_plan
    if pending is not None:
        pending = _state(pending, PLAN_TERMINAL)
    return _replace(context, pending_clarification=None, pending_plan=pending)


def emergency_stop(context: TurnContext) -> TurnContext:
    """The user hit the stop key. Nothing pending survives and nothing becomes runnable.

    Stronger than cancel: the plan is dropped entirely, so there is nothing left to accept, to
    complete, or even to correct. Dropping it is what makes it unrunnable - a PendingPlan is frozen, so
    a copy somebody else still holds cannot be marked TERMINAL from here, and every transition reads
    the plan from the context rather than from a caller's reference. Whatever the user wants next, they
    say again from the start. app/executor/emergency_stop.py owns stopping work already in flight; this
    is only the session's own state."""
    return TurnContext(previous_action_context=context.previous_action_context,
                       pending_clarification=None, pending_plan=None, budget=context.budget,
                       used_plan_ids=context.used_plan_ids)


def end_session(context: TurnContext) -> TurnContext:
    """Session over. Nothing is written anywhere; the state simply stops existing."""
    return TurnContext()


def remember_action(context: TurnContext, kind: str, target: str) -> TurnContext:
    """Keep the safe summary of an action that actually happened, for a follow-up like "close it".

    The privacy decision is not made here: it is app/brain/models.previous_action_context() that
    decides per kind what may be kept, and it drops a type_text payload."""
    return _replace(context, previous_action_context=previous_action_context(kind, target))


# --- The user corrects a plan (never the assistant, and never twice) ----------------------------------

def correct_plan(context: TurnContext,
                 correction: str) -> tuple[ReplanRequest, TurnContext] | LifecycleRefusal:
    """The plan was wrong or it failed, and the user says in their own words what to do instead.

    Returns (request, context) for the caller to send to the Brain later. THE OLD PLAN IS TERMINAL BY
    THE TIME THIS RETURNS, before any replacement exists, so there is no moment at which two plans
    could both be runnable. The user is not editing steps: they are describing what they wanted.

    This is not retry. A step that failed for a mechanical reason is the Executor's business
    (execute_with_recovery); this happens only because a person asked, and only once per request."""
    pending = context.pending_plan
    if pending is None:
        return LifecycleRefusal(NO_PLAN_PENDING, "There's no plan to change.")
    if not context.budget.may_replan:
        return LifecycleRefusal(REPLAN_SPENT,
                                "I've already had one go at correcting that. Tell me the whole thing "
                                "again and I'll start fresh.")
    if pending.state == PLAN_RUNNING:
        return LifecycleRefusal(PLAN_ALREADY_RUNNING,
                                "I'm in the middle of that one. Stop it first, then tell me what to "
                                "change.")
    if pending.state == PLAN_TERMINAL and pending.failure is None and _finished(pending):
        return LifecycleRefusal(NOT_CORRECTABLE, "That one's already done. Just tell me the new thing.")
    if not isinstance(correction, str) or not correction.strip():
        return LifecycleRefusal(MISSING_CORRECTION, "What should I do differently?")
    dead = _state(pending, PLAN_TERMINAL)   # rejected-and-corrected in one step, if it was still open
    request = ReplanRequest(original_text=dead.original_text, correction=correction,
                            steps=plan_summary(dead), completed_steps=dead.completed_steps,
                            failure=dead.failure)
    return request, _with_budget(_replace(context, pending_plan=dead), replans=1)


def install_replacement(context: TurnContext, plan: Plan,
                        plan_id: str | None = None) -> TurnContext | LifecycleRefusal:
    """Put the corrected plan in place of the dead one. It is a NEW plan, with a new id, PROPOSED.

    What local state guarantees: the old plan is TERMINAL and can never be accepted again, and the new
    plan starts with nothing completed, under an id the old "yes" does not match. What it cannot
    guarantee is that the model's new steps don't repeat work - that is what the ReplanRequest tells it,
    and why the request carries the original completed step numbers."""
    old = context.pending_plan
    if old is None:
        return LifecycleRefusal(NO_PLAN_PENDING, "There's nothing being replaced.")
    if old.state != PLAN_TERMINAL:
        return LifecycleRefusal(PLAN_STILL_LIVE,
                                "The last plan is still open; it has to be finished or dropped first.")
    if plan_id is not None and plan_id == old.plan_id:
        return LifecycleRefusal(REUSED_PLAN_ID, "A replacement plan needs its own id.")
    return propose_plan(_replace(context, pending_plan=None), plan, old.original_text, plan_id)


# --- Pure queries -------------------------------------------------------------------------------------

def remaining_steps(pending: PendingPlan) -> tuple[PlanStep, ...]:
    """The steps of this plan that have not been carried out. Never includes a completed one."""
    return tuple(step for step in pending.plan.steps if step.number not in pending.completed_steps)


def plan_summary(pending: PendingPlan) -> tuple[PlanStepSummary, ...]:
    """The plan as it is safe to describe it - to the user, in a log, or to a model.

    Uses ExecutorAction.log_label, so a type_text step is "12 characters" and never the text."""
    return tuple(PlanStepSummary(number=step.number, kind=step.action.kind,
                                 target=step.action.log_label, why=step.why,
                                 completed=step.number in pending.completed_steps)
                 for step in pending.plan.steps)


def _step_numbers(pending: PendingPlan) -> tuple[int, ...]:
    return tuple(step.number for step in pending.plan.steps)


def _finished(pending: PendingPlan) -> bool:
    return bool(pending.plan.steps) and set(pending.completed_steps) == set(_step_numbers(pending))


def _state(pending: PendingPlan, state: str, **changes) -> PendingPlan:
    """A new PendingPlan in a new state. completed_steps is only ever passed forward or added to."""
    fields = {"plan_id": pending.plan_id, "original_text": pending.original_text,
              "plan": pending.plan, "state": state, "completed_steps": pending.completed_steps,
              "failure": pending.failure}
    fields.update(changes)
    return PendingPlan(**fields)
