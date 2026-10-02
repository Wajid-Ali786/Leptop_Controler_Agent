"""PHASE 3 FROZEN DONE-WHEN, as one piece of evidence.

docs/step4-production-development.md Section 6 (FROZEN):

    "5 loosely-worded commands (including the mixed-language one from Phase 2) produce correct plans or
     correct clarification questions, the 'usko message kar do' ambiguity case is handled exactly as
     specified above at least 5 times in a row, and a simulated Claude-unavailable test correctly falls
     back to the Phase 0/1 error path instead of crashing."

Everything the three clauses need is spread across tests/test_console_brain.py, tests/test_brain_routing.py
and tests/test_brain_provider.py, which is where the behaviour belongs. This file exists because the
Done-when is a conjunction and one clause had no end-to-end evidence: the mixed-language case from Phase 2
was covered at the ROUTING boundary (it reaches the Brain), in the prompt (it goes out unchanged) and in
the validator (it comes back unchanged) - but nothing carried it through the console to a plan or a
clarification, which is what the Done-when actually asks for.

So: five loose commands end to end, the ambiguity case five times in a row, and the unavailable fallback.

WHAT THIS PROVES AND WHAT IT DOES NOT. The provider is scripted, so these tests prove the PIPELINE turns
an interpretation into the right plan, the right question, or the right refusal, and that nothing runs
when it should not. They say nothing about how well Claude understands any of these sentences - a mocked
reply cannot show that, and the real-provider evidence for that is the Slice 2 and Slice 3A real smokes
recorded in the closeout. Phase 2 made the same distinction for the mixed-language case, where the frozen
clause passed through its "correctly clarified" branch rather than through semantic understanding.
"""
import pytest

from app.brain import logic as brain
from app.brain.interpreter import Unavailable
from app.brain.models import CloseAppArgs, Intent, NeedsClarification, NotSupported, OpenAppArgs
from app.console import Status
from app.executor.logic import resolve
from app.executor.models import CLOSE_APP, OPEN_APP
from app.safety.models import RiskLevel
# The harness the Brain console tests already use - a scripted keyboard, a scripted provider and a
# recording stand-in for the Executor. A second harness would be a second opinion about how the console
# behaves.
from tests.test_console_brain import (Brainless, Script, executor, no_stop, run,  # noqa: F401
                                      understood)

FROZEN_UNAVAILABLE = ("I can't reach my reasoning service right now, so I can only do direct commands "
                      "until it's back.")


def opened(app="notepad", why="you asked for notepad"):
    return Intent(OPEN_APP, OpenAppArgs(app), why=why)


def closed(app="notepad", why="you asked to close notepad"):
    return Intent(CLOSE_APP, CloseAppArgs(app), why=why, risk_floor=RiskLevel.MEDIUM)


# --- Clause 1: five loosely-worded commands ----------------------------------------------------------
# Each is a real sentence nobody would call a command, each reaches the Brain rather than the
# deterministic parser, and each ends in a plan or a question - never a guess and never an action.

FIVE = [
    "could you open notepad for me please",
    "notepad kholo",                      # the mixed-language one from Phase 2
    "usko message kar do",
    "open the editor",
    "ab isko band kar do",
]


@pytest.mark.parametrize("text", FIVE)
def test_none_of_the_five_can_be_handled_without_the_brain(text):
    """The premise of the clause: these are loose, so the deterministic parser cannot serve them. If any
    of them resolved locally it would not be testing Phase 3 at all."""
    outcome = brain.route(text, resolve)
    assert isinstance(outcome, brain.BrainEligible), f"{text!r} did not need the Brain"


def test_one_a_loose_open_produces_a_plan(executor):
    """1/5. "could you open notepad for me please" -> a one-step plan, shown and waiting."""
    shown = Script("no")                                   # declined first: the plan is only PROPOSED
    reply, context = run(FIVE[0], script=shown, interpret=Brainless(understood(opened())))
    assert "1. Open notepad" in shown.output, shown.output
    assert reply.status is Status.CANCELLED
    assert executor.calls == [], "nothing runs before yes"

    accepted, context = run(FIVE[0], script=Script("yes"), interpret=Brainless(understood(opened())))
    assert accepted.status is Status.RAN, accepted.message
    assert [step.action.kind for step in context.pending_plan.plan.steps] == [OPEN_APP]
    assert [action.kind for action in executor.actions] == [OPEN_APP]


def test_two_the_mixed_language_command_produces_a_plan(executor):
    """2/5, THE PHASE 2 CASE. "notepad kholo" - Roman Urdu, which the Listener is required to leave
    exactly as spoken and the deterministic parser refuses because it is verb-first and English.

    When the Brain understands it, it becomes an ordinary plan: no transliteration, no rewriting, and the
    app name resolves into an Executor capability that already exists."""
    shown = Script("no")
    reply, _context = run(FIVE[1], script=shown, interpret=Brainless(understood(opened(why="notepad kholo"))))
    assert "1. Open notepad" in shown.output, shown.output
    assert reply.status is Status.CANCELLED and executor.calls == []

    accepted, context = run(FIVE[1], script=Script("yes"),
                            interpret=Brainless(understood(opened(why="notepad kholo"))))
    assert accepted.status is Status.RAN, accepted.message
    assert [step.action.kind for step in context.pending_plan.plan.steps] == [OPEN_APP]
    assert [action.target for action in executor.actions] == ["notepad"]


def test_two_the_mixed_language_command_may_instead_be_correctly_clarified(executor):
    """2/5, the other permitted branch - and the one Phase 2's own evidence went through. The frozen
    clause accepts "correct plans OR correct clarification questions", so a mixed-language line the Brain
    is unsure of must ask rather than guess at an app."""
    script = Script("notepad", "no")               # answer the question, then decline the plan
    brainless = Brainless(NeedsClarification(question="Notepad kholun?", missing="app"),
                          understood(opened()))
    reply, context = run(FIVE[1], script=script, interpret=brainless)
    assert "Notepad kholun?" in script.output
    assert [step.action.kind for step in context.pending_plan.plan.steps] == [OPEN_APP]
    assert reply.status is Status.CANCELLED
    assert executor.calls == [], "the question alone ran nothing"


def test_three_the_ambiguous_one_asks_and_never_guesses(executor):
    """3/5. Covered in full by clause 2 below; here only as one of the five."""
    script = Script("Ali")
    brainless = Brainless(NeedsClarification(question="Kis ko message karun?", missing="recipient"),
                          NotSupported(what="send a message", message="I can't send messages yet."))
    reply, _context = run(FIVE[2], script=script, interpret=brainless)
    assert "Kis ko message karun?" in script.output
    assert reply.status is Status.EXPLAINED
    assert executor.calls == []


def test_four_a_missing_detail_is_asked_about_before_planning(executor):
    """4/5. "open the editor" names no app this computer has. The right answer is a question, then a plan
    built from the answer - not a guess at which editor was meant."""
    script = Script("notepad", "no")               # answer the question, then decline the plan
    brainless = Brainless(NeedsClarification(question="Which one - notepad or calculator?", missing="app"),
                          understood(opened()))
    reply, context = run(FIVE[3], script=script, interpret=brainless)
    assert "Which one" in script.output
    assert brainless.calls == 2, "the answer went back to the Brain once"
    assert [step.action.kind for step in context.pending_plan.plan.steps] == [OPEN_APP]
    assert reply.status is Status.CANCELLED
    assert executor.calls == [], "a question followed by a declined plan runs nothing"


def test_five_a_loose_mixed_language_close_produces_a_plan_that_keeps_its_risk(executor):
    """5/5. "ab isko band kar do" - loose, mixed-language, and a CLOSE, so the plan must carry the floor
    that closing deserves rather than arriving as an ordinary low-risk step."""
    accepted, context = run(FIVE[4], script=Script("yes"), interpret=Brainless(understood(closed())))
    assert accepted.status is Status.RAN, accepted.message
    assert [step.action.kind for step in context.pending_plan.plan.steps] == [CLOSE_APP]
    assert executor.floors == [RiskLevel.MEDIUM], executor.floors


def test_all_five_only_ever_select_capabilities_the_executor_already_has():
    """Action selection may not invent a capability. Whatever the five turn into, the only kinds any of
    them produced above are kinds the Executor already implements."""
    from app.brain.models import ARGS_FOR_KIND
    from app.executor.logic import _PREPARERS
    assert set(ARGS_FOR_KIND) <= set(_PREPARERS), set(ARGS_FOR_KIND) - set(_PREPARERS)
    for invented in ("send_message", "message", "whatsapp", "sms", "email", "browse", "search_web"):
        assert invented not in ARGS_FOR_KIND


# --- Clause 2: the ambiguity case, five times in a row ------------------------------------------------

@pytest.mark.parametrize("iteration", [1, 2, 3, 4, 5])
def test_usko_message_kar_do_is_handled_the_same_way_five_times_in_a_row(iteration, executor):
    """FROZEN: "Usko message kar do" -> "Ali ko?" rather than guessing. Five consecutive iterations, each
    a fresh TurnContext, asserting the whole specified behaviour:

      * no recipient is guessed
      * exactly ONE clarification question
      * the answer is not echoed back to the screen
      * messaging is still NotSupported, because communication integrations are Phase 10
      * nothing is executed at any point
    """
    script = Script("Ali")
    brainless = Brainless(NeedsClarification(question="Kis ko message karun?", missing="recipient"),
                          NotSupported(what="send a message", message="I can't send messages yet."))
    reply, context = run("usko message kar do", script=script, interpret=brainless)

    questions = [line for line in script.lines if "Kis ko message karun?" in line]
    assert len(questions) == 1, f"iteration {iteration}: {questions}"
    assert brainless.calls == 2, f"iteration {iteration}: one interpretation, one after the answer"
    assert "Ali" not in script.output.replace("Kis ko message karun?", ""), "the recipient was echoed"
    assert reply.status is Status.EXPLAINED and "can't send messages" in reply.message
    assert context.pending_plan is None and context.pending_clarification is None
    assert executor.calls == [], f"iteration {iteration}: something was executed"


def test_messaging_remains_outside_phase_3_entirely():
    """Communication integrations are Phase 10. Nothing was added to make the ambiguity case "work"."""
    from app.brain.models import ARGS_FOR_KIND
    from app.planner.models import TYPED_CONSOLE, VOICE_CONSOLE
    for invented in ("send_message", "message", "whatsapp", "sms", "email"):
        assert invented not in ARGS_FOR_KIND
        assert invented not in TYPED_CONSOLE.may_plan and invented not in VOICE_CONSOLE.may_plan


# --- Clause 3: Claude unavailable falls back to the Phase 0/1 error path ------------------------------

@pytest.mark.parametrize("text", FIVE)
def test_the_unavailable_fallback_is_the_frozen_sentence_and_nothing_runs(text, executor):
    """Whole-string equality on the frozen sentence, for every one of the five - no prefix, no suffix, no
    second line, and no local guess appended."""
    reply, context = run(text, interpret=Brainless(Unavailable("ClaudeUnavailableError")))
    assert reply.status is Status.UNAVAILABLE
    assert reply.message == FROZEN_UNAVAILABLE
    assert reply.message == brain.UNAVAILABLE_MESSAGE
    assert executor.calls == [], "no action without an interpretation"
    assert context.pending_plan is None


def test_direct_commands_still_work_in_the_same_session_after_the_provider_failed(executor):
    """The Phase 0/1 path is what "falls back" means: the same session, immediately afterwards, runs a
    deterministic command locally and asks the Brain nothing at all."""
    unavailable = Brainless(Unavailable("ClaudeUnavailableError"))
    reply, context = run(FIVE[0], interpret=unavailable)
    assert reply.message == FROZEN_UNAVAILABLE

    direct, _context = run("open notepad", context=context,
                           interpret=lambda prompt: pytest.fail("a direct command must not ask the Brain"))
    assert direct.status is Status.RAN, direct.message
    assert [action.kind for action in executor.actions] == [OPEN_APP]
    assert unavailable.calls == 1, "the failed line cost one attempt and nothing more"


def test_an_unavailable_provider_raises_nothing(executor):
    """"instead of crashing": the fallback is a returned reply, not an exception escaping the console."""
    for text in FIVE + ["open notepad", "", "   "]:
        reply, _context = run(text, interpret=Brainless(Unavailable("ClaudeUnavailableError")))
        assert isinstance(reply.status, Status)
