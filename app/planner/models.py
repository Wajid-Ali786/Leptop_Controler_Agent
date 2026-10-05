"""
Data shapes defined by the Planner (docs/step4 Section 6; Build Plan Section 2: "turns intent into an
ordered list of steps").

Phase 3 Slice 1A: contracts only. Nothing here runs, authorizes or verifies anything, and no method
here changes state - the session shapes at the bottom are data the caller will drive in a later slice.

Reading order: a Plan is the semantic result (what would happen, in order). PendingPlan is the
session's grip on one particular plan (which one, how far it got) - so a Plan has no identity of its
own and cannot be confused with a run of it.
"""
from dataclasses import dataclass, field

from app.brain.models import Interpretation, PreviousActionContext
from app.executor.models import (CLICK, CLICK_TARGET, CLOSE_APP, CLOSE_BROWSER, OPEN_APP,
                                 OPEN_BROWSER, REFRESH, SCROLL, SHORTCUT, TYPE_TEXT, WINDOW_CONTROL,
                                 ExecutorAction)
from app.safety.models import RiskLevel

# A plan stays short enough for a person to read before accepting it, and short enough that a wrong
# plan cannot do much. An execution and safety bound - never a measure of how good the Brain is.
MAX_PLAN_STEPS = 5


@dataclass(frozen=True, repr=False)
class PlanStep:
    """One step of a plan: a real ExecutorAction, plus what the user needs in order to inspect it.

    `risk_floor` travels with the step because it becomes Action.minimum_level at the safety gate,
    where it can raise the level but never lower it.

    `why` comes from the model and may echo the user's own words, so like Intent.why it is display text
    rather than log metadata and the repr gives its length only. The value is unchanged."""
    number: int
    action: ExecutorAction
    why: str = ""
    risk_floor: RiskLevel = RiskLevel.LOW

    def __repr__(self) -> str:
        length = len(self.why) if isinstance(self.why, str) else 0
        return (f"PlanStep(number={self.number!r}, action={self.action!r}, "
                f"why=<{length} characters>, risk_floor={self.risk_floor!r})")


@dataclass(frozen=True)
class Plan:
    """An ordered, numbered, inspectable list of steps. Semantic data only - no identity, no state."""
    steps: tuple[PlanStep, ...]

    def __len__(self) -> int:
        return len(self.steps)


@dataclass(frozen=True)
class PlanRefusal:
    """No plan was built, and why. `message` is safe to show."""
    reason: str
    message: str


# Why a plan could not be built. Categories, safe to log - never the request text.
PLAN_NO_STEPS = "no_steps"                   # an Understood interpretation with nothing in it
PLAN_TOO_MANY_STEPS = "too_many_steps"       # more than MAX_PLAN_STEPS
PLAN_UNKNOWN_KIND = "unknown_kind"           # a kind the Executor does not implement
PLAN_WRONG_ARGS = "wrong_args"               # args that do not match the kind
PLAN_UNRESOLVED = "unresolved_target"        # the constructed action's target does not resolve
PLAN_NOT_HERE = "not_available_here"         # this front end may not plan that kind

PlannerOutcome = Plan | PlanRefusal


# --- Which capabilities each front end may have the BRAIN plan ----------------------------------------
# Phase 2 measured that app/console.py builds a FocusHandover and voice mode does not, and that
# console.HANDS_OVER covers six kinds - so in voice mode all six would act on the console window the
# user is talking to. Until the Phase 9 command window exists, the Brain may only plan the two kinds
# that need no hand-over there.
#
# This restricts what the BRAIN may newly plan. It does not touch the exact deterministic commands
# Phase 1/2 already ship: those never come through the Planner, and none of them is removed.

@dataclass(frozen=True)
class FrontEnd:
    """The caller's identity and what the Brain may plan for it. Passed in, never inferred from global
    state, and never taken from the model."""
    name: str
    may_plan: frozenset[str]

    def allows(self, kind: str) -> bool:
        return kind in self.may_plan


ALL_KINDS = frozenset({OPEN_APP, CLOSE_APP, CLICK, CLICK_TARGET, TYPE_TEXT, SHORTCUT, SCROLL, REFRESH,
                       WINDOW_CONTROL, OPEN_BROWSER, CLOSE_BROWSER})
# click_target is deliberately NOT here: like a coordinate click it needs the target window in front,
# so in voice mode it would act on the console the user is talking to. Phase 3's limitation, unchanged.
NO_HANDOVER_KINDS = frozenset({OPEN_APP, CLOSE_APP})

TYPED_CONSOLE = FrontEnd(name="typed console", may_plan=ALL_KINDS)
VOICE_CONSOLE = FrontEnd(name="voice console", may_plan=NO_HANDOVER_KINDS)


# --- Session state: in memory, never persisted --------------------------------------------------------
# Defined in Slice 1A so the later slices had somewhere coherent to put what they need; the pure
# transitions between these states arrive in Slice 1B and live in app/planner/logic.py, not here. These
# stay data: no execution, and no method that changes anything.

PLAN_PROPOSED = "proposed"       # shown to the user; nothing has run
PLAN_RUNNING = "running"         # accepted; steps are being executed
PLAN_TERMINAL = "terminal"       # finished, failed, cancelled or stopped - it can never run again
PLAN_STATES = (PLAN_PROPOSED, PLAN_RUNNING, PLAN_TERMINAL)

# There is deliberately no fourth "failed but correctable" state. A failed plan is TERMINAL, because
# nothing about it may ever run again; whether the user may CORRECT it is a separate question, answered
# by the failure context plus the remaining re-plan allowance, not by making the plan runnable.


def _private(text: str) -> str:
    """How a private string appears in a repr: its length, never its content.

    The project's existing convention, matching app/safety/models.py, app/listener/models.py and
    ExecutorAction.log_label - always "characters", so the count alone can never be mistaken for a
    word of the text itself. The stored value is untouched; this is about accidental display."""
    return f"<{len(text) if isinstance(text, str) else 0} characters>"


@dataclass(frozen=True, repr=False)
class PendingClarification:
    """A question the assistant asked and is waiting on.

    `original_text` is kept because the answer alone is meaningless - "calculator" only means something
    beside the request it answers. It is held in memory, and its repr shows a length only: it is the
    user's own words, and a repr is the way private text reaches a log by accident."""
    original_text: str
    question: str
    missing: str = ""

    def __repr__(self) -> str:
        return (f"PendingClarification(original_text={_private(self.original_text)}, "
                f"question={self.question!r}, missing={self.missing!r})")


@dataclass(frozen=True, repr=False)
class ClarificationContinuation:
    """Everything the future Brain adapter needs to interpret an answer to a question it asked.

    Pure data, built by app/planner/logic.answer_clarification(). Building one is NOT a model call: the
    caller sends it on, and this slice never does. Both the request and the answer are the user's own
    words, so both are private in the repr."""
    original_text: str
    question: str
    missing: str
    answer: str

    def __repr__(self) -> str:
        return (f"ClarificationContinuation(original_text={_private(self.original_text)}, "
                f"question={self.question!r}, missing={self.missing!r}, "
                f"answer={_private(self.answer)})")


@dataclass(frozen=True)
class FailureContext:
    """Why a running plan stopped. `safe_message` is the Executor's own user-facing message - never an
    exception, never a traceback, so it is the one string here that is meant to be readable."""
    step_number: int
    safe_message: str


@dataclass(frozen=True)
class PlanStepSummary:
    """One step of a plan, reduced to what is safe to send to a cloud model.

    Deliberately NOT a PlanStep: a type_text step's target is the user's own words. `target` is the
    action's log_label, so typed text is described by its length and nothing else."""
    number: int
    kind: str
    target: str
    why: str = ""
    completed: bool = False


@dataclass(frozen=True, repr=False)
class ReplanRequest:
    """The user rejected or saw a plan fail, and said in their own words what to do instead.

    Pure data for the future Brain adapter, built by app/planner/logic.correct_plan(). It carries the
    SAFE summary of the old plan, which original steps completed, and the failure message if there was
    one - never an exception, never a traceback, never a raw type_text payload."""
    original_text: str
    correction: str
    steps: tuple[PlanStepSummary, ...]
    completed_steps: tuple[int, ...] = ()
    failure: FailureContext | None = None

    def __repr__(self) -> str:
        return (f"ReplanRequest(original_text={_private(self.original_text)}, "
                f"correction={_private(self.correction)}, steps={self.steps!r}, "
                f"completed_steps={self.completed_steps!r}, failure={self.failure!r})")


@dataclass(frozen=True, repr=False)
class PendingPlan:
    """The session's grip on ONE plan: which plan, and how far it got.

    `plan_id` lives here rather than on Plan so that accepting twice, or re-planning, cannot be
    confused with the semantic plan itself. `completed_steps` holds step numbers only - the outcome of
    the run belongs to the Executor's own results, not to a second copy here - and no transition in
    app/planner/logic.py ever removes a number from it, which is what stops finished work being
    treated as pending again."""
    plan_id: str
    original_text: str
    plan: Plan
    state: str = PLAN_PROPOSED
    completed_steps: tuple[int, ...] = ()
    failure: FailureContext | None = None

    def __repr__(self) -> str:
        return (f"PendingPlan(plan_id={self.plan_id!r}, original_text={_private(self.original_text)}, "
                f"plan={self.plan!r}, state={self.state!r}, "
                f"completed_steps={self.completed_steps!r}, failure={self.failure!r})")


# --- How many Brain calls one root request may cause --------------------------------------------------
# A LOOP bound, not a cost control: Rule 8's rate, token and money limits already exist in
# app/brain/cost_controls.py and are not duplicated here. This answers a different question - "can this
# one request keep asking the model?" - and the answer is: at most three times, and never on its own.

MAX_INTERPRETATIONS = 1     # the first reading of the request
MAX_CLARIFICATIONS = 1      # one round of "which one did you mean?"
MAX_REPLANS = 1             # one user-driven correction
MAX_BRAIN_CALLS = MAX_INTERPRETATIONS + MAX_CLARIFICATIONS + MAX_REPLANS


@dataclass(frozen=True)
class BrainBudget:
    """What one root request has already spent. Every allowance is one, so nothing can loop: a second
    clarification or a second correction is refused rather than asked for."""
    interpretations: int = 0
    clarifications: int = 0
    replans: int = 0

    @property
    def calls(self) -> int:
        return self.interpretations + self.clarifications + self.replans

    @property
    def may_interpret(self) -> bool:
        return self.interpretations < MAX_INTERPRETATIONS

    @property
    def may_clarify(self) -> bool:
        return self.clarifications < MAX_CLARIFICATIONS

    @property
    def may_replan(self) -> bool:
        return self.replans < MAX_REPLANS


@dataclass(frozen=True)
class TurnContext:
    """Everything the session remembers, which is deliberately almost nothing.

    Session-only, never written to disk. Phase 4 owns memory; Phase 6 owns real conversation.

    `used_plan_ids` is the one piece of history kept, and it exists for a single reason: an id the user
    has already been shown must never identify a second plan, or a stale "yes" to the first plan would
    accept the second one. Opaque ids only, no request text, and gone when the session ends."""
    previous_action_context: PreviousActionContext | None = None
    pending_clarification: PendingClarification | None = None
    pending_plan: PendingPlan | None = None
    budget: BrainBudget = BrainBudget()
    used_plan_ids: frozenset[str] = frozenset()   # an id is never live twice in one session


@dataclass(frozen=True)
class LifecycleRefusal:
    """A transition that was asked for and not allowed, and why. `message` is safe to show.

    Returned instead of raising, because a stale click or a repeated answer is ordinary user behaviour,
    not an error - and because a refusal that must be handled is harder to ignore than an exception."""
    reason: str
    message: str


# Why a transition was refused. Categories, safe to log - never the request text.
STALE_PLAN_ID = "stale_plan_id"                  # that id is not the plan currently pending
NO_PLAN_PENDING = "no_plan_pending"              # there is no plan to act on
NOT_PROPOSED = "not_proposed"                    # already accepted, or already finished
NOT_RUNNING = "not_running"                      # steps can only complete while the plan is running
PLAN_ALREADY_RUNNING = "plan_already_running"    # one live plan at a time
PLAN_STILL_LIVE = "plan_still_live"              # a replacement needs the old plan finished first
PLAN_FINISHED = "plan_finished"                  # TERMINAL: it can never run again
UNKNOWN_STEP = "unknown_step"                    # no step with that number
STEP_ALREADY_DONE = "step_already_done"          # it has already been carried out once
NO_CLARIFICATION_PENDING = "no_clarification"    # nothing was asked
INTERPRETATION_SPENT = "interpretation_spent"    # this request has already been read once
CLARIFICATION_SPENT = "clarification_spent"      # the one allowed round is used up
REPLAN_SPENT = "replan_spent"                    # the one allowed correction is used up
NOT_CORRECTABLE = "not_correctable"              # nothing has been rejected or has failed
MISSING_CORRECTION = "missing_correction"        # the user must say what to do differently
UNSAFE_FAILURE = "unsafe_failure"                # a failure was given something other than its message
REUSED_PLAN_ID = "reused_plan_id"                # a replacement must be a new plan, with a new id
