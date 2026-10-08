"""
Tests for Phase 5 Slice 3 - the typed command "click Seven in calculator" reaching the proven Slice 2
UIA click, and nothing about the screen reaching Claude on the way.

The one property the whole file exists to hold:

    the Brain says WHAT to click. This computer decides WHERE it is. Nothing read off the screen
    goes back to the provider, and no observation result costs a second model call.

Everything here is offline. The provider is a scripted function, the accessibility tree is a list of
fake elements, the desktop is fake, and the physical click is recorded - but the REAL Brain validator,
the REAL planner, the REAL safety gate and the REAL Executor pipeline decide every outcome.

Checks about code are made against its AST with docstrings and comments stripped. This project has
written a substring assertion that matched its own explanation of a rule five times now, so the habit
is not optional here.
"""
import ast
import inspect
import json
import textwrap
from types import SimpleNamespace

import pytest

from app import console
from app.brain import logic as brain
from app.brain.models import (ARGS_FIELDS, ARGS_FOR_KIND, MAX_CONTROL, NEUTRAL_ARGS_VALUES,
                              ClickArgs, ClickTargetArgs, Intent, OpenAppArgs, PreviousActionContext,
                              interpretation_schema, previous_action_context)
from app.executor import adapter as executor_adapter
from app.executor import emergency_stop
from app.executor import logic as executor
from app.executor.emergency_stop import EmergencyStopError
from app.executor.models import (CLICK, CLICK_TARGET, OPEN_APP, RESOLVE_NO_TARGET,
                                 RESOLVE_UNKNOWN_APP, ExecutorAction, Outcome, Resolved, Unresolved)
from app.planner import logic as planner
from app.planner.models import (ALL_KINDS, NO_HANDOVER_KINDS, TYPED_CONSOLE, VOICE_CONSOLE, Plan,
                                PlanRefusal, PlanStepSummary, TurnContext)
from app.safety import logic as safety_logic
from app.safety.models import RiskLevel
from app.verifier import adapter as verifier_adapter
from app.verifier.models import ActiveTarget, Screen, UiaElement, WindowInfo, WindowState
from config import settings
from tests import fake_window_props
from tests.test_console_brain import Brainless, Script, understood  # noqa: F401

CONTROL = "Seven"
APP = "calculator"
WINDOW = 7100
WINDOW_BOUNDS = (0, 0, 800, 600)
BUTTON_BOUNDS = (300, 400, 400, 440)
CENTRE = (350, 420)

CONFIG = (
    "safety:\n"
    '  risky_keywords: [delete, shutdown, "shut down", send]\n'
    "  safe_words: [sender]\n"
    "executor:\n"
    "  apps:\n"
    "    calculator: calc.exe\n"
    "    notepad: notepad.exe\n"
    "  max_attempts: 3\n"
    "  activation_settle_seconds: 2.0\n"
    "  max_type_characters: 400\n"
    "observation:\n"
    "  snapshot_timeout_seconds: 2.0\n"
    "  freshness_seconds: 2.0\n"
    "verifier:\n"
    "  window_timeout_seconds: 0.2\n"
    "  poll_interval_seconds: 0.01\n"
    "  app_windows:\n"
    '    calculator: "^Calculator$"\n'
    '    notepad: "Notepad$"\n'
)


def element(**overrides) -> UiaElement:
    fields = dict(window_handle=WINDOW, control_type="Button", runtime_id="7-1",
                  automation_id="num7Button", class_name="Button", bounds=BUTTON_BOUNDS,
                  enabled=True, focused=False, offscreen=False, is_password=False,
                  patterns=("Invoke",))
    fields.update(overrides)
    return UiaElement(**fields)


def always(answer):
    return lambda *args: answer


def code_of(obj) -> str:
    """Source with every docstring removed, so a rule about what code DOES is never satisfied or
    broken by prose describing it."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(obj)))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)) \
                and node.body and isinstance(node.body[0], ast.Expr) \
                and isinstance(node.body[0].value, ast.Constant) \
                and isinstance(node.body[0].value.value, str):
            node.body.pop(0)
            if not node.body and not isinstance(node, ast.Module):
                node.body.append(ast.Pass())
    return ast.unparse(tree)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """One owned Calculator window with one fake control in it, and a recorded physical click."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_PATH", config_path)

    desktop = SimpleNamespace(
        windows=[WindowInfo(WINDOW, "Calculator", class_name="ApplicationFrameWindow")],
        matches=[element()],
        control_name="seven",         # what the fake accessibility tree calls it, already normalised
        window_bounds=WINDOW_BOUNDS,
        uia_error=None,
        on_top=WINDOW,
        front=None,                   # nothing of ours is in front until it is brought there
        minimized=False,
        pointer=None,
        clicks=[],
        prompts=[],
        activations=[],
        uia_reads=0,
    )

    def uia_find_by_name(window_handle, normalized_name, timeout_seconds=2.0):
        """Models the real adapter: it COMPARES the normalised name and returns only exact matches.

        An earlier version of this fake ignored the name and returned everything, which made the test
        that proves "7" is not a synonym for "Seven" pass for the wrong reason."""
        desktop.uia_reads += 1
        if desktop.uia_error:
            raise verifier_adapter.VerifierAdapterError(desktop.uia_error)
        if normalized_name != desktop.control_name:
            return []
        return [e for e in desktop.matches if e.window_handle == window_handle]

    def click(x, y):
        desktop.clicks.append((x, y))
        desktop.pointer = desktop.pointer or (x, y)

    def activate_window(handle):
        """The auto-focus boundary. A named click brings its own owned window forward now, so these
        tests - which are about the Brain and Planner wiring, not about activation - let it arrive.
        tests/test_window_activation.py is where refusal is exercised."""
        desktop.activations.append(handle)
        desktop.front = handle
        desktop.on_top = handle
        return True

    def window_state(handle):
        if not any(w.handle == handle for w in desktop.windows):
            return None
        return WindowState(minimized=desktop.minimized, maximized=False, has_minimize_box=True,
                           has_maximize_box=True, tool_window=False, hung=False)

    def active_target():
        front = next((w for w in desktop.windows if w.handle == desktop.front), None)
        return ActiveTarget(window=front)

    monkeypatch.setattr(verifier_adapter, "uia_find_by_name", uia_find_by_name)
    monkeypatch.setattr(verifier_adapter, "uia_window_bounds",
                        lambda handle: desktop.window_bounds)
    monkeypatch.setattr(verifier_adapter, "list_windows", lambda: list(desktop.windows))
    monkeypatch.setattr(verifier_adapter, "list_screens",
                        lambda: [Screen(0, 0, 1920, 1080, primary=True)])
    monkeypatch.setattr(verifier_adapter, "window_at",
                        lambda x, y: next((w for w in desktop.windows
                                           if w.handle == desktop.on_top), None))
    monkeypatch.setattr(verifier_adapter, "cursor_position", lambda: desktop.pointer or (0, 0))
    monkeypatch.setattr(executor_adapter, "click", click)
    monkeypatch.setattr(executor_adapter, "activate_window", activate_window)
    monkeypatch.setattr(verifier_adapter, "window_state", window_state)
    monkeypatch.setattr(verifier_adapter, "active_target", active_target)
    fake_window_props.install(monkeypatch, executor_adapter,
                              exists=lambda handle: any(w.handle == handle for w in desktop.windows))

    real_authorize = safety_logic.authorize

    def recording_authorize(action, confirm=None):
        decision = real_authorize(action, confirm)
        desktop.prompts.append((action.description, decision.assessment.level, decision.confirmed))
        return decision

    monkeypatch.setattr(executor, "authorize", recording_authorize)

    executor.forget_session_windows()
    emergency_stop.reset("named-click-test")
    yield desktop
    executor.forget_session_windows()
    emergency_stop.reset("named-click-test")


def own(app=APP, handles=(WINDOW,)):
    """Record a window as one this session opened and can prove it owns - the real production path:
    _remember_opened tags each handle and keeps only the ones whose token read back."""
    assert executor._remember_opened(app, frozenset(handles)), "the fake desktop refused the token"


def named(control=CONTROL, app=APP) -> ExecutorAction:
    return ExecutorAction(CLICK_TARGET, app, control)


# =====================================================================================================
# BRAIN / SCHEMA (matrix 1-5)
# =====================================================================================================

def reply(**intent):
    """A canonical provider reply for one understood intent, every field present."""
    fields = {name: value for name, value in NEUTRAL_ARGS_VALUES.items()}
    fields.update(intent)
    fields.setdefault("kind", CLICK_TARGET)
    fields.setdefault("why", "you asked to click it")
    fields.setdefault("risk_floor", "medium")
    return json.dumps({"kind": "understood", "restated": "click Seven in calculator",
                       "intents": [fields], "question": "", "missing": "", "because": "",
                       "what": "", "message": ""})


def test_a_named_click_reply_becomes_a_structured_target_not_coordinates():
    """1 + 3. The user's own word survives exactly; no position is anywhere in the result."""
    result = brain.validate_interpretation(reply(control=CONTROL, app=APP), max_type_characters=400)
    intent = result.intents[0]
    assert isinstance(intent.args, ClickTargetArgs)
    assert intent.args.control == CONTROL and intent.args.app == APP
    assert not hasattr(intent.args, "x") and not hasattr(intent.args, "y")


@pytest.mark.parametrize("control", ["Seven", "7", "Log In", "  Save  ", "Zufügen", "محفوظ کریں"])
def test_the_users_own_word_is_preserved_exactly(control):
    """3. Not trimmed, not translated, not tidied - including scripts the Brain is told to keep."""
    result = brain.validate_interpretation(reply(control=control), max_type_characters=400)
    assert result.intents[0].args.control == control


@pytest.mark.parametrize("coordinates", [{"x": 110}, {"y": 435}, {"x": 110, "y": 435}])
def test_the_provider_cannot_supply_coordinates_for_a_named_click(coordinates):
    """2. x and y belong to `click`. For click_target they are not used, so the existing canonical-form
    rule rejects the whole reply rather than ignoring them - no trimming, no 'helpful' repair."""
    result = brain.validate_interpretation(reply(control=CONTROL, **coordinates),
                                           max_type_characters=400)
    assert isinstance(result, brain.InterpretationError), result
    assert result.reason == brain.NOT_CANONICAL


def test_the_schema_has_no_position_field_for_a_named_click():
    """2. Structural: the fields this kind uses are named in one place, and neither is a coordinate."""
    assert ARGS_FIELDS[CLICK_TARGET] == ("control", "app")
    assert "x" not in ARGS_FIELDS[CLICK_TARGET] and "y" not in ARGS_FIELDS[CLICK_TARGET]
    assert ARGS_FOR_KIND[CLICK_TARGET] is ClickTargetArgs
    schema = interpretation_schema(400)
    intent = schema["properties"]["intents"]["items"]["properties"]
    assert "control" in intent and intent["control"]["type"] == "string"
    # every field still required, so the shape stays flat and unions stay at zero
    assert set(intent) == set(intent) & set(schema["properties"]["intents"]["items"]["required"])


def test_a_control_name_longer_than_the_bound_is_refused():
    result = brain.validate_interpretation(reply(control="x" * (MAX_CONTROL + 1)),
                                           max_type_characters=400)
    assert isinstance(result, brain.InterpretationError) and result.reason == brain.TOO_LONG


def test_no_observed_screen_content_can_enter_an_interpretation_request():
    """4. The Brain's request is built from the user's words and PreviousActionContext. Neither can
    carry a UIA label, an automation id, a runtime id or a rectangle - proved from the context builder,
    which keeps a fixed set of kinds and drops everything else."""
    for kind in (CLICK_TARGET, CLICK, "type_text"):
        context = previous_action_context(kind, "anything at all")
        assert context.safe_target is None, f"{kind} must not keep a target"
    assert set(PreviousActionContext.__dataclass_fields__) == {"kind", "safe_target"}
    source = code_of(previous_action_context)
    for forbidden in ("control", "bounds", "runtime_id", "automation_id", "observation"):
        assert forbidden not in source, forbidden


def test_the_observation_layer_is_never_reachable_from_the_brain():
    """4 + 5. app/brain imports no observation module, so no screen read can reach a prompt."""
    root = settings.PROJECT_ROOT / "app" / "brain"
    for path in root.glob("*.py"):
        imported = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                imported.add(f"{node.module}.{node.names[0].name}")
            elif isinstance(node, ast.Import):
                imported |= {alias.name for alias in node.names}
        assert not any("observation" in name or "verifier" in name for name in imported), \
            f"{path.name}: {imported}"


# =====================================================================================================
# PLANNER (matrix 6-9)
# =====================================================================================================

def plan_for(*intents, frontend=TYPED_CONSOLE):
    return planner.build_plan(understood(*intents), frontend, executor.resolve)


def named_intent(control=CONTROL, app=APP, floor=RiskLevel.MEDIUM):
    return Intent(CLICK_TARGET, ClickTargetArgs(control=control, app=app), why="you asked", risk_floor=floor)


def test_the_plan_step_carries_the_control_and_the_app(world):
    """7. Explicit app context is preserved, and so is the user's word."""
    plan = plan_for(named_intent())
    assert isinstance(plan, Plan), getattr(plan, "message", plan)
    action = plan.steps[0].action
    assert action.kind == CLICK_TARGET and action.target == APP and action.control == CONTROL


def test_the_plan_preview_names_the_control_and_never_a_coordinate(world):
    """6 + 10 of the brief."""
    plan = plan_for(named_intent())
    summary = PlanStepSummary(1, CLICK_TARGET, APP)
    line = console._describe(summary, plan.steps[0].action.control)
    assert line == 'Click "Seven" in calculator'
    for digit in ("(", ")", "350", "420", ","):
        assert digit not in line, f"a coordinate leaked into the plan preview: {line}"


def test_the_plan_summary_sent_to_the_model_carries_no_control_name(world):
    """4. A PlanStepSummary may travel to the provider inside a ReplanRequest. The user's word for a
    control is not needed in order to re-plan, so it is not there.

    Asserted on the SUMMARY that is actually handed over, not only on the accessor it is built from:
    an earlier version of this test checked ExecutorAction.log_label, and a mutant that put the
    control name into the summary while leaving log_label alone sailed straight past it."""
    plan = plan_for(named_intent())
    action = plan.steps[0].action
    assert action.log_label == APP
    assert CONTROL not in action.log_label
    assert CONTROL not in repr(action)
    assert CONTROL not in action.description

    context = planner.propose_plan(TurnContext(), plan, "click Seven in calculator",
                                   planner.new_plan_id())
    summaries = planner.plan_summary(context.pending_plan)
    assert summaries and summaries[0].kind == CLICK_TARGET
    for summary in summaries:
        rendered = f"{summary.kind} {summary.target} {summary.why}"
        assert CONTROL not in rendered, f"the control name reached a model-bound summary: {rendered}"
    # and nothing else on the summary shape could carry it either
    assert set(PlanStepSummary.__dataclass_fields__) == {"number", "kind", "target", "why",
                                                         "completed"}


def test_a_named_click_with_no_control_is_refused_by_the_planner(world):
    """8. Nothing is guessed, and the Executor's own wording is used."""
    outcome = plan_for(named_intent(control=""))
    assert isinstance(outcome, PlanRefusal)
    assert executor.resolve(named(control="")).reason == RESOLVE_NO_TARGET


def test_a_named_click_in_an_unconfigured_app_is_refused_by_the_planner(world):
    """8. An app we do not have is a refusal that names the apps we do."""
    outcome = plan_for(named_intent(app="photoshop"))
    assert isinstance(outcome, PlanRefusal)
    assert "photoshop" in outcome.message and APP in outcome.message


def test_a_named_click_is_planned_for_the_typed_console_only(world):
    """Voice would act on the console window the user is talking to - Phase 3's limitation, kept."""
    assert CLICK_TARGET in ALL_KINDS
    assert CLICK_TARGET not in NO_HANDOVER_KINDS
    assert TYPED_CONSOLE.allows(CLICK_TARGET) and not VOICE_CONSOLE.allows(CLICK_TARGET)
    assert isinstance(plan_for(named_intent(), frontend=VOICE_CONSOLE), PlanRefusal)


def test_a_named_click_brings_its_own_window_forward_instead_of_asking():
    """Changed by the auto-focus slice, and the reason is the point of it.

    Slice 3 put click_target in HANDS_OVER because a click only lands where it is aimed if the target
    window is in front. It is the one action that does not need the USER to arrange that, though: the
    ownership token proved which window it means before anything was read, so the Executor brings that
    window forward itself, after the confirmation. Every action that genuinely cannot know its window
    still asks."""
    assert console.needs_handover(CLICK_TARGET) is False
    assert CLICK_TARGET in console.NO_HANDOVER and CLICK_TARGET not in console.HANDS_OVER
    for still_asks in (CLICK, "type_text", "shortcut", "scroll", "refresh", "window_control"):
        assert console.needs_handover(still_asks) is True, still_asks


# =====================================================================================================
# APP / WINDOW CONTEXT (matrix 16-19)
# =====================================================================================================

def test_an_explicit_app_selects_that_apps_owned_window(world):
    """16."""
    own(APP)
    world.windows.append(WindowInfo(7200, "Untitled - Notepad"))
    own("notepad", handles=(7200,))
    result = executor.execute(named(app=APP), always(True))
    assert result.ok, result.message
    assert world.clicks == [CENTRE]


def test_one_owned_app_is_used_when_the_user_did_not_say_which(world):
    """17. "open calculator" then "click Seven" - resolved from this session's own ownership records,
    which only ever hold windows the Verifier confirmed and the token proved. No Phase 6 context."""
    own(APP)
    result = executor.execute(named(app=""), always(True))
    assert result.ok, result.message
    assert world.clicks == [CENTRE]


def test_nothing_opened_yet_refuses_rather_than_guessing(world):
    """18. No verified owned window exists, so there is nothing a named click may act in - even though
    a Calculator window is visible on the fake desktop."""
    result = executor.execute(named(app=""), always(True))
    assert not result.ok and world.clicks == []
    assert "haven't opened anything yet" in result.message
    assert world.prompts == [], "nothing should have been confirmed"


def test_a_visible_window_we_did_not_open_is_not_a_target(world):
    """18 + 2 of the brief: never act in an arbitrary window merely because it is visible.

    SLICE 3 REWRITE. Unchanged behaviour, changed wording. Ownership stopped being the click gate, so
    the refusal no longer says "I can't prove it's mine" - it says there is no window to click in at
    all, which is the truth here: this Calculator is neither owned NOR recorded by open_app. Merely
    being visible on the desktop has never made a window a target and still does not.

    Strengthened rather than loosened: it now also asserts that NOTHING was confirmed, so a future
    change that quietly made a visible window clickable behind a prompt would fail here too."""
    result = executor.execute(named(app=APP), always(True))
    assert not result.ok and world.clicks == []
    assert "I don't have a window of" in result.message, result.message
    assert "Open it first" in result.message
    assert world.prompts == [], "a visible stranger reached the confirmation"


def test_a_failed_open_cannot_establish_a_window_for_a_named_click(world):
    """18. _remember_opened keeps only handles whose ownership token was attached AND read back, so an
    open that could not be proved leaves nothing behind to click in."""
    assert executor._remember_opened(APP, frozenset()) is False
    result = executor.execute(named(app=APP), always(True))
    assert not result.ok and world.clicks == []


def test_an_untaggable_window_cannot_establish_context(world, monkeypatch):
    """18. The UIPI case: a window we cannot mark is a window we cannot prove, so it is not clickable."""
    monkeypatch.setattr(executor_adapter, "tag_window", lambda handle, token: False)
    assert executor._remember_opened(APP, frozenset({WINDOW})) is False
    result = executor.execute(named(app=APP), always(True))
    assert not result.ok and world.clicks == []


def test_two_owned_apps_and_no_named_app_refuses(world):
    """19. Ambiguous context is not a coin toss."""
    own(APP)
    world.windows.append(WindowInfo(7200, "Untitled - Notepad"))
    own("notepad", handles=(7200,))
    result = executor.execute(named(app=""), always(True))
    assert not result.ok and world.clicks == []
    assert "more than one app" in result.message and APP in result.message


def test_two_owned_windows_of_the_same_app_refuses(world):
    """19."""
    world.windows.append(WindowInfo(7300, "Calculator", class_name="ApplicationFrameWindow"))
    own(APP, handles=(WINDOW, 7300))
    result = executor.execute(named(app=APP), always(True))
    assert not result.ok and world.clicks == []
    assert "2 calculator windows" in result.message


def test_an_unrelated_previous_action_cannot_establish_context(world):
    """19. Short-term context is not ownership. A remembered "minimize" leaves no window to click in."""
    context = planner.remember_action(TurnContext(), "window_control", "minimize")
    assert context.previous_action_context.kind == "window_control"
    result = executor.execute(named(app=""), always(True))
    assert not result.ok and world.clicks == []


# =====================================================================================================
# LOCAL RESOLUTION (matrix 10-15)
# =====================================================================================================

def test_found_proceeds_locally(world):
    """10."""
    own(APP)
    result = executor.execute(named(), always(True))
    assert result.ok and world.clicks == [CENTRE]


@pytest.mark.parametrize("break_it, expect", [
    ({"matches": [element(automation_id="a"), element(automation_id="b")]}, "not going to guess"),  # 11
    ({"matches": []}, "couldn't find anything called"),                                             # 12
    ({"uia_error": "no accessibility tree"}, "couldn't read that window"),                          # 13
])
def test_a_local_resolution_that_is_not_found_stops_with_zero_clicks(world, break_it, expect):
    """11 + 12 + 13. Nothing is confirmed either: the user is not asked about something that cannot
    happen."""
    own(APP)
    for key, value in break_it.items():
        setattr(world, key, value)
    result = executor.execute(named(), always(True))
    assert not result.ok and world.clicks == []
    assert expect in result.message, result.message
    assert world.prompts == []


def test_seven_does_not_alias_to_a_digit(world):
    """14. Exact accessible-name matching is authoritative, and that is an accepted limitation of this
    slice: a control called "Seven" is not found by asking for "7"."""
    own(APP)
    result = executor.execute(named(control="7"), always(True))
    assert not result.ok and world.clicks == []
    assert "'7'" in result.message


def test_the_executor_never_consults_application_aliases_for_a_control(world):
    """15. Applications Memory maps an app name to a configured app. It is not a control-synonym
    table, and the named-click path does not reach it at all."""
    source = code_of(executor._prepare_named_click) + code_of(executor._context_for_named_click) \
        + code_of(executor._resolve_click_target)
    for forbidden in ("memory", "queries", "alias", "application("):
        assert forbidden not in source.lower(), forbidden
    imported = set()
    for node in ast.walk(ast.parse((settings.PROJECT_ROOT / "app" / "executor" / "logic.py")
                                   .read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not any("memory" in name for name in imported), imported


# =====================================================================================================
# SAFETY (matrix 20-24)
# =====================================================================================================

def test_a_named_click_is_medium_and_names_the_users_own_word(world):
    """20. And the prompt is the user's words plus the window - never anything read off the screen.

    SLICE 3 REWRITE of the expected sentence only. The confirmation now also says WHOSE window it is,
    because with ownership gone as the click gate this sentence is the authorization. Still MEDIUM,
    still one prompt, still nothing read off the screen in it."""
    own(APP)
    result = executor.execute(named(), always(True))
    assert result.ok
    description, level, confirmed = world.prompts[-1]
    assert level is RiskLevel.MEDIUM and confirmed is True
    assert description == 'click "Seven" in the calculator window I opened ("Calculator")'
    assert "I opened" in description, "the prompt no longer says the assistant opened this window"


def test_yes_permits_and_anything_else_denies(world):
    """21 + 22."""
    own(APP)
    assert executor.execute(named(), always(True)).ok
    assert world.clicks == [CENTRE]
    for answer in (False, None, "yes", "y", 1, ""):
        world.clicks.clear()
        with pytest.raises(safety_logic.ActionDeniedError):
            executor.execute(named(), always(answer))
        assert world.clicks == []


def test_a_command_typed_instead_of_yes_is_a_cancellation(world):
    """22 of the matrix, 9 of the brief: a confirmation is not a place to issue a new command."""
    own(APP)
    for typed in ("click Eight in calculator", "open notepad", "cancel"):
        with pytest.raises(safety_logic.ActionDeniedError):
            executor.execute(named(), always(typed))
    assert world.clicks == []


def test_a_brain_low_floor_cannot_soften_a_named_click(world):
    """23."""
    own(APP)
    result = executor.execute(named(), always(True), risk_floor=RiskLevel.LOW)
    assert result.ok
    assert world.prompts[-1][1] is RiskLevel.MEDIUM


def test_a_higher_brain_floor_still_raises(world):
    """24. The floor is raise-only, and raising still works."""
    own(APP)
    result = executor.execute(named(), always(True), risk_floor=RiskLevel.HIGH)
    assert result.ok
    assert world.prompts[-1][1] is RiskLevel.HIGH


# =====================================================================================================
# EXECUTION (matrix 25-29)
# =====================================================================================================

def test_the_slice_two_bridge_is_what_runs(world):
    """25 + 26 + 27. One click, at the point the local resolver chose, from bounds read after the
    confirmation - and the Brain never saw a number."""
    own(APP)
    moved = (500, 100, 600, 140)
    calls = []
    real = executor.observation.reidentify

    def recording(target, observed):
        calls.append(target.name)
        world.matches = [element(bounds=moved)]      # the control moves while the user is answering
        return real(target, observed)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(executor.observation, "reidentify", recording)
        result = executor.execute(named(), always(True))
    assert result.ok, result.message
    assert calls == [CONTROL], "re-identification must be asked for the user's own name"
    assert world.clicks == [(550, 120)], "the click follows the control, not the old bounds"
    assert world.activations == [WINDOW], "its own owned window, brought forward exactly once"


def test_the_named_click_shares_one_physical_click_with_the_coordinate_path():
    """25 + 28. There is no second click implementation, and both preparers go through the one."""
    assert code_of(executor._send_click).count("adapter.click") == 1
    assert "_send_click" in code_of(executor._prepare_click)
    assert "_prepare_target_click" in code_of(executor._prepare_named_click)
    root = settings.PROJECT_ROOT / "app"
    callers = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "click" and isinstance(node.func.value, ast.Name) \
                    and node.func.value.id == "adapter":
                callers.append(path.relative_to(root).as_posix())
    assert callers == ["executor/logic.py"], callers


def test_the_coordinate_click_path_is_unchanged(world):
    """28. Its wording was tuned during Phase 1 acceptance, and its route is untouched."""
    result = executor.execute(ExecutorAction(CLICK, "350, 420"), always(True))
    assert result.ok and result.outcome is Outcome.UNVERIFIED
    assert result.message == "Clicked at (350, 420). I can't check what the click did."
    assert world.prompts[-1][0] == 'click at (350, 420) on window "Calculator"'
    assert executor.resolve(ExecutorAction(CLICK, "350, 420")) == Resolved((350, 420))
    # and a named click does not resolve like a coordinate one
    assert isinstance(executor.resolve(ExecutorAction(CLICK, "Seven")), Unresolved)


def test_no_pywinauto_action_api_is_used(world):
    """29. The whole named-click flow with every action method on the element as a tripwire."""
    forbidden = ("click", "click_input", "invoke", "set_focus", "type_keys", "select", "toggle",
                 "expand", "collapse", "iface_invoke", "set_text", "close")
    tripped = []

    class Tripwire(UiaElement):
        pass

    for name in forbidden:
        setattr(Tripwire, name, property(lambda self, n=name: tripped.append(n) or 1 / 0))
    world.matches = [Tripwire(**{f.name: getattr(element(), f.name)
                                 for f in UiaElement.__dataclass_fields__.values()})]
    own(APP)
    result = executor.execute(named(), always(True))
    assert result.ok, result.message
    assert tripped == []


# =====================================================================================================
# PROVIDER / PRIVACY (matrix 30-34)
# =====================================================================================================

def test_local_resolution_costs_zero_provider_calls(world):
    """31. The whole point of §8: one interpretation at most, and then everything local.

    Tripwired at the REAL entry point - app/brain/interpreter.interpret, the only function in the
    project that talks to the provider - so reaching it during local resolution fails the test rather
    than quietly costing a request."""
    from app.brain import interpreter

    own(APP)
    reached = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(interpreter, "interpret",
                      lambda *a, **k: reached.append("provider") or pytest.fail("asked the model"))
        result = executor.execute(named(), always(True))
    assert result.ok, result.message
    assert reached == []
    assert world.uia_reads >= 2, "resolve plus re-identify both happened locally"


def test_no_observation_result_can_cause_a_second_provider_call():
    """5 + 31. Structural: the Executor cannot reach the Brain at all, in either direction."""
    source = (settings.PROJECT_ROOT / "app" / "executor" / "logic.py").read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
        elif isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
    assert not any(name.startswith(("app.brain", "anthropic", "httpx")) for name in imported), imported


def test_the_brain_payload_can_carry_no_uia_content(world):
    """32. Everything the Brain is given for a named click is the user's own words or a fixed-vocabulary
    context; the observed element's own fields have nowhere to go."""
    own(APP)
    executor.execute(named(), always(True))
    context = previous_action_context(CLICK_TARGET, APP)
    payload = json.dumps({"kind": context.kind, "safe_target": context.safe_target})
    for leaked in ("num7Button", "7-1", "Button", "350", "420", "Calculator", str(BUTTON_BOUNDS)):
        assert leaked not in payload, leaked


def test_the_control_name_and_command_stay_out_of_the_logs(world, caplog):
    """33. The project does not write command text to disk, and a control name is command text."""
    caplog.set_level("DEBUG")
    own(APP)
    result = executor.execute(named(), always(True))
    assert result.ok
    logged = " ".join(record.getMessage() for record in caplog.records)
    for secret in (CONTROL, "num7Button", "7-1", "Calculator"):
        assert secret not in logged, f"{secret!r} reached the logs: {logged}"
    assert "screen target" in logged and "source=uia" in logged


def test_no_screen_content_is_persisted_in_memory(world):
    """34. Memory is not touched by this path at all, so there is nothing to persist."""
    own(APP)
    executor.execute(named(), always(True))
    assert not (settings.PROJECT_ROOT / "data" / "memory.db").exists()


# =====================================================================================================
# RESULT SEMANTICS (matrix 35-37) - including the "Done: all steps finished" correction
# =====================================================================================================

def test_a_delivered_click_is_still_unverified(world):
    """35 + 36. Finding the target precisely says nothing about what the control then did."""
    own(APP)
    result = executor.execute(named(), always(True))
    assert result.ok and result.outcome is Outcome.UNVERIFIED and result.verified is False
    assert "can't check what the click did" in result.message


def _accepted_plan(*intents):
    """A plan proposed and accepted through the real lifecycle, ready for _run_plan."""
    plan = plan_for(*intents)
    assert isinstance(plan, Plan), getattr(plan, "message", plan)
    context = planner.propose_plan(TurnContext(), plan, "click Seven in calculator",
                                   planner.new_plan_id())
    context = planner.accept_plan(context, context.pending_plan.plan_id)
    assert isinstance(context, TurnContext), context
    return context


def _ran(message, *, verified):
    """A CommandReply shaped like one the Executor really produced."""
    from app.console import CommandReply, Status
    from app.executor.models import ActionResult
    action = named()
    result = ActionResult(action, True, message,
                          outcome=Outcome.DONE if verified else Outcome.UNVERIFIED)
    assert result.verified is verified
    return CommandReply(Status.RAN, message, action, result)


def _finish_plan(world, context, replies):
    """Drive the real _run_plan with scripted step results, and return its closing line."""
    script = Script()
    prompts = console.Prompts(read=script.read, write=script.write, confirm=always(True))
    queued = list(replies)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(console, "run_action", lambda *a, **k: queued.pop(0))
        reply, _context = console._run_plan(context, prompts, None, None, TYPED_CONSOLE)
    return reply.message


def test_a_plan_whose_click_could_not_be_checked_does_not_report_it_as_done(world):
    """11 of the brief, tested by running it. "Done: all N steps finished." over an UNVERIFIED step
    would claim an outcome nobody proved - and a named click is always UNVERIFIED."""
    context = _accepted_plan(named_intent())
    message = _finish_plan(world, context,
                           [_ran('Clicked "Seven". I can\'t check what the click did.', verified=False)])
    assert message == console.PLAN_DONE_UNVERIFIED.format(count=1, plural="", unverified=1)
    assert "Done:" not in message
    assert "isn't confirmation that it worked" in message


def test_a_fully_verified_plan_still_says_done_exactly_as_before(world):
    """11. The correction must not reword the ordinary case - a plan of verified steps is unchanged."""
    opened = Intent(OPEN_APP, OpenAppArgs(APP), why="you asked", risk_floor=RiskLevel.LOW)
    context = _accepted_plan(opened)
    message = _finish_plan(world, context, [_ran("Opened calculator.", verified=True)])
    assert message == "Done: all 1 step finished."
    assert console.PLAN_DONE.format(count=2, plural="s") == "Done: all 2 steps finished."


def test_a_mixed_plan_counts_only_the_steps_it_could_not_check(world):
    """11. Two steps, one of them observable: the sentence has to be true about both."""
    opened = Intent(OPEN_APP, OpenAppArgs(APP), why="you asked", risk_floor=RiskLevel.LOW)
    context = _accepted_plan(opened, named_intent())
    message = _finish_plan(world, context, [_ran("Opened calculator.", verified=True),
                                            _ran('Clicked "Seven".', verified=False)])
    assert message == console.PLAN_DONE_UNVERIFIED.format(count=2, plural="s", unverified=1)


# =====================================================================================================
# EMERGENCY STOP / ISOLATION (matrix 38-43)
# =====================================================================================================

def test_the_emergency_stop_before_resolution_prevents_everything(world):
    """38."""
    own(APP)
    emergency_stop.trigger("test")
    with pytest.raises(EmergencyStopError):
        executor.execute(named(), always(True))
    assert world.clicks == [] and world.prompts == [] and world.uia_reads == 0


def test_the_emergency_stop_after_the_confirmation_prevents_the_click(world):
    """39."""
    own(APP)

    def confirm(action, assessment):
        emergency_stop.trigger("mid-confirmation")
        return True

    with pytest.raises(EmergencyStopError):
        executor.execute(named(), confirm)
    assert world.clicks == []


def test_there_is_no_second_stop_mechanism():
    """13 of the brief: the named-click path adds no stop of its own."""
    source = code_of(executor._prepare_named_click) + code_of(executor._context_for_named_click)
    assert "emergency_stop" not in source, "the existing checkpoints already cover this path"
    assert "emergency_stop.check()" in code_of(executor.execute)


def test_an_offline_test_cannot_reach_real_uia_or_a_real_click():
    """40 + 41. Asked without the world fixture, so nothing is faked."""
    from safety_guards import PhysicalDesktopEscaped
    with pytest.raises(PhysicalDesktopEscaped):
        verifier_adapter.uia_find_by_name(WINDOW, "seven")
    with pytest.raises(PhysicalDesktopEscaped):
        executor_adapter.click(10, 10)
    with pytest.raises(PhysicalDesktopEscaped):
        import pywinauto  # noqa: F401


def test_the_provider_guard_is_still_armed():
    """42. httpx2.HTTPTransport is the transport the Anthropic client sends over, and the one the
    repository-root guard replaces - not httpx, which this test asked for the first time and which
    proved nothing because nothing in this project sends over it."""
    import httpx2

    from safety_guards import ProviderEscaped
    with pytest.raises(ProviderEscaped):
        httpx2.Client().get("https://api.anthropic.com/v1/messages")
