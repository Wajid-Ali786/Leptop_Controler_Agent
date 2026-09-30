"""
Tests for app/planner/. Tests are added alongside each feature (CLAUDE.md rule 11).

Phase 3 Slice 1A: plan CONSTRUCTION only. build_plan() is pure - it authorizes nothing, executes
nothing, verifies nothing, and reads no window. Everything below is about the four checks that stand
between an Intent the Brain produced and a PlanStep the safety gate would see:

  the kind is one the Executor implements; the args are the shape for that kind; WE format the target;
  and the target resolves by the Executor's own rule.

The formatting check is the reason this file is long. The Planner writes the exact target string the
Executor will parse, so a bug there would be a silent mis-execution rather than a refusal - the Brain
saying "click 500, 300" and the Executor clicking somewhere else.
"""
import ast
from dataclasses import FrozenInstanceError, fields, is_dataclass
from pathlib import Path

import pytest

from app.brain.models import (ClickArgs, CloseAppArgs, Intent, OpenAppArgs, RefreshArgs, ScrollArgs,
                              ShortcutArgs, TypeTextArgs, Understood, WindowControlArgs)
from app.executor.logic import resolve
from app.executor.models import (CLICK, CLOSE_APP, OPEN_APP, REFRESH, SCROLL, SHORTCUT, TYPE_TEXT,
                                 WINDOW_CONTROL, ExecutorAction)
from app.planner.logic import build_plan
from app.planner.models import (MAX_PLAN_STEPS, PLAN_NOT_HERE, PLAN_NO_STEPS, PLAN_TOO_MANY_STEPS,
                                PLAN_UNKNOWN_KIND, PLAN_UNRESOLVED, PLAN_WRONG_ARGS, TYPED_CONSOLE,
                                VOICE_CONSOLE, FailureContext, FrontEnd, PendingClarification,
                                PendingPlan, Plan, PlanRefusal, PlanStep, TurnContext)
from app.safety.models import RiskLevel


def plan(*intents, frontend=TYPED_CONSOLE, restated=""):
    return build_plan(Understood(intents=tuple(intents), restated=restated), frontend, resolve)


def one(kind, args, **kwargs):
    """A single-intent plan, for the many cases about one step."""
    return plan(Intent(kind, args, **kwargs))


# --- D. Typed args become the exact target string the Executor parses ----------------------------------

@pytest.mark.parametrize("kind, args, target", [
    (OPEN_APP, OpenAppArgs("notepad"), "notepad"),
    (OPEN_APP, OpenAppArgs("  Notepad  "), "Notepad"),
    (CLOSE_APP, CloseAppArgs("calculator"), "calculator"),
    (CLICK, ClickArgs(500, 300), "500, 300"),
    (CLICK, ClickArgs(0, 0), "0, 0"),
    (CLICK, ClickArgs(-4, -9), "-4, -9"),
    (TYPE_TEXT, TypeTextArgs("hello world"), "hello world"),
    (SHORTCUT, ShortcutArgs("ctrl+a"), "ctrl+a"),
    (SCROLL, ScrollArgs("down", 3), "down 3"),
    (SCROLL, ScrollArgs("up", 1), "up 1"),
    (REFRESH, RefreshArgs(), ""),
    (WINDOW_CONTROL, WindowControlArgs("minimize"), "minimize"),
])
def test_each_args_shape_formats_one_exact_target(kind, args, target):
    """The formats are the Executor's, not a second opinion: each target below is one resolve() accepts,
    and the round-trip test after this one proves the Executor reads back what the Brain meant."""
    built = one(kind, args)
    assert isinstance(built, Plan), getattr(built, "message", built)
    assert built.steps[0].action == ExecutorAction(kind=kind, target=target)


@pytest.mark.parametrize("args, value", [(ClickArgs(500, 300), (500, 300)),
                                         (ClickArgs(-4, -9), (-4, -9)),
                                         (ScrollArgs("down", 3), ("down", 3)),
                                         (ScrollArgs("up", 20), ("up", 20))])
def test_a_formatted_target_reads_back_as_the_same_numbers(args, value):
    """The risk this closes: a target that resolves but means something else. resolve() returns the
    canonical value, so the Brain's numbers can be compared with the Executor's."""
    kind = CLICK if isinstance(args, ClickArgs) else SCROLL
    step = one(kind, args).steps[0]
    assert resolve(step.action).value == value


def test_typed_text_is_carried_verbatim():
    """Trimming or re-spacing it would change what the user gets typed."""
    payload = "  Dear Sir,\n\n  regards  "
    step = one(TYPE_TEXT, TypeTextArgs(payload)).steps[0]
    assert step.action.target == payload


def test_the_planner_writes_the_target_so_the_brain_cannot_smuggle_one_in():
    """A model that puts a whole command in an app name gets a refusal, not a second parse: the string
    is only ever used as ONE field of ONE action, and it has to resolve as that."""
    built = one(OPEN_APP, OpenAppArgs("notepad and type hello"))
    assert isinstance(built, PlanRefusal) and built.reason == PLAN_UNRESOLVED


@pytest.mark.parametrize("args", [ClickArgs("500", 300), ClickArgs(500, None), ClickArgs(True, 3),
                                  ClickArgs(1.5, 2)])
def test_click_coordinates_that_are_not_whole_numbers_are_refused(args):
    """Never coerced: f-string formatting would happily turn 1.5 or True into a target string."""
    built = one(CLICK, args)
    assert isinstance(built, PlanRefusal) and built.reason == PLAN_WRONG_ARGS


@pytest.mark.parametrize("args", [ScrollArgs("left", 3), ScrollArgs("sideways", 1), ScrollArgs("", 1),
                                  ScrollArgs("down", "3"), ScrollArgs("down", True)])
def test_scroll_arguments_outside_the_executors_vocabulary_are_refused(args):
    built = one(SCROLL, args)
    assert isinstance(built, PlanRefusal) and built.reason == PLAN_WRONG_ARGS


@pytest.mark.parametrize("operation", ["sideways", "minimise", "", "MINIMIZE"])
def test_an_unknown_window_operation_is_refused(operation):
    built = one(WINDOW_CONTROL, WindowControlArgs(operation))
    assert isinstance(built, PlanRefusal) and built.reason == PLAN_WRONG_ARGS


# --- E. Steps are numbered 1..n, in the order asked for ------------------------------------------------

def test_steps_are_numbered_from_one_in_order():
    built = plan(Intent(OPEN_APP, OpenAppArgs("notepad")),
                 Intent(TYPE_TEXT, TypeTextArgs("hello")),
                 Intent(WINDOW_CONTROL, WindowControlArgs("minimize")))
    assert [step.number for step in built.steps] == [1, 2, 3]
    assert [step.action.kind for step in built.steps] == [OPEN_APP, TYPE_TEXT, WINDOW_CONTROL]
    assert len(built) == 3


def test_the_order_is_the_intents_order_not_a_sorted_one():
    built = plan(Intent(WINDOW_CONTROL, WindowControlArgs("minimize")),
                 Intent(OPEN_APP, OpenAppArgs("notepad")))
    assert [step.action.kind for step in built.steps] == [WINDOW_CONTROL, OPEN_APP]


def test_repeating_the_same_intent_gives_distinct_numbered_steps():
    built = plan(*[Intent(SCROLL, ScrollArgs("down", 3))] * 3)
    assert [step.number for step in built.steps] == [1, 2, 3]


# --- F. Zero steps, and more than five, are refused ----------------------------------------------------

def test_an_understood_interpretation_with_nothing_in_it_is_refused():
    built = plan()
    assert isinstance(built, PlanRefusal) and built.reason == PLAN_NO_STEPS
    assert built.message


def test_the_step_limit_is_five():
    assert MAX_PLAN_STEPS == 5


def test_exactly_five_steps_is_allowed():
    built = plan(*[Intent(REFRESH, RefreshArgs())] * MAX_PLAN_STEPS)
    assert isinstance(built, Plan) and len(built) == MAX_PLAN_STEPS


def test_six_steps_is_refused_and_says_how_many_it_would_have_been():
    built = plan(*[Intent(REFRESH, RefreshArgs())] * (MAX_PLAN_STEPS + 1))
    assert isinstance(built, PlanRefusal) and built.reason == PLAN_TOO_MANY_STEPS
    assert str(MAX_PLAN_STEPS + 1) in built.message and str(MAX_PLAN_STEPS) in built.message


def test_a_long_plan_is_refused_before_any_step_is_built():
    """The bound is a safety bound, so it is checked first - a 20-step request is not validated step by
    step until something else happens to fail."""
    asked = []

    def spy(action):
        asked.append(action)
        return resolve(action)

    built = build_plan(Understood(intents=tuple([Intent(REFRESH, RefreshArgs())] * 20)),
                       TYPED_CONSOLE, spy)
    assert isinstance(built, PlanRefusal) and built.reason == PLAN_TOO_MANY_STEPS
    assert asked == []


# --- G. A target that does not resolve is refused BY THE PLANNER ---------------------------------------

@pytest.mark.parametrize("kind, args", [
    (OPEN_APP, OpenAppArgs("photoshop")),
    (OPEN_APP, OpenAppArgs("the calculator")),
    (OPEN_APP, OpenAppArgs("")),
    (CLOSE_APP, CloseAppArgs("everything")),
    (SHORTCUT, ShortcutArgs("copy")),
    (SHORTCUT, ShortcutArgs("")),
    (SCROLL, ScrollArgs("down", 0)),
    (SCROLL, ScrollArgs("down", 21)),
    (TYPE_TEXT, TypeTextArgs("")),
    (TYPE_TEXT, TypeTextArgs("x" * 1001)),
    (TYPE_TEXT, TypeTextArgs("a\tb")),
])
def test_an_unresolvable_target_never_becomes_a_step(kind, args):
    built = one(kind, args)
    assert isinstance(built, PlanRefusal) and built.reason == PLAN_UNRESOLVED


def test_the_refusal_quotes_the_executors_own_words():
    """One source of truth for what the user is told: the Planner does not write a second wording for a
    condition the Executor already explains."""
    built = one(OPEN_APP, OpenAppArgs("photoshop"))
    assert built.message == resolve(ExecutorAction(OPEN_APP, "photoshop")).message


def test_a_later_unresolvable_step_refuses_the_whole_plan():
    """All-or-nothing: a plan the user accepts must be one that can be carried out, so half a plan is
    never offered."""
    built = plan(Intent(OPEN_APP, OpenAppArgs("notepad")),
                 Intent(OPEN_APP, OpenAppArgs("photoshop")),
                 Intent(REFRESH, RefreshArgs()))
    assert isinstance(built, PlanRefusal) and built.reason == PLAN_UNRESOLVED


def test_the_planner_asks_resolve_about_the_action_it_built():
    """Not about the Brain's args, and not about a string it wrote down separately."""
    seen = []

    def spy(action):
        seen.append(action)
        return resolve(action)

    build_plan(Understood(intents=(Intent(CLICK, ClickArgs(500, 300)),)), TYPED_CONSOLE, spy)
    assert seen == [ExecutorAction(CLICK, "500, 300")]


# --- H. A kind the Executor does not implement cannot become a step ------------------------------------

@pytest.mark.parametrize("kind", ["shell", "run", "send_message", "web_search", "teleport", "",
                                  "open_app ", "OPEN_APP"])
def test_an_unimplemented_kind_is_refused(kind):
    built = one(kind, OpenAppArgs("notepad"))
    assert isinstance(built, PlanRefusal) and built.reason == PLAN_UNKNOWN_KIND


@pytest.mark.parametrize("kind, args", [(OPEN_APP, ClickArgs(1, 2)),
                                        (CLICK, OpenAppArgs("notepad")),
                                        (TYPE_TEXT, ShortcutArgs("ctrl+a")),
                                        (SHORTCUT, TypeTextArgs("ctrl+a")),
                                        (SCROLL, RefreshArgs()),
                                        (WINDOW_CONTROL, CloseAppArgs("notepad")),
                                        (REFRESH, OpenAppArgs("notepad")),
                                        (OPEN_APP, "notepad"),
                                        (OPEN_APP, {"app": "notepad"}),
                                        (OPEN_APP, None)])
def test_args_that_do_not_belong_to_the_kind_are_refused(kind, args):
    """Including the shapes that look alike: ShortcutArgs and TypeTextArgs both hold one string, and
    typing 'ctrl+a' is not pressing it."""
    built = one(kind, args)
    assert isinstance(built, PlanRefusal) and built.reason == PLAN_WRONG_ARGS


def test_a_wrong_args_refusal_does_not_repeat_the_request_back():
    """It is shown to the user, and the request may be anything - including a typed payload."""
    built = one(TYPE_TEXT, ShortcutArgs("my password is hunter2"))
    assert "hunter2" not in built.message


# --- I. The front end's capability set is enforced -----------------------------------------------------

@pytest.mark.parametrize("kind, args", [(OPEN_APP, OpenAppArgs("notepad")),
                                        (CLOSE_APP, CloseAppArgs("notepad"))])
def test_voice_mode_may_plan_the_kinds_that_need_no_hand_over(kind, args):
    assert isinstance(plan(Intent(kind, args), frontend=VOICE_CONSOLE), Plan)
    assert isinstance(plan(Intent(kind, args), frontend=TYPED_CONSOLE), Plan)


@pytest.mark.parametrize("kind, args", [(CLICK, ClickArgs(500, 300)),
                                        (TYPE_TEXT, TypeTextArgs("hello")),
                                        (SHORTCUT, ShortcutArgs("ctrl+a")),
                                        (SCROLL, ScrollArgs("down", 3)),
                                        (REFRESH, RefreshArgs()),
                                        (WINDOW_CONTROL, WindowControlArgs("minimize"))])
def test_voice_mode_may_not_plan_a_kind_that_would_act_on_the_console_window(kind, args):
    """Measured in Phase 2: voice mode passes no focus, so all six of these would act on the window the
    user is talking to. The typed console builds a hand-over and may plan them."""
    built = plan(Intent(kind, args), frontend=VOICE_CONSOLE)
    assert isinstance(built, PlanRefusal) and built.reason == PLAN_NOT_HERE
    assert VOICE_CONSOLE.name in built.message
    assert isinstance(plan(Intent(kind, args), frontend=TYPED_CONSOLE), Plan)


def test_the_capability_guard_runs_before_the_target_is_resolved():
    """So voice mode is told it cannot do that here, rather than being told its target is wrong."""
    built = plan(Intent(CLICK, ClickArgs("not a number", 3)), frontend=VOICE_CONSOLE)
    assert built.reason == PLAN_NOT_HERE


def test_a_front_ends_capabilities_are_passed_in_not_inferred():
    """No global state: the same intent gives different answers only because the caller differs."""
    nothing = FrontEnd(name="test caller", may_plan=frozenset())
    assert plan(Intent(REFRESH, RefreshArgs()), frontend=nothing).reason == PLAN_NOT_HERE
    everything = FrontEnd(name="test caller", may_plan=frozenset({REFRESH}))
    assert isinstance(plan(Intent(REFRESH, RefreshArgs()), frontend=everything), Plan)


# --- J. The risk floor travels with the step, unchanged ------------------------------------------------

@pytest.mark.parametrize("floor", list(RiskLevel))
def test_the_brains_risk_floor_is_carried_onto_the_step(floor):
    """It becomes Action.minimum_level at the safety gate, which may raise it but never lower it - so
    the Planner must not quietly drop or normalise it."""
    step = one(OPEN_APP, OpenAppArgs("notepad"), risk_floor=floor).steps[0]
    assert step.risk_floor is floor


def test_the_default_floor_is_the_lowest_and_the_gate_still_decides():
    step = one(OPEN_APP, OpenAppArgs("notepad")).steps[0]
    assert step.risk_floor is RiskLevel.LOW


def test_each_step_keeps_its_own_floor_and_its_own_reason():
    built = plan(Intent(OPEN_APP, OpenAppArgs("notepad"), why="you asked for a note",
                        risk_floor=RiskLevel.LOW),
                 Intent(CLOSE_APP, CloseAppArgs("calculator"), why="it is no longer needed",
                        risk_floor=RiskLevel.HIGH))
    assert [step.risk_floor for step in built.steps] == [RiskLevel.LOW, RiskLevel.HIGH]
    assert [step.why for step in built.steps] == ["you asked for a note", "it is no longer needed"]


def test_a_plan_is_inspectable_without_running_it():
    """The frozen requirement. Everything a person needs in order to accept or reject is on the step."""
    step = one(OPEN_APP, OpenAppArgs("notepad"), why="so you can write").steps[0]
    assert (step.number, step.action.kind, step.action.target, step.why) == (1, OPEN_APP, "notepad",
                                                                            "so you can write")


# --- L. The shapes are data: frozen, identity-free, session-only ---------------------------------------

@pytest.mark.parametrize("shape", [PlanStep(1, ExecutorAction(REFRESH, "")), Plan(steps=()),
                                   PlanRefusal("r", "m"), PendingClarification("t", "q"),
                                   FailureContext(1, "m"), PendingPlan("id", "t", Plan(steps=())),
                                   TurnContext(), FrontEnd("test caller", frozenset())])
def test_every_planner_shape_is_frozen(shape):
    assert is_dataclass(shape)
    for field in fields(shape):
        with pytest.raises(FrozenInstanceError):
            setattr(shape, field.name, "changed")


def test_a_plan_has_no_identity_of_its_own():
    """Identity belongs to a RUN of a plan, so accepting twice cannot be mistaken for one acceptance."""
    assert [field.name for field in fields(Plan)] == ["steps"]
    assert not hasattr(Plan(steps=()), "plan_id")
    assert [field.name for field in fields(PendingPlan)][0] == "plan_id"


def test_two_plans_with_the_same_steps_are_equal():
    """Data, not a run: equality is about what would happen."""
    assert one(REFRESH, RefreshArgs()) == one(REFRESH, RefreshArgs())


def test_the_session_shapes_have_no_method_that_changes_anything():
    """The shapes are data; the transitions live in app/planner/logic.py. Slice 1B added reprs and
    read-only budget properties, which answer questions - so the invariant is checked as written rather
    than by name: a method here must be a property or a repr/len, and must change nothing that outlives
    the call."""
    tree = ast.parse(Path("app/planner/models.py").read_text(encoding="utf-8"))
    allowed = {"__len__", "__repr__", "allows"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for body in node.body:
            if not isinstance(body, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = {name.id for name in body.decorator_list if isinstance(name, ast.Name)}
            where = f"{node.name}.{body.name}"
            assert body.name in allowed or "property" in decorators, where
            for inner in ast.walk(body):
                # A local name changes nothing outside the call; assigning to an attribute or an item
                # does, and that is what a data shape must never do.
                if isinstance(inner, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                    for target in getattr(inner, "targets", None) or [inner.target]:
                        assert isinstance(target, ast.Name), f"{where}: {ast.unparse(target)}"
                if isinstance(inner, ast.Call):
                    shown = ast.unparse(inner.func)
                    assert "setattr" not in shown and "__dict__" not in shown, f"{where}: {shown}"


def test_a_completed_step_number_can_only_be_a_step_that_exists():
    """The shape says step NUMBERS, not outcomes - one source of truth for what happened, which is the
    Executor's own results."""
    built = plan(Intent(REFRESH, RefreshArgs()), Intent(REFRESH, RefreshArgs()))
    pending = PendingPlan(plan_id="p1", original_text="refresh twice", plan=built,
                          completed_steps=(1,))
    numbers = [step.number for step in pending.plan.steps]
    assert all(number in numbers for number in pending.completed_steps)
    assert not any(field.name == "results" for field in fields(PendingPlan))


# --- M. It does not authorize, execute, verify or read anything ----------------------------------------

def test_building_a_plan_touches_no_safety_gate_no_executor_and_no_window(monkeypatch):
    from app.executor import logic as executor_logic
    from app.safety import logic as safety
    from app.verifier import logic as verifier

    def refuse(*args, **kwargs):
        raise AssertionError("planning must not act")

    monkeypatch.setattr(safety, "authorize", refuse)
    monkeypatch.setattr(safety, "assess", refuse)
    monkeypatch.setattr(executor_logic, "execute", refuse)
    monkeypatch.setattr(executor_logic, "execute_with_recovery", refuse)
    for name in ("active_target", "screens", "window_at", "snapshot_windows", "find_open"):
        monkeypatch.setattr(verifier, name, refuse, raising=False)
    for kind, args in [(OPEN_APP, OpenAppArgs("notepad")), (CLOSE_APP, CloseAppArgs("photoshop")),
                       (CLICK, ClickArgs(1, 2)), (TYPE_TEXT, TypeTextArgs("hi")),
                       (SHORTCUT, ShortcutArgs("ctrl+a")), (SCROLL, ScrollArgs("down", 3)),
                       (REFRESH, RefreshArgs()), (WINDOW_CONTROL, WindowControlArgs("minimize")),
                       ("teleport", OpenAppArgs("mars"))]:
        one(kind, args)
        plan(Intent(kind, args), frontend=VOICE_CONSOLE)


def test_the_planner_can_plan_without_being_able_to_act():
    """Same reason as the Brain: importing app.executor.logic would pull the OS adapter in. resolve()
    is injected."""
    imported = set()
    for node in ast.walk(ast.parse(Path("app/planner/logic.py").read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert "app.executor.logic" not in imported, imported
    assert "app.executor.adapter" not in imported
    assert "app.safety.logic" not in imported, "the Planner does not authorize; main.py does"
    assert "anthropic" not in imported
    # uuid is stdlib and is here only to mint an opaque plan id; it reaches no device and no network.
    assert imported <= {"app.brain.models", "app.executor.models", "app.planner.models",
                        "uuid"}, imported


def test_a_plan_is_never_written_to_disk(tmp_path, monkeypatch):
    """Session-only. Phase 4 owns memory; a plan that outlived the session would be a plan nobody saw
    being accepted."""
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.rglob("*"))
    plan(Intent(OPEN_APP, OpenAppArgs("notepad")), Intent(TYPE_TEXT, TypeTextArgs("secret")))
    assert set(tmp_path.rglob("*")) == before
