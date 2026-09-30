"""
The Phase 3 routing decision, and the Brain's contracts (Slice 1A).

No model, no network, no prompt, no schema: route() is pure, and the Interpretation types are data.

The routing table below is the point of the whole slice. The deterministic parser is verb-first, so a
loosely worded line frequently parses into a real action with an unusable target - "open the calculator"
becomes ExecutorAction(open_app, 'the calculator'). Routing on "did it parse" would hide most loose
phrasings from the Brain, so the question is whether the target RESOLVES.
"""
import ast
from pathlib import Path

import pytest

from app.brain import logic as brain
from app.brain.models import (ARGS_FOR_KIND, INTERPRETATIONS, ClickArgs, CloseAppArgs, Intent,
                              NeedsClarification, NotACommand, NotSupported, OpenAppArgs,
                              PreviousActionContext, RefreshArgs, ScrollArgs, ShortcutArgs,
                              TypeTextArgs, Understood, WindowControlArgs, previous_action_context)
from app.executor import commands
from app.executor.logic import resolve
from app.executor.models import (CLICK, CLOSE_APP, OPEN_APP, REFRESH, SCROLL, SHORTCUT, TYPE_TEXT,
                                 WINDOW_CONTROL)
from app.safety.models import RiskLevel


def route(text):
    return brain.route(text, resolve)


# --- The locked routing table --------------------------------------------------------------------------

LOCAL = "local"
BRAIN_ELIGIBLE = "brain"
LOCAL_REFUSAL = "local_refusal"

TABLE = [
    ("open notepad", LOCAL, None),
    ("open the calculator", BRAIN_ELIGIBLE, brain.UNRESOLVED_TARGET),
    ("open notepad please", BRAIN_ELIGIBLE, brain.UNRESOLVED_TARGET),
    ("open notepad and type hello", BRAIN_ELIGIBLE, brain.UNRESOLVED_TARGET),
    ("close everything", BRAIN_ELIGIBLE, brain.UNRESOLVED_TARGET),
    ("close the editor", BRAIN_ELIGIBLE, brain.UNRESOLVED_TARGET),
    ("open it", BRAIN_ELIGIBLE, brain.UNRESOLVED_TARGET),
    ("minimize the window", BRAIN_ELIGIBLE, brain.MALFORMED_COMMAND),
    ("notepad kholo", BRAIN_ELIGIBLE, brain.UNKNOWN_COMMAND),
    ("", LOCAL_REFUSAL, None),
    ("   ", LOCAL_REFUSAL, None),
    ("type hello world", LOCAL, None),
    ("close", BRAIN_ELIGIBLE, brain.AMBIGUOUS_COMMAND),
    ("minimize", LOCAL, None),
    ("scroll down 3", LOCAL, None),
    ("refresh", LOCAL, None),
    ("shortcut ctrl+a", LOCAL, None),
    ("click 500, 300", LOCAL, None),
    ("usko message kar do", BRAIN_ELIGIBLE, brain.UNKNOWN_COMMAND),
    ("aaj mausam kaisa hai?", BRAIN_ELIGIBLE, brain.UNKNOWN_COMMAND),
    ("likho mera naam Wajid hai", BRAIN_ELIGIBLE, brain.UNKNOWN_COMMAND),
]


@pytest.mark.parametrize("text, expected, reason", TABLE, ids=[case[0][:26] or "empty" for case in TABLE])
def test_the_routing_table_is_locked(text, expected, reason):
    outcome = route(text)
    if expected == LOCAL:
        assert isinstance(outcome, brain.LocalAction), f"{text!r} must stay on the free local path"
    elif expected == LOCAL_REFUSAL:
        assert isinstance(outcome, brain.LocalRefusal)
    else:
        assert isinstance(outcome, brain.BrainEligible), f"{text!r} must be able to reach the Brain"
        assert outcome.reason == reason


def test_an_exact_command_keeps_its_parsed_action():
    outcome = route("open notepad")
    assert outcome.action.kind == OPEN_APP and outcome.action.target == "notepad"


def test_an_exact_type_command_never_needs_the_brain():
    """So an ordinary typed payload is not sent to a cloud model: it parses and resolves locally."""
    outcome = route("type my password is hunter2")
    assert isinstance(outcome, brain.LocalAction)
    assert outcome.action.kind == TYPE_TEXT


def test_a_brain_eligible_line_carries_the_local_answer_for_the_offline_case():
    """When the Brain can't be reached, nothing runs and the caller still has something true to say."""
    outcome = route("open the calculator")
    assert outcome.text == "open the calculator"
    assert outcome.local_message == ("I don't know an app called 'the calculator'. "
                                     "Apps I can open: calculator, notepad.")
    assert outcome.parsed is not None and outcome.parsed.kind == OPEN_APP


def test_a_brain_eligible_refusal_carries_the_parsers_own_message():
    outcome = route("minimize the window")
    assert outcome.local_message == ("minimize acts on the active window and takes nothing after it. "
                                     "Say minimize. Nothing was done.")
    assert outcome.parsed is None, "there was no action to carry"


def test_an_empty_line_never_reaches_the_brain():
    outcome = route("")
    assert isinstance(outcome, brain.LocalRefusal) and outcome.refusal.kind == commands.EMPTY


def test_route_calls_resolve_only_for_a_parsed_action():
    """A refusal has no action, so there is nothing to resolve - and resolve() must not be invented."""
    asked = []

    def spy(action):
        asked.append(action)
        return resolve(action)

    brain.route("notepad kholo", spy)
    assert asked == []
    brain.route("open notepad", spy)
    assert [action.kind for action in asked] == [OPEN_APP]


def test_route_runs_nothing(monkeypatch):
    from app.executor import logic as executor_logic
    from app.verifier import logic as verifier

    def refuse(*args, **kwargs):
        raise AssertionError("routing must not act")

    monkeypatch.setattr(executor_logic, "execute", refuse)
    monkeypatch.setattr(executor_logic, "execute_with_recovery", refuse)
    monkeypatch.setattr(verifier, "active_target", refuse)
    for text, _, _ in TABLE:
        route(text)


def test_the_brain_can_reason_without_being_able_to_act():
    """app/brain/logic.py must not import the Executor's logic module, which would drag its OS adapter
    into the Brain's import graph. resolve() is injected for exactly this reason."""
    source = Path("app/brain/logic.py").read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert "app.executor.logic" not in imported, imported
    assert "app.executor.adapter" not in imported
    assert "anthropic" not in imported, "the provider boundary is app/brain/adapter.py"
    # json and its own models module arrived with Slice 2's prompt/schema/validation work. Both are
    # pure: no socket, no device, no settings read.
    assert imported <= {"dataclasses", "json", "app.brain.models", "app.executor",
                        "app.executor.models"}, imported
    assert "config.settings" not in imported, "the Brain's semantics do not read config; callers pass it"


# --- The Interpretation union is closed and typed ------------------------------------------------------

def test_the_interpretation_set_is_exactly_four():
    assert INTERPRETATIONS == (Understood, NeedsClarification, NotSupported, NotACommand)


def test_every_implemented_executor_kind_has_exactly_one_args_shape():
    """The vocabulary cannot widen: ARGS_FOR_KIND is keyed by the Executor's own constants, so a kind
    the Executor does not implement cannot be represented at all."""
    from app.executor import logic as executor_logic
    assert set(ARGS_FOR_KIND) == set(executor_logic._PREPARERS)
    assert len(set(ARGS_FOR_KIND.values())) == len(ARGS_FOR_KIND), "one shape per kind"


def test_an_unimplemented_capability_cannot_be_named():
    assert "shell" not in ARGS_FOR_KIND and "run" not in ARGS_FOR_KIND
    assert "message" not in ARGS_FOR_KIND and "search" not in ARGS_FOR_KIND
    assert ARGS_FOR_KIND.get("teleport") is None


def test_the_scroll_and_window_vocabularies_match_the_executors():
    from app.brain.models import SCROLL_DIRECTIONS, WINDOW_OPERATIONS
    from app.executor import logic as executor_logic
    assert set(WINDOW_OPERATIONS) == set(executor_logic._WINDOW_OPERATIONS)
    assert set(SCROLL_DIRECTIONS) == {"up", "down"}


def test_an_intent_defaults_to_the_lowest_risk_and_carries_the_existing_type():
    intent = Intent(OPEN_APP, OpenAppArgs("notepad"))
    assert intent.risk_floor is RiskLevel.LOW
    raised = Intent(CLOSE_APP, CloseAppArgs("notepad"), risk_floor=RiskLevel.HIGH)
    assert raised.risk_floor is RiskLevel.HIGH and isinstance(raised.risk_floor, RiskLevel)


@pytest.mark.parametrize("args", [OpenAppArgs("notepad"), CloseAppArgs("notepad"), ClickArgs(1, 2),
                                  TypeTextArgs("hi"), ShortcutArgs("ctrl+a"), ScrollArgs("down", 3),
                                  RefreshArgs(), WindowControlArgs("minimize")])
def test_args_shapes_are_immutable(args):
    """A plan must mean the same thing when it is shown and when it runs, so nothing can edit an
    argument after the user has read it."""
    import dataclasses
    for name in [field.name for field in dataclasses.fields(args)] or ["anything"]:
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(args, name, "changed")


# --- The type_text payload must not leak --------------------------------------------------------------

SECRET = "my password is hunter2"


def test_a_typed_payload_never_appears_in_its_own_repr():
    assert SECRET not in repr(TypeTextArgs(SECRET))
    assert "characters" in repr(TypeTextArgs(SECRET))


def test_a_typed_payload_can_never_reach_the_previous_action_context():
    context = previous_action_context(TYPE_TEXT, SECRET)
    assert context.safe_target is None
    assert SECRET not in repr(context) and "hunter2" not in repr(context)
    assert SECRET not in str(vars(context))


@pytest.mark.parametrize("kind, target, kept", [
    (OPEN_APP, "notepad", "notepad"),
    (CLOSE_APP, "calculator", "calculator"),
    (WINDOW_CONTROL, "minimize", "minimize"),
    (TYPE_TEXT, SECRET, None),
    (CLICK, "500, 300", None),
    (SHORTCUT, "ctrl+a", None),
    (SCROLL, "down 3", None),
    (REFRESH, "", None),
])
def test_only_short_fixed_vocabulary_targets_are_kept_as_context(kind, target, kept):
    """Kept only where it is both safe and useful for resolving a follow-up like "close it"."""
    assert previous_action_context(kind, target) == PreviousActionContext(kind=kind, safe_target=kept)


def test_the_context_builder_is_the_only_way_a_target_is_kept():
    """A caller cannot hand a raw ExecutorAction in: the context holds two plain strings and nothing
    else, so there is no field an action could hide in."""
    from dataclasses import fields
    assert [field.name for field in fields(PreviousActionContext)] == ["kind", "safe_target"]
    context = previous_action_context(OPEN_APP, "notepad")
    assert all(value is None or isinstance(value, str) for value in vars(context).values())
    assert not hasattr(context, "action") and not hasattr(context, "target")
