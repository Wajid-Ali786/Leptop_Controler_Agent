"""
Window ownership across all three open paths (Slice 3B real-session audit).

THE QUESTION THIS FILE ANSWERS. A real Voice session opened Notepad through a Brain-created plan, and a
later close in the same transcript reported "I only close windows I opened in this session". Is that a
defect, or is it the correct answer for a second process?

So these tests drive the SAME production code three ways - deterministic, typed Brain, spoken Brain -
against a completely fake desktop (the FakeDesktop and `world` fixture that app/executor's own close
tests already use), and ask after each one whether ownership was recorded and whether the close is
permitted.

Nothing launches, nothing closes, and no window exists outside the fake.
"""
import pytest

from app import console, voice_console
from app.brain.models import CloseAppArgs, Intent, OpenAppArgs, Understood
from app.executor import logic as executor_logic
from app.executor.models import CLOSE_APP, OPEN_APP, ActionResult, ExecutorAction
from app.listener.models import Transcript
from app.planner.models import VOICE_CONSOLE, TurnContext
# The fake desktop lives with the close tests; reusing it is the point - a second fake would be a second
# opinion about how windows behave.
from tests.test_executor_close_app import (USERS_NOTEPAD, FakeDesktop, close_app,  # noqa: F401
                                            open_app, users_notepad, world)


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

    def __call__(self, prompt=""):
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


def owned(name="notepad"):
    """What the Executor's own ownership store says, read directly."""
    return [set(group.handles) for group in executor_logic._session_windows.get(name, [])]


def understood(*intents):
    return Understood(intents=tuple(intents), restated="what you asked for")


@pytest.fixture
def brain(monkeypatch):
    """A scripted provider installed where both consoles reach it."""
    def install(*answers):
        replies = list(answers)
        requests = []

        def interpret(prompt):
            requests.append(prompt)
            assert replies, "the Brain was asked more often than the test prepared for"
            return replies.pop(0)

        monkeypatch.setattr(console.interpreter, "interpret", interpret)
        return requests
    return install


# --- 1. The ownership store itself --------------------------------------------------------------------

def test_ownership_is_a_module_level_store_so_its_lifetime_is_the_process(world):
    """Not per command, not per TurnContext, not per front end: one dict in app/executor/logic.py."""
    assert isinstance(executor_logic._session_windows, dict)
    assert owned() == [], "this test starts with nothing owned"
    open_app("notepad")
    assert len(owned()) == 1, "one group, recorded by the Executor itself"


def test_nothing_in_production_clears_ownership_mid_process():
    """forget_session_windows() exists for the test suite's own cleanup. If production called it
    anywhere, ownership would vanish under a running session - so this asserts it does not."""
    import ast
    from pathlib import Path

    from config import settings
    callers = []
    for path in (settings.PROJECT_ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and "forget_session_windows" in ast.unparse(node.func):
                callers.append(path.name)
    for extra in ("main.py",):
        tree = ast.parse((settings.PROJECT_ROOT / extra).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and "forget_session_windows" in ast.unparse(node.func):
                callers.append(extra)
    assert callers == [], f"production clears ownership in {callers}"


def test_a_verified_open_can_never_report_success_without_recording_ownership(world):
    """The two are one statement in app/executor/logic.py: _remember_opened() is the line before the
    success result, and the Verifier only reports ok when a window was really shown - which means the
    handle set is never empty. There is no in-process path between them."""
    open_app("notepad")
    assert len(owned()) == 1
    groups = owned()
    assert groups[0], "the recorded group is never empty"


# --- 2. The three open paths, compared ----------------------------------------------------------------

def test_a_deterministic_open_then_close_works(world):
    """A: the Phase 1 path, as a baseline for the other two."""
    open_app("notepad")
    assert len(owned()) == 1
    result = close_app("notepad")
    assert result.ok, result.message
    assert owned() == [], "closing forgets the group"


def test_a_typed_brain_open_then_close_works(world, brain):
    """B: the same Executor, reached through handle_typed_line and the Planner."""
    requests = brain(understood(Intent(OPEN_APP, OpenAppArgs("notepad"))),
                     understood(Intent(CLOSE_APP, CloseAppArgs("notepad"))))
    context = TurnContext()
    screen = Screen()

    reply, context = console.handle_typed_line("could you open notepad please", context,
                                               console.Prompts(read=Script("yes"), write=screen))
    # A whole-plan reply is a summary ("all 1 step finished"), not one ActionResult - so ownership is
    # read from the Executor's own store, which is the authoritative answer anyway.
    assert reply.status is console.Status.RAN, reply.message
    assert len(owned()) == 1, "a Brain-planned open records ownership exactly like a typed one"

    reply, context = console.handle_typed_line("could you close it please", context,
                                               console.Prompts(read=Script("yes"), write=screen,
                                                               confirm=lambda a, b: True))
    assert "I only close windows I opened" not in reply.message, reply.message
    assert reply.status is console.Status.RAN, reply.message
    assert owned() == [], "the close found the group and forgot it"
    assert len(requests) == 2


def test_a_spoken_brain_open_then_close_works_in_one_session(world, brain):
    """C: THE REAL CASE. Two separate utterances inside ONE run_voice_console session.

    If the reported failure were an in-process defect, this is where it would appear."""
    brain(understood(Intent(OPEN_APP, OpenAppArgs("notepad"))),
          understood(Intent(CLOSE_APP, CloseAppArgs("notepad"))))
    screen = Screen()
    heard = iter([Transcript(text="could you open notepad please"),
                  Transcript(text="could you close it please")])
    ownership_after_open = []

    real_close = executor_logic._close_session_group

    def watch(action, session):
        ownership_after_open.append(owned())
        return real_close(action, session)

    import pytest as _pytest
    monkeypatch = _pytest.MonkeyPatch()
    monkeypatch.setattr(executor_logic, "_close_session_group", watch)
    try:
        voice_console.run_voice_console(
            lambda: next(heard),
            read=Script("listen", "a", "yes",            # utterance 1: accept transcript, accept plan
                        "listen", "a", "yes", "yes",     # utterance 2: accept, accept plan, safety yes
                        "exit"),
            write=screen)
    finally:
        monkeypatch.undo()

    assert "Opened notepad" in screen.text, screen.text
    assert ownership_after_open, "the close path was reached"
    assert ownership_after_open[0], "ownership from the spoken Brain open was visible to the close"
    assert "I only close windows I opened" not in screen.text, screen.text


def test_all_three_paths_use_the_one_ownership_mechanism():
    """No second tracker for the Brain: _remember_opened is called from exactly one place, inside the
    Executor, below every front end."""
    import ast
    from pathlib import Path

    from config import settings
    sites = []
    for path in (settings.PROJECT_ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("_remember_opened"):
                sites.append(path.name)
    assert sites == ["logic.py"], sites


# --- 3. What a new process looks like -----------------------------------------------------------------

def test_a_new_application_session_owns_nothing(world):
    """`exit` in voice mode returns from run_voice_mode to main(), and the PROCESS ends - so the next
    `python main.py --voice` starts with an empty store. forget_session_windows() is how a test
    simulates that boundary; production never calls it."""
    open_app("notepad")
    assert len(owned()) == 1
    executor_logic.forget_session_windows()          # stands in for a new process
    assert owned() == []
    result = close_app("notepad")
    assert not result.ok
    assert "I only close windows I opened in this session" in result.message
    assert "but I didn't open" in result.message, result.message


def test_that_message_is_exactly_what_the_real_session_reported(world):
    """The observed sentence, reproduced deliberately with ownership absent. It is the CORRECT answer for
    a process that did not open the window - which is what makes a second process the explanation."""
    world.desktop.add("Untitled - Notepad", "Notepad")      # a Notepad this session did not open
    result = close_app("notepad")
    assert not result.ok
    assert "I didn't open" in result.message or "I only close windows I opened" in result.message


# --- 4. Ownership safety must not be weakened ---------------------------------------------------------

def test_a_pre_existing_notepad_is_never_owned(world):
    """A Notepad the user already had open must stay untouchable. Since Slice 2 it is put there
    explicitly, because its presence is what makes "open notepad" reuse rather than launch."""
    users_notepad(world)
    assert owned() == []
    result = close_app("notepad")
    assert not result.ok, "the user's own window is not ours to close"
    assert ("close", USERS_NOTEPAD.handle) not in world.calls


def test_with_one_owned_and_one_pre_existing_only_the_owned_one_is_closed(world):
    open_app("notepad")
    stranger = world.desktop.add("Untitled - Notepad", "Notepad")   # after the open; see users_notepad
    ours = [handle for group in owned() for handle in group]
    result = close_app("notepad")
    assert result.ok, result.message
    closed = [handle for kind, handle in world.calls if kind == "close"]
    assert stranger not in closed, "a window we did not open was asked to close"
    assert set(closed) <= set(ours)


def test_a_failed_open_records_no_ownership(world):
    world.desktop.reaction = "close"
    result = executor_logic.execute(ExecutorAction(OPEN_APP, "photoshop"))
    assert not result.ok
    assert owned("photoshop") == [] and owned() == []


def test_an_open_whose_window_never_appears_records_no_ownership(world, monkeypatch):
    """Unverified means unowned: the Verifier must have SEEN a window."""
    from app.verifier import logic as verifier
    monkeypatch.setattr(verifier, "wait_for_new_window",
                        lambda expectation, before: verifier.VerificationResult(
                            False, "notepad was started, but no new window appeared.", retryable=True))
    result = executor_logic.execute(ExecutorAction(OPEN_APP, "notepad"))
    assert not result.ok
    assert owned() == [], "nothing was verified, so nothing is owned"


def test_previous_action_context_cannot_be_used_as_ownership_proof(world, brain):
    """The context says "the last thing I did was open notepad". That is conversation, not a title deed:
    with the ownership store emptied, the close still refuses."""
    from app.planner import logic as session
    brain(understood(Intent(CLOSE_APP, CloseAppArgs("notepad"))))
    context = session.remember_action(TurnContext(), OPEN_APP, "notepad")
    assert context.previous_action_context.safe_target == "notepad"
    users_notepad(world)
    assert owned() == [], "nothing is actually owned"
    screen = Screen()
    reply, _context = console.handle_typed_line(
        "could you close it please", context,
        console.Prompts(read=Script("yes", ""), write=screen, confirm=lambda a, b: True))
    # The refusal REACHES THE USER, which is the invariant. Since the Slice 3 smoke's ordering fix it
    # is printed immediately above the correction prompt rather than carried out as the closing line,
    # so it is checked on the screen and on the result rather than on the reply's final sentence.
    printed = [line for line in screen.lines
               if "I only close windows I opened in this session" in line]
    assert len(printed) == 1, screen.lines
    assert reply.result is not None
    assert "I only close windows I opened in this session" in reply.result.message


def test_closing_still_asks_the_medium_safety_question(world, brain):
    """Ownership is not permission: a close is still MEDIUM and still confirmed."""
    asked = []
    brain(understood(Intent(OPEN_APP, OpenAppArgs("notepad"))),
          understood(Intent(CLOSE_APP, CloseAppArgs("notepad"))))
    context = TurnContext()
    reply, context = console.handle_typed_line("could you open notepad please", context,
                                               console.Prompts(read=Script("yes"), write=Screen()))
    assert len(owned()) == 1
    reply, context = console.handle_typed_line(
        "could you close it please", context,
        console.Prompts(read=Script("yes"), write=Screen(),
                        confirm=lambda action, assessment: asked.append(assessment.level) or False))
    assert asked, "the safety gate was asked"
    assert not reply.result or not reply.result.ok, "a denied confirmation closes nothing"
    assert len(owned()) == 1, "and the window is still ours, still open"


def test_an_emergency_stop_does_not_register_ownership(world, monkeypatch):
    from app.executor import emergency_stop
    emergency_stop.trigger("ownership-test")
    try:
        with pytest.raises(emergency_stop.EmergencyStopError):
            executor_logic.execute(ExecutorAction(OPEN_APP, "notepad"))
        assert owned() == []
    finally:
        emergency_stop.reset("ownership-test")
