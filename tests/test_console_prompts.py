"""
SLICE 1 of the whole-project weakness plan: ONE rule set for every console prompt.

Four of the owner's fifteen real-session failures were here, and none of them was a missing feature:

  * a command typed at the plan "Proceed?" prompt was consumed as "not yes" and thrown away;
  * `e` and `yees` at that prompt killed the plan instead of being asked again;
  * `exit` at "Try again?" was queued correctly and then followed by ANOTHER question, so the console
    did not leave;
  * "which browser?" answered `yes` spent the single clarification round and left nothing behind.

The rule set is in app/console.py's module docstring; classify_answer() is the whole of it, and this
file is the matrix for it. The numbers in the docstrings are the brief's.

WHAT DELIBERATELY DID NOT CHANGE, and is pinned here rather than assumed:
  * The Medium-and-above confirmation still takes only the exact word "yes". A command typed there
    cancels the action and is queued; it can never approve, defer or resume one.
  * MAX_CLARIFICATIONS is still 1 and the Brain allowance is still 3 per root command. Rule 6 does not
    buy a second round - it stops an unusable answer from cashing the first one.
  * A caller with no console loop to hand a line back to (app/voice_console.py) is bit-identical: one
    `if not handoff` in classify_answer is the whole reason.
"""
import ast
import inspect

import pytest

from app import console
from app.brain.models import (CloseAppArgs, Intent, NeedsClarification, NotACommand, OpenAppArgs,
                              Understood)
from app.console import (ABANDON_WORDS, CLARIFICATION, CLOSED_QUESTION, FREE_TEXT, Answer,
                         PendingCommand, Prompts, Status, classify_answer, handle_typed_line,
                         run_console)
from app.executor.models import ActionResult, CLOSE_APP, ExecutorAction, OPEN_APP
from app.planner.models import MAX_BRAIN_CALLS, TurnContext
from app.safety.models import Action, RiskAssessment, RiskLevel

LOOSE = "could you open notepad for me please"
# The owner's exact line, typed at the plan prompt. Not paraphrased: the point is that it survives
# whole, including the capital, the full stop and the stray "the".
REAL_SESSION_LINE = "Open M. Shakar Profile in the chrome"


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
    """A scripted provider. One prepared answer per call, and it REFUSES to be called again - so an
    extra model call is a loud failure rather than a quiet cost."""

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


class NoFocus:
    def note_console_window(self):
        pass

    def hand_over(self, *args, **kwargs):
        return None


@pytest.fixture(autouse=True)
def no_stop(monkeypatch):
    monkeypatch.setattr(console.emergency_stop, "is_stopped", lambda: False)


def understood(*intents):
    return Understood(intents=tuple(intents) or (open_notepad(),), restated="what you asked for")


def open_notepad():
    return Intent(OPEN_APP, OpenAppArgs("notepad"), why="you asked for notepad",
                  risk_floor=RiskLevel.LOW)


def plan_prompt(*answers, interpret=None, pending=None, context=None):
    """One Brain-eligible line, through the real orchestration, up to and past the plan prompt."""
    script = Script(*answers)
    pending = PendingCommand() if pending is None else pending
    prompts = Prompts(read=script.read, write=script.write, pending=pending)
    interpret = Brainless(understood()) if interpret is None else interpret
    reply, ctx = handle_typed_line(LOOSE, context or TurnContext(), prompts, interpret=interpret,
                                   focus=NoFocus())
    return reply, ctx, script, pending


@pytest.fixture
def executor(monkeypatch):
    """The console's one way to act, replaced. Every call is recorded instead of happening."""
    calls = []

    def fake(action, confirm=None, offer_retry=None, *, risk_floor=RiskLevel.LOW):
        calls.append(action)
        return ActionResult(action, True, f"did {action.kind} {action.target}")

    monkeypatch.setattr(console, "execute_with_recovery", fake)
    return calls


# ======================================================================================================
# RULE 1 - A COMMAND IS NEVER CONSUMED (matrix 1-6)
# ======================================================================================================

def test_a_deterministic_command_at_the_plan_prompt_is_queued_and_the_plan_abandoned(executor):
    """1. The plan is dead and the line is waiting. Nothing was run, and nothing was lost."""
    reply, _ctx, _script, pending = plan_prompt("open calculator")
    assert reply.status is Status.CANCELLED
    assert pending.take() == "open calculator"
    assert executor == [], "a cancelled plan ran a step"


def test_a_natural_language_command_at_the_plan_prompt_is_queued_unchanged(executor):
    """2. The half the old detector could not do. "in the browser click on Demo Account" has no verb
    the deterministic parser recognises, so is_fresh_command() alone would have swallowed it. At a
    yes-or-no prompt it does not have to be recognised - it only has to not be an answer."""
    line = "in the browser click on Demo Account"
    assert console.is_fresh_command(line) is False, "the premise: the narrow detector misses it"
    _reply, _ctx, _script, pending = plan_prompt(line)
    assert pending.take() == line


def test_the_owners_exact_line_survives_the_plan_prompt_whole(executor):
    """3. Byte for byte, including the capital and the stray "the".

    Also records WHICH route catches it, because that was worth knowing and is not obvious: the line
    starts with "Open", so the deterministic parser does recognise the verb and the narrow detector
    would have queued this one anyway. It is the line in test 2 - no leading verb - that needed the
    closed-question rule. Both are pinned so a regression says which half broke."""
    assert console.is_fresh_command(REAL_SESSION_LINE) is True, (
        "the parser recognises 'Open ...'; test 2 covers the lines it does not")
    _reply, _ctx, _script, pending = plan_prompt(REAL_SESSION_LINE)
    assert pending.take() == REAL_SESSION_LINE


def test_the_owners_exact_line_reaches_the_outer_loop_exactly_once(monkeypatch):
    """3, the other half, through the REAL run_console loop: received once, not twice, not never.

    The loop is driven to the plan prompt by a Brain-eligible line, the owner's line is typed there,
    and what the loop then does with it is recorded at handle_typed_line - the one door into the
    orchestration."""
    seen = []
    real = console.handle_typed_line

    def watched(text, context, prompts, **kwargs):
        seen.append(text)
        kwargs.setdefault("interpret", Brainless(understood()))
        return real(text, context, prompts, **kwargs)

    monkeypatch.setattr(console, "handle_typed_line", watched)
    monkeypatch.setattr(console, "execute_with_recovery",
                        lambda action, **kw: ActionResult(action, True, "ok"))
    script = Script(LOOSE, REAL_SESSION_LINE, "exit")
    code = run_console(read=script.read, write=script.write, focus=NoFocus())

    assert code == 0
    assert seen.count(REAL_SESSION_LINE) == 1, f"the line was handled {seen.count(REAL_SESSION_LINE)}x"
    assert seen == [LOOSE, REAL_SESSION_LINE], seen


def test_a_command_at_the_clarification_prompt_is_queued_too(executor):
    """4. The remaining prompt. Retry and correction are covered in test_console_prompt_handoff.py."""
    needs = NeedsClarification(question="Which application?", missing="app")
    brain = Brainless(needs)
    _reply, _ctx, _script, pending = plan_prompt("open calculator", interpret=brain)
    # the clarification question was asked, and the command typed at it was handed back
    assert pending.take() == "open calculator"
    assert brain.calls == 1, "handing a command back must not send an answer to the provider"


def test_the_queued_command_runs_exactly_once_and_the_slot_empties(monkeypatch):
    """5. take() empties the slot, so nothing is left behind in a hidden queue."""
    ran = []
    monkeypatch.setattr(console, "execute_with_recovery",
                        lambda action, **kw: ran.append(action.target) or ActionResult(action, True, "ok"))
    monkeypatch.setattr(console.interpreter, "interpret", Brainless(understood()))
    script = Script(LOOSE, "open calculator", "exit")
    run_console(read=script.read, write=script.write, focus=NoFocus())
    assert ran == ["calculator"], ran


def test_free_text_at_the_correction_prompt_is_still_the_correction():
    """6. The prompt where prose IS the answer. Only the narrow detector applies there, which is what
    keeps a correction usable - widening it to "anything that is not yes" would have broken this."""
    for prose in ("use the other Ali", "the other one", "not that window", "I meant the big one"):
        script = Script(prose)
        prompts = Prompts(read=script.read, write=script.write, pending=PendingCommand())
        assert console._ask_text(prompts, console.CORRECTION_PROMPT) == prose, prose
        assert prompts.pending.take() is None, f"{prose!r} was queued instead of used"


@pytest.mark.parametrize("shape, expected", [(CLOSED_QUESTION, Answer.COMMAND),
                                             (FREE_TEXT, Answer.ANSWER),
                                             (CLARIFICATION, Answer.ANSWER)])
def test_the_shape_of_the_question_decides_what_unrecognised_prose_means(shape, expected):
    """1 and 6 as one rule rather than two special cases: the SHAPE of the question is the difference,
    and nothing else is."""
    assert classify_answer("the other one", shape) is expected


# ======================================================================================================
# RULE 2 - exit AND help ALWAYS WORK, IMMEDIATELY (matrix 7-9)
# ======================================================================================================

@pytest.mark.parametrize("shape", [CLOSED_QUESTION, FREE_TEXT, CLARIFICATION])
@pytest.mark.parametrize("word, expected", [("exit", Answer.EXIT), ("help", Answer.HELP),
                                            ("EXIT", Answer.EXIT), ("  help  ", Answer.HELP)])
def test_exit_and_help_mean_the_same_thing_at_every_prompt(shape, word, expected):
    """7 and 9, at the rule. They are console words, not answers, so the shape of the question is
    irrelevant to them."""
    assert classify_answer(word, shape) is expected


def test_exit_at_the_retry_prompt_does_not_open_the_correction_prompt(monkeypatch):
    """8. The owner's exact complaint: `exit` WAS queued correctly, and then the failure opened the
    correction prompt, so another question appeared and the console did not leave.

    The fix is the abandoned flag, not a special case for `exit`: once a nested prompt has been ended
    by the user, nothing may ask them anything else about this root command."""
    def failing(action, confirm=None, offer_retry=None, *, risk_floor=RiskLevel.LOW):
        result = ActionResult(action, False, "it didn't open", retryable=True)
        if offer_retry is not None:
            offer_retry(result)
        return result

    monkeypatch.setattr(console, "execute_with_recovery", failing)
    monkeypatch.setattr(console.interpreter, "interpret", Brainless(understood()))
    script = Script(LOOSE, "yes", "exit")
    code = run_console(read=script.read, write=script.write, focus=NoFocus())

    assert code == 0, "the console did not leave"
    assert console.CORRECTION_PROMPT not in script.asked, (
        f"a question was asked after exit: {script.asked}")
    assert script.asked[-1].startswith("Try again?"), script.asked


def test_help_at_a_nested_prompt_shows_the_help_and_returns_to_the_prompt(monkeypatch):
    """9. help cancels the pending interaction, prints the commands, and the loop goes back to "> "."""
    monkeypatch.setattr(console, "execute_with_recovery",
                        lambda action, **kw: ActionResult(action, True, "ok"))
    monkeypatch.setattr(console.interpreter, "interpret", Brainless(understood()))
    script = Script(LOOSE, "help", "exit")
    run_console(read=script.read, write=script.write, focus=NoFocus())
    from app.executor import commands
    assert commands.HELP in script.lines, "help was swallowed by the plan prompt"
    assert script.asked[-1] == "> ", script.asked


def test_exit_and_help_need_a_loop_to_hand_them_to():
    """Without a console loop there is nothing to exit FROM, so they stay what they always were: not
    yes. That is what keeps the voice console unchanged."""
    for word in ("exit", "help"):
        assert classify_answer(word, CLOSED_QUESTION, handoff=False) is Answer.ANSWER


# ======================================================================================================
# RULE 3 - cancel / no / stop ARE EXPLICIT (matrix 10-12)
# ======================================================================================================

@pytest.mark.parametrize("word", sorted(ABANDON_WORDS))
def test_each_abandon_word_abandons_a_closed_question(word):
    """10, at the rule. All three, at a yes-or-no prompt."""
    assert classify_answer(word, CLOSED_QUESTION) is Answer.ABANDON
    assert classify_answer(word.upper(), CLOSED_QUESTION) is Answer.ABANDON


@pytest.mark.parametrize("word", sorted(ABANDON_WORDS))
def test_the_plan_prompt_says_the_plan_was_abandoned(word, executor):
    """10. "Nothing was run." left the owner guessing WHICH of the plan, the action and the question
    had gone. The message now names it."""
    reply, _ctx, _script, pending = plan_prompt(word)
    assert reply.status is Status.CANCELLED
    assert reply.message == console.PLAN_ABANDONED, reply.message
    assert "plan" in reply.message.lower()
    assert executor == [], "a step ran after the plan was cancelled"
    assert pending.take() is None, "an abandon word was queued as a command"


@pytest.mark.parametrize("word", sorted(ABANDON_WORDS))
def test_the_retry_prompt_says_it_stopped_trying(word):
    pending = PendingCommand()
    script = Script(word)
    offer = console._offer_retry(script.read, script.write, pending)
    assert offer(ActionResult(ExecutorAction(OPEN_APP, "notepad"), False, "no")) is False
    assert console.RETRY_ABANDONED in script.lines, script.lines


@pytest.mark.parametrize("word", sorted(ABANDON_WORDS))
def test_the_correction_prompt_says_it_left_the_plan_alone(word):
    """10. And it is no longer a paid model call about the word "no" - the brief's C, one level deeper."""
    script = Script(word)
    prompts = Prompts(read=script.read, write=script.write, pending=PendingCommand())
    assert console._ask_text(prompts, console.CORRECTION_PROMPT) is None
    assert console.CORRECTION_ABANDONED in script.lines, script.lines


def test_cancelling_the_plan_runs_nothing_and_spends_no_brain_call(executor):
    """11. Zero actions, zero clicks, and the provider is not asked again - Brainless would raise."""
    brain = Brainless(understood())
    reply, _ctx, _script, _pending = plan_prompt("cancel", interpret=brain)
    assert executor == []
    assert brain.calls == 1, "cancelling asked the provider something"
    assert reply.status is Status.CANCELLED


def test_cancelling_asks_no_further_question(executor):
    """11 and Rule 2's "nothing pending resumes": the correction prompt is NOT offered after an
    explicit cancel. "Return to >" means return to ">"."""
    _reply, _ctx, script, _pending = plan_prompt("cancel", interpret=Brainless(understood()))
    assert console.CORRECTION_PROMPT not in script.asked, script.asked


def test_the_normal_prompt_invents_no_pending_operation(monkeypatch):
    """12. At "> " these are ordinary lines. The loop special-cases exactly two words, and neither of
    them is an abandon word, so there is nothing for "cancel" to cancel."""
    source = inspect.getsource(run_console)
    assert "ABANDON_WORDS" not in source and "classify_answer" not in source
    assert source.count("EXIT_WORD") == 1 and source.count("HELP_WORD") == 1

    ran = []
    monkeypatch.setattr(console, "execute_with_recovery",
                        lambda action, **kw: ran.append(action) or ActionResult(action, True, "ok"))
    monkeypatch.setattr(console.interpreter, "interpret",
                        Brainless(NotACommand(message="That doesn't look like something to do.")))
    script = Script("cancel", "exit")
    assert run_console(read=script.read, write=script.write, focus=NoFocus()) == 0
    assert ran == [], "something ran because of a bare cancel"


# ======================================================================================================
# RULE 4 - A NEAR-MISS RE-ASKS ONCE, AT THE PLAN PROMPT ONLY (matrix 13-17)
# ======================================================================================================

@pytest.mark.parametrize("word", ["y", "yees", "e", "yse", "ye", "yess", "s", "yesss"])
def test_the_near_miss_rule_covers_the_mistypes(word):
    """13. The owner typed `e` and `yees`. The rule: only YES's own letters, and never longer than YES
    plus two - derived from YES itself so the two cannot drift apart."""
    assert classify_answer(word, CLOSED_QUESTION) is Answer.NEAR_MISS


@pytest.mark.parametrize("word", ["yes", "no", "stop", "cancel", "exit", "help", "", "   ",
                                  "yesplease", "y e s", "yeses!", "open chrome", REAL_SESSION_LINE,
                                  "1", "True", "yessss"])
def test_the_near_miss_rule_catches_nothing_else(word):
    """13's other half. Nothing that is a real answer, a console word, blank, or a command - and
    nothing longer than the limit ("yessss" is six)."""
    assert classify_answer(word, CLOSED_QUESTION) is not Answer.NEAR_MISS


def test_a_near_miss_is_asked_again_once(executor):
    """13. The plan is still sitting there unaccepted, so asking again costs nothing: no model call, no
    action, no state change."""
    brain = Brainless(understood())
    reply, _ctx, script, _pending = plan_prompt("yees", "yes", interpret=brain)
    assert script.asked.count(console.PROCEED_PROMPT) == 2, script.asked
    assert console.NEAR_MISS_MESSAGE in script.lines
    assert brain.calls == 1, "the re-ask cost a model call"
    assert reply.status is Status.RAN, reply.message
    assert [action.target for action in executor] == ["notepad"]


def test_a_second_near_miss_rejects_the_plan_and_does_not_loop(executor):
    """14 and 25. ONE re-ask. This is structural, not a counter: _plan_answer is straight-line code
    with two reads and no loop, and the second near-miss is returned as a plain "not yes"."""
    reply, _ctx, script, _pending = plan_prompt("yees", "y", interpret=Brainless(understood()))
    assert script.asked.count(console.PROCEED_PROMPT) == 2, "it asked a third time"
    assert reply.status is Status.CANCELLED
    assert executor == []

    source = inspect.getsource(console._plan_answer)
    tree = ast.parse(source.strip())
    assert not [node for node in ast.walk(tree) if isinstance(node, (ast.While, ast.For))], (
        "the re-ask became a loop")
    assert source.count("_read_answer(") == 2, "exactly two reads, so exactly one re-ask"


def test_yes_after_a_re_ask_runs_the_plan(executor):
    """15. Covered by test_a_near_miss_is_asked_again_once; asserted on its own so a regression says
    which half broke."""
    reply, _ctx, _script, _pending = plan_prompt("e", "yes", interpret=Brainless(understood()))
    assert reply.status is Status.RAN
    assert [action.target for action in executor] == ["notepad"]


def test_a_real_command_at_the_plan_prompt_is_not_a_near_miss(executor):
    """16. Two protections, deliberately. The command is queued, AND classify_answer asks
    is_fresh_command BEFORE _is_near_miss, so a recognised command can never reach the near-miss rule
    at all."""
    _reply, _ctx, script, pending = plan_prompt("open calculator", interpret=Brainless(understood()))
    assert script.asked.count(console.PROCEED_PROMPT) == 1, "a command triggered the re-ask"
    assert pending.take() == "open calculator"

    source = inspect.getsource(console.classify_answer)
    assert source.index("is_fresh_command(line)") < source.index("_is_near_miss(word)"), (
        "the near-miss rule can now swallow a command")


def test_the_near_miss_rule_does_not_reach_the_safety_confirmation():
    """17. Rule 4 is the plan prompt ONLY. A mistyped yes at a Medium confirmation denies, as it always
    has - the action is about to touch the machine, so a second chance there is not ours to give."""
    script = Script("yees")
    confirm = console._confirm(script.read, script.write, PendingCommand())
    assert confirm(Action("close app notepad"),
                   RiskAssessment(RiskLevel.MEDIUM, "closing can lose unsaved work")) is False
    assert script.asked == ["Type yes to go ahead (anything else cancels): "], script.asked
    assert console.NEAR_MISS_MESSAGE not in script.lines


# ======================================================================================================
# RULE 5 - THE SAFETY CONFIRMATION KEEPS ITS EXCEPTION (matrix 18-21)
# ======================================================================================================

def confirm_with(*answers, pending=None):
    script = Script(*answers)
    confirm = console._confirm(script.read, script.write, pending)
    allowed = confirm(Action("close app notepad"),
                      RiskAssessment(RiskLevel.MEDIUM, "closing can lose unsaved work"))
    return allowed, script


@pytest.mark.parametrize("answer", ["yes", "YES", "Yes", "  yes  ", "yEs"])
def test_only_the_exact_word_yes_approves(answer):
    """18. Unchanged, including the existing and documented "any capitals, surrounding spaces ignored".

    NOTE FOR THE OWNER: the brief's item 19 lists `YES` among the answers that should DENY. That would
    be a change to the Safety gate's rule, which section 3 of the same brief puts out of scope, and it
    would contradict app/console.py's documented contract and three existing tests. So capitals still
    approve and this is flagged rather than quietly decided either way."""
    allowed, _script = confirm_with(answer, pending=PendingCommand())
    assert allowed is True


@pytest.mark.parametrize("answer", ["y", "yees", "e", "1", "True", "", "   ", "ok", "sure",
                                    "yes please", "no", "cancel", "stop", "exit", "help",
                                    "close explorer", REAL_SESSION_LINE])
def test_everything_else_denies(answer):
    """19. Every shape of wrong answer, including the two console words, all three abandon words, a
    near-miss, a deterministic command and a natural-language one."""
    allowed, _script = confirm_with(answer, pending=PendingCommand())
    assert allowed is False, f"{answer!r} approved a MEDIUM action"


@pytest.mark.parametrize("answer", ["y", "1", "no", "exit", "close explorer", REAL_SESSION_LINE])
def test_a_denial_is_decided_without_a_loop_to_hand_anything_back_to(answer):
    """19. The denial does not depend on the handoff existing. Same answers, no slot, same result -
    so no front end can get a different safety answer by not owning a loop."""
    allowed, _script = confirm_with(answer, pending=None)
    assert allowed is False


def test_a_command_at_the_confirmation_cancels_it_queues_unchanged_and_never_resumes(monkeypatch):
    """20, end to end through the real loop and the REAL safety gate.

    A two-step plan: the first step is confirmed, the second asks, and a command is typed at that
    question. Required: the second action never happens, the typed line is queued unchanged, it runs
    once afterwards as its own root command, and the cancelled action does not come back."""
    acted = []

    def executor(action, confirm=None, offer_retry=None, *, risk_floor=RiskLevel.LOW):
        if confirm is not None and action.target == "notepad":
            # the real console confirmation, reading the next scripted line
            if confirm(Action(f"close app {action.target}"),
                       RiskAssessment(RiskLevel.MEDIUM, "closing can lose unsaved work")) is not True:
                from app.safety.logic import ActionDeniedError
                raise ActionDeniedError("I didn't do that: you didn't confirm it.")
        acted.append(action.target)
        return ActionResult(action, True, f"did {action.kind} {action.target}")

    monkeypatch.setattr(console, "execute_with_recovery", executor)
    monkeypatch.setattr(console.interpreter, "interpret", Brainless(understood()))
    script = Script(LOOSE, "yes", REAL_SESSION_LINE, "exit")
    code = run_console(read=script.read, write=script.write, focus=NoFocus())

    assert code == 0
    assert acted == [], f"a confirmation that was cancelled still acted: {acted}"
    assert console.CONFIRM_ABANDONED_FOR_COMMAND in script.lines, script.lines
    # the cancelled action is not retried, and no further question was asked about it
    assert console.CORRECTION_PROMPT not in script.asked, script.asked
    assert not any(prompt.startswith("Try again?") for prompt in script.asked), script.asked


def test_the_confirmation_queues_the_line_unchanged():
    """20's "unchanged": the slot holds exactly what was typed, not a normalised version of it."""
    pending = PendingCommand()
    allowed, _script = confirm_with(f"  {REAL_SESSION_LINE}  ", pending=pending)
    assert allowed is False
    assert pending.take() == f"  {REAL_SESSION_LINE}  "


def test_no_action_runs_on_any_denial(monkeypatch):
    """21. Through the real gate: authorize() sees False and raises, so nothing reaches the adapter."""
    from app.safety.logic import authorize, ActionDeniedError
    for answer in ("y", "no", "exit", "close explorer"):
        script = Script(answer)
        confirm = console._confirm(script.read, script.write, PendingCommand())
        with pytest.raises(ActionDeniedError):
            authorize(Action("close app notepad", minimum_level=RiskLevel.MEDIUM,
                             minimum_reason="closing can lose unsaved work"), confirm=confirm)


def test_the_confirmation_prompt_wording_is_unchanged():
    """Section 3: Safety is untouched, down to what the user reads."""
    script = Script("yes")
    confirm = console._confirm(script.read, script.write, PendingCommand())
    confirm(Action("close app notepad"), RiskAssessment(RiskLevel.MEDIUM, "closing can lose work"))
    assert script.asked == ["Type yes to go ahead (anything else cancels): "]


# ======================================================================================================
# RULE 6 - AN UNUSABLE CLARIFICATION ANSWER DOES NOT BURN THE ROUND (matrix 22-28)
# ======================================================================================================

APP_QUESTION = NeedsClarification(question="Which browser do you mean?", missing="app")


def clarify_with(*answers, interpret, pending=None):
    return plan_prompt(*answers, interpret=interpret, pending=pending)


def test_an_unusable_answer_does_not_cash_the_brain_round():
    """22. The round is RESERVED when the question is asked (app/planner/logic.begin_clarification) and
    CASHED at interpret(). An answer that cannot be the missing value never reaches that line.

    Brainless is prepared with one answer only, so a second provider call would raise."""
    brain = Brainless(APP_QUESTION)
    _reply, _ctx, _script, _pending = clarify_with("yes", "cancel", interpret=brain)
    assert brain.calls == 1, "the unusable answer was sent to the provider"


@pytest.mark.parametrize("answer", ["yes", "no", "y", "n", "ok", "okay", "sure", "yeah", "yep",
                                    "nope", "nah", "YES", " ok "])
def test_a_bare_confirmation_cannot_be_a_missing_value(answer):
    """22, at the rule. A clarification asks for a VALUE that is absent - an app, a recipient, which of
    several. "yes" is never that value, and knowing so needs no model call."""
    assert classify_answer(answer, CLARIFICATION) is Answer.UNUSABLE
    # ...and the same words are ordinary prose at the correction prompt, where they are not a value
    assert classify_answer(answer, FREE_TEXT) is not Answer.UNUSABLE


def test_the_same_question_is_asked_again_once_for_free(executor):
    """23. The SAME question, locally, with no new provider call - not a second clarification round."""
    brain = Brainless(APP_QUESTION, understood())
    reply, _ctx, script, _pending = clarify_with("yes", "notepad", "yes", interpret=brain)
    assert script.lines.count(APP_QUESTION.question) == 2, script.lines
    assert console.CLARIFY_UNUSABLE in script.lines
    assert brain.calls == 2, "the local re-ask spent a call of its own"
    assert reply.status is Status.RAN, reply.message


def test_a_second_unusable_answer_abandons_the_root_command_honestly():
    """24. No guess, no third question, and the message says the command was left alone."""
    brain = Brainless(APP_QUESTION)
    reply, ctx, script, _pending = clarify_with("yes", "no", interpret=brain)
    assert reply.status is Status.NO_PLAN
    assert reply.message == console.CLARIFY_GIVEN_UP, reply.message
    assert script.lines.count(APP_QUESTION.question) == 2, "it asked a third time"
    assert brain.calls == 1
    assert ctx.pending_clarification is None, "the question is still pending"


def test_the_local_re_ask_cannot_loop():
    """25. Structural: _clarify has no loop over the answer, and reads the clarification prompt exactly
    twice. A counter could be mutated into an unbounded one; straight-line code cannot."""
    source = inspect.getsource(console._clarify)
    assert source.count("_read_answer(prompts, CLARIFY_PROMPT, CLARIFICATION)") == 2
    tree = ast.parse(source.strip())
    assert not [node for node in ast.walk(tree) if isinstance(node, (ast.While, ast.For))
                and "CLARIFY_PROMPT" in ast.unparse(node)], "the re-ask became a loop"


def test_a_usable_answer_still_cashes_the_round(executor):
    """26. Unchanged: a real answer goes to the provider, exactly once."""
    brain = Brainless(APP_QUESTION, understood())
    reply, _ctx, _script, _pending = clarify_with("notepad", "yes", interpret=brain)
    assert brain.calls == 2
    assert reply.status is Status.RAN


def test_the_bound_is_still_one_clarification_round():
    """27. The model asking a SECOND question is still refused - Rule 6 buys a local re-ask of the
    known question, never a second round."""
    second = NeedsClarification(question="Which window though?", missing="which_of")
    brain = Brainless(APP_QUESTION, second)
    reply, _ctx, script, _pending = clarify_with("notepad", interpret=brain)
    assert reply.status is Status.NO_PLAN
    assert brain.calls == 2, "a third provider call was made"
    assert second.question not in script.lines, "the second question was asked"


def test_the_budget_constants_did_not_move():
    """27 and section 3, at the source. Rule 6 must not have widened the allowance."""
    from app.planner.models import MAX_CLARIFICATIONS, MAX_INTERPRETATIONS, MAX_REPLANS
    assert (MAX_INTERPRETATIONS, MAX_CLARIFICATIONS, MAX_REPLANS) == (1, 1, 1)
    assert MAX_BRAIN_CALLS == 3


def test_a_choice_question_names_the_candidates_it_knows():
    """28. "Which browser?" with no options was unanswerable without guessing - which is how the round
    was lost. The configured application names are local knowledge: no model call, no desktop read, no
    Memory lookup."""
    from app.executor.logic import configured_app_names
    brain = Brainless(APP_QUESTION)
    _reply, _ctx, script, _pending = clarify_with("cancel", interpret=brain)
    named = [line for line in script.lines if line.startswith("I know about:")]
    assert named, script.lines
    for app in configured_app_names():
        assert app in named[0], f"{app} was not offered"


def test_candidates_are_only_offered_when_the_missing_thing_is_an_application():
    """28 stays honest: the console does not know who a recipient is, so it claims nothing."""
    other = NeedsClarification(question="Which one?", missing="which_of")
    assert console._clarification_question(other) == [other.question]
    app = console._clarification_question(APP_QUESTION)
    assert len(app) == 2 and app[0] == APP_QUESTION.question


def test_naming_the_candidates_reads_configuration_and_nothing_else():
    """28's cost. One read, of the one published source of that answer, and a broken configuration adds
    nothing rather than failing the turn."""
    source = inspect.getsource(console._clarification_question)
    tree = ast.parse(source.strip())
    called = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    assert "configured_app_names" in called
    for forbidden in ("interpret", "memory_queries.application", "verifier.active_target",
                      "adapter.browser_sessions"):
        assert forbidden not in called, f"{forbidden} was consulted to print a question"
    assert "except SettingsError" in source


# ======================================================================================================
# COST (matrix 29-30)
# ======================================================================================================

def test_a_queued_command_costs_exactly_one_interpretation(monkeypatch):
    """29. "Exactly as if I had typed it at >" - so one interpretation, with a fresh allowance, and not
    one interpretation per prompt it passed through."""
    brain = Brainless(understood(), NotACommand(message="no"))
    monkeypatch.setattr(console.interpreter, "interpret", brain)
    monkeypatch.setattr(console, "execute_with_recovery",
                        lambda action, **kw: ActionResult(action, True, "ok"))
    script = Script(LOOSE, REAL_SESSION_LINE, "exit")
    run_console(read=script.read, write=script.write, focus=NoFocus())
    assert brain.calls == 2, f"one for the plan, one for the queued line; got {brain.calls}"


def test_an_abandoned_prompt_spends_no_brain_call(executor):
    """30. Every way of ending a prompt, and none of them sends anything."""
    for answer in ("cancel", "no", "stop", "exit", "help", "open calculator", REAL_SESSION_LINE):
        brain = Brainless(understood())
        plan_prompt(answer, interpret=brain)
        assert brain.calls == 1, f"{answer!r} cost {brain.calls} calls"


def test_the_queued_line_starts_a_fresh_root_command(monkeypatch):
    """29's "as if I had typed it at >": the allowance resets, because run_console hands the line to
    the same entry point as any other line and handle_typed_line calls begin_root_command()."""
    source = inspect.getsource(console.run_console)
    assert "queued = pending.take()" in source
    assert "handle_typed_line(line, context, prompts, focus=focus)" in source
    assert "session.begin_root_command(context)" in inspect.getsource(console.handle_typed_line)


# ======================================================================================================
# THE RULE SET ITSELF
# ======================================================================================================

def test_classify_answer_is_pure():
    """It is asked what a line MEANS, hundreds of times, including from a safety confirmation. It must
    not act, spend, read the desktop or call a provider."""
    source = inspect.getsource(classify_answer)
    tree = ast.parse(source.strip())
    called = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    assert called <= {"isinstance", "line.strip", "line.strip().lower", "is_fresh_command",
                      "_is_near_miss", "_is_short_slip", "set", "len"}, called
    for forbidden in ("interpret", "run_action", "execute_with_recovery", "authorize", "verifier",
                      "adapter", "put", "abandon", "session"):
        assert forbidden not in called, f"classify_answer calls {forbidden}"


def test_every_meaning_is_reachable_and_nothing_else_exists():
    """The vocabulary is closed, so a prompt cannot invent a meaning the rule set does not cover."""
    assert {member for member in Answer} == {Answer.NONE, Answer.YES, Answer.EXIT, Answer.HELP,
                                            Answer.ABANDON, Answer.COMMAND, Answer.NEAR_MISS,
                                            Answer.UNUSABLE, Answer.ANSWER}
    produced = set()
    for shape in (CLOSED_QUESTION, FREE_TEXT, CLARIFICATION):
        for line in ("", "yes", "exit", "help", "cancel", "open chrome", "yees", "ok",
                     "the other one", None):
            produced.add(classify_answer(line, shape))
    assert produced == set(Answer), set(Answer) - produced


def test_the_near_miss_limit_is_derived_from_yes():
    """A hardcoded 5 would survive YES changing; this cannot."""
    source = inspect.getsource(console)
    assert "NEAR_MISS_LIMIT = len(YES) + 2" in source
    assert "_YES_LETTERS = frozenset(YES)" in source


def test_a_front_end_without_a_loop_sees_the_old_answer_space(monkeypatch):
    """Section 3: the voice console is unchanged, and this is the single `if` that guarantees it. With
    no slot, the only meanings that can come back are the three that existed before this slice."""
    seen = set()
    for shape in (CLOSED_QUESTION, FREE_TEXT, CLARIFICATION):
        for line in ("", "yes", "YES", "exit", "help", "cancel", "no", "open chrome", "yees",
                     "ok", "the other one", None, 7):
            seen.add(classify_answer(line, shape, handoff=False))
    assert seen == {Answer.NONE, Answer.YES, Answer.ANSWER}, seen


def test_the_voice_console_still_passes_no_slot():
    """...and that it genuinely does not own one.

    Checked on the Prompts it BUILDS, by AST, not by searching its source for a name. A substring
    search here first "found" app.listener.models.PendingCommand - a spoken-command candidate, which
    has nothing to do with the console's one-line slot. That is the eighth time in this project a
    substring has matched the wrong thing, so the structural checks go through the parse tree."""
    from app import voice_console
    tree = ast.parse(inspect.getsource(voice_console))
    built = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and ast.unparse(node.func) in ("console.Prompts", "Prompts")]
    assert built, "voice no longer builds its own Prompts; re-check this invariant"
    for call in built:
        assert "pending" not in {keyword.arg for keyword in call.keywords}, ast.unparse(call)
    # and the two reusable prompts it borrows are the slot-free ones
    assert inspect.getsource(console.typed_confirmation).count("_confirm(read, write)") == 1, (
        "the reusable confirmation gained a slot")
    assert inspect.getsource(console.typed_retry_offer).count("_offer_retry(read, write)") == 1


def test_the_abandoned_flag_is_spent_once_per_root_command():
    """At the mechanism."""
    pending = PendingCommand()
    assert pending.abandoned is False
    pending.abandon()
    assert pending.abandoned is True
    pending.begin_root_command()
    assert pending.abandoned is False


def test_one_cancel_does_not_silence_the_rest_of_the_session(monkeypatch):
    """FOUND BY MUTATION, and it was a real hole rather than a weak assertion only.

    Removing the loop's `pending.begin_root_command()` left the abandonment set for the whole session,
    so after ONE `cancel` nothing would ever ask a follow-up question again - and the only test of it
    was a substring search that the mutant satisfied from inside its own comment. (Ninth time in this
    project that a substring has matched prose rather than code.) So it is behavioural now: a plan is
    cancelled, and the NEXT root command's correction prompt must still be offered.

    The structural half is an AST check that the call is really a call."""
    brain = Brainless(understood(), understood())
    monkeypatch.setattr(console.interpreter, "interpret", brain)

    def failing(action, confirm=None, offer_retry=None, *, risk_floor=RiskLevel.LOW):
        return ActionResult(action, False, "it didn't open")

    monkeypatch.setattr(console, "execute_with_recovery", failing)
    #        root 1: plan -> cancel (abandons)   root 2: plan -> yes -> step fails -> correction
    script = Script(LOOSE, "cancel", LOOSE, "yes", "", "exit")
    assert run_console(read=script.read, write=script.write, focus=NoFocus()) == 0
    assert script.asked.count(console.CORRECTION_PROMPT) == 1, (
        f"the earlier cancel was still suppressing questions: {script.asked}")

    tree = ast.parse(inspect.getsource(run_console).strip())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and ast.unparse(node.func) == "pending.begin_root_command"]
    assert len(calls) == 1, "the abandonment is no longer spent per root command"


def test_only_one_thing_suppresses_a_follow_up_question():
    """One mechanism, in one place: _abandoned(). A per-prompt special case for `exit` is exactly how
    the old console ended up with three different ideas of what a line meant."""
    source = inspect.getsource(console)
    users = [node.name for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.FunctionDef) and "_abandoned(prompts)" in ast.unparse(node)
             and node.name != "_abandoned"]
    assert users == ["_offer_correction"], users


# ======================================================================================================
# THE THREE DEFECTS THE OWNER'S REAL CONSOLE SMOKE FOUND
#
# Slice 1's behaviour was right; its REPORTING was not. All three are in the code the slice touched.
#
#   1. every abandonment message printed twice - two layers both reported the same outcome
#   2. a short typo ("yas") fell through to the command branch and was queued, costing a wasted turn
#   3. the correction prompt was offered after a SAFETY DENIAL - being asked what to do differently
#      after choosing not to do something
# ======================================================================================================

def _denying_executor():
    """An Executor whose action the safety gate refuses, exactly as a declined confirmation does.

    ActionDeniedError takes the assessment as well; a first draft of this raised it with one argument,
    and the resulting TypeError was swallowed by run_console's never-crash handler - which made
    test_no_correction_is_offered_after_a_safety_denial pass for the wrong reason. The sanity assertion
    in that test is there so this cannot happen quietly again."""
    def executor(action, confirm=None, offer_retry=None, *, risk_floor=RiskLevel.LOW):
        from app.safety.logic import ActionDeniedError
        raise ActionDeniedError("Action not run - MEDIUM risk (closing can lose unsaved work); "
                                "the user did not confirm.",
                                RiskAssessment(RiskLevel.MEDIUM, "closing can lose unsaved work"))
    return executor


# --- 1. Printed twice ---------------------------------------------------------------------------------

def test_an_abandoned_plan_is_reported_exactly_once(monkeypatch):
    """1. The owner saw "Cancelled the plan; I will do what you just typed instead." twice.

    CAUSE: two layers reported the same outcome. _offer_correction wrote outcome.message AND returned
    that outcome, and run_console writes every reply's message. Not a stray print - a missing decision
    about which layer owns the reporting."""
    monkeypatch.setattr(console.interpreter, "interpret",
                        Brainless(understood(), NotACommand(message="no")))
    monkeypatch.setattr(console, "execute_with_recovery",
                        lambda action, **kw: ActionResult(action, True, "ok"))
    script = Script(LOOSE, REAL_SESSION_LINE, "exit")
    run_console(read=script.read, write=script.write, focus=NoFocus())
    printed = script.lines.count(console.PLAN_ABANDONED_FOR_COMMAND)
    assert printed == 1, f"printed {printed}x"


@pytest.mark.parametrize("answer, message", [("cancel", "PLAN_ABANDONED"),
                                             ("exit", "PLAN_ABANDONED"),
                                             (REAL_SESSION_LINE, "PLAN_ABANDONED_FOR_COMMAND")])
def test_no_plan_outcome_is_reported_twice(answer, message, monkeypatch):
    """1, across every way of ending the plan prompt."""
    monkeypatch.setattr(console.interpreter, "interpret",
                        Brainless(understood(), NotACommand(message="no")))
    monkeypatch.setattr(console, "execute_with_recovery",
                        lambda action, **kw: ActionResult(action, True, "ok"))
    script = Script(LOOSE, answer, "exit")
    run_console(read=script.read, write=script.write, focus=NoFocus())
    text = getattr(console, message)
    assert script.lines.count(text) == 1, f"{answer!r}: printed {script.lines.count(text)}x"


def test_a_denied_step_is_reported_exactly_once(monkeypatch):
    """1, the owner's verbatim case: "Action not run - MEDIUM risk: ...; the user did not confirm."
    appeared above the correction prompt and again below it."""
    monkeypatch.setattr(console.interpreter, "interpret", Brainless(understood()))
    monkeypatch.setattr(console, "execute_with_recovery", _denying_executor())
    script = Script(LOOSE, "yes", "exit")
    run_console(read=script.read, write=script.write, focus=NoFocus())
    denials = [line for line in script.lines if "did not confirm" in line]
    assert len(denials) == 1, f"the denial was reported {len(denials)}x: {denials}"


def test_one_layer_owns_the_reporting():
    """1, structurally. run_console writes every reply message, so nothing below it may write the same
    message as well. _offer_correction must not write outcome.message."""
    source = inspect.getsource(console._offer_correction)
    assert source.count("prompts.write(outcome.message)") == 1, (
        "the reason is reported more than once, or not at all, by _offer_correction")
    # AMENDED BY THE SLICE 3 SMOKE FIX. The original claim was "_offer_correction never writes",
    # which removed the duplicate but also removed the only report that came BEFORE the correction
    # prompt - so the reason arrived after the question about it. It now writes exactly once, and
    # only on the path where a question follows; every other path leaves the reporting to
    # run_console. Still exactly one report either way, which the ordering tests below check
    # behaviourally.
    before_prompt = source.index("prompts.write(outcome.message)") < source.index("CORRECTION_PROMPT")
    assert before_prompt, "the reason is written after the prompt that asks about it"
    assert "write(reply.message)" in inspect.getsource(run_console)


def test_a_failing_step_of_several_is_shown_in_sequence(monkeypatch):
    """1's other half: fixing the duplicate must not lose the message. In a plan of several steps the
    failing one is numbered and printed where it happened, exactly like a successful one."""
    monkeypatch.setattr(console.interpreter, "interpret",
                        Brainless(understood(open_notepad(), open_notepad())))
    seen = []

    def deny_second(action, confirm=None, offer_retry=None, *, risk_floor=RiskLevel.LOW):
        from app.safety.logic import ActionDeniedError
        seen.append(action.target)
        if len(seen) == 1:
            return ActionResult(action, True, f"did {action.kind} {action.target}")
        raise ActionDeniedError("Action not run - MEDIUM risk (closing can lose unsaved work); "
                                "the user did not confirm.",
                                RiskAssessment(RiskLevel.MEDIUM, "closing can lose unsaved work"))

    monkeypatch.setattr(console, "execute_with_recovery", deny_second)
    script = Script(LOOSE, "yes", "exit")
    run_console(read=script.read, write=script.write, focus=NoFocus())
    assert any(line.startswith("2. ") and "did not confirm" in line for line in script.lines), (
        f"the failing step was not shown in sequence: {script.lines}")
    denials = [line for line in script.lines if "did not confirm" in line]
    assert len(denials) == 1, f"reported {len(denials)}x: {denials}"


def test_a_one_step_plan_reports_its_own_reason_as_the_reply(monkeypatch):
    """1, AND THE INVARIANT MY FIRST ATTEMPT AT IT BROKE.

    The first version of this fix replaced the reply's message with a plan-level "Stopped there." for
    every failure. The full suite caught it on an OWNERSHIP test: reply.message no longer carried
    "I only close windows I opened in this session". The refusal was still printed - but
    app/voice_console.py SPEAKS reply.message, so the real reason would have been printed and never
    said, and any future caller that only reads the reply would have lost it too.

    So a one-step plan returns the step's own reply unchanged, and run_console reports it once."""
    monkeypatch.setattr(console.interpreter, "interpret", Brainless(understood()))
    monkeypatch.setattr(console, "execute_with_recovery", _denying_executor())
    script = Script(LOOSE, "yes", "exit")
    run_console(read=script.read, write=script.write, focus=NoFocus())
    denials = [line for line in script.lines if "did not confirm" in line]
    assert len(denials) == 1, f"reported {len(denials)}x: {denials}"
    assert not denials[0].startswith("1. "), "a one-step plan numbered its only step"


def test_the_reply_of_a_failed_one_step_plan_still_explains_itself():
    """The same invariant at the value, not the printout - this is what voice speaks.

    SCOPED BY THE SLICE 3 SMOKE FIX: it holds on every path where nothing else has reported the
    reason. When a correction IS offered and then declined, the reason was printed immediately above
    that prompt and the closing line is CORRECTION_DECLINED instead, so it is not repeated - see
    test_the_reason_is_still_reported_exactly_once. The trade-off, named rather than hidden: on that
    one path app/voice_console.py speaks the closing line, having printed the reason."""
    executed = []

    def deny(action, confirm=None, offer_retry=None, *, risk_floor=RiskLevel.LOW):
        executed.append(action)
        return ActionResult(action, False,
                            "notepad is open, but I didn't open it, so I left it alone.")

    import app.console as c
    # no budget to replan, so no correction is offered and the reply is the only report there is
    script = Script("yes")
    prompts = Prompts(read=script.read, write=script.write, pending=PendingCommand())
    saved = c.execute_with_recovery
    c.execute_with_recovery = deny
    try:
        reply, _ctx = handle_typed_line(LOOSE, TurnContext(), prompts,
                                        interpret=Brainless(understood()), focus=NoFocus())
    finally:
        c.execute_with_recovery = saved
    assert executed, "nothing ran, so this proves nothing"
    # The reason reached the USER exactly once. On this path a correction was offered, so it was
    # printed immediately above that prompt and the reply's closing line does not repeat it.
    said = [line for line in script.lines if "I didn't open it" in line]
    assert len(said) == 1, script.lines
    assert reply.message == console.CORRECTION_DECLINED, reply.message
    assert reply.result is not None and "I didn't open it" in reply.result.message, (
        "the reason must still be on the result, for any caller that needs it")


# --- 2. A short typo became a command attempt ---------------------------------------------------------

@pytest.mark.parametrize("word", ["yas", "yap", "nvm", "1", "True", "asdf", "qq", "k", "nope",
                                  "ys!", "yesh"])
def test_a_short_unrecognised_word_just_denies(word):
    """2. The owner typed `yas` at the confirmation and got "I will do what you just typed instead"
    followed by a complaint that no request was found in "yas" - a wasted turn for a slip of the finger.

    THE RULE: at a yes-or-no prompt, one word, no space, at most SHORT_WORD_LIMIT characters, that the
    deterministic parser does not recognise, is a mistyped answer rather than a request. It denies and
    is NOT queued."""
    assert classify_answer(word, CLOSED_QUESTION) is Answer.ANSWER, word


@pytest.mark.parametrize("word", ["help", "exit", "refresh", "minimize", "maximize", "restore",
                                  # these three are SHORTER than SHORT_WORD_LIMIT, so they are the ones
                                  # that prove the ordering is what protects a real command, not the
                                  # length. Without is_fresh_command() being asked first they would all
                                  # be read as a slip of the finger.
                                  "open", "click", "type"])
def test_a_real_short_command_still_works(word):
    """2's safety net. Short commands are protected by the PARSER, not by the length: the rule applies
    only to words is_fresh_command() does not recognise, so every genuine one-word command is untouched
    however short it is."""
    assert len(word) > 0
    assert classify_answer(word, CLOSED_QUESTION) in (Answer.COMMAND, Answer.EXIT, Answer.HELP), word


def test_the_shortest_real_commands_are_inside_the_limit():
    """...and the premise of the test above, stated so it cannot rot: if every recognised command were
    longer than the limit, the ordering would be untested and a mutant that reversed it would survive.

    At SHORT_WORD_LIMIT = 4 the words that still prove it are "open" and "type". "click" is five, so it
    is now outside the limit and no longer carries this particular guarantee - which is the same change
    that gives bare "close" (also five) its guidance back."""
    inside = [word for word in ("open", "type") if len(word) <= console.SHORT_WORD_LIMIT]
    assert inside == ["open", "type"], (inside, console.SHORT_WORD_LIMIT)


@pytest.mark.parametrize("line", [REAL_SESSION_LINE, "in the browser click on Demo Account",
                                  "open the thing", "put notepad in front", "a b"])
def test_anything_with_a_space_is_still_queued(line):
    """2 must not narrow Rule 1. The rule is about a SINGLE short word; a phrase is a request."""
    assert classify_answer(line, CLOSED_QUESTION) is Answer.COMMAND, line


@pytest.mark.parametrize("word", ["notepad", "calculator", "chromium", "explorer"])
def test_a_longer_single_word_is_still_queued(word):
    """2's boundary. Over the limit, a bare word may well be a request, so it is still handed back."""
    assert classify_answer(word, CLOSED_QUESTION) is Answer.COMMAND, word


def test_the_short_word_rule_does_not_reach_the_free_text_prompts():
    """2 is a CLOSED_QUESTION rule. At the correction prompt a short word is prose, as it always was."""
    for word in ("yas", "nvm", "k"):
        assert classify_answer(word, FREE_TEXT) is Answer.ANSWER
        assert classify_answer(word, CLARIFICATION) in (Answer.ANSWER, Answer.UNUSABLE)


def test_a_short_typo_at_the_confirmation_denies_and_queues_nothing():
    """2, at the owner's exact prompt. Denied, nothing queued, and no "I will do what you typed" line."""
    pending = PendingCommand()
    allowed, script = confirm_with("yas", pending=pending)
    assert allowed is False
    assert pending.take() is None, "a slip of the finger was queued as a command"
    assert console.CONFIRM_ABANDONED_FOR_COMMAND not in script.lines, script.lines


def test_the_short_word_limit_is_named_and_small():
    source = inspect.getsource(console)
    assert "SHORT_WORD_LIMIT" in source
    assert 0 < console.SHORT_WORD_LIMIT <= 6, console.SHORT_WORD_LIMIT


# --- 3. A correction prompt after a Safety denial -----------------------------------------------------

def test_no_correction_is_offered_after_a_safety_denial(monkeypatch):
    """3. A denial is the user's DECISION, not a failure to correct. Being asked what to do differently
    after choosing not to do something is wrong, and it is the same category as an explicit cancel -
    which Slice 1 already stopped asking after."""
    monkeypatch.setattr(console.interpreter, "interpret", Brainless(understood()))
    monkeypatch.setattr(console, "execute_with_recovery", _denying_executor())
    script = Script(LOOSE, "yes", "exit")
    run_console(read=script.read, write=script.write, focus=NoFocus())
    assert any("did not confirm" in line for line in script.lines), (
        f"the denial never happened, so this proves nothing: {script.lines}")
    assert console.CORRECTION_PROMPT not in script.asked, (
        f"a correction was asked after a denial: {script.asked}")


def test_a_denial_is_in_the_no_correction_set():
    """3, at the rule, so the deterministic and Brain paths cannot disagree."""
    assert Status.DENIED in console.NO_CORRECTION_AFTER
    assert Status.RAN not in console.NO_CORRECTION_AFTER, "a genuine failure must still be correctable"


def test_a_step_that_genuinely_failed_can_still_be_corrected(monkeypatch):
    """3 must not over-reach. When the MACHINE got in the way - not the user - the correction prompt is
    exactly the right question, and it is still asked."""
    monkeypatch.setattr(console.interpreter, "interpret", Brainless(understood()))
    monkeypatch.setattr(console, "execute_with_recovery",
                        lambda action, **kw: ActionResult(action, False, "notepad did not open"))
    script = Script(LOOSE, "yes", "", "exit")
    run_console(read=script.read, write=script.write, focus=NoFocus())
    assert console.CORRECTION_PROMPT in script.asked, script.asked


def test_a_command_at_the_correction_prompt_is_queued_and_runs_once(monkeypatch):
    """3's last question, answered by observation rather than by reading the code: the owner typed
    `close notepad` at the correction prompt that should not have been there, and asked whether it was
    queued, consumed or lost.

    It was QUEUED and it did run. What they saw was the duplicate report (defect 1) landing between the
    prompt and the command, which made a working handoff look like a stuck one. The correction prompt
    after a denial is gone now, so this pins the same handoff on the path where that prompt still
    belongs: a step the MACHINE failed."""
    ran = []

    def failing(action, confirm=None, offer_retry=None, *, risk_floor=RiskLevel.LOW):
        ran.append(action.target)
        if action.target == "notepad" and action.kind == OPEN_APP:
            return ActionResult(action, False, "notepad did not open")
        return ActionResult(action, True, f"did {action.kind} {action.target}")

    monkeypatch.setattr(console.interpreter, "interpret", Brainless(understood()))
    monkeypatch.setattr(console, "execute_with_recovery", failing)
    script = Script(LOOSE, "yes", "close notepad", "exit")
    assert run_console(read=script.read, write=script.write, focus=NoFocus()) == 0

    assert console.CORRECTION_PROMPT in script.asked, "the premise: this path still asks"
    assert ran == ["notepad", "notepad"], f"the queued command did not run exactly once: {ran}"
    failures = [line for line in script.lines if "did not open" in line]
    assert len(failures) == 1, f"the failure was reported {len(failures)}x: {failures}"


def test_the_plan_level_message_says_what_stopped_and_where(monkeypatch):
    """1's replacement message has to be worth printing. For a multi-step plan it names the step, so
    "nothing after it was run" is checkable; for a single step there is no "after" to describe."""
    three = understood(open_notepad(), open_notepad(), open_notepad())
    monkeypatch.setattr(console.interpreter, "interpret", Brainless(three))
    seen = []

    def deny_second(action, confirm=None, offer_retry=None, *, risk_floor=RiskLevel.LOW):
        from app.safety.logic import ActionDeniedError
        seen.append(action.target)
        if len(seen) == 1:
            return ActionResult(action, True, f"did {action.kind} {action.target}")
        raise ActionDeniedError("Action not run - MEDIUM risk (closing can lose unsaved work); "
                                "the user did not confirm.",
                                RiskAssessment(RiskLevel.MEDIUM, "closing can lose unsaved work"))

    monkeypatch.setattr(console, "execute_with_recovery", deny_second)
    script = Script(LOOSE, "yes", "exit")
    run_console(read=script.read, write=script.write, focus=NoFocus())
    assert console.PLAN_STOPPED_EARLY.format(number=2, total=3) in script.lines, script.lines
    assert console.PLAN_STOPPED_AT.format(number=2, total=3) not in script.lines, (
        "the wording for a last step was used for a middle one")
    assert len(seen) == 2, f"step 3 ran after the plan stopped: {seen}"


def test_the_last_step_of_several_does_not_claim_there_was_an_after(monkeypatch):
    """"nothing after it was run" is only true when there WAS something after it."""
    monkeypatch.setattr(console.interpreter, "interpret",
                        Brainless(understood(open_notepad(), open_notepad())))
    seen = []

    def deny_second(action, confirm=None, offer_retry=None, *, risk_floor=RiskLevel.LOW):
        from app.safety.logic import ActionDeniedError
        seen.append(action.target)
        if len(seen) == 1:
            return ActionResult(action, True, "did it")
        raise ActionDeniedError("Action not run - MEDIUM risk (x); the user did not confirm.",
                                RiskAssessment(RiskLevel.MEDIUM, "x"))

    monkeypatch.setattr(console, "execute_with_recovery", deny_second)
    script = Script(LOOSE, "yes", "exit")
    run_console(read=script.read, write=script.write, focus=NoFocus())
    assert console.PLAN_STOPPED_AT.format(number=2, total=2) in script.lines, script.lines
    assert not any("nothing after it" in line for line in script.lines), script.lines


def test_the_short_slip_rule_is_reached_only_after_the_parser_has_said_no():
    """2, structurally. The order is the safety net for every real short command, so it is pinned."""
    source = inspect.getsource(classify_answer)
    assert source.index("is_fresh_command(line)") < source.index("_is_short_slip(word)"), (
        "a short command can now be mistaken for a slip of the finger")
    assert source.index("_is_near_miss(word)") < source.index("_is_short_slip(word)"), (
        "a mistyped yes is no longer re-asked; it is denied as a slip instead")


# ======================================================================================================
# SLICE 1, FINAL FOLLOW-UPS
#
#   1. SHORT_WORD_LIMIT = 4, so a bare "close" keeps its existing guidance instead of being swallowed
#      as noise. The word is NOT special-cased: the behaviour falls out of the parser, the limit and
#      the ordinary handoff.
#   2. NO_CORRECTION_AFTER also covers STOPPED and NOT_HANDED_OVER. Rewording the request addresses
#      neither an emergency stop nor focus that never arrived.
# ======================================================================================================

class RefusingFocus:
    """Focus that never arrives, which is what Status.NOT_HANDED_OVER means."""

    DIDNT_SWITCH = "You didn't switch to another window, so I did nothing."

    def note_console_window(self):
        pass

    def hand_over(self, prompt):
        return self.DIDNT_SWITCH


def refresh_intent():
    from app.brain.models import RefreshArgs
    from app.executor.models import REFRESH
    return Intent(REFRESH, RefreshArgs(), why="you asked for a refresh", risk_floor=RiskLevel.LOW)


# --- 1. SHORT_WORD_LIMIT = 4 --------------------------------------------------------------------------

def test_the_limit_is_four():
    assert console.SHORT_WORD_LIMIT == 4


def test_bare_close_is_not_a_short_slip():
    """1. Five characters, so it is over the limit and the slip rule does not reach it. The parser
    calls "close" AMBIGUOUS rather than a command, which is exactly why the limit had to come down:
    at five it was denied as noise and the guidance was lost."""
    assert len("close") > console.SHORT_WORD_LIMIT
    assert console.is_fresh_command("close") is False, (
        "the premise: the parser does not recognise a bare close, it calls it ambiguous")
    assert classify_answer("close", CLOSED_QUESTION) is Answer.COMMAND


def test_bare_close_is_queued_from_a_yes_no_prompt():
    """1. Queued, not consumed, at the prompt the owner was typing into."""
    pending = PendingCommand()
    allowed, _script = confirm_with("close", pending=pending)
    assert allowed is False, "a bare close must never approve anything"
    assert pending.take() == "close"


def test_bare_close_is_handled_exactly_as_if_it_had_been_typed_at_the_normal_prompt(monkeypatch):
    """1, THE REGRESSION, end to end through the real run_console loop: `close` typed at the plan
    prompt is queued, and the outer loop receives it exactly once.

    ONE CORRECTION TO THE BRIEF, flagged rather than faked. The brief asks that this produce "the
    existing Close what? guidance". It does not, and it did not before this slice either: a bare
    `close` is BRAIN-ELIGIBLE (reason "ambiguous_command"), not a local refusal, so the typed console
    sends it to the Brain. The parser's sentence survives as BrainEligible.local_message, and
    app/console.py and app/brain/logic.py both record the earlier decision NOT to show it on this path.
    Producing it would mean changing routing, which is not one of the two changes asked for.

    So what is pinned here is what the owner actually needs and what IS true: `close` is no longer
    swallowed as noise, and it is handled exactly as if typed at ">" - no better and no worse."""
    from app.executor import commands
    seen = []
    real = console.handle_typed_line

    def watched(text, context, prompts, **kwargs):
        seen.append(text)
        return real(text, context, prompts, **kwargs)

    brain = Brainless(understood(), NeedsClarification(question="Close what?", missing="app"))
    monkeypatch.setattr(console, "handle_typed_line", watched)
    monkeypatch.setattr(console.interpreter, "interpret", brain)
    monkeypatch.setattr(console, "execute_with_recovery",
                        lambda action, **kw: ActionResult(action, True, "ok"))
    script = Script(LOOSE, "close", "cancel", "exit")
    assert run_console(read=script.read, write=script.write, focus=NoFocus()) == 0

    assert seen == [LOOSE, "close"], seen
    assert seen.count("close") == 1, f"the loop received it {seen.count('close')}x"
    assert brain.calls == 2, "one for the plan, one for the queued line, as if typed at '>'"
    # it reached the question the Brain asked, and Slice 1's naming of the candidates applies to it
    assert "Close what?" in script.lines
    assert any(line.startswith("I know about:") for line in script.lines), script.lines


def test_the_parsers_own_close_guidance_is_still_reachable():
    """1's flagged half, stated at the source so the gap is recorded rather than implied: the sentence
    exists and routing still carries it; nothing on the typed path shows it."""
    from app.brain import logic as brain
    from app.executor import commands
    from app.executor.logic import resolve
    route = brain.route("close", resolve)
    assert isinstance(route, brain.BrainEligible)
    assert route.parsed is None and route.reason == "ambiguous_command"
    assert route.local_message == commands.AMBIGUOUS_CLOSE
    assert "Close what?" in commands.AMBIGUOUS_CLOSE


def test_yas_still_denies_without_being_queued_and_costs_nothing(monkeypatch):
    """1's other half, end to end. Lowering the limit must not let the slip back through."""
    brain = Brainless(understood())
    monkeypatch.setattr(console.interpreter, "interpret", brain)
    monkeypatch.setattr(console, "execute_with_recovery",
                        lambda action, **kw: ActionResult(action, True, "ok"))
    script = Script(LOOSE, "yas", "exit")
    run_console(read=script.read, write=script.write, focus=NoFocus())
    assert len("yas") <= console.SHORT_WORD_LIMIT
    assert classify_answer("yas", CLOSED_QUESTION) is Answer.ANSWER
    assert console.PLAN_ABANDONED_FOR_COMMAND not in script.lines, (
        "yas was queued as a command again")
    assert brain.calls == 1, f"the slip cost {brain.calls} calls"


@pytest.mark.parametrize("word", ["close", "click", "scroll", "yesss", "chrome"])
def test_five_characters_and_over_is_never_a_slip(word):
    """1, at the boundary. Five is now outside, which is the whole point of the change."""
    assert len(word) >= 5
    assert classify_answer(word, CLOSED_QUESTION) is not Answer.ANSWER, word


# --- 2. STOPPED and NOT_HANDED_OVER -------------------------------------------------------------------

def test_the_no_correction_set_is_exactly_these_three():
    """2. Named explicitly so it cannot be widened by accident - Status.RAN in particular would stop a
    genuine failure being correctable at all."""
    assert set(console.NO_CORRECTION_AFTER) == {Status.STOPPED, Status.NOT_HANDED_OVER, Status.DENIED}


def test_an_emergency_stop_is_reported_once_and_asks_nothing(monkeypatch):
    """2A. The stop reason comes back once, no correction is offered, and nothing else ran."""
    acted = []

    def stopping(action, confirm=None, offer_retry=None, *, risk_floor=RiskLevel.LOW):
        acted.append(action.target)
        raise console.EmergencyStopError()

    monkeypatch.setattr(console.interpreter, "interpret", Brainless(understood()))
    monkeypatch.setattr(console, "execute_with_recovery", stopping)
    script = Script(LOOSE, "yes", "exit")
    assert run_console(read=script.read, write=script.write, focus=NoFocus()) == 0

    stops = [line for line in script.lines if line == console.STOPPED_MESSAGE]
    assert len(stops) == 1, f"the stop was reported {len(stops)}x: {script.lines}"
    assert console.CORRECTION_PROMPT not in script.asked, script.asked
    assert acted == ["notepad"], f"something else ran after the stop: {acted}"


def test_a_stop_part_way_through_an_action_is_also_not_offered_a_correction(monkeypatch):
    """2A, the other way a stop arrives: interrupted mid-action, carrying how much had happened. The
    progress message must survive - only the question goes."""
    def interrupted(action, confirm=None, offer_retry=None, *, risk_floor=RiskLevel.LOW):
        result = ActionResult(action, False, "I stopped part way through.")
        raise console.ActionInterruptedError("stopped", result)

    monkeypatch.setattr(console.interpreter, "interpret", Brainless(understood()))
    monkeypatch.setattr(console, "execute_with_recovery", interrupted)
    script = Script(LOOSE, "yes", "exit")
    run_console(read=script.read, write=script.write, focus=NoFocus())
    partial = [line for line in script.lines if "part way through" in line]
    assert len(partial) == 1, f"the progress was reported {len(partial)}x: {partial}"
    assert console.CORRECTION_PROMPT not in script.asked, script.asked


def test_focus_that_never_arrives_is_reported_once_and_asks_nothing(monkeypatch):
    """2B. A refresh needs the user to put a window in front. When they do not, no wording of the
    request would have helped, so the console says so once and stops asking."""
    acted = []
    monkeypatch.setattr(console.interpreter, "interpret", Brainless(understood(refresh_intent())))
    monkeypatch.setattr(console, "execute_with_recovery",
                        lambda action, **kw: acted.append(action.target) or ActionResult(action, True, "ok"))
    script = Script(LOOSE, "yes", "exit")
    run_console(read=script.read, write=script.write, focus=RefusingFocus())

    refusals = [line for line in script.lines if line == RefusingFocus.DIDNT_SWITCH]
    assert len(refusals) == 1, f"reported {len(refusals)}x: {script.lines}"
    assert console.CORRECTION_PROMPT not in script.asked, script.asked
    assert acted == [], f"the action ran without focus: {acted}"
    assert not any(prompt.startswith("Try again?") for prompt in script.asked), script.asked


def test_the_handover_itself_is_unchanged():
    """2 must suppress the PROMPT and nothing else. The hand-over, its prompts and its timing are not
    touched: run_action still asks for it for exactly the same kinds, in the same place."""
    source = inspect.getsource(console.run_action)
    assert "needs_handover(action.kind)" in source
    assert "focus.hand_over(HAND_OVER_PROMPT)" in source
    assert "NO_CORRECTION_AFTER" not in source, "the suppression leaked into the hand-over"
    assert console.HANDS_OVER == frozenset({console.CLICK, console.TYPE_TEXT, console.SHORTCUT,
                                            console.SCROLL, console.REFRESH, console.WINDOW_CONTROL})


def test_a_genuine_failure_is_still_offered_a_correction(monkeypatch):
    """2C. The line that must not move. Status.RAN with a failed result is the machine getting in the
    way, which is what the correction prompt is for."""
    monkeypatch.setattr(console.interpreter, "interpret", Brainless(understood()))
    monkeypatch.setattr(console, "execute_with_recovery",
                        lambda action, **kw: ActionResult(action, False, "notepad did not open"))
    script = Script(LOOSE, "yes", "", "exit")
    run_console(read=script.read, write=script.write, focus=NoFocus())
    assert console.CORRECTION_PROMPT in script.asked, script.asked
    failures = [line for line in script.lines if "did not open" in line]
    assert len(failures) == 1, f"reported {len(failures)}x: {failures}"


def test_the_retry_offer_is_untouched_by_the_suppression(monkeypatch):
    """2 must not change retry semantics. A retryable failure still offers the retry; only the
    CORRECTION prompt is suppressed, and only for the three decided statuses."""
    attempts = []

    def failing(action, confirm=None, offer_retry=None, *, risk_floor=RiskLevel.LOW):
        attempts.append(action.target)
        result = ActionResult(action, False, "notepad did not open", retryable=True)
        if offer_retry is not None:
            offer_retry(result)
        return result

    monkeypatch.setattr(console.interpreter, "interpret", Brainless(understood()))
    monkeypatch.setattr(console, "execute_with_recovery", failing)
    script = Script(LOOSE, "yes", "no", "", "exit")
    run_console(read=script.read, write=script.write, focus=NoFocus())
    assert any(prompt.startswith("Try again?") for prompt in script.asked), script.asked


# ======================================================================================================
# THE ORDERING REGRESSION THE SLICE 3 SMOKE FOUND
#
# Observed four times on the real console:
#
#     Proceed? Type yes to run it, or cancel: yes
#     Tell me what to do differently, or press Enter to leave it: click Sign login
#     I couldn't find anything called 'Sign in' in that window.
#
# The reason came AFTER the question about it. Cause: the "printed twice" fix made run_console the
# single owner of reply reporting, and _run_plan's ONE-STEP branch returns the step's reply unchanged
# without printing it - so for a one-step plan nothing was reported before _offer_correction asked.
# Multi-step plans were never affected: they print the failing step in sequence.
#
# The fix is ordering, not eligibility. A target name that was not found IS correctable - the owner's
# own smoke proved rewording helps - so nothing new joins NO_CORRECTION_AFTER.
# ======================================================================================================

class Transcript(Script):
    """Script, plus ONE ordered record of everything the user saw, writes and prompts interleaved.

    The regression is invisible to separate `lines` and `asked` lists: both contained the right
    strings, in the wrong order relative to each other."""

    def __init__(self, *answers):
        super().__init__(*answers)
        self.events = []

    def read(self, prompt=""):
        self.events.append(("ask", prompt))
        return super().read(prompt)

    def write(self, text=""):
        self.events.append(("say", str(text)))
        return super().write(text)

    def index_of_say(self, needle):
        return next((i for i, (kind, text) in enumerate(self.events)
                     if kind == "say" and needle in text), None)

    def index_of_ask(self, needle):
        return next((i for i, (kind, text) in enumerate(self.events)
                     if kind == "ask" and needle in text), None)


NOT_FOUND = "I couldn't find anything called 'Sign in' in that window."


def failing_executor(message=NOT_FOUND, *, fail_target="notepad"):
    def executor(action, confirm=None, offer_retry=None, *, risk_floor=RiskLevel.LOW):
        if action.target == fail_target or fail_target is None:
            return ActionResult(action, False, message)
        return ActionResult(action, True, f"did {action.kind} {action.target}")
    return executor


def test_the_failure_reason_is_reported_before_the_correction_prompt(monkeypatch):
    """1. THE REGRESSION, at the owner's exact shape: a one-step plan whose only step failed."""
    monkeypatch.setattr(console.interpreter, "interpret", Brainless(understood()))
    monkeypatch.setattr(console, "execute_with_recovery", failing_executor())
    script = Transcript(LOOSE, "yes", "", "exit")
    run_console(read=script.read, write=script.write, focus=NoFocus())

    reason = script.index_of_say(NOT_FOUND)
    prompt = script.index_of_ask(console.CORRECTION_PROMPT)
    assert reason is not None, script.events
    assert prompt is not None, "the premise: this failure IS still offered a correction"
    assert reason < prompt, (
        f"the reason was reported AFTER the correction prompt:\n"
        + "\n".join(f"  {kind}: {text}" for kind, text in script.events))


def test_the_reason_is_still_reported_exactly_once(monkeypatch):
    """1. Fixing the order must not bring back the duplicate the previous fix removed."""
    monkeypatch.setattr(console.interpreter, "interpret", Brainless(understood()))
    monkeypatch.setattr(console, "execute_with_recovery", failing_executor())
    script = Transcript(LOOSE, "yes", "", "exit")
    run_console(read=script.read, write=script.write, focus=NoFocus())
    said = [text for kind, text in script.events if kind == "say" and NOT_FOUND in text]
    assert len(said) == 1, said


def test_a_multi_step_plan_was_never_affected_and_still_is_not(monkeypatch):
    """1. The regression guard for the half that always worked: the failing step prints in sequence."""
    monkeypatch.setattr(console.interpreter, "interpret",
                        Brainless(understood(open_notepad(), open_notepad())))
    seen = []

    def one_then_fail(action, confirm=None, offer_retry=None, *, risk_floor=RiskLevel.LOW):
        seen.append(action.target)
        if len(seen) == 1:
            return ActionResult(action, True, "did it")
        return ActionResult(action, False, NOT_FOUND)

    monkeypatch.setattr(console, "execute_with_recovery", one_then_fail)
    script = Transcript(LOOSE, "yes", "", "exit")
    run_console(read=script.read, write=script.write, focus=NoFocus())
    reason = script.index_of_say(NOT_FOUND)
    prompt = script.index_of_ask(console.CORRECTION_PROMPT)
    assert reason is not None and prompt is not None
    assert reason < prompt, script.events
    assert len([t for k, t in script.events if k == "say" and NOT_FOUND in t]) == 1


def test_an_ownership_refusal_is_reported_before_any_correction_prompt(monkeypatch):
    """1, the owner's second case: `close chrome` refusing for want of ownership. Same code path, so
    the same ordering - and the ownership REASON is what has to be visible first."""
    refusal = ("I only close windows I opened in this session. 1 chrome window is open, but I didn't "
               "open it, so I left it alone.")
    monkeypatch.setattr(console.interpreter, "interpret",
                        Brainless(understood(Intent(CLOSE_APP, CloseAppArgs("chrome"),
                                                    why="you asked", risk_floor=RiskLevel.LOW))))
    monkeypatch.setattr(console, "execute_with_recovery",
                        failing_executor(refusal, fail_target="chrome"))
    script = Transcript("could you close chrome for me", "yes", "", "exit")
    run_console(read=script.read, write=script.write, focus=NoFocus())
    reason = script.index_of_say(refusal)
    prompt = script.index_of_ask(console.CORRECTION_PROMPT)
    assert reason is not None, script.events
    if prompt is not None:
        assert reason < prompt, script.events
    assert len([t for k, t in script.events if k == "say" and refusal in t]) == 1


@pytest.mark.parametrize("status", sorted(s.value for s in console.NO_CORRECTION_AFTER))
def test_the_excluded_statuses_are_unchanged(status):
    """1. "Statuses already in NO_CORRECTION_AFTER stay excluded" - and nothing new joined them.
    A target name that was not found is Status.RAN with a failed result, and it stays correctable,
    because the owner's own smoke showed rewording working."""
    assert set(console.NO_CORRECTION_AFTER) == {Status.DENIED, Status.STOPPED,
                                                Status.NOT_HANDED_OVER}
    assert Status.RAN not in console.NO_CORRECTION_AFTER


def test_a_denied_action_still_reports_once_and_asks_nothing(monkeypatch):
    """1. The ordering fix must not start asking after a denial, which Slice 1's follow-up stopped."""
    monkeypatch.setattr(console.interpreter, "interpret", Brainless(understood()))
    monkeypatch.setattr(console, "execute_with_recovery", _denying_executor())
    script = Transcript(LOOSE, "yes", "exit")
    run_console(read=script.read, write=script.write, focus=NoFocus())
    assert script.index_of_ask(console.CORRECTION_PROMPT) is None, script.events
    assert len([t for k, t in script.events if k == "say" and "did not confirm" in t]) == 1


# --- the owner's `click Sign login`, traced -----------------------------------------------------------

def test_a_command_typed_at_the_correction_prompt_is_queued_not_consumed(monkeypatch):
    """THE TRACE the owner asked for. `click Sign login` at the correction prompt was NOT used as
    correction text: the deterministic parser recognises "click", so Slice 1's handoff queued it and
    the outer loop ran it as a NEW root command. The plan that appeared immediately afterwards was
    that command's own plan, with a fresh Brain allowance - not a re-plan of the failed one.

    Pinned because it is the behaviour that made the prompt useful in the real session."""
    seen = []
    real = console.handle_typed_line

    def watched(text, context, prompts, **kwargs):
        seen.append(text)
        return real(text, context, prompts, **kwargs)

    brain = Brainless(understood(), NotACommand(message="no"))
    monkeypatch.setattr(console, "handle_typed_line", watched)
    monkeypatch.setattr(console.interpreter, "interpret", brain)
    monkeypatch.setattr(console, "execute_with_recovery", failing_executor())
    script = Transcript(LOOSE, "yes", "click Sign login", "exit")
    assert run_console(read=script.read, write=script.write, focus=NoFocus()) == 0

    assert seen == [LOOSE, "click Sign login"], seen
    assert seen.count("click Sign login") == 1, "it ran more than once"
    assert brain.calls == 2, "one for the plan, one for the queued command - not a replan as well"
    # and the reason for the ORIGINAL failure still came first
    assert script.index_of_say(NOT_FOUND) < script.index_of_ask(console.CORRECTION_PROMPT)


def test_free_text_at_the_correction_prompt_is_still_a_correction(monkeypatch):
    """...and the other half: prose there is still the correction, so the prompt keeps its purpose."""
    brain = Brainless(understood(), understood())
    monkeypatch.setattr(console.interpreter, "interpret", brain)
    monkeypatch.setattr(console, "execute_with_recovery", failing_executor())
    script = Transcript(LOOSE, "yes", "the other one", "cancel", "exit")
    run_console(read=script.read, write=script.write, focus=NoFocus())
    assert brain.calls == 2, "the correction did not reach the provider"
    assert script.asked.count(console.PROCEED_PROMPT) == 2, "no replacement plan was offered"
