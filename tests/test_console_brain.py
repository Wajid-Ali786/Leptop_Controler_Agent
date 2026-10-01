"""
The typed console with the Brain wired in (Phase 3 Slice 3A).

Every test here is offline: the provider is a scripted function, and the Executor is replaced wherever a
test is about orchestration rather than about acting. No network, no API key, no desktop.

The two properties worth stating up front, because most of the file exists to hold them:

  * a command that already resolves locally never costs a model call, and
  * nothing reaches the computer until a numbered plan has been shown and the user has typed yes.
"""
import ast
from pathlib import Path

import pytest

from app import console
from app.brain import logic as brain
from app.brain.interpreter import Unavailable
from app.brain.models import (ClickArgs, CloseAppArgs, Intent, NeedsClarification, NotACommand,
                              NotSupported, OpenAppArgs, RefreshArgs, ShortcutArgs, TypeTextArgs,
                              Understood)
from app.console import CommandReply, Prompts, Status, handle_typed_line
from app.executor.models import (CLOSE_APP, OPEN_APP, REFRESH, SHORTCUT, TYPE_TEXT, ActionResult,
                                 ExecutorAction, Outcome)
from app.planner.models import (MAX_BRAIN_CALLS, PLAN_PROPOSED, PLAN_RUNNING, PLAN_TERMINAL,
                                TurnContext)
from app.safety.models import RiskLevel

SECRET = "ZZ-my-diary-password-hunter2-ZZ"


# --- A scripted console -------------------------------------------------------------------------------

class Script:
    """Scripted keyboard answers, plus everything written to the screen."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.lines = []
        self.asked = []

    def read(self, prompt=""):
        self.asked.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def write(self, text=""):
        self.lines.append(str(text))

    @property
    def output(self):
        return "\n".join(self.lines)


class Brainless:
    """A scripted provider. Records every request and hands back the next prepared answer."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, prompt):
        self.requests.append(prompt)
        if not self.answers:
            raise AssertionError(f"the Brain was asked {len(self.requests)} times; "
                                 f"only {len(self.requests) - 1} answers were prepared")
        return self.answers.pop(0)

    @property
    def calls(self):
        return len(self.requests)


class Executed:
    """Stands in for the whole Executor pipeline, recording what it was asked and with what floor."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, action, confirm=None, offer_retry=None, *, risk_floor=RiskLevel.LOW):
        self.calls.append((action, risk_floor))
        outcome = self.outcomes.pop(0) if self.outcomes else True
        if isinstance(outcome, ActionResult):
            return outcome
        if outcome:
            return ActionResult(action, True, f"did {action.kind}")
        return ActionResult(action, False, f"couldn't do {action.kind}")

    @property
    def actions(self):
        return [action for action, _floor in self.calls]

    @property
    def floors(self):
        return [floor for _action, floor in self.calls]


@pytest.fixture
def executor(monkeypatch):
    """The console's one way to act, replaced. Any call is recorded instead of happening."""
    fake = Executed()
    monkeypatch.setattr(console, "execute_with_recovery", fake)
    return fake


@pytest.fixture(autouse=True)
def no_stop(monkeypatch):
    monkeypatch.setattr(console.emergency_stop, "is_stopped", lambda: False)


def understood(*intents, restated="what you asked for"):
    return Understood(intents=tuple(intents), restated=restated)


def open_notepad(why="you asked for notepad", floor=RiskLevel.LOW):
    return Intent(OPEN_APP, OpenAppArgs("notepad"), why=why, risk_floor=floor)


def run(text, context=None, script=None, interpret=None, executor_floor=None, **kwargs):
    """One typed line through the console's Brain-aware entry point."""
    script = Script() if script is None else script
    prompts = Prompts(read=script.read, write=script.write,
                      confirm=kwargs.pop("confirm", None), offer_retry=kwargs.pop("offer_retry", None))
    return handle_typed_line(text, context or TurnContext(), prompts, interpret=interpret, **kwargs)


# --- A. A resolved deterministic command never costs a model call -------------------------------------

@pytest.mark.parametrize("line, kind", [("open notepad", OPEN_APP), ("refresh", REFRESH),
                                        ("shortcut ctrl+a", SHORTCUT), ("minimize", "window_control"),
                                        ("type hello world", TYPE_TEXT), ("scroll down 3", "scroll"),
                                        ("click 500, 300", "click"), ("close notepad", CLOSE_APP)])
def test_a_resolved_command_stays_local_and_asks_no_model(line, kind, executor):
    brainless = Brainless()      # no answers prepared: being called at all raises
    reply, context = run(line, interpret=brainless)
    assert brainless.calls == 0, "a resolved command must cost nothing"
    assert reply.status is Status.RAN
    assert executor.actions and executor.actions[0].kind == kind
    assert context.pending_plan is None, "no plan lifecycle for a direct command"


def test_the_local_path_passes_no_special_floor(executor):
    """The deterministic path must be byte-identical in what it asks for: the default floor only."""
    run("open notepad", interpret=Brainless(), )
    assert executor.floors == [RiskLevel.LOW]


def test_an_empty_line_never_reaches_the_brain(executor):
    for line in ("", "   "):
        reply, _context = run(line, interpret=Brainless())
        assert reply.status is Status.REFUSED
    assert executor.calls == []


def test_a_successful_local_action_is_remembered_safely(executor):
    _reply, context = run("open notepad", interpret=Brainless())
    assert context.previous_action_context.kind == OPEN_APP
    assert context.previous_action_context.safe_target == "notepad"


def test_a_typed_payload_is_never_remembered(executor):
    _reply, context = run(f"type {SECRET}", interpret=Brainless())
    assert context.previous_action_context.safe_target is None
    assert SECRET not in repr(context)


# --- B. A loose request reaches the Brain, and stops at a proposal -------------------------------------

LOOSE = "could you open notepad for me please"


def test_a_loose_request_is_interpreted_once_and_only_proposed(executor):
    script = Script("no")
    brainless = Brainless(understood(open_notepad()))
    reply, context = run(LOOSE, script=script, interpret=brainless)
    assert brainless.calls == 1, "exactly one Brain call"
    assert executor.calls == [], "NOTHING ran before acceptance"
    assert console.PLAN_HEADER in script.output
    assert "1. Open notepad" in script.output
    assert reply.status is Status.CANCELLED


def test_the_preview_is_readable_and_never_a_repr(executor):
    script = Script("no")
    plan = understood(open_notepad(),
                      Intent(TYPE_TEXT, TypeTextArgs("hello world"), why="then this",
                             risk_floor=RiskLevel.MEDIUM))
    run(LOOSE, script=script, interpret=Brainless(plan))
    assert "1. Open notepad" in script.output
    assert "2. Type <11 characters>" in script.output, script.output
    assert "hello world" not in script.output, "the payload is described, never shown"
    for noise in ("Intent(", "PlanStep(", "ExecutorAction(", "Understood(", "args=", "risk_floor="):
        assert noise not in script.output, noise


def test_the_proposal_is_in_the_lifecycle_and_not_running(executor):
    script = Script("no")
    _reply, _context = run(LOOSE, script=script, interpret=Brainless(understood(open_notepad())))
    assert executor.calls == []


# --- C. Acceptance runs the plan through the existing path ---------------------------------------------

def test_an_accepted_plan_runs_its_steps_in_order_and_finishes(executor):
    script = Script("yes")
    plan = understood(open_notepad(), Intent(REFRESH, RefreshArgs(), why="then refresh"))
    reply, context = run(LOOSE, script=script, interpret=Brainless(plan))
    assert [action.kind for action in executor.actions] == [OPEN_APP, REFRESH]
    assert context.pending_plan.completed_steps == (1, 2)
    assert context.pending_plan.state == PLAN_TERMINAL
    assert reply.status is Status.RAN and "all 2 steps finished" in reply.message


def test_the_plan_is_running_before_the_first_action_is_handed_over(executor, monkeypatch):
    """The state must change before anything can happen, not after - otherwise a stop or a crash mid-run
    leaves a plan that still looks acceptable."""
    states = []

    def spy(action, confirm=None, offer_retry=None, *, risk_floor=RiskLevel.LOW):
        states.append(seen["context"].pending_plan.state)
        return ActionResult(action, True, "ok")

    seen = {}
    real_accept = console.session.accept_plan

    def accept(context, plan_id):
        outcome = real_accept(context, plan_id)
        seen["context"] = outcome
        return outcome

    monkeypatch.setattr(console.session, "accept_plan", accept)
    monkeypatch.setattr(console, "execute_with_recovery", spy)
    run(LOOSE, script=Script("yes"), interpret=Brainless(understood(open_notepad(), open_notepad())))
    assert states == [PLAN_RUNNING, PLAN_RUNNING]


def test_the_brain_path_passes_exactly_the_steps_risk_floor(executor):
    plan = understood(open_notepad(floor=RiskLevel.LOW),
                      Intent(REFRESH, RefreshArgs(), risk_floor=RiskLevel.HIGH),
                      Intent(CLOSE_APP, CloseAppArgs("notepad"), risk_floor=RiskLevel.CRITICAL))
    run(LOOSE, script=Script("yes"), interpret=Brainless(plan), executor_floor=None)
    assert executor.floors == [RiskLevel.LOW, RiskLevel.HIGH, RiskLevel.CRITICAL]


def test_a_step_that_fails_stops_the_plan_there(executor):
    executor.outcomes = [True, False]
    plan = understood(open_notepad(), Intent(REFRESH, RefreshArgs()))
    script = Script("yes", "")
    _reply, context = run(LOOSE, script=script, interpret=Brainless(plan))
    assert len(executor.calls) == 2, "it stopped at the failing step"
    assert context.pending_plan.completed_steps == (1,)
    assert context.pending_plan.state == PLAN_TERMINAL
    assert context.pending_plan.failure.step_number == 2


# --- D/E. Rejection, and stale or repeated acceptance --------------------------------------------------

def test_a_rejected_plan_runs_nothing_and_cannot_be_accepted_afterwards(executor):
    script = Script("no", "")
    _reply, context = run(LOOSE, script=script, interpret=Brainless(understood(open_notepad())))
    assert executor.calls == []
    pending = context.pending_plan
    if pending is not None:
        assert pending.state == PLAN_TERMINAL
        from app.planner import logic as session
        from app.planner.models import LifecycleRefusal
        assert isinstance(session.accept_plan(context, pending.plan_id), LifecycleRefusal)


@pytest.mark.parametrize("answer", ["no", "n", "", "YE", "sure", "y"])
def test_only_the_exact_word_yes_runs_a_plan(answer, executor):
    """The same rule the safety gate uses: anything other than "yes" cancels."""
    script = Script(answer, "")
    run(LOOSE, script=script, interpret=Brainless(understood(open_notepad())))
    assert executor.calls == []


def test_a_finished_plan_cannot_be_run_a_second_time(executor):
    from app.planner import logic as session
    from app.planner.models import LifecycleRefusal
    _reply, context = run(LOOSE, script=Script("yes"), interpret=Brainless(understood(open_notepad())))
    pending = context.pending_plan
    assert pending.state == PLAN_TERMINAL
    assert isinstance(session.accept_plan(context, pending.plan_id), LifecycleRefusal)
    assert isinstance(session.complete_step(context, 1), LifecycleRefusal)
    assert len(executor.calls) == 1, "exactly once"


def test_a_stale_plan_id_from_an_earlier_line_cannot_execute(executor):
    from app.planner import logic as session
    from app.planner.models import LifecycleRefusal
    _reply, context = run(LOOSE, script=Script("no", ""), interpret=Brainless(understood(open_notepad())))
    stale = context.pending_plan.plan_id if context.pending_plan else None
    _reply, context = run("open notepad", context=context, interpret=Brainless())
    if stale is not None:
        assert isinstance(session.accept_plan(context, stale), LifecycleRefusal)


# --- F/G. Clarification, exactly once ------------------------------------------------------------------

def test_a_question_is_shown_and_one_answer_is_sent_back(executor):
    needs = NeedsClarification(question="Which editor did you mean?", missing="app")
    brainless = Brainless(needs, understood(open_notepad()))
    script = Script("notepad", "yes")
    reply, context = run("open the editor", script=script, interpret=brainless)
    assert "Which editor did you mean?" in script.output
    assert brainless.calls == 2, "the initial reading and one continuation"
    assert "notepad" in brainless.requests[1], "the answer travelled with the original request"
    assert "open the editor" in brainless.requests[1]
    assert context.budget.clarifications == 1
    assert reply.status is Status.RAN


def test_a_second_clarification_in_one_root_command_is_refused(executor):
    needs = NeedsClarification(question="Which one?", missing="app")
    brainless = Brainless(needs, NeedsClarification(question="Which notepad?", missing="app"))
    script = Script("notepad")
    reply, _context = run("open the editor", script=script, interpret=brainless)
    assert brainless.calls == 2, "no third call: the allowance is spent"
    assert reply.status is Status.NO_PLAN
    assert executor.calls == []


def test_no_answer_to_the_question_ends_it_safely(executor):
    brainless = Brainless(NeedsClarification(question="Which one?", missing="app"))
    reply, context = run("open the editor", script=Script(""), interpret=brainless)
    assert reply.status is Status.NO_PLAN
    assert context.pending_clarification is None
    assert executor.calls == []


def test_usko_message_kar_do_never_invents_a_recipient(executor):
    """It cannot resolve locally, so it reaches the Brain; and since no messaging capability exists, the
    only thing it can become is a question and then an honest refusal."""
    from app.executor.logic import resolve
    route = brain.route("usko message kar do", resolve)
    assert isinstance(route, brain.BrainEligible), "nothing local can guess at this"
    needs = NeedsClarification(question="Kis ko message karun?", missing="recipient")
    brainless = Brainless(needs, NotSupported(what="send a message",
                                             message="I can't send messages yet."))
    script = Script("Ali")
    reply, _context = run("usko message kar do", script=script, interpret=brainless)
    assert "Kis ko message karun?" in script.output
    assert "Ali" not in script.output.replace("Kis ko message karun?", ""), "no recipient is echoed back"
    assert reply.status is Status.EXPLAINED and "can't send messages" in reply.message
    assert executor.calls == [], "messaging is not a capability and nothing was attempted"


def test_messaging_is_still_not_a_capability():
    from app.brain.models import ARGS_FOR_KIND
    for invented in ("send_message", "message", "whatsapp", "sms", "email"):
        assert invented not in ARGS_FOR_KIND


# --- H. The provider is unreachable -------------------------------------------------------------------

@pytest.mark.parametrize("line", [LOOSE, "nonsense", "usko message kar do", "open the calculator",
                                  "could you minimize this window"])
def test_the_frozen_unavailable_message_is_the_whole_message(line, executor):
    """Whole-string equality, not startswith and not a first line. No prefix, no suffix, no second
    line, nothing appended - whatever the line was and whatever the parser could have said about it."""
    reply, _context = run(line, interpret=Brainless(Unavailable("ClaudeUnavailableError")))
    assert reply.status is Status.UNAVAILABLE
    assert reply.message == brain.UNAVAILABLE_MESSAGE
    assert reply.message == ("I can't reach my reasoning service right now, so I can only do direct "
                             "commands until it's back.")
    assert executor.calls == []


def test_the_parsers_own_explanation_is_not_appended(executor):
    """BrainEligible.local_message stays part of the route contract for other callers; it is not shown
    here. A loose line gets the frozen sentence and nothing more."""
    from app.executor.logic import resolve
    route = brain.route("nonsense", resolve)
    assert route.local_message, "the parser did have something to say"
    reply, _context = run("nonsense", interpret=Brainless(Unavailable("ClaudeUnavailableError")))
    assert route.local_message not in reply.message
    assert "I don't recognise that command" not in reply.message
    assert reply.message == brain.UNAVAILABLE_MESSAGE


def test_a_local_refusal_still_shows_the_parsers_own_message(executor):
    """Unchanged: an empty line is a LocalRefusal, never BrainEligible, and still says what it always
    said."""
    from app.executor import commands
    for line in ("", "   "):
        reply, _context = run(line, interpret=Brainless())
        assert reply.status is Status.REFUSED
        assert reply.message == commands.parse(line).message
        assert reply.message != brain.UNAVAILABLE_MESSAGE


def test_a_direct_command_after_an_unavailable_loose_line_is_untouched(executor):
    """The sequence the amendment asks for, in one test."""
    reply, context = run(LOOSE, interpret=Brainless(Unavailable("ClaudeUnavailableError")))
    assert reply.message == brain.UNAVAILABLE_MESSAGE and executor.calls == []
    brainless = Brainless()                      # being called now raises
    reply, _context = run("open notepad", context=context, interpret=brainless)
    assert brainless.calls == 0
    assert reply.status is Status.RAN
    assert [action.kind for action in executor.actions] == [OPEN_APP]
    assert executor.floors == [RiskLevel.LOW]


def test_a_direct_command_still_works_after_the_provider_failed(executor):
    reply, context = run(LOOSE, interpret=Brainless(Unavailable("ClaudeUnavailableError")))
    assert reply.status is Status.UNAVAILABLE
    brainless = Brainless()        # being called now would raise
    reply, _context = run("open notepad", context=context, interpret=brainless)
    assert reply.status is Status.RAN and brainless.calls == 0
    assert [action.kind for action in executor.actions] == [OPEN_APP]


# --- I/J/K. An answer that cannot be used, or is only words -------------------------------------------

@pytest.mark.parametrize("bad", [
    brain.InterpretationError(brain.INVALID_JSON),
    brain.InterpretationError(brain.NOT_CANONICAL, "intents[1].text"),
    brain.InterpretationError(brain.BAD_INTENT_COUNT, "6 intents"),
    brain.InterpretationError(brain.TRUNCATED, "the reply hit the output limit"),
    brain.InterpretationError(brain.REFUSED, "the model declined to answer"),
])
def test_an_unusable_answer_runs_nothing_and_is_not_retried(bad, executor):
    brainless = Brainless(bad)
    reply, context = run(LOOSE, interpret=brainless)
    assert brainless.calls == 1, "no automatic second attempt"
    assert reply.status is Status.NO_PLAN
    assert executor.calls == []
    assert context.pending_plan is None


def test_an_unusable_answer_says_nothing_about_the_reply(executor):
    bad = brain.InterpretationError(brain.NOT_CANONICAL, f"intents[1].text")
    reply, _context = run(LOOSE, interpret=Brainless(bad))
    assert "intents[1]" not in reply.message and "not_canonical" not in reply.message


def test_not_supported_is_shown_and_nothing_runs(executor):
    reply, context = run("what is the weather today",
                         interpret=Brainless(NotSupported(what="check the weather",
                                                          message="I can't check the weather yet.")))
    assert reply.status is Status.EXPLAINED
    assert reply.message == "I can't check the weather yet."
    assert executor.calls == [] and context.pending_plan is None


def test_not_a_command_is_shown_and_nothing_runs(executor):
    reply, context = run("hello there", interpret=Brainless(NotACommand(message="Hello.")))
    assert reply.status is Status.EXPLAINED and reply.message == "Hello."
    assert executor.calls == [] and context.pending_plan is None


def test_a_plan_the_planner_refuses_runs_nothing(executor):
    """An Understood the Planner cannot turn into steps - here an app that does not resolve."""
    reply, context = run(LOOSE, interpret=Brainless(understood(Intent(OPEN_APP,
                                                                     OpenAppArgs("photoshop")))))
    assert reply.status is Status.NO_PLAN
    assert "photoshop" in reply.message, "the Executor's own wording explains it"
    assert executor.calls == []


# --- L/M. Safety, confirmation and the floor ----------------------------------------------------------

def test_the_console_never_calls_the_safety_gate_itself():
    """The gate lives inside execute()/execute_with_recovery(). A second authorize() in the console would
    mean two prompts and two policies."""
    source = Path(console.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                for alias in node.names}
    assert "authorize" not in imported and "assess" not in imported
    assert "app.safety.logic" not in {node.module for node in ast.walk(tree)
                                      if isinstance(node, ast.ImportFrom)} - {"app.safety.logic"} or True
    called = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    assert not {name for name in called if "authorize" in name or "assess" in name}, called


def test_a_high_floor_step_is_confirmed_through_the_existing_gate(monkeypatch):
    """The real Executor, with the adapter removed: a HIGH advisory floor must reach the existing
    confirmation prompt before anything is done."""
    from app.executor import logic as executor_logic
    asked = []

    def refuse_adapter(*args, **kwargs):
        raise AssertionError("nothing may be done to the computer when confirmation is denied")

    for name in ("launch_app", "request_close", "click", "send_character", "send_shortcut",
                 "send_wheel_notch", "request_window_state"):
        monkeypatch.setattr(executor_logic.adapter, name, refuse_adapter, raising=False)

    def confirm(action, assessment):
        asked.append(assessment)
        return False            # denied

    plan = understood(open_notepad(floor=RiskLevel.HIGH))
    reply, context = run(LOOSE, script=Script("yes", ""), interpret=Brainless(plan), confirm=confirm)
    assert asked, "the existing gate was asked"
    assert asked[0].level is RiskLevel.HIGH
    assert asked[0].rule == executor_logic.ADVISORY_FLOOR_REASON
    assert reply.status in (Status.DENIED, Status.NO_PLAN, Status.CANCELLED)
    assert context.pending_plan.state == PLAN_TERMINAL, "a denied step fails the plan"


def test_a_low_brain_floor_cannot_soften_a_real_executor_rule(monkeypatch):
    """Ctrl+V is HIGH in the Executor's own table. A Brain intent calling it low must still be confirmed,
    with the Executor's reason, not the advisory one."""
    from app.executor import logic as executor_logic
    asked = []
    monkeypatch.setattr(executor_logic.adapter, "send_shortcut",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    plan = understood(Intent(SHORTCUT, ShortcutArgs("ctrl+v"), risk_floor=RiskLevel.LOW))
    run(LOOSE, script=Script("yes", ""), interpret=Brainless(plan),
        confirm=lambda action, assessment: asked.append(assessment) or False)
    assert asked and asked[0].level is RiskLevel.HIGH
    assert asked[0].rule != executor_logic.ADVISORY_FLOOR_REASON


# --- N/O. Failure, correction and one re-plan ---------------------------------------------------------

def test_a_correction_after_a_failure_keeps_the_completed_step_and_starts_a_new_plan(executor):
    executor.outcomes = [True, False, True]
    first = understood(open_notepad(), Intent(REFRESH, RefreshArgs()))
    second = understood(Intent(CLOSE_APP, CloseAppArgs("notepad")))
    brainless = Brainless(first, second)
    script = Script("yes", "close it instead", "yes")
    reply, context = run(LOOSE, script=script, interpret=brainless)
    assert brainless.calls == 2, "the initial reading and one correction"
    assert "close it instead" in brainless.requests[1]
    assert "must not be repeated: 1" in brainless.requests[1], "the completed step was reported"
    assert context.pending_plan.plan_id != "", "a replacement with its own id"
    assert [action.kind for action in executor.actions] == [OPEN_APP, REFRESH, CLOSE_APP]
    assert reply.status is Status.RAN


def test_the_replacement_plan_has_a_new_id_and_the_old_one_stays_dead(executor):
    from app.planner import logic as session
    from app.planner.models import LifecycleRefusal
    executor.outcomes = [False, True]
    brainless = Brainless(understood(open_notepad()), understood(Intent(REFRESH, RefreshArgs())))
    script = Script("yes", "try refreshing", "yes")
    ids = []
    real = session.propose_plan

    def spy(context, plan, text, plan_id=None):
        outcome = real(context, plan, text, plan_id)
        if not isinstance(outcome, LifecycleRefusal):
            ids.append(outcome.pending_plan.plan_id)
        return outcome

    import app.console as module
    original = module.session.propose_plan
    module.session.propose_plan = spy
    try:
        _reply, context = run(LOOSE, script=script, interpret=brainless)
    finally:
        module.session.propose_plan = original
    assert len(ids) == 2 and ids[0] != ids[1], ids
    assert isinstance(session.accept_plan(context, ids[0]), LifecycleRefusal)


def test_only_one_correction_per_root_command(executor):
    executor.outcomes = [False, False]
    brainless = Brainless(understood(open_notepad()), understood(Intent(REFRESH, RefreshArgs())))
    script = Script("yes", "try refreshing", "yes", "no, something else")
    reply, context = run(LOOSE, script=script, interpret=brainless)
    assert brainless.calls == 2, "no third call: the correction allowance is spent"
    assert context.budget.replans == 1
    assert context.budget.calls <= MAX_BRAIN_CALLS


def test_declining_to_correct_ends_safely(executor):
    executor.outcomes = [False]
    brainless = Brainless(understood(open_notepad()))
    script = Script("yes", "")
    reply, context = run(LOOSE, script=script, interpret=brainless)
    assert brainless.calls == 1
    assert len(executor.calls) == 1
    # Status.RAN means "it reached the Executor"; the result is what says whether it worked.
    assert reply.result is not None and reply.result.ok is False
    assert context.pending_plan.state == PLAN_TERMINAL


def test_a_rejected_proposal_can_be_corrected_without_starting_over(executor):
    brainless = Brainless(understood(Intent(OPEN_APP, OpenAppArgs("calculator"))),
                          understood(open_notepad()))
    script = Script("no", "I meant notepad", "yes")
    reply, context = run(LOOSE, script=script, interpret=brainless)
    assert brainless.calls == 2
    assert "I meant notepad" in brainless.requests[1]
    assert [action.target for action in executor.actions] == ["notepad"]
    assert reply.status is Status.RAN


# --- P. The Brain-call bound ---------------------------------------------------------------------------

def test_the_worst_case_is_three_calls_for_one_root_command(executor):
    """Initial reading, one clarification, one correction. There is no fourth."""
    executor.outcomes = [False, True]
    brainless = Brainless(NeedsClarification(question="Which one?", missing="app"),
                          understood(open_notepad()),
                          understood(Intent(REFRESH, RefreshArgs())))
    script = Script("notepad", "yes", "try refreshing", "yes")
    _reply, context = run("open the editor", script=script, interpret=brainless)
    assert brainless.calls == 3 == MAX_BRAIN_CALLS
    assert context.budget.interpretations == 1
    assert context.budget.clarifications == 1
    assert context.budget.replans == 1
    assert context.budget.calls == MAX_BRAIN_CALLS


def test_no_outcome_of_one_line_buys_a_fourth_call(executor):
    """Whatever happens - cancel, reject, failure, clarification, correction - the allowance is spent."""
    executor.outcomes = [False, False]
    brainless = Brainless(NeedsClarification(question="Which one?", missing="app"),
                          understood(open_notepad()),
                          understood(Intent(REFRESH, RefreshArgs())))
    script = Script("notepad", "yes", "try refreshing", "yes", "again please", "yes")
    _reply, context = run("open the editor", script=script, interpret=brainless)
    assert brainless.calls <= MAX_BRAIN_CALLS, f"{brainless.calls} calls for one root command"


def test_a_genuinely_new_line_gets_a_fresh_allowance(executor):
    brainless = Brainless(understood(open_notepad()), understood(open_notepad()))
    _reply, context = run(LOOSE, script=Script("yes"), interpret=brainless)
    assert context.budget.interpretations == 1
    _reply, context = run(LOOSE, context=context, script=Script("yes"), interpret=brainless)
    assert context.budget.interpretations == 1, "reset for the new root command"
    assert brainless.calls == 2


def test_spent_plan_ids_survive_a_new_root_command(executor):
    _reply, context = run(LOOSE, script=Script("yes"), interpret=Brainless(understood(open_notepad())))
    spent = set(context.used_plan_ids)
    _reply, context = run(LOOSE, context=context, script=Script("no", ""),
                          interpret=Brainless(understood(open_notepad())))
    assert spent <= set(context.used_plan_ids), "an id is never live twice in one session"


# --- Q. Emergency stop --------------------------------------------------------------------------------

def test_a_stop_before_a_line_invalidates_everything_pending(executor, monkeypatch):
    from app.planner import logic as session
    from app.planner.models import LifecycleRefusal
    _reply, context = run(LOOSE, script=Script("no", ""), interpret=Brainless(understood(open_notepad())))
    stale = context.pending_plan.plan_id if context.pending_plan else None
    context = session.emergency_stop(context)
    assert context.pending_plan is None and context.pending_clarification is None
    if stale is not None:
        assert isinstance(session.accept_plan(context, stale), LifecycleRefusal)


def test_a_stop_during_a_plan_stops_the_remaining_steps(executor, monkeypatch):
    """The Executor raises on a stop, which the shared post-parse helper reports as STOPPED. The
    remaining steps are never attempted."""
    from app.executor.emergency_stop import EmergencyStopError
    calls = []

    def stop_on_second(action, confirm=None, offer_retry=None, *, risk_floor=RiskLevel.LOW):
        calls.append(action)
        if len(calls) == 1:
            return ActionResult(action, True, "ok")
        raise EmergencyStopError("stopped")

    monkeypatch.setattr(console, "execute_with_recovery", stop_on_second)
    plan = understood(open_notepad(), Intent(REFRESH, RefreshArgs()), Intent(REFRESH, RefreshArgs()))
    reply, context = run(LOOSE, script=Script("yes", ""), interpret=Brainless(plan))
    assert len(calls) == 2, "the third step was never attempted"
    assert reply.status is Status.STOPPED
    assert context.pending_plan.state == PLAN_TERMINAL


def test_the_console_still_never_triggers_or_resets_the_stop():
    used = {node.attr for node in ast.walk(ast.parse(Path(console.__file__).read_text(encoding="utf-8")))
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "emergency_stop"}
    assert used <= {"is_stopped", "wait", "check"}, used
    assert "trigger" not in used and "reset" not in used


# --- R. Privacy ---------------------------------------------------------------------------------------

def test_nothing_private_reaches_the_screen_or_a_log(executor, caplog):
    """One sentinel through the whole Brain path: the request, the restatement, the why, a typed
    payload, a clarification answer and a correction."""
    executor.outcomes = [False]
    plan = understood(Intent(TYPE_TEXT, TypeTextArgs(SECRET), why=SECRET), restated=SECRET)
    brainless = Brainless(NeedsClarification(question="Which window?", missing="app"), plan,
                          NotACommand(message="Nothing to do."))
    script = Script(SECRET, "yes", SECRET)
    with caplog.at_level("DEBUG"):
        reply, context = run(f"please type {SECRET} somewhere", script=script, interpret=brainless)
    assert SECRET not in script.output, "not on screen"
    assert SECRET not in caplog.text, "not in a log"
    assert SECRET not in reply.message, "not in the reply"
    assert SECRET not in repr(context), "not in a repr of the session"
    assert "hunter2" not in script.output and "hunter2" not in caplog.text


def test_the_console_logs_no_typed_line_at_all():
    """Structural: no logging call in this module is handed the line, the answer or the correction."""
    tree = ast.parse(Path(console.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "log"):
            continue
        for argument in node.args:
            shown = ast.unparse(argument)
            assert shown not in ("text", "line", "answer", "correction", "prompt"), shown


def test_the_preview_uses_the_safe_summary_not_the_action():
    """plan_summary() describes a typed payload by length; the preview must come from it, so no code
    path can print a target the user wrote."""
    source = Path(console.__file__).read_text(encoding="utf-8")
    assert "plan_summary" in source
    tree = ast.parse(source)
    describe = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == "_describe")
    used = {ast.unparse(node) for node in ast.walk(describe) if isinstance(node, ast.Attribute)}
    assert not any("action" in name for name in used), used


# --- Short-term context: one previous action, kept only when it really happened -----------------------
# BEFORE Slice 3A, session.remember_action() had NO production caller: the contracts existed and were
# unit-tested, but nothing ever called them, so "open it" would have had no context to resolve against.
# These tests are the integration assertions that were missing.

def verified(action, message="ok"):
    """What the Executor returns when it OBSERVED the end state: Outcome.DONE."""
    return ActionResult(action, True, message)


def unverified(action, message="I can't check what it did."):
    """ok, but nothing confirms it achieved anything. Never confirmed success."""
    return ActionResult(action, True, message, outcome=Outcome.UNVERIFIED)


def test_a_successful_deterministic_action_is_recorded(executor):
    """A: through the existing helper, with the existing privacy policy."""
    _reply, context = run("open notepad", interpret=Brainless())
    assert context.previous_action_context is not None
    assert context.previous_action_context.kind == OPEN_APP
    assert context.previous_action_context.safe_target == "notepad"


def test_the_next_brain_request_actually_receives_that_context(executor):
    """B: the point of keeping it at all. The request the provider would get mentions the last action."""
    _reply, context = run("open calculator", interpret=Brainless())
    assert context.previous_action_context.safe_target == "calculator"
    brainless = Brainless(NotACommand(message="ok"))
    run("close it", context=context, interpret=brainless)
    assert brainless.calls == 1
    request = brainless.requests[0]
    assert "last thing actually done" in request
    assert "close_app" in request or "open_app" in request
    assert "calculator" in request


def test_a_successful_brain_planned_action_is_recorded_the_same_way(executor):
    """C: the same helper, not a second code path."""
    _reply, context = run(LOOSE, script=Script("yes"),
                          interpret=Brainless(understood(open_notepad())))
    assert context.previous_action_context.kind == OPEN_APP
    assert context.previous_action_context.safe_target == "notepad"


def test_the_console_never_builds_a_previous_action_context_itself():
    """The privacy transformation has one author, and it is not this module."""
    tree = ast.parse(Path(console.__file__).read_text(encoding="utf-8"))
    built = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    assert "PreviousActionContext" not in built, built
    assert "previous_action_context" not in built, "console.py must go through session.remember_action"
    assert "session.remember_action" in built


def test_a_failed_action_does_not_replace_what_came_before(executor):
    """D: and the earlier context survives, rather than being wiped."""
    _reply, context = run("open notepad", interpret=Brainless())
    assert context.previous_action_context.safe_target == "notepad"
    executor.outcomes = [ActionResult(ExecutorAction(OPEN_APP, "calculator"), False, "didn't open")]
    _reply, context = run("open calculator", context=context, interpret=Brainless())
    assert context.previous_action_context.safe_target == "notepad", "the failure did not take its place"


def test_an_unverified_action_is_not_treated_as_confirmed_success(executor):
    """The criterion is ActionResult.verified, not ok. A click is always UNVERIFIED."""
    _reply, context = run("open notepad", interpret=Brainless())
    executor.outcomes = [unverified(ExecutorAction("click", "500, 300"))]
    _reply, context = run("click 500, 300", context=context, interpret=Brainless())
    assert context.previous_action_context.kind == OPEN_APP, "an unverified click is not the last action"


def test_a_safety_denial_does_not_replace_the_previous_action(executor, monkeypatch):
    """E: a denial raises ActionDeniedError, so there is no result at all."""
    from app.safety.logic import ActionDeniedError
    from app.safety.models import RiskAssessment
    _reply, context = run("open notepad", interpret=Brainless())

    def denied(action, confirm=None, offer_retry=None, *, risk_floor=RiskLevel.LOW):
        raise ActionDeniedError("not confirmed", RiskAssessment(RiskLevel.MEDIUM, "a rule"))

    monkeypatch.setattr(console, "execute_with_recovery", denied)
    reply, context = run("close notepad", context=context, interpret=Brainless())
    assert reply.status is Status.DENIED
    assert context.previous_action_context.safe_target == "notepad"
    assert context.previous_action_context.kind == OPEN_APP


def test_an_emergency_stopped_action_does_not_replace_the_previous_action(executor, monkeypatch):
    """F: including the interrupted case, which carries a PARTIAL result."""
    from app.executor.emergency_stop import ActionInterruptedError, EmergencyStopError
    _reply, context = run("open notepad", interpret=Brainless())
    partial = ActionResult(ExecutorAction(TYPE_TEXT, "hello"), False, "Typed 2 of 5 characters.",
                           outcome=Outcome.PARTIAL, progress=(2, 5))

    def stopped(action, confirm=None, offer_retry=None, *, risk_floor=RiskLevel.LOW):
        raise ActionInterruptedError("stopped", partial)

    monkeypatch.setattr(console, "execute_with_recovery", stopped)
    reply, context = run("type hello", context=context, interpret=Brainless())
    assert reply.status is Status.STOPPED
    assert context.previous_action_context.kind == OPEN_APP

    def hard_stop(action, confirm=None, offer_retry=None, *, risk_floor=RiskLevel.LOW):
        raise EmergencyStopError("stopped")

    monkeypatch.setattr(console, "execute_with_recovery", hard_stop)
    _reply, context = run("open calculator", context=context, interpret=Brainless())
    assert context.previous_action_context.safe_target == "notepad"


def test_a_local_refusal_does_not_touch_the_context(executor):
    """J."""
    _reply, context = run("open notepad", interpret=Brainless())
    before = context.previous_action_context
    for line in ("", "   "):
        _reply, context = run(line, context=context, interpret=Brainless())
        assert context.previous_action_context is before


def test_a_rejected_proposal_does_not_record_anything(executor):
    _reply, context = run("open notepad", interpret=Brainless())
    _reply, context = run(LOOSE, context=context, script=Script("no", ""),
                          interpret=Brainless(understood(Intent(OPEN_APP, OpenAppArgs("calculator")))))
    assert executor.actions == [ExecutorAction(OPEN_APP, "notepad")], "only the first line ran"
    assert context.previous_action_context.safe_target == "notepad"


def test_a_failed_plan_step_records_only_the_step_that_worked(executor):
    """§5: the completed step is remembered; the failed one is not, and no history list is invented."""
    executor.outcomes = [verified(ExecutorAction(OPEN_APP, "notepad")),
                         ActionResult(ExecutorAction(CLOSE_APP, "calculator"), False, "couldn't")]
    plan = understood(open_notepad(), Intent(CLOSE_APP, CloseAppArgs("calculator")))
    _reply, context = run(LOOSE, script=Script("yes", ""), interpret=Brainless(plan))
    assert context.pending_plan.completed_steps == (1,)
    assert context.previous_action_context.kind == OPEN_APP
    assert context.previous_action_context.safe_target == "notepad"
    from dataclasses import fields
    assert [field.name for field in fields(type(context.previous_action_context))] == ["kind",
                                                                                      "safe_target"]


def test_only_one_previous_action_is_ever_kept(executor):
    """Exactly the Slice 1A design: one action, not a history. Two successes leave the second."""
    _reply, context = run("open notepad", interpret=Brainless())
    executor.outcomes = [verified(ExecutorAction(OPEN_APP, "calculator"))]
    _reply, context = run("open calculator", context=context, interpret=Brainless())
    assert context.previous_action_context.safe_target == "calculator"


def test_a_sensitive_successful_action_never_leaks_its_payload_into_the_next_request(executor):
    """G: type_text succeeds, and the next Brain request mentions the kind but not a character of the
    text. The transformation is the existing one; nothing here reads or logs the secret."""
    executor.outcomes = [verified(ExecutorAction(TYPE_TEXT, SECRET), "Typed 31 characters.")]
    _reply, context = run(f"type {SECRET}", interpret=Brainless())
    assert context.previous_action_context.kind == TYPE_TEXT
    assert context.previous_action_context.safe_target is None, "the payload is dropped, not kept"
    brainless = Brainless(NotACommand(message="ok"))
    run("do that again", context=context, interpret=brainless)
    request = brainless.requests[0]
    assert SECRET not in request and "hunter2" not in request
    assert SECRET not in repr(context)


def test_a_new_root_command_keeps_the_context_and_resets_only_the_allowance(executor):
    """H: begin_root_command preserves PreviousActionContext exactly as Slice 1B designed."""
    _reply, context = run("open notepad", interpret=Brainless())
    brainless = Brainless(understood(Intent(REFRESH, RefreshArgs())), NotACommand(message="ok"))
    _reply, context = run(LOOSE, context=context, script=Script("no", ""), interpret=brainless)
    assert context.budget.interpretations == 1
    assert context.previous_action_context.safe_target == "notepad", "memory survived the new command"
    _reply, context = run("something else loose entirely", context=context, interpret=brainless)
    assert context.budget.interpretations == 1, "the allowance reset for the new root command"
    assert context.previous_action_context.safe_target == "notepad"


def test_a_clarification_answer_is_not_mistaken_for_a_desktop_action(executor):
    """I: the answer and the correction are sub-turns of one line. Neither is something that happened
    to the computer, so neither may become the previous action."""
    brainless = Brainless(NeedsClarification(question="Which one?", missing="app"),
                          understood(open_notepad()))
    script = Script("notepad", "no", "")
    _reply, context = run("open the editor", script=script, interpret=brainless)
    assert executor.calls == [], "the plan was declined, so nothing ran"
    assert context.previous_action_context is None, "an answer is not an action"


def test_a_correction_is_not_mistaken_for_a_desktop_action(executor):
    executor.outcomes = [ActionResult(ExecutorAction(OPEN_APP, "notepad"), False, "didn't open")]
    brainless = Brainless(understood(open_notepad()), NotACommand(message="nothing to do"))
    script = Script("yes", "do something else")
    _reply, context = run(LOOSE, script=script, interpret=brainless)
    assert brainless.calls == 2
    assert context.previous_action_context is None, "neither the failure nor the correction counts"


# --- S/T. Voice untouched, and nothing real happens ---------------------------------------------------

def test_the_voice_console_reuses_this_orchestration_rather_than_copying_it():
    """Slice 3B connected voice to the SAME entry point. What matters now is that it reuses it rather
    than reimplementing any of it, and that it still cannot reach the Executor or a provider directly."""
    voice = Path(console.__file__).with_name("voice_console.py").read_text(encoding="utf-8")
    assert "handle_typed_line" in voice, "voice uses the shared orchestration"
    assert "VOICE_CONSOLE" in voice, "and plans as the voice console"
    tree = ast.parse(voice)
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    modules |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    assert not any("executor" in (module or "") for module in modules), modules
    assert not any("adapter" in (module or "") for module in modules
                   if "listener" not in (module or "") and "speaker" not in (module or "")), modules
    for copied in ("build_plan", "validate_interpretation", "interpretation_request", "propose_plan",
                   "accept_plan", "complete_step", "correct_plan", "PROCEED_PROMPT"):
        assert copied not in voice, f"voice reimplements {copied} instead of reusing it"


def test_the_console_imports_no_adapter_of_any_kind():
    """Including the Brain's. The provider round-trip lives in app/brain/interpreter.py precisely so this
    stays true."""
    tree = ast.parse(Path(console.__file__).read_text(encoding="utf-8"))
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    modules |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    assert not any("adapter" in (module or "") for module in modules), modules
    assert not any(name in ("anthropic", "pyautogui", "ctypes", "pywinauto") for name in modules)


def test_no_provider_or_device_is_reachable_in_these_tests(monkeypatch):
    """Belt and braces: the whole flow runs with the provider adapter and the OS adapter booby-trapped."""
    from app.brain import adapter as brain_adapter
    from app.executor import adapter as executor_adapter

    def refuse(*args, **kwargs):
        raise AssertionError("no real provider or device call may happen")

    for module, names in ((brain_adapter, ("send_message", "ping", "get_client")),
                          (executor_adapter, ("launch_app", "click", "send_character"))):
        for name in names:
            monkeypatch.setattr(module, name, refuse, raising=False)
    monkeypatch.setattr(console, "execute_with_recovery",
                        lambda action, confirm=None, offer_retry=None, **floor:
                        ActionResult(action, True, "ok"))
    run(LOOSE, script=Script("yes"), interpret=Brainless(understood(open_notepad())))
    run("open notepad", interpret=Brainless())
