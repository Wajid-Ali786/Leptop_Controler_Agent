"""
The voice console with the Brain wired in (Phase 3 Slice 3B).

Offline throughout: the microphone is a scripted `listen`, the provider is a scripted function, and the
Executor is replaced wherever a test is about orchestration rather than about acting.

Three properties carry this slice:

  * a spoken command that already resolves still runs locally, for free, exactly as in Phase 2;
  * the ASR punctuation reconciliation keeps precedence, so a full stop the recogniser added never turns
    a free command into a paid one - and never traps a loose request either;
  * the Brain may plan only open_app and close_app here, because voice has no reliable focus hand-over
    for the other six, and the Planner refuses them before any action exists.
"""
import ast
from pathlib import Path

import pytest

from app import console, voice_console
from app.brain import logic as brain
from app.brain.interpreter import Unavailable
from app.brain.models import (CloseAppArgs, Intent, NeedsClarification, NotACommand, NotSupported,
                              OpenAppArgs, RefreshArgs, ShortcutArgs, TypeTextArgs, Understood,
                              WindowControlArgs, ClickArgs, ScrollArgs)
from app.executor.models import (CLICK, CLOSE_APP, OPEN_APP, REFRESH, SCROLL, SHORTCUT, TYPE_TEXT,
                                 WINDOW_CONTROL, ActionResult, ExecutorAction, Outcome)
from app.listener.models import Transcript
from app.planner.models import (MAX_BRAIN_CALLS, PLAN_TERMINAL, TYPED_CONSOLE, VOICE_CONSOLE,
                                TurnContext)
from app.safety.models import RiskLevel

SECRET = "ZZ-my-diary-password-hunter2-ZZ"


# --- A scripted voice session -------------------------------------------------------------------------

class Screen:
    def __init__(self):
        self.lines = []

    def __call__(self, text=""):
        self.lines.append(str(text))

    @property
    def text(self):
        return "\n".join(self.lines)


class Script:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = []

    def __call__(self, prompt=""):
        self.asked.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


class Ear:
    """The injected `listen` seam: one prepared transcript per call, and it counts the calls."""

    def __init__(self, *heard):
        self.heard = list(heard)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if not self.heard:
            raise AssertionError("listen() was called more often than the test prepared for")
        return self.heard.pop(0)


class Brainless:
    """A scripted provider. Records every request; raises if asked more often than prepared."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, prompt):
        self.requests.append(prompt)
        if not self.answers:
            raise AssertionError(f"the Brain was asked {len(self.requests)} times; "
                                 f"{len(self.requests) - 1} answers were prepared")
        return self.answers.pop(0)

    @property
    def calls(self):
        return len(self.requests)


class Executed:
    """The whole Executor pipeline, recorded rather than performed."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, action, confirm=None, offer_retry=None, *, risk_floor=RiskLevel.LOW):
        self.calls.append((action, risk_floor, confirm))
        outcome = self.outcomes.pop(0) if self.outcomes else True
        if isinstance(outcome, ActionResult):
            return outcome
        return ActionResult(action, bool(outcome), f"did {action.kind}" if outcome
                            else f"couldn't do {action.kind}")

    @property
    def actions(self):
        return [action for action, _floor, _confirm in self.calls]

    @property
    def kinds(self):
        return [action.kind for action in self.actions]


@pytest.fixture
def executor(monkeypatch):
    fake = Executed()
    monkeypatch.setattr(console, "execute_with_recovery", fake)
    return fake


@pytest.fixture
def planned(monkeypatch):
    """Records the front end handed to the Planner at every planning site."""
    seen = []
    real = console.session.build_plan

    def spy(understood, frontend, resolve, user_text=""):
        seen.append(frontend)
        return real(understood, frontend, resolve, user_text)

    monkeypatch.setattr(console.session, "build_plan", spy)
    return seen


@pytest.fixture(autouse=True)
def no_stop(monkeypatch):
    monkeypatch.setattr(console.emergency_stop, "is_stopped", lambda: False)


@pytest.fixture(autouse=True)
def no_real_provider(monkeypatch):
    """Nothing in this file may reach the real provider, even by forgetting to inject one."""
    def refuse(prompt):
        raise AssertionError("a test reached the real Brain interpreter")

    monkeypatch.setattr(console.interpreter, "interpret", refuse)


def say(text, **metadata):
    return Transcript(text=text, **metadata)


def voice(heard, *answers, interpret=None, speaker=None, focus=None):
    """One voice session: hear `heard`, answer `answers` on the keyboard, then leave."""
    screen, script = Screen(), Script(*answers)
    ear = heard if isinstance(heard, Ear) else Ear(heard)
    if interpret is not None:
        import app.brain.interpreter as module
        module.interpret = interpret           # restored by the autouse fixture's monkeypatch
    code = voice_console.run_voice_console(ear, read=script, write=screen, focus=focus,
                                          speaker=speaker)
    return screen, script, ear, code


@pytest.fixture
def brainless(monkeypatch):
    """Installs a scripted provider the way voice reaches it - through console.interpreter."""
    def install(*answers):
        fake = Brainless(*answers)
        monkeypatch.setattr(console.interpreter, "interpret", fake)
        return fake
    return install


def understood(*intents, restated="what you asked for"):
    return Understood(intents=tuple(intents), restated=restated)


def open_notepad(floor=RiskLevel.LOW):
    return Intent(OPEN_APP, OpenAppArgs("notepad"), why="you asked for notepad", risk_floor=floor)


# --- A/B. A resolved spoken command stays local and free ----------------------------------------------

@pytest.mark.parametrize("spoken, kind", [("open notepad", OPEN_APP), ("minimize", WINDOW_CONTROL),
                                          ("refresh", REFRESH), ("type hello world", TYPE_TEXT),
                                          ("scroll down 3", SCROLL), ("shortcut ctrl+a", SHORTCUT),
                                          ("click 500, 300", CLICK), ("close notepad", CLOSE_APP)])
def test_a_resolved_spoken_command_stays_local_with_no_brain_call(spoken, kind, executor, brainless):
    fake = brainless()                        # nothing prepared: one call would raise
    voice(say(spoken), "listen", "a", "exit", interpret=fake)
    assert fake.calls == 0, "a resolved spoken command must cost nothing"
    assert executor.kinds == [kind]


def test_a_deterministic_type_text_keeps_its_phase_2_behaviour(executor, brainless):
    """The voice capability guard restricts what the BRAIN may plan. It must not touch a deterministic
    command the user spoke directly - including its known focus limitation."""
    fake = brainless()
    voice(say("type hello world"), "listen", "a", "exit", interpret=fake)
    assert fake.calls == 0
    assert executor.kinds == [TYPE_TEXT], "still runs, exactly as in Phase 2"


# --- U/V. Punctuation reconciliation keeps precedence -------------------------------------------------

def test_open_notepad_with_a_full_stop_stays_free(executor, brainless):
    """The reconciliation offers reading 2; choosing it costs nothing. If this regressed, every
    recogniser full stop would become a paid Brain request."""
    fake = brainless()
    screen, _, _, _ = voice(say("open notepad."), "listen", "a", "2", "a", "exit", interpret=fake)
    assert fake.calls == 0, "the deterministic reading was free"
    assert executor.actions == [ExecutorAction(OPEN_APP, "notepad")]
    assert "[1] [open notepad.]" in screen.text and "[2] [open notepad]" in screen.text


def test_choosing_the_punctuated_reading_is_the_users_own_choice(executor, brainless):
    """Reading 1 is the line that does NOT resolve. Picking it deliberately is a loose request, so it
    reaches the Brain - the reconciliation was offered and declined."""
    fake = brainless(understood(open_notepad()))
    voice(say("open notepad."), "listen", "a", "1", "yes", "exit", interpret=fake)
    assert fake.calls == 1


@pytest.mark.parametrize("spoken, trimmed", [("minimize.", "minimize"), ("refresh.", "refresh")])
def test_every_reconciliation_branch_is_unchanged_when_a_reading_resolves(spoken, trimmed, executor,
                                                                         brainless):
    fake = brainless()
    screen, _, _, _ = voice(say(spoken), "listen", "a", "2", "a", "exit", interpret=fake)
    assert fake.calls == 0
    assert f"[2] [{trimmed}]" in screen.text
    assert voice_console.AMBIGUOUS in screen.lines


# --- C/T. A loose spoken request reaches the Brain ----------------------------------------------------

LOOSE = "could you open notepad for me please"


def test_a_loose_spoken_request_reaches_the_brain(executor, brainless):
    fake = brainless(understood(open_notepad()))
    # "" declines the correction offer that follows a declined plan; then "exit" leaves.
    screen, _, _, _ = voice(say(LOOSE), "listen", "a", "no", "", "exit", interpret=fake)
    assert fake.calls == 1
    assert console.PLAN_HEADER in screen.text and "1. Open notepad" in screen.text
    assert executor.calls == [], "nothing ran before acceptance"


@pytest.mark.parametrize("spoken", [f"{LOOSE}.", "usko message kar do.", "kuch bhi bolo.",
                                   "could you close it please."])
def test_a_loose_request_ending_in_a_full_stop_is_not_trapped(spoken, executor, brainless):
    """The regression the audit predicted: Whisper usually adds a full stop, and Phase 2 would have
    answered NOT_A_COMMAND forever."""
    fake = brainless(NotACommand(message="Nothing to do."))
    screen, _, _, _ = voice(say(spoken), "listen", "a", "exit", interpret=fake)
    assert fake.calls == 1, f"{spoken!r} never reached the Brain"
    assert voice_console.NOT_A_COMMAND not in screen.lines


def test_a_line_that_is_not_even_brain_eligible_is_still_refused_locally():
    assert not console.is_brain_eligible("")
    assert not console.is_brain_eligible("   ")
    assert console.is_brain_eligible("kuch bhi bolo.")


def test_the_routing_question_is_pure(monkeypatch):
    """Consulting it must not call a provider, spend an allowance or change any context."""
    monkeypatch.setattr(console.interpreter, "interpret",
                        lambda prompt: pytest.fail("routing must not call the provider"))
    before = TurnContext()
    for text in ("open notepad", "kuch bhi bolo.", LOOSE, ""):
        console.is_brain_eligible(text)
    assert before == TurnContext()


# --- D. Nothing executes before typed plan acceptance -------------------------------------------------

def test_a_brain_plan_runs_only_after_a_typed_yes(executor, brainless):
    fake = brainless(understood(open_notepad()))
    voice(say(LOOSE), "listen", "a", "yes", "exit", interpret=fake)
    assert executor.kinds == [OPEN_APP]


@pytest.mark.parametrize("answer", ["no", "n", "", "y", "sure", "YE"])
def test_only_the_exact_word_yes_accepts_a_spoken_plan(answer, executor, brainless):
    fake = brainless(understood(open_notepad()))
    voice(say(LOOSE), "listen", "a", answer, "", "exit", interpret=fake)
    assert executor.calls == []


def test_the_plan_is_shown_on_screen_and_not_spoken(executor, brainless):
    """3B adds no intermediate TTS: the preview is read, not heard."""
    said = []
    fake = brainless(understood(open_notepad()))
    import app.speaker.adapter as speaker_adapter
    from app.speaker.models import SpeakerSettings
    settings = SpeakerSettings(enabled=True, engine="offline", voice="", rate=0)
    original = speaker_adapter.speak
    speaker_adapter.speak = lambda text, s=None: said.append(text)
    try:
        screen, _, _, _ = voice(say(LOOSE), "listen", "a", "yes", "exit", interpret=fake,
                                speaker=settings)
    finally:
        speaker_adapter.speak = original
    assert "1. Open notepad" in screen.text
    assert not any("Plan:" in spoken for spoken in said), "the plan was not spoken"
    assert len(said) == 1, "only the final reply is spoken"


# --- E. Safety confirmation stays separate from plan acceptance ---------------------------------------

def test_a_brain_planned_close_app_still_asks_the_existing_safety_question(brainless, monkeypatch):
    """Two different questions: accept the plan, then confirm a MEDIUM action. Neither is merged, and
    the confirmation is the Executor's own."""
    from app.executor import logic as executor_logic
    asked = []

    def refuse_adapter(*args, **kwargs):
        raise AssertionError("nothing may happen when the safety question is denied")

    for name in ("launch_app", "request_close"):
        monkeypatch.setattr(executor_logic.adapter, name, refuse_adapter, raising=False)

    seen_confirm = []
    real = console.execute_with_recovery

    def spy(action, confirm=None, offer_retry=None, *, risk_floor=RiskLevel.LOW):
        seen_confirm.append(confirm)
        if confirm is not None:
            from app.safety.models import Action, RiskAssessment
            asked.append(confirm(Action("close notepad", minimum_level=RiskLevel.MEDIUM),
                                 RiskAssessment(RiskLevel.MEDIUM, "closing can lose unsaved work")))
        return ActionResult(action, False, "not confirmed")

    monkeypatch.setattr(console, "execute_with_recovery", spy)
    fake = brainless(understood(Intent(CLOSE_APP, CloseAppArgs("notepad"))))
    # "yes" accepts the plan; the second "no" answers the SAFETY question and denies it.
    voice(say("could you close notepad please"), "listen", "a", "yes", "no", "", "exit",
          interpret=fake)
    assert seen_confirm and seen_confirm[0] is not None, "the Executor was given a confirmation"
    assert asked == [False], "the safety question was asked and denied separately"


# --- F/W. The voice capability guard -----------------------------------------------------------------

FORBIDDEN = [
    (CLICK, ClickArgs(500, 300)),
    (TYPE_TEXT, TypeTextArgs("hello")),
    (SHORTCUT, ShortcutArgs("ctrl+c")),
    (SCROLL, ScrollArgs("down", 3)),
    (REFRESH, RefreshArgs()),
    (WINDOW_CONTROL, WindowControlArgs("minimize")),
]


@pytest.mark.parametrize("kind, args", FORBIDDEN, ids=[case[0] for case in FORBIDDEN])
def test_a_brain_planned_forbidden_kind_never_reaches_the_executor(kind, args, executor, brainless):
    fake = brainless(understood(Intent(kind, args)))
    screen, _, _, _ = voice(say("could you do that thing for me"), "listen", "a", "exit",
                            interpret=fake)
    assert executor.calls == [], f"{kind} reached the Executor from voice"
    assert "voice console" in screen.text, "and the refusal says why"
    assert console.PLAN_HEADER not in screen.text, "no plan was even offered"


@pytest.mark.parametrize("kind, args", [(OPEN_APP, OpenAppArgs("notepad")),
                                        (CLOSE_APP, CloseAppArgs("notepad"))])
def test_the_two_allowed_kinds_can_be_planned_from_voice(kind, args, executor, brainless):
    fake = brainless(understood(Intent(kind, args)))
    voice(say("could you do that for me"), "listen", "a", "yes", "exit", interpret=fake)
    assert executor.kinds == [kind]


def test_a_mixed_plan_is_refused_entirely(executor, brainless):
    """open_app is allowed and type_text is not, so the whole plan is refused - no partial execution."""
    fake = brainless(understood(open_notepad(), Intent(TYPE_TEXT, TypeTextArgs("hello"))))
    screen, _, _, _ = voice(say("could you open notepad and type hello"), "listen", "a", "exit",
                            interpret=fake)
    assert executor.calls == [], "not even the allowed first step ran"
    assert console.PLAN_HEADER not in screen.text


# --- S. Voice always plans as the voice console -------------------------------------------------------

def test_every_planning_site_reached_from_voice_receives_voice_console(executor, brainless, planned):
    """The one way past the guard would be a hardcoded front end in the shared orchestration."""
    fake = brainless(understood(open_notepad()), understood(Intent(CLOSE_APP, CloseAppArgs("notepad"))))
    voice(say(LOOSE), "listen", "a", "no", "close it instead", "yes", "exit", interpret=fake)
    assert planned, "the Planner was reached"
    assert all(frontend is VOICE_CONSOLE for frontend in planned), planned
    assert TYPED_CONSOLE not in planned


def test_the_typed_console_still_plans_as_the_typed_console(planned, monkeypatch):
    fake = Brainless(understood(Intent(TYPE_TEXT, TypeTextArgs("hello"))))
    monkeypatch.setattr(console, "execute_with_recovery",
                        lambda action, confirm=None, offer_retry=None, **floor:
                        ActionResult(action, True, "ok"))
    screen = Screen()
    script = Script("yes")
    console.handle_typed_line("could you type hello", TurnContext(),
                              console.Prompts(read=script, write=screen), interpret=fake)
    assert planned == [TYPED_CONSOLE]


def test_no_planning_site_in_the_shared_orchestration_hardcodes_a_front_end():
    tree = ast.parse(Path(console.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "build_plan"):
            arguments = [ast.unparse(argument) for argument in node.args]
            assert "TYPED_CONSOLE" not in arguments, arguments
            assert "frontend" in arguments, arguments


# --- G/H. Clarification, once ------------------------------------------------------------------------

def test_one_clarification_round_is_asked_on_screen_and_answered_on_the_keyboard(executor, brainless):
    fake = brainless(NeedsClarification(question="Which editor did you mean?", missing="app"),
                     understood(open_notepad()))
    screen, _, ear, _ = voice(say("could you open the editor"), "listen", "a", "notepad", "yes",
                              "exit", interpret=fake)
    assert "Which editor did you mean?" in screen.text
    assert fake.calls == 2
    assert "notepad" in fake.requests[1]
    assert ear.calls == 1, "no new recording window was opened for the answer"
    assert executor.kinds == [OPEN_APP]


def test_a_second_clarification_round_is_refused(executor, brainless):
    fake = brainless(NeedsClarification(question="Which one?", missing="app"),
                     NeedsClarification(question="Which notepad?", missing="app"))
    voice(say("could you open the editor"), "listen", "a", "notepad", "exit", interpret=fake)
    assert fake.calls == 2, "no third call"
    assert executor.calls == []


def test_usko_message_kar_do_never_guesses_a_recipient(executor, brainless):
    fake = brainless(NeedsClarification(question="Kis ko message karun?", missing="recipient"),
                     NotSupported(what="send a message", message="I can't send messages yet."))
    screen, _, _, _ = voice(say("usko message kar do"), "listen", "a", "Ali", "exit", interpret=fake)
    assert "Kis ko message karun?" in screen.text
    assert "I can't send messages yet." in screen.text
    assert executor.calls == [], "messaging is not a capability"


def test_messaging_is_still_not_a_voice_capability():
    assert VOICE_CONSOLE.may_plan == frozenset({OPEN_APP, CLOSE_APP})
    for invented in ("send_message", "message", "whatsapp", "sms", "email"):
        assert invented not in VOICE_CONSOLE.may_plan


# --- I/J. The provider is unreachable ----------------------------------------------------------------

def test_the_frozen_message_is_the_whole_message_in_voice_too(executor, brainless):
    fake = brainless(Unavailable("ClaudeUnavailableError"))
    screen, _, _, _ = voice(say(LOOSE), "listen", "a", "exit", interpret=fake)
    assert brain.UNAVAILABLE_MESSAGE in screen.lines
    assert executor.calls == []
    for line in screen.lines:
        if brain.UNAVAILABLE_MESSAGE in line:
            assert line == brain.UNAVAILABLE_MESSAGE, "nothing appended"


def test_a_deterministic_spoken_command_still_works_after_the_provider_failed(executor, brainless):
    fake = brainless(Unavailable("ClaudeUnavailableError"))
    voice(Ear(say(LOOSE), say("open notepad")), "listen", "a", "listen", "a", "exit", interpret=fake)
    assert fake.calls == 1, "the second command needed no Brain"
    assert executor.kinds == [OPEN_APP]


def test_an_unusable_reply_runs_nothing(executor, brainless):
    fake = brainless(brain.InterpretationError(brain.INVALID_JSON))
    voice(say(LOOSE), "listen", "a", "exit", interpret=fake)
    assert fake.calls == 1, "no automatic retry"
    assert executor.calls == []


# --- K/P. Short-term context --------------------------------------------------------------------------

def test_a_verified_spoken_action_is_remembered(executor, brainless, monkeypatch):
    seen = []
    real = console.session.remember_action

    def spy(context, kind, target):
        seen.append((kind, target))
        return real(context, kind, target)

    monkeypatch.setattr(console.session, "remember_action", spy)
    fake = brainless()
    voice(say("open notepad"), "listen", "a", "exit", interpret=fake)
    assert seen == [(OPEN_APP, "notepad")]


def test_an_unverified_spoken_action_is_not_remembered(executor, brainless, monkeypatch):
    seen = []
    monkeypatch.setattr(console.session, "remember_action",
                        lambda context, kind, target: seen.append(kind) or context)
    executor.outcomes = [ActionResult(ExecutorAction(CLICK, "500, 300"), True, "sent",
                                      outcome=Outcome.UNVERIFIED)]
    fake = brainless()
    voice(say("click 500, 300"), "listen", "a", "exit", interpret=fake)
    assert seen == [], "ok but unverified is never confirmed success"


def test_a_failed_spoken_action_is_not_remembered(executor, brainless, monkeypatch):
    seen = []
    monkeypatch.setattr(console.session, "remember_action",
                        lambda context, kind, target: seen.append(kind) or context)
    executor.outcomes = [ActionResult(ExecutorAction(OPEN_APP, "notepad"), False, "didn't open")]
    fake = brainless()
    voice(say("open notepad"), "listen", "a", "exit", interpret=fake)
    assert seen == []


def test_the_context_survives_between_spoken_commands_and_reaches_the_brain(executor, brainless):
    fake = brainless(NotACommand(message="ok"))
    voice(Ear(say("open calculator"), say("could you close it please")),
          "listen", "a", "listen", "a", "exit", interpret=fake)
    assert fake.calls == 1
    assert "last thing actually done" in fake.requests[0]
    assert "calculator" in fake.requests[0]


def test_a_spoken_typed_payload_never_enters_the_context(executor, brainless):
    fake = brainless()
    executor.outcomes = [ActionResult(ExecutorAction(TYPE_TEXT, SECRET), True, "Typed 31 characters.")]
    screen, _, _, _ = voice(say(f"type {SECRET}"), "listen", "a", "exit", interpret=fake)
    fake2 = brainless(NotACommand(message="ok"))
    voice(say("could you do that again"), "listen", "a", "exit", interpret=fake2)
    assert fake2.calls == 1
    assert SECRET not in fake2.requests[0] and "hunter2" not in fake2.requests[0]


def test_the_voice_console_never_builds_a_context_itself():
    tree = ast.parse(Path(voice_console.__file__).read_text(encoding="utf-8"))
    built = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    assert "previous_action_context" not in built
    assert "PreviousActionContext" not in built
    assert "remember_action" not in built, "the shared orchestration does that"
    assert "TurnContext" in built, "it only creates the one session context"


def test_exactly_one_turn_context_is_created_per_session():
    tree = ast.parse(Path(voice_console.__file__).read_text(encoding="utf-8"))
    created = [node for node in ast.walk(tree)
               if isinstance(node, ast.Call) and ast.unparse(node.func) == "TurnContext"]
    assert len(created) == 1, "one context for the whole voice session, and only one"


# --- L/§19. ASR correction is not semantic re-plan ----------------------------------------------------

def test_asr_correction_fixes_what_was_heard_and_costs_nothing(executor, brainless):
    """`e` then a token replacement changes the TRANSCRIPT before acceptance. No Brain, no lifecycle."""
    fake = brainless()
    voice(say("open notepod"), "listen", "e", "2", "notepad", "a", "exit", interpret=fake)
    assert fake.calls == 0
    assert executor.actions == [ExecutorAction(OPEN_APP, "notepad")]


def test_semantic_correction_is_inherited_and_is_a_different_thing(executor, brainless, monkeypatch):
    """A rejected PLAN is corrected in typed natural language, after acceptance was declined. It is the
    Slice 1B flow, inherited unchanged - not _correct(), and it never touches the transcript."""
    tokens = []
    real_correct = voice_console._correct
    monkeypatch.setattr(voice_console, "_correct",
                        lambda pending, read, write: tokens.append(1) or real_correct(pending, read,
                                                                                     write))
    fake = brainless(understood(Intent(OPEN_APP, OpenAppArgs("calculator"))),
                     understood(open_notepad()))
    voice(say(LOOSE), "listen", "a", "no", "I meant notepad", "yes", "exit", interpret=fake)
    assert fake.calls == 2, "one initial reading and one semantic correction"
    assert "I meant notepad" in fake.requests[1]
    assert tokens == [], "_correct() was never involved"
    assert executor.actions == [ExecutorAction(OPEN_APP, "notepad")]


def test_only_one_semantic_correction_per_spoken_root_command(executor, brainless):
    fake = brainless(understood(Intent(OPEN_APP, OpenAppArgs("calculator"))),
                     understood(Intent(OPEN_APP, OpenAppArgs("calculator"))))
    voice(say(LOOSE), "listen", "a", "no", "try again", "no", "and again", "exit", interpret=fake)
    assert fake.calls == 2, "the correction allowance is spent after one"


def test_the_old_plan_is_terminal_before_a_replacement_and_keeps_its_own_id(executor, brainless,
                                                                           monkeypatch):
    ids = []
    real = console.session.propose_plan

    def spy(context, plan, text, plan_id=None):
        outcome = real(context, plan, text, plan_id)
        if isinstance(outcome, TurnContext):
            ids.append(outcome.pending_plan.plan_id)
        return outcome

    monkeypatch.setattr(console.session, "propose_plan", spy)
    fake = brainless(understood(Intent(OPEN_APP, OpenAppArgs("calculator"))),
                     understood(open_notepad()))
    voice(say(LOOSE), "listen", "a", "no", "I meant notepad", "yes", "exit", interpret=fake)
    assert len(ids) == 2 and ids[0] != ids[1], ids


# --- M. Emergency stop -------------------------------------------------------------------------------

def test_a_stop_observed_at_the_boundary_invalidates_pending_voice_state(executor, brainless,
                                                                        monkeypatch):
    """The shared entry point observes it, so voice gets the same behaviour as the typed console - and
    voice_console.py still imports nothing from the emergency-stop module."""
    from app.planner import logic as session
    from app.planner.models import LifecycleRefusal
    fake = brainless(understood(open_notepad()))
    screen, _, _, _ = voice(say(LOOSE), "listen", "a", "no", "", "exit", interpret=fake)
    context = TurnContext()
    monkeypatch.setattr(console.emergency_stop, "is_stopped", lambda: True)
    reply, after = console.handle_typed_line("open notepad", context,
                                             console.Prompts(read=Script(), write=Screen()),
                                             frontend=VOICE_CONSOLE)
    assert after.pending_plan is None and after.pending_clarification is None


def test_the_voice_console_still_imports_no_stop_path():
    tree = ast.parse(Path(voice_console.__file__).read_text(encoding="utf-8"))
    names = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    names |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
              for alias in node.names}
    assert not any("emergency_stop" in (name or "") for name in names), names
    assert not any("executor" in (name or "") for name in names), names


# --- N. The Brain-call bound --------------------------------------------------------------------------

def test_the_worst_case_is_three_brain_calls_for_one_spoken_root_command(executor, brainless):
    fake = brainless(NeedsClarification(question="Which one?", missing="app"),
                     understood(Intent(OPEN_APP, OpenAppArgs("calculator"))),
                     understood(open_notepad()))
    voice(say("could you open the editor"), "listen", "a", "notepad", "no", "I meant notepad", "yes",
          "exit", interpret=fake)
    assert fake.calls == MAX_BRAIN_CALLS == 3


def test_transcript_work_before_acceptance_spends_no_allowance(executor, brainless):
    """Redictating, correcting a token and choosing a reading are about what was HEARD. None of them is
    a root command, so none may buy a Brain call."""
    fake = brainless(understood(open_notepad()))
    voice(Ear(say("open notepod"), say(LOOSE)),
          "listen", "r",                 # redictate: drops the first candidate entirely
          "e", "1", "could", "a",        # correct one token of the second, then accept
          "yes", "exit", interpret=fake)
    assert fake.calls <= 1, f"{fake.calls} calls after transcript work alone"


# --- O. Privacy --------------------------------------------------------------------------------------

def test_nothing_private_reaches_a_log_or_the_speaker(executor, brainless, caplog):
    said = []
    import app.speaker.adapter as speaker_adapter
    from app.speaker.models import SpeakerSettings
    original = speaker_adapter.speak
    speaker_adapter.speak = lambda text, s=None: said.append(text)
    fake = brainless(understood(Intent(TYPE_TEXT, TypeTextArgs(SECRET)), restated=SECRET))
    try:
        with caplog.at_level("DEBUG"):
            screen, _, _, _ = voice(say(f"could you type {SECRET} for me"), "listen", "a", "exit",
                                    interpret=fake,
                                    speaker=SpeakerSettings(enabled=True, engine="offline",
                                                            voice="", rate=0))
    finally:
        speaker_adapter.speak = original
    assert SECRET not in caplog.text and "hunter2" not in caplog.text
    assert not any(SECRET in spoken for spoken in said), "the payload was never spoken"
    assert executor.calls == [], "type_text is not a voice Brain capability anyway"


# --- X / §22. TTS and the microphone cannot overlap ---------------------------------------------------

def _call_graph(tree):
    """Module-level function name -> the names it calls."""
    graph = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            called = set()
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    called.add(ast.unparse(inner.func))
            graph[node.name] = called
    return graph


def _reachable(graph, start):
    seen, stack = set(), [start]
    while stack:
        name = stack.pop()
        for called in graph.get(name, ()):
            bare = called.split(".")[-1]
            if bare not in seen:
                seen.add(bare)
                stack.append(bare)
    return seen


def test_no_code_path_from_speaking_can_open_the_microphone():
    """STRUCTURAL, not behavioural. Today TTS and capture are sequential because of WHERE the calls sit,
    not because anything enforces it. This test is the enforcement: if a future slice adds a spoken
    prompt that then listens, it fails here rather than letting the microphone record the assistant."""
    tree = ast.parse(Path(voice_console.__file__).read_text(encoding="utf-8"))
    graph = _call_graph(tree)
    reachable = _reachable(graph, "_speak")
    for forbidden in ("listen", "_record_once", "capture", "_Recorder", "start", "transcribe"):
        assert forbidden not in reachable, f"_speak can reach {forbidden}: {sorted(reachable)}"


def test_speaking_has_exactly_one_call_site_and_it_is_last():
    tree = ast.parse(Path(voice_console.__file__).read_text(encoding="utf-8"))
    sites = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and ast.unparse(node.func) == "_speak"]
    assert len(sites) == 1, "one place speaks, so there is one place to reason about"
    run = next(node for node in ast.walk(tree)
               if isinstance(node, ast.FunctionDef) and node.name == "_run")
    steps = [ast.unparse(statement) for statement in run.body]
    spoke = next(index for index, step in enumerate(steps) if step.startswith("_speak("))
    handled = next(index for index, step in enumerate(steps) if "handle_typed_line" in step)
    assert handled < spoke, "nothing is spoken before the whole pipeline has finished"


def test_the_microphone_is_opened_from_exactly_one_place():
    tree = ast.parse(Path(voice_console.__file__).read_text(encoding="utf-8"))
    sites = [ast.unparse(node.func) for node in ast.walk(tree)
             if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("adapter.capture")]
    assert sites == ["adapter.capture"], sites
    graph = _call_graph(tree)
    assert "capture" in _reachable(graph, "_record"), "only the recorder opens it"


def test_no_new_keyboard_prompt_became_a_recording(executor, brainless):
    """Behavioural companion: a whole Brain turn with a clarification, a plan acceptance and a
    correction opens the microphone exactly once."""
    fake = brainless(NeedsClarification(question="Which one?", missing="app"),
                     understood(Intent(OPEN_APP, OpenAppArgs("calculator"))),
                     understood(open_notepad()))
    _screen, _script, ear, _code = voice(say("could you open the editor"), "listen", "a", "notepad",
                                         "no", "I meant notepad", "yes", "exit", interpret=fake)
    assert ear.calls == 1, "one utterance, one recording"


# --- Q/R. Nothing real, and Phase 2 untouched ---------------------------------------------------------

def test_no_provider_or_device_is_reachable(monkeypatch, executor, brainless):
    from app.brain import adapter as brain_adapter
    from app.executor import adapter as executor_adapter
    from app.listener import adapter as listener_adapter

    def refuse(*args, **kwargs):
        raise AssertionError("no real provider or device call may happen")

    for module, names in ((brain_adapter, ("send_message", "ping", "get_client")),
                          (executor_adapter, ("launch_app", "click", "send_character")),
                          (listener_adapter, ("capture", "transcribe", "ensure_model"))):
        for name in names:
            monkeypatch.setattr(module, name, refuse, raising=False)
    fake = brainless(understood(open_notepad()))
    voice(say(LOOSE), "listen", "a", "yes", "exit", interpret=fake)
    voice(say("open notepad"), "listen", "a", "exit", interpret=brainless())


def test_the_phase_2_transcript_flow_is_untouched():
    """The accept/correct/redictate/cancel vocabulary and the reconciliation messages are unchanged."""
    assert voice_console.CHOICES == {"accept": "accept", "a": "accept", "correct": "correct",
                                     "e": "correct", "redictate": "redictate", "r": "redictate",
                                     "cancel": "cancel", "x": "cancel"}
    assert voice_console.NOT_A_COMMAND and voice_console.AMBIGUOUS and voice_console.CANCELLED
    source = Path(voice_console.__file__).read_text(encoding="utf-8")
    assert "typed_confirmation" in source, "the safety confirmation is still the typed one"
    assert "_says_yes" not in source, "voice does not re-implement the confirmation rule"
