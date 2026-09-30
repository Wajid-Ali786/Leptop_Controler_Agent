"""
The Phase 3 session lifecycle (Slice 1B): one bounded clarification round, the plan's
PROPOSED -> RUNNING -> TERMINAL states, and the user-driven correction that replaces a dead plan.

Everything here is pure. No model is called, nothing is executed, nothing is written anywhere.

Two of these tests are load-bearing rather than descriptive:

  * a step that has been carried out can never be carried out again through the same plan, and
  * a plan that is finished, cancelled, stopped or corrected can never be accepted again.

Model advice is not the guarantee for either. Local state is, which is why the invariants are also
checked across thousands of arbitrary transition orders at the bottom of this file.
"""
import ast
import itertools
from pathlib import Path

import pytest

from app.brain.models import (Intent, NeedsClarification, OpenAppArgs, TypeTextArgs, Understood,
                              WindowControlArgs)
from app.executor.logic import resolve
from app.executor.models import CLOSE_APP, OPEN_APP, TYPE_TEXT, ExecutorAction
from app.planner import logic as session
from app.planner.models import (CLARIFICATION_SPENT, INTERPRETATION_SPENT, MAX_BRAIN_CALLS,
                                MAX_CLARIFICATIONS, MAX_INTERPRETATIONS, MAX_REPLANS,
                                MISSING_CORRECTION, NO_CLARIFICATION_PENDING, NO_PLAN_PENDING,
                                NOT_CORRECTABLE, NOT_PROPOSED, NOT_RUNNING, PLAN_ALREADY_RUNNING,
                                PLAN_FINISHED, PLAN_NO_STEPS, PLAN_PROPOSED, PLAN_RUNNING,
                                PLAN_STATES, PLAN_STILL_LIVE, PLAN_TERMINAL, REPLAN_SPENT,
                                REUSED_PLAN_ID, STALE_PLAN_ID, STEP_ALREADY_DONE, TYPED_CONSOLE,
                                UNKNOWN_STEP, UNSAFE_FAILURE, BrainBudget, ClarificationContinuation,
                                FailureContext, LifecycleRefusal, PendingClarification, PendingPlan,
                                Plan, PlanStep, PlanStepSummary, ReplanRequest, TurnContext)

SECRET = "ZZ-my-diary-password-hunter2-ZZ"


def _build(understood):
    """Built once and shared: a Plan is frozen, and resolve() reads config/config.yaml on every call -
    around 50ms - which the transition-order test below would otherwise pay a hundred thousand times."""
    plan = session.build_plan(understood, TYPED_CONSOLE, resolve)
    assert isinstance(plan, Plan), plan
    return plan


TWO_STEPS = _build(Understood(intents=(Intent(OPEN_APP, OpenAppArgs("calculator"), why="you asked"),
                                       Intent(OPEN_APP, OpenAppArgs("notepad"), why="then this"))))
SECRET_STEP = _build(Understood(intents=(Intent(TYPE_TEXT, TypeTextArgs(SECRET), why="you asked"),),
                                restated=SECRET))


def two_step_plan():
    """open calculator, then open notepad - the scenario named in the Slice 1B requirements."""
    assert len(TWO_STEPS) == 2
    return TWO_STEPS


def proposed(plan=None, text="open the calculator and notepad", plan_id="plan-1"):
    context = session.propose_plan(TurnContext(), plan or two_step_plan(), text, plan_id)
    assert isinstance(context, TurnContext)
    return context


def running(**kwargs):
    context = proposed(**kwargs)
    return session.accept_plan(context, context.pending_plan.plan_id)


# --- A. Clarification begins, is consumed, and can be cancelled ----------------------------------------

def test_a_clarification_is_remembered_with_what_it_was_about():
    needs = NeedsClarification(question="Which editor did you mean?", missing="app")
    context = session.begin_clarification(TurnContext(), needs, original_text="open the editor")
    assert isinstance(context, TurnContext)
    pending = context.pending_clarification
    assert pending == PendingClarification(original_text="open the editor",
                                           question="Which editor did you mean?", missing="app")


def test_answering_consumes_the_question_so_it_cannot_be_answered_twice():
    context = session.begin_clarification(TurnContext(), NeedsClarification("Which one?"), "open it")
    continuation, context = session.answer_clarification(context, "notepad")
    assert context.pending_clarification is None
    again = session.answer_clarification(context, "notepad")
    assert isinstance(again, LifecycleRefusal) and again.reason == NO_CLARIFICATION_PENDING


def test_answering_when_nothing_was_asked_is_refused():
    outcome = session.answer_clarification(TurnContext(), "notepad")
    assert isinstance(outcome, LifecycleRefusal) and outcome.reason == NO_CLARIFICATION_PENDING


def test_a_clarification_can_be_cancelled_and_cancelling_twice_is_harmless():
    context = session.begin_clarification(TurnContext(), NeedsClarification("Which one?"), "open it")
    context = session.cancel_clarification(context)
    assert context.pending_clarification is None
    assert session.cancel_clarification(context).pending_clarification is None


def test_cancel_clears_a_pending_clarification_too():
    context = session.begin_clarification(TurnContext(), NeedsClarification("Which one?"), "open it")
    assert session.cancel(context).pending_clarification is None


# --- B. One clarification round, and only one ----------------------------------------------------------

def test_the_allowance_is_one_round_per_request():
    assert (MAX_INTERPRETATIONS, MAX_CLARIFICATIONS, MAX_REPLANS) == (1, 1, 1)
    assert MAX_BRAIN_CALLS == 3


def test_a_second_question_about_the_same_request_is_refused_rather_than_asked():
    context = session.begin_clarification(TurnContext(), NeedsClarification("Which one?"), "open it")
    _continuation, context = session.answer_clarification(context, "notepad")
    again = session.begin_clarification(context, NeedsClarification("Which notepad?"), "open it")
    assert isinstance(again, LifecycleRefusal) and again.reason == CLARIFICATION_SPENT
    assert context.pending_clarification is None, "a refused question is not left pending"


@pytest.mark.parametrize("between", [session.cancel_clarification, session.cancel,
                                     session.emergency_stop, lambda c: c])
def test_the_allowance_is_spent_by_asking_so_no_route_gets_a_second_round(between):
    """Spent at the question, not at the answer: cancelling, stopping or abandoning the round does not
    hand it back, so there is no path that asks twice for one request."""
    context = session.begin_clarification(TurnContext(), NeedsClarification("Which one?"), "open it")
    context = between(context)
    again = session.begin_clarification(context, NeedsClarification("Which one?"), "open it")
    assert isinstance(again, LifecycleRefusal) and again.reason == CLARIFICATION_SPENT


def test_only_a_new_root_command_gives_a_fresh_allowance():
    context = session.begin_clarification(TurnContext(), NeedsClarification("Which one?"), "open it")
    _continuation, context = session.answer_clarification(context, "notepad")
    context = session.begin_root_command(context)
    assert context.budget == BrainBudget()
    assert isinstance(session.begin_clarification(context, NeedsClarification("Which?"), "x"),
                      TurnContext)


def test_one_interpretation_per_request():
    context = session.spend_interpretation(TurnContext())
    assert isinstance(context, TurnContext) and context.budget.interpretations == 1
    again = session.spend_interpretation(context)
    assert isinstance(again, LifecycleRefusal) and again.reason == INTERPRETATION_SPENT


# --- C. The answer becomes data for the Brain, without calling it --------------------------------------

def test_an_answer_produces_everything_the_future_brain_needs():
    needs = NeedsClarification(question="Which editor did you mean?", missing="app")
    context = session.begin_clarification(TurnContext(), needs, original_text="open the editor")
    continuation, _context = session.answer_clarification(context, "notepad")
    assert continuation == ClarificationContinuation(original_text="open the editor",
                                                    question="Which editor did you mean?",
                                                    missing="app", answer="notepad")


def test_no_provider_is_reached_anywhere_in_the_clarification_round(monkeypatch):
    from app.brain import adapter as brain_adapter

    def refuse(*args, **kwargs):
        raise AssertionError("the lifecycle must not call the model")

    for name in ("send_message", "ping", "get_client"):
        monkeypatch.setattr(brain_adapter, name, refuse)
    context = session.begin_clarification(TurnContext(), NeedsClarification("Which one?"), "open it")
    session.answer_clarification(context, "notepad")


# --- D. A new command clears stale pending interaction -------------------------------------------------

def test_a_new_root_command_drops_a_stale_question():
    context = session.begin_clarification(TurnContext(), NeedsClarification("Which one?"), "open it")
    assert session.begin_root_command(context).pending_clarification is None


def test_a_new_root_command_drops_a_proposal_nobody_accepted():
    context = session.begin_root_command(proposed())
    assert context.pending_plan is None
    assert isinstance(session.accept_plan(context, "plan-1"), LifecycleRefusal)


def test_a_new_root_command_leaves_a_running_plan_alone():
    """Pure state cannot stop work in flight; the caller stops it. Silently forgetting a running plan
    would lose track of what is happening on the machine."""
    context = session.begin_root_command(running())
    assert context.pending_plan is not None and context.pending_plan.state == PLAN_RUNNING


def test_a_new_root_command_keeps_short_term_memory():
    context = session.remember_action(TurnContext(), OPEN_APP, "notepad")
    assert session.begin_root_command(context).previous_action_context.safe_target == "notepad"


def test_short_term_memory_uses_the_privacy_safe_summariser_from_slice_1a():
    context = session.remember_action(TurnContext(), TYPE_TEXT, SECRET)
    assert context.previous_action_context.safe_target is None
    assert SECRET not in repr(context)


# --- E/F/G/H. Proposal and acceptance ------------------------------------------------------------------

def test_a_proposal_starts_proposed_with_nothing_done():
    pending = proposed().pending_plan
    assert pending.state == PLAN_PROPOSED
    assert pending.completed_steps == ()
    assert pending.failure is None


def test_an_empty_plan_is_never_proposed():
    outcome = session.propose_plan(TurnContext(), Plan(steps=()), "do nothing", "plan-1")
    assert isinstance(outcome, LifecycleRefusal) and outcome.reason == PLAN_NO_STEPS


def test_the_matching_id_moves_it_to_running():
    context = session.accept_plan(proposed(), "plan-1")
    assert context.pending_plan.state == PLAN_RUNNING


def test_accepting_the_same_plan_twice_is_refused():
    context = running()
    again = session.accept_plan(context, "plan-1")
    assert isinstance(again, LifecycleRefusal) and again.reason == NOT_PROPOSED
    assert context.pending_plan.state == PLAN_RUNNING, "the first acceptance still stands"


def test_a_stale_id_is_refused():
    outcome = session.accept_plan(proposed(), "plan-0")
    assert isinstance(outcome, LifecycleRefusal) and outcome.reason == STALE_PLAN_ID


def test_accepting_with_no_plan_pending_is_refused():
    outcome = session.accept_plan(TurnContext(), "plan-1")
    assert isinstance(outcome, LifecycleRefusal) and outcome.reason == NO_PLAN_PENDING


def test_a_second_plan_cannot_be_proposed_while_one_is_running():
    outcome = session.propose_plan(running(), two_step_plan(), "something else", "plan-2")
    assert isinstance(outcome, LifecycleRefusal) and outcome.reason == PLAN_ALREADY_RUNNING


# --- I/J/K/L. Completing steps ------------------------------------------------------------------------

def test_no_step_can_complete_before_the_plan_is_running():
    outcome = session.complete_step(proposed(), 1)
    assert isinstance(outcome, LifecycleRefusal) and outcome.reason == NOT_RUNNING


def test_a_completed_step_is_recorded_and_the_plan_keeps_running():
    context = session.complete_step(running(), 1)
    assert context.pending_plan.completed_steps == (1,)
    assert context.pending_plan.state == PLAN_RUNNING


def test_the_same_step_cannot_be_completed_twice():
    """The guard that stops finished work being done again."""
    context = session.complete_step(running(), 1)
    again = session.complete_step(context, 1)
    assert isinstance(again, LifecycleRefusal) and again.reason == STEP_ALREADY_DONE
    assert context.pending_plan.completed_steps == (1,), "and it is not recorded twice"


@pytest.mark.parametrize("number", [0, 3, -1, 99])
def test_a_step_number_that_is_not_in_the_plan_is_refused(number):
    outcome = session.complete_step(running(), number)
    assert isinstance(outcome, LifecycleRefusal) and outcome.reason == UNKNOWN_STEP


def test_completing_every_step_finishes_the_plan():
    context = session.complete_step(running(), 1)
    context = session.complete_step(context, 2)
    assert context.pending_plan.state == PLAN_TERMINAL
    assert context.pending_plan.completed_steps == (1, 2)


def test_steps_completed_out_of_order_still_finish_the_plan_exactly_once():
    context = session.complete_step(running(), 2)
    assert context.pending_plan.state == PLAN_RUNNING
    context = session.complete_step(context, 1)
    assert context.pending_plan.state == PLAN_TERMINAL
    assert sorted(context.pending_plan.completed_steps) == [1, 2]


def test_a_finished_plan_accepts_nothing_further():
    context = session.complete_step(session.complete_step(running(), 1), 2)
    for outcome in (session.complete_step(context, 1), session.accept_plan(context, "plan-1")):
        assert isinstance(outcome, LifecycleRefusal) and outcome.reason == PLAN_FINISHED


def test_remaining_steps_never_includes_a_completed_one():
    context = session.complete_step(running(), 1)
    assert [step.number for step in session.remaining_steps(context.pending_plan)] == [2]


# --- M/N. Cancel and emergency stop -------------------------------------------------------------------

def test_cancelling_makes_the_plan_terminal_and_unrunnable():
    context = session.cancel(running())
    assert context.pending_plan.state == PLAN_TERMINAL
    assert isinstance(session.accept_plan(context, "plan-1"), LifecycleRefusal)
    assert isinstance(session.complete_step(context, 1), LifecycleRefusal)


def test_cancelling_with_nothing_pending_is_harmless():
    assert session.cancel(TurnContext()) == TurnContext()


def test_emergency_stop_clears_every_pending_thing():
    context = session.begin_clarification(running(), NeedsClarification("Which one?"), "open it")
    context = session.emergency_stop(context)
    assert context.pending_plan is None and context.pending_clarification is None


def test_after_an_emergency_stop_nothing_is_runnable_or_correctable():
    context = session.emergency_stop(running())
    for outcome in (session.accept_plan(context, "plan-1"), session.complete_step(context, 1),
                    session.correct_plan(context, "do it differently")):
        assert isinstance(outcome, LifecycleRefusal) and outcome.reason == NO_PLAN_PENDING


def test_an_emergency_stop_keeps_short_term_memory_of_what_already_happened():
    """What was already done is a fact about the machine; stopping does not unmake it."""
    context = session.remember_action(running(), OPEN_APP, "calculator")
    assert session.emergency_stop(context).previous_action_context.safe_target == "calculator"


def test_ending_the_session_leaves_nothing_behind():
    context = session.complete_step(running(), 1)
    context = session.begin_clarification(context, NeedsClarification("Which one?"), SECRET)
    assert session.end_session(context) == TurnContext()


# --- O/X. A failure stores a safe message, and nothing else -------------------------------------------

def test_a_failure_records_the_step_and_the_message_the_user_was_shown():
    context = session.complete_step(running(), 1)
    context = session.fail_plan(context, 2, "I couldn't find notepad.")
    assert context.pending_plan.failure == FailureContext(step_number=2,
                                                          safe_message="I couldn't find notepad.")
    assert context.pending_plan.state == PLAN_TERMINAL
    assert context.pending_plan.completed_steps == (1,), "what was already done is still recorded"


@pytest.mark.parametrize("not_a_message", [RuntimeError("boom"), ValueError("boom"), None, 42,
                                           "", "   ", ["boom"]])
def test_an_exception_or_anything_that_is_not_a_message_is_refused(not_a_message):
    """A traceback or an exception object must never end up somewhere it can be shown to the user."""
    outcome = session.fail_plan(running(), 2, not_a_message)
    assert isinstance(outcome, LifecycleRefusal) and outcome.reason == UNSAFE_FAILURE


def test_a_failure_context_holds_only_two_plain_fields():
    from dataclasses import fields
    assert [field.name for field in fields(FailureContext)] == ["step_number", "safe_message"]
    failure = FailureContext(2, "I couldn't find notepad.")
    assert isinstance(failure.step_number, int) and isinstance(failure.safe_message, str)
    assert not any(isinstance(value, BaseException) for value in vars(failure).values())


def test_a_plan_that_is_not_running_cannot_fail():
    outcome = session.fail_plan(proposed(), 1, "nope")
    assert isinstance(outcome, LifecycleRefusal) and outcome.reason == NOT_RUNNING


def test_a_failure_on_a_step_that_does_not_exist_is_refused():
    outcome = session.fail_plan(running(), 9, "nope")
    assert isinstance(outcome, LifecycleRefusal) and outcome.reason == UNKNOWN_STEP


# --- P/Q/R/T. The user corrects the plan ---------------------------------------------------------------

def failed_after_first_step():
    context = session.complete_step(running(), 1)
    return session.fail_plan(context, 2, "I couldn't find notepad.")


def test_a_correction_produces_a_request_the_future_brain_can_use():
    request, _context = session.correct_plan(failed_after_first_step(), "use wordpad instead")
    assert isinstance(request, ReplanRequest)
    assert request.correction == "use wordpad instead"
    assert request.original_text == "open the calculator and notepad"
    assert request.completed_steps == (1,)
    assert request.failure == FailureContext(2, "I couldn't find notepad.")
    assert [(step.number, step.kind, step.completed) for step in request.steps] == [
        (1, OPEN_APP, True), (2, OPEN_APP, False)]


def test_a_correction_needs_the_user_to_say_something():
    for nothing in ("", "   ", None):
        outcome = session.correct_plan(failed_after_first_step(), nothing)
        assert isinstance(outcome, LifecycleRefusal) and outcome.reason == MISSING_CORRECTION


def test_the_old_plan_is_already_dead_when_the_request_is_produced():
    """There is no moment at which two plans could both be runnable."""
    _request, context = session.correct_plan(failed_after_first_step(), "use wordpad")
    assert context.pending_plan.state == PLAN_TERMINAL
    assert isinstance(session.accept_plan(context, "plan-1"), LifecycleRefusal)


def test_a_rejected_proposal_can_be_corrected_without_ever_having_run():
    context = session.reject_plan(proposed())
    assert context.pending_plan.state == PLAN_TERMINAL
    request, context = session.correct_plan(context, "not notepad, wordpad")
    assert request.completed_steps == () and request.failure is None


def test_a_plan_can_be_corrected_straight_from_the_proposal():
    _request, context = session.correct_plan(proposed(), "not like that")
    assert context.pending_plan.state == PLAN_TERMINAL, "correcting rejects it in the same step"


def test_a_running_plan_cannot_be_corrected_underneath_itself():
    outcome = session.correct_plan(running(), "actually do something else")
    assert isinstance(outcome, LifecycleRefusal) and outcome.reason == PLAN_ALREADY_RUNNING


def test_a_plan_that_finished_successfully_is_not_correctable():
    context = session.complete_step(session.complete_step(running(), 1), 2)
    outcome = session.correct_plan(context, "do it again differently")
    assert isinstance(outcome, LifecycleRefusal) and outcome.reason == NOT_CORRECTABLE


def test_only_one_correction_per_request():
    _request, context = session.correct_plan(failed_after_first_step(), "use wordpad")
    context = session.install_replacement(context, two_step_plan(), "plan-2")
    context = session.reject_plan(context)
    again = session.correct_plan(context, "no, something else again")
    assert isinstance(again, LifecycleRefusal) and again.reason == REPLAN_SPENT


def test_a_replacement_must_carry_a_new_id():
    _request, context = session.correct_plan(failed_after_first_step(), "use wordpad")
    outcome = session.install_replacement(context, two_step_plan(), "plan-1")
    assert isinstance(outcome, LifecycleRefusal) and outcome.reason == REUSED_PLAN_ID


def test_a_replacement_refuses_the_old_id_even_when_it_was_never_registered():
    """Two independent guards, and this proves the second one earns its place. A TurnContext is a plain
    dataclass a caller may build directly, so a dead plan can exist whose id was never recorded as
    spent; install_replacement must still refuse to reuse it."""
    dead = PendingPlan(plan_id="plan-1", original_text="open both", plan=TWO_STEPS,
                       state=PLAN_TERMINAL)
    context = TurnContext(pending_plan=dead, used_plan_ids=frozenset())
    outcome = session.install_replacement(context, two_step_plan(), "plan-1")
    assert isinstance(outcome, LifecycleRefusal) and outcome.reason == REUSED_PLAN_ID
    assert isinstance(session.install_replacement(context, two_step_plan(), "plan-2"), TurnContext)


def test_a_replacement_starts_fresh_and_proposed():
    _request, context = session.correct_plan(failed_after_first_step(), "use wordpad")
    context = session.install_replacement(context, two_step_plan(), "plan-2")
    pending = context.pending_plan
    assert (pending.plan_id, pending.state, pending.completed_steps, pending.failure) == (
        "plan-2", PLAN_PROPOSED, (), None)


def test_a_replacement_cannot_be_installed_while_the_old_plan_is_still_open():
    outcome = session.install_replacement(running(), two_step_plan(), "plan-2")
    assert isinstance(outcome, LifecycleRefusal) and outcome.reason == PLAN_STILL_LIVE


def test_an_id_that_has_already_named_a_plan_is_never_live_again():
    """Found by the transition-order test below, not by hand: propose -> reject -> propose with the same
    id would have made a dead id live, and the "yes" the user gave the first plan would have accepted
    the second one."""
    context = session.reject_plan(proposed())
    outcome = session.propose_plan(context, two_step_plan(), "again", "plan-1")
    assert isinstance(outcome, LifecycleRefusal) and outcome.reason == REUSED_PLAN_ID
    context = session.emergency_stop(context)
    assert context.pending_plan is None, "even with nothing pending, the id stays spent"
    outcome = session.propose_plan(context, two_step_plan(), "again", "plan-1")
    assert isinstance(outcome, LifecycleRefusal) and outcome.reason == REUSED_PLAN_ID


def test_a_spent_id_stays_spent_across_a_new_root_command():
    context = session.begin_root_command(session.reject_plan(proposed()))
    outcome = session.propose_plan(context, two_step_plan(), "a new request", "plan-1")
    assert isinstance(outcome, LifecycleRefusal) and outcome.reason == REUSED_PLAN_ID


def test_ids_are_forgotten_only_when_the_session_ends():
    context = session.end_session(session.reject_plan(proposed()))
    assert context.used_plan_ids == frozenset()
    assert isinstance(session.propose_plan(context, two_step_plan(), "fresh session", "plan-1"),
                      TurnContext)


def test_a_generated_plan_id_is_opaque_and_unique():
    ids = {session.new_plan_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(SECRET not in identifier and "plan" not in identifier for identifier in ids)


# --- S. Completed work can never run again through the old plan ---------------------------------------

def test_the_scenario_from_the_requirements_end_to_end():
    """Two steps; step 1 done; step 2 fails; the user corrects. Step 1 must never run again, and the
    old plan must never be accepted again - by local state, not by trusting the model."""
    context = failed_after_first_step()
    old_id = context.pending_plan.plan_id
    request, context = session.correct_plan(context, "use wordpad for the second one")

    # what the future Brain is TOLD
    assert request.completed_steps == (1,)
    assert [step.completed for step in request.steps] == [True, False]

    # what local state ENFORCES, whatever the Brain then says
    assert isinstance(session.accept_plan(context, old_id), LifecycleRefusal)
    assert isinstance(session.complete_step(context, 1), LifecycleRefusal)
    context = session.install_replacement(context, two_step_plan(), "plan-2")
    assert isinstance(session.accept_plan(context, old_id), LifecycleRefusal)
    context = session.accept_plan(context, "plan-2")
    assert isinstance(session.accept_plan(context, old_id), LifecycleRefusal)


def test_a_reference_to_the_old_pending_plan_cannot_be_used_to_revive_it():
    """The shapes are frozen, and every transition reads the plan from the context - so holding an old
    PROPOSED copy gets you nothing."""
    context = proposed()
    stale = context.pending_plan
    context = session.cancel(context)
    assert stale.state == PLAN_PROPOSED, "the old copy is unchanged, as a frozen value should be"
    assert isinstance(session.accept_plan(context, stale.plan_id), LifecycleRefusal)


def test_completed_steps_are_append_only_across_every_transition():
    context = session.complete_step(running(), 1)
    for step in (session.cancel, session.reject_plan, session.begin_root_command):
        outcome = step(context)
        if isinstance(outcome, TurnContext) and outcome.pending_plan is not None:
            assert 1 in outcome.pending_plan.completed_steps
    _request, corrected = session.correct_plan(session.fail_plan(context, 2, "nope"), "differently")
    assert corrected.pending_plan.completed_steps == (1,)


# --- U. No loop, under any order of transitions -------------------------------------------------------

TRANSITIONS = {
    "interpret": lambda c: session.spend_interpretation(c),
    "ask": lambda c: session.begin_clarification(c, NeedsClarification("Which one?"), "open it"),
    "answer": lambda c: session.answer_clarification(c, "notepad"),
    "propose": lambda c: session.propose_plan(c, two_step_plan(), "open both", "plan-x"),
    "accept": lambda c: session.accept_plan(c, "plan-x"),
    "step1": lambda c: session.complete_step(c, 1),
    "step2": lambda c: session.complete_step(c, 2),
    "fail": lambda c: session.fail_plan(c, 2, "I couldn't find notepad."),
    "reject": lambda c: session.reject_plan(c),
    "correct": lambda c: session.correct_plan(c, "do it differently"),
    "replace": lambda c: session.install_replacement(c, two_step_plan(), "plan-y"),
    "cancel": lambda c: session.cancel(c),
    "stop": lambda c: session.emergency_stop(c),
}


def apply(context, name):
    """Apply one transition, ignoring refusals - a refusal is a no-op by design."""
    outcome = TRANSITIONS[name](context)
    if isinstance(outcome, tuple):
        outcome = outcome[1]
    return context if isinstance(outcome, LifecycleRefusal) else outcome


@pytest.mark.parametrize("length", [3, 4])
def test_the_bounds_hold_under_every_order_of_transitions(length):
    """The real proof that nothing loops: no sequence of user actions, in any order, can buy a fourth
    Brain call, un-complete a step, or bring a finished plan back to life."""
    names = list(TRANSITIONS)
    for sequence in itertools.product(names, repeat=length):
        context = TurnContext()
        completed, terminal = (), False
        for name in sequence:
            context = apply(context, name)
            budget = context.budget
            assert budget.interpretations <= MAX_INTERPRETATIONS, sequence
            assert budget.clarifications <= MAX_CLARIFICATIONS, sequence
            assert budget.replans <= MAX_REPLANS, sequence
            assert budget.calls <= MAX_BRAIN_CALLS, sequence
            pending = context.pending_plan
            if pending is None:
                completed, terminal = (), False
                continue
            assert pending.state in PLAN_STATES, sequence
            if pending.plan_id == "plan-x":
                assert set(completed) <= set(pending.completed_steps), sequence
                assert not (terminal and pending.state != PLAN_TERMINAL), sequence
                completed, terminal = pending.completed_steps, pending.state == PLAN_TERMINAL


def test_no_transition_calls_another_transition_that_would_ask_the_model_again():
    """Structural: the two functions that produce something to send are only ever called by the caller,
    never by each other, so there is no internal path that spends a second allowance."""
    tree = ast.parse(Path("app/planner/logic.py").read_text(encoding="utf-8"))
    producers = {"answer_clarification", "correct_plan"}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            called = {inner.func.id for inner in ast.walk(node)
                      if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)}
            assert not (producers & called), f"{node.name} calls {producers & called}"


# --- V/W. Private text does not appear in a repr ------------------------------------------------------

def secret_plan():
    return SECRET_STEP


def test_the_original_command_does_not_appear_in_a_session_repr():
    context = session.propose_plan(TurnContext(), two_step_plan(), SECRET, "plan-1")
    context = session.begin_clarification(context, NeedsClarification("Which one?"), SECRET)
    for shown in (repr(context), str(context), f"{context}", repr(context.pending_plan),
                  repr(context.pending_clarification)):
        assert SECRET not in shown
        assert "hunter2" not in shown
    assert context.pending_plan.original_text == SECRET, "the value itself is still there to work with"


def test_a_typed_payload_does_not_appear_in_a_plan_repr():
    """Checked on the nested objects, not assumed from TypeTextArgs: a Plan holds PlanSteps, which hold
    ExecutorActions, and a repr walks all of them."""
    plan = secret_plan()
    for shown in (repr(plan), repr(plan.steps[0]), repr(plan.steps[0].action), str(plan)):
        assert SECRET not in shown and "hunter2" not in shown
    assert plan.steps[0].action.target == SECRET


def test_a_typed_payload_does_not_appear_through_a_whole_session():
    context = session.propose_plan(TurnContext(), secret_plan(), SECRET, "plan-1")
    context = session.accept_plan(context, "plan-1")
    context = session.fail_plan(context, 1, "I couldn't type that.")
    request, context = session.correct_plan(context, SECRET)
    for shown in (repr(context), repr(request), str(request), repr(request.steps)):
        assert SECRET not in shown and "hunter2" not in shown


def test_the_step_summary_describes_a_typed_payload_by_its_length():
    context = session.propose_plan(TurnContext(), secret_plan(), "type it", "plan-1")
    summary = session.plan_summary(context.pending_plan)[0]
    assert summary == PlanStepSummary(number=1, kind=TYPE_TEXT, target=f"{len(SECRET)} characters",
                                      why="you asked", completed=False)


def test_the_understood_interpretation_does_not_show_the_request_back():
    assert SECRET not in repr(Understood(intents=(), restated=SECRET))
    assert Understood(intents=(), restated=SECRET).restated == SECRET


def test_a_clarification_answer_does_not_appear_in_a_repr():
    continuation = ClarificationContinuation("open it", "Which one?", "app", SECRET)
    assert SECRET not in repr(continuation) and SECRET not in str(continuation)
    assert continuation.answer == SECRET


def test_the_redaction_reports_a_length_in_the_projects_existing_format():
    """Same shape as app/safety/models.py and ExecutorAction.log_label, so it reads the same in a log."""
    assert f"<{len(SECRET)} characters>" in repr(PendingClarification(SECRET, "Which one?"))
    assert f"<{len(SECRET)} characters>" in repr(ReplanRequest(SECRET, SECRET, ()))


# --- Y. No side effects, and the recovery boundary ----------------------------------------------------

def test_the_whole_lifecycle_touches_no_provider_no_safety_no_executor_and_no_window(monkeypatch):
    from app.brain import adapter as brain_adapter
    from app.executor import adapter as executor_adapter
    from app.executor import logic as executor_logic
    from app.safety import logic as safety
    from app.verifier import logic as verifier

    def refuse(*args, **kwargs):
        raise AssertionError("the session lifecycle must not act")

    for module, names in ((brain_adapter, ("send_message", "ping", "get_client")),
                          (safety, ("authorize", "assess")),
                          (executor_logic, ("execute", "execute_with_recovery")),
                          (executor_adapter, ("launch_app", "click", "send_character")),
                          (verifier, ("active_target", "screens", "snapshot_windows"))):
        for name in names:
            monkeypatch.setattr(module, name, refuse, raising=False)
    for sequence in itertools.product(list(TRANSITIONS), repeat=2):
        context = TurnContext()
        for name in sequence:
            context = apply(context, name)


def test_the_lifecycle_writes_nothing_to_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.rglob("*"))
    context = session.propose_plan(TurnContext(), secret_plan(), SECRET, "plan-1")
    context = session.accept_plan(context, "plan-1")
    session.correct_plan(session.fail_plan(context, 1, "no"), "differently")
    assert set(tmp_path.rglob("*")) == before


def test_semantic_re_planning_does_not_duplicate_the_executors_low_level_recovery():
    """Two different jobs, kept apart on purpose. execute_with_recovery() retries the SAME action when
    the machine got in the way; correct_plan() asks for a DIFFERENT plan because a person said so. So:
    nothing here retries, and nothing here starts without a human sentence."""
    imported = set()
    for node in ast.walk(ast.parse(Path("app/planner/logic.py").read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert "app.executor.logic" not in imported, imported
    assert not [name for name in vars(session) if "retry" in name or "recover" in name]
    for nothing in ("", "   ", None):
        assert isinstance(session.correct_plan(failed_after_first_step(), nothing), LifecycleRefusal)


def test_a_failure_alone_never_starts_a_correction():
    """No automatic re-plan: failing leaves the plan dead and the allowance untouched until a person
    spends it."""
    context = failed_after_first_step()
    assert context.budget.replans == 0
    assert context.pending_plan.state == PLAN_TERMINAL
    assert context.pending_clarification is None
