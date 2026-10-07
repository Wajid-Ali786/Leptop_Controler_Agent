"""
Tests for the auto-focus slice: a named UIA click brings its OWN window forward after the
confirmation, so the user never has to switch windows for it.

The property the file exists to hold:

    the only window this can ever bring forward is the one the ownership token already proved, the
    activation only ever happens AFTER the user has typed yes, and a Windows refusal costs zero
    clicks and says so.

Offline throughout. SetForegroundWindow is the real boundary and is centrally refused, so it is
recorded here instead; the REAL safety gate, the REAL observation rules and the REAL Executor
pipeline decide every outcome.

Checks about code are made against its AST with docstrings and comments stripped. A substring search
over this project's source finds the sentence explaining a rule and calls it a violation, which has
happened five times; the habit is not optional.
"""
import ast
import inspect
import textwrap
from types import SimpleNamespace

import pytest

import safety_guards
from app import console
from app.executor import adapter as executor_adapter
from app.executor import emergency_stop
from app.executor import logic as executor
from app.executor.emergency_stop import EmergencyStopError
from app.executor.models import (CLICK, CLICK_TARGET, OPEN_APP, TYPE_TEXT, WINDOW_CONTROL,
                                 ExecutorAction, Outcome)
from app.safety import logic as safety_logic
from app.safety.models import RiskLevel
from app.verifier import adapter as verifier_adapter
from app.verifier.models import (ActiveTarget, Screen, UiaElement, WindowInfo, WindowState)
from config import settings
from tests import fake_window_props

CONTROL = "Seven"
APP = "calculator"
WINDOW = 8100
OTHER = 8200
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
                  enabled=True, focused=False, offscreen=False, is_password=False, patterns=("Invoke",))
    fields.update(overrides)
    return UiaElement(**fields)


def always(answer):
    return lambda *args: answer


def code_of(obj) -> str:
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


def code_of_named(path, name: str) -> str:
    """One named function's code, read from the FILE with docstrings stripped.

    Needed because the autouse desktop guard REPLACES guarded adapter functions with its refuser, so
    inspect.getsource() on app.executor.adapter.activate_window returns the refuser's source. A rule
    about the boundary's real code has to come from the file."""
    module = ast.parse(path.read_text(encoding="utf-8"))
    found = [node for node in ast.walk(module)
             if isinstance(node, ast.FunctionDef) and node.name == name]
    assert len(found) == 1, f"{name} defined {len(found)} times in {path.name}"
    node = found[0]
    for inner in ast.walk(node):
        if isinstance(inner, (ast.FunctionDef, ast.ClassDef)) and inner.body \
                and isinstance(inner.body[0], ast.Expr) \
                and isinstance(inner.body[0].value, ast.Constant) \
                and isinstance(inner.body[0].value.value, str):
            inner.body.pop(0)
            if not inner.body:
                inner.body.append(ast.Pass())
    return ast.unparse(node)


EXECUTOR_ADAPTER = settings.PROJECT_ROOT / "app" / "executor" / "adapter.py"


def code_of_module(path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
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
    """One owned Calculator window, a fake foreground, and every physical boundary recorded."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_PATH", config_path)

    desktop = SimpleNamespace(
        windows=[WindowInfo(WINDOW, "Calculator", class_name="ApplicationFrameWindow"),
                 WindowInfo(OTHER, "Calculator", class_name="ApplicationFrameWindow")],
        matches=[element()],
        control_name="seven",
        front=OTHER,                  # the console stands in for "something else is in front"
        pointer=CENTRE,
        minimized=False,
        gone=False,
        accepts=True,                 # whether SetForegroundWindow reports success
        arrives=True,                 # whether the window actually reaches the foreground
        activations=[],
        clicks=[],
        prompts=[],
        order=[],
    )

    def activate_window(handle):
        desktop.activations.append(handle)
        desktop.order.append("activate")
        if desktop.gone:
            raise executor_adapter.WindowGoneError("the window is already closed")
        if desktop.arrives:
            desktop.front = handle
        return desktop.accepts

    def window_state(handle):
        if desktop.gone or not any(w.handle == handle for w in desktop.windows):
            return None
        return WindowState(minimized=desktop.minimized, maximized=False, has_minimize_box=True,
                           has_maximize_box=True, tool_window=False, hung=False)

    def active_target():
        front = next((w for w in desktop.windows if w.handle == desktop.front), None)
        return ActiveTarget(window=front)

    def uia_find_by_name(handle, normalized_name, timeout_seconds=2.0):
        desktop.order.append("uia")
        if normalized_name != desktop.control_name:
            return []
        return [e for e in desktop.matches if e.window_handle == handle]

    def click(x, y):
        desktop.order.append("click")
        desktop.clicks.append((x, y))
        desktop.pointer = (x, y)      # the Executor checks where the pointer ended up

    monkeypatch.setattr(executor_adapter, "activate_window", activate_window)
    monkeypatch.setattr(executor_adapter, "click", click)
    monkeypatch.setattr(verifier_adapter, "window_state", window_state)
    monkeypatch.setattr(verifier_adapter, "active_target", active_target)
    monkeypatch.setattr(verifier_adapter, "uia_find_by_name", uia_find_by_name)
    monkeypatch.setattr(verifier_adapter, "uia_window_bounds", lambda handle: (0, 0, 800, 600))
    monkeypatch.setattr(verifier_adapter, "list_windows", lambda: list(desktop.windows))
    monkeypatch.setattr(verifier_adapter, "list_screens",
                        lambda: [Screen(0, 0, 1920, 1080, primary=True)])
    monkeypatch.setattr(verifier_adapter, "window_at",
                        lambda x, y: next((w for w in desktop.windows
                                           if w.handle == desktop.front), None))
    monkeypatch.setattr(verifier_adapter, "cursor_position", lambda: desktop.pointer)
    fake_window_props.install(monkeypatch, executor_adapter,
                              exists=lambda handle: any(w.handle == handle for w in desktop.windows))

    real_authorize = safety_logic.authorize

    def recording_authorize(action, confirm=None):
        desktop.order.append("confirm")
        decision = real_authorize(action, confirm)
        desktop.prompts.append((action.description, decision.assessment.level, decision.confirmed))
        return decision

    monkeypatch.setattr(executor, "authorize", recording_authorize)

    executor.forget_session_windows()
    emergency_stop.reset("activation-test")
    yield desktop
    executor.forget_session_windows()
    emergency_stop.reset("activation-test")


def own(app=APP, handles=(WINDOW,)):
    assert executor._remember_opened(app, frozenset(handles)), "the fake desktop refused the token"


def named(control=CONTROL, app=APP) -> ExecutorAction:
    return ExecutorAction(CLICK_TARGET, app, control)


# =====================================================================================================
# ARCHITECTURE (matrix 1-5)
# =====================================================================================================

def test_activation_lives_only_in_the_executor_adapter():
    """1 + 2. The Verifier observes; the Executor acts. Activation is an action."""
    root = settings.PROJECT_ROOT / "app"
    definers, callers = [], []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "activate_window":
                definers.append(path.relative_to(root).as_posix())
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "activate_window":
                callers.append(path.relative_to(root).as_posix())
    assert definers == ["executor/adapter.py"], definers
    assert callers == ["executor/logic.py"], callers
    # and no verifier file names a foreground API at all
    for path in (root / "verifier").glob("*.py"):
        code = code_of_module(path)
        for forbidden in ("SetForegroundWindow", "SwitchToThisWindow", "BringWindowToTop",
                          "AttachThreadInput", "ShowWindow", "set_focus", "SetFocus"):
            assert forbidden not in code, f"{path.name}: {forbidden}"


def test_only_set_foreground_window_is_used():
    """3 of the report, and the binding decision. One API, and no escalation past a refusal."""
    code = code_of_named(EXECUTOR_ADAPTER, "activate_window")
    # argtypes, restype, and the one call - no fourth mention, and nothing else is reached for
    assert code.count("SetForegroundWindow") == 3, code
    for forbidden in ("SwitchToThisWindow", "BringWindowToTop", "AttachThreadInput", "ShowWindow",
                      "SetActiveWindow", "SetWindowPos", "keybd_event", "SendInput", "AllowSetForeground"):
        assert forbidden not in code, forbidden
    # nowhere else in the Executor either
    whole = code_of_module(settings.PROJECT_ROOT / "app" / "executor" / "adapter.py")
    for forbidden in ("SwitchToThisWindow", "BringWindowToTop", "AttachThreadInput"):
        assert forbidden not in whole, forbidden


def test_the_adapter_does_not_wait_or_poll():
    """2 of the brief: waiting belongs in logic, beside the emergency-stop checkpoints."""
    code = code_of_named(EXECUTOR_ADAPTER, "activate_window")
    for forbidden in ("sleep", "while ", "monotonic", "deadline", "wait"):
        assert forbidden not in code, forbidden


def test_activation_is_not_a_new_action_or_a_brain_capability():
    """3 + 4. No new ExecutorAction kind, and nothing the model can ask for."""
    from app.brain.models import ARGS_FIELDS, ARGS_FOR_KIND, interpretation_schema
    from app.planner.models import ALL_KINDS
    assert "activate" not in " ".join(ARGS_FOR_KIND)
    assert "activate" not in " ".join(ALL_KINDS)
    assert set(ARGS_FOR_KIND) == set(executor._PREPARERS), "the vocabulary must not widen"
    schema = interpretation_schema(400)
    intent = schema["properties"]["intents"]["items"]["properties"]
    assert "activate" not in " ".join(intent)
    assert "activate" not in " ".join(f for fields in ARGS_FIELDS.values() for f in fields)


def test_activation_is_a_registered_desktop_boundary():
    """5. Centrally guarded, in the one place, with no new marker category."""
    assert "activate_window" in safety_guards.DESKTOP_BOUNDARIES
    assert set(safety_guards.DESKTOP_EXEMPT) == {"real_desktop", "real_elevated", "real_clipboard",
                                                 "real_voice_console"}


# =====================================================================================================
# ORDER (matrix 6-12)
# =====================================================================================================

def test_the_whole_order_is_resolve_confirm_activate_reidentify_click(world):
    """6 + 7 + 9 + 10 + 11 + 12, in one recording. The UIA read happens before the confirmation, the
    activation only after it, and the re-identification after that."""
    own()
    result = executor.execute(named(), always(True))
    assert result.ok, result.message
    assert world.order == ["uia", "confirm", "activate", "uia", "click"], world.order


def test_activation_never_happens_before_the_confirmation(world):
    """7. The console has to keep the keyboard until the user has typed yes."""
    own()
    seen = []

    def confirm(action, assessment):
        seen.append(list(world.activations))
        return True

    executor.execute(named(), confirm)
    assert seen == [[]], "a window was brought forward before the user answered"


@pytest.mark.parametrize("answer", [False, None, "yes", "y", 1, "", "click seven"])
def test_a_non_yes_answer_activates_nothing_and_clicks_nothing(world, answer):
    """8 + 32 + 33."""
    own()
    with pytest.raises(safety_logic.ActionDeniedError):
        executor.execute(named(), always(answer))
    assert world.activations == [] and world.clicks == []


def test_activation_is_attempted_exactly_once(world):
    """9."""
    own()
    assert executor.execute(named(), always(True)).ok
    assert world.activations == [WINDOW]


def test_the_foreground_is_verified_after_activation_not_assumed(world):
    """10 + 17. The call's own answer is not proof: it is read back by handle."""
    own()
    world.accepts = False          # Windows reports failure...
    world.arrives = True           # ...but the window does arrive anyway
    result = executor.execute(named(), always(True))
    assert result.ok, "the handle read-back decides, not the call's return value"
    assert world.clicks == [CENTRE]


def test_reidentification_only_happens_after_a_successful_activation(world):
    """11 + 38. A refused activation stops before the second UIA read."""
    own()
    world.arrives = False
    result = executor.execute(named(), always(True))
    assert not result.ok
    assert world.order == ["uia", "confirm", "activate"], world.order


# =====================================================================================================
# OWNERSHIP (matrix 13-16)
# =====================================================================================================

def test_only_the_proven_owned_handle_is_ever_activated(world):
    """13 + 14. Two windows share the title "Calculator"; only the owned one may be touched."""
    own(handles=(WINDOW,))
    assert executor.execute(named(), always(True)).ok
    assert world.activations == [WINDOW]
    assert OTHER not in world.activations


def test_a_title_matching_unowned_window_is_never_activated(world):
    """14. Nothing is owned, so there is nothing to bring forward - though two windows match."""
    result = executor.execute(named(), always(True))
    assert not result.ok and world.activations == [] and world.clicks == []
    assert world.prompts == [], "nothing should have been confirmed either"


def test_a_handle_without_the_ownership_token_is_never_activated(world, monkeypatch):
    """15. The stale/reused-handle case: the record exists, the token no longer matches."""
    own()
    monkeypatch.setattr(executor_adapter, "window_token", lambda handle: 0xDEAD)
    result = executor.execute(named(), always(True))
    assert not result.ok and world.activations == [] and world.clicks == []
    assert "prove is mine" in result.message


def test_two_owned_windows_refuse_before_any_activation(world):
    """16."""
    own(handles=(WINDOW, OTHER))
    result = executor.execute(named(), always(True))
    assert not result.ok and world.activations == [] and world.clicks == []
    assert "2 calculator windows" in result.message


# =====================================================================================================
# FOREGROUND (matrix 17-21)
# =====================================================================================================

def test_the_foreground_never_arriving_is_a_retryable_refusal(world):
    """18 + 20 + 37. Bounded, honest, and it does not blame UIA or the click."""
    own()
    world.arrives = False
    result = executor.execute(named(), always(True))
    assert not result.ok and result.retryable is True
    assert world.clicks == []
    assert "couldn't bring calculator to the front" in result.message
    assert "Put calculator in front and say that again" in result.message
    for overclaim in ("UIA", "accessibility", "click failed", "Clicked"):
        assert overclaim not in result.message, overclaim


def test_the_wrong_window_arriving_in_front_clicks_nothing(world):
    """19. Something else took the foreground instead."""
    own()

    def activate_window(handle):
        world.activations.append(handle)
        world.front = OTHER           # a different window ends up in front
        return True

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(executor_adapter, "activate_window", activate_window)
        result = executor.execute(named(), always(True))
    assert not result.ok and world.clicks == []
    assert result.retryable is True


def test_the_wait_is_bounded_by_its_setting(world, monkeypatch):
    """20. No infinite wait, and the bound comes from configuration rather than a literal."""
    own()
    world.arrives = False
    ticks = iter([0.0] + [0.0] * 3 + [99.0] * 50)
    monkeypatch.setattr(executor, "_clock", lambda: next(ticks))
    result = executor.execute(named(), always(True))
    assert not result.ok and world.clicks == []
    assert "monotonic" not in code_of(executor._bring_owned_window_forward)
    assert "activation_settle_seconds" in code_of(executor._activation_settings)


def test_an_unreadable_activation_setting_fails_closed(world, tmp_path, monkeypatch):
    """7 of the brief: no hardcoded timeout, and an unusable one does not fall back to a guess."""
    broken = tmp_path / "broken.yaml"
    broken.write_text(CONFIG.replace("activation_settle_seconds: 2.0",
                                     "activation_settle_seconds: nonsense"), encoding="utf-8")
    own()
    monkeypatch.setattr(settings, "CONFIG_PATH", broken)
    result = executor.execute(named(), always(True))
    assert not result.ok and world.clicks == []
    assert "activation_settle_seconds" in result.message


def test_the_emergency_stop_during_the_wait_stops_everything(world):
    """21 + the brief's §11. The wait is interruptible, and it is the existing stop."""
    own()
    world.arrives = False

    real_wait = emergency_stop.wait

    def wait(seconds):
        emergency_stop.trigger("during the wait")
        return real_wait(seconds)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(executor.emergency_stop, "wait", wait)
        with pytest.raises(EmergencyStopError):
            executor.execute(named(), always(True))
    assert world.clicks == []


def test_the_wait_is_interruptible_rather_than_a_raw_sleep():
    """21. Proved against the code: a raw sleep could not be interrupted by the stop.

    Slice 2 extracted the mechanism into _activate_to_front so that open_app and the named click share
    ONE activation path, so that is where the wait now lives. The property is unchanged, and it is
    checked across BOTH halves so neither can gain a raw sleep."""
    mechanism = code_of(executor._activate_to_front)
    assert "emergency_stop.wait" in mechanism
    for code in (mechanism, code_of(executor._bring_owned_window_forward)):
        assert "time.sleep" not in code and "sleep(" not in code


def test_there_is_exactly_one_activation_path():
    """Slice 2's requirement: open_app reuses the auto-focus activation rather than writing a second
    one. Only the shared mechanism may call activate_window, and only it may read the foreground back."""
    # Parsed from the module's code with docstrings stripped: a prose mention of activate_window
    # (there are several) must not satisfy or break a rule about who CALLS it.
    tree = ast.parse(code_of_module(settings.PROJECT_ROOT / "app" / "executor" / "logic.py"))
    callers = sorted(node.name for node in ast.walk(tree)
                     if isinstance(node, ast.FunctionDef)
                     and any(isinstance(call, ast.Call)
                             and ast.unparse(call.func) == "adapter.activate_window"
                             for call in ast.walk(node)))
    assert callers == ["_activate_to_front"], callers


def test_the_stop_is_checked_before_activation(world):
    """11 of the brief. Stopped before anything: no activation, no click."""
    own()
    emergency_stop.trigger("before")
    with pytest.raises(EmergencyStopError):
        executor.execute(named(), always(True))
    assert world.activations == [] and world.clicks == []


# =====================================================================================================
# MINIMIZED (matrix 22-25)
# =====================================================================================================

def test_a_minimized_owned_window_is_refused_not_restored(world):
    """22 + 25. Un-minimising someone's window is a change nobody asked for."""
    own()
    world.minimized = True
    result = executor.execute(named(), always(True))
    assert not result.ok and result.retryable is True
    assert world.activations == [] and world.clicks == []
    assert "minimized" in result.message and "Bring it back up" in result.message


def test_nothing_restores_a_background_window(world):
    """23 + 24. No ShowWindow anywhere, and window_control is still active-window only."""
    code = code_of_module(settings.PROJECT_ROOT / "app" / "executor" / "adapter.py")
    assert "ShowWindow" not in code
    state_reader = code_of_named(EXECUTOR_ADAPTER, "request_window_state")
    assert "PostMessageW" in state_reader, "window control still posts, and still to the active window"
    assert "SetForegroundWindow" not in state_reader
    activation = code_of(executor._bring_owned_window_forward)
    for forbidden in ("request_window_state", "restore", "SC_RESTORE", "ShowWindow"):
        assert forbidden not in activation, forbidden


def test_a_window_that_vanished_before_activation_is_reported_honestly(world):
    own()
    world.gone = True
    result = executor.execute(named(), always(True))
    assert not result.ok and world.clicks == []
    assert "isn't open any more" in result.message


# =====================================================================================================
# HANDOVER (matrix 26-30)
# =====================================================================================================

def test_a_named_click_no_longer_uses_the_focus_handover():
    """26 + 30. The prompts the owner had to answer are simply not asked for this kind."""
    assert console.needs_handover(CLICK_TARGET) is False
    assert CLICK_TARGET in console.NO_HANDOVER
    assert CLICK_TARGET not in console.HANDS_OVER


@pytest.mark.parametrize("kind", [CLICK, TYPE_TEXT, "shortcut", "scroll", "refresh", WINDOW_CONTROL])
def test_every_other_action_still_hands_focus_over(kind):
    """27 + 28 + 29. They land wherever focus is and cannot know their window, so nothing changed."""
    assert console.needs_handover(kind) is True
    assert kind in console.HANDS_OVER


def test_open_and_close_are_unchanged_too():
    from app.executor.models import CLOSE_APP
    assert console.needs_handover(OPEN_APP) is False and console.needs_handover(CLOSE_APP) is False


def test_every_kind_is_still_classified_explicitly():
    """29. The rule that a new action cannot silently default to either set."""
    from app.brain.models import ARGS_FOR_KIND
    assert console.HANDS_OVER | console.NO_HANDOVER == set(ARGS_FOR_KIND)
    assert not console.HANDS_OVER & console.NO_HANDOVER


def test_the_named_click_path_asks_for_no_switch_prompt(world):
    """30. Driven through the console's own entry point with a hand-over that fails if touched."""
    own()

    class Explode:
        def note_console_window(self):
            pass

        def hand_over(self, prompt):
            raise AssertionError(f"a named click asked for a hand-over: {prompt!r}")

    reply = console.run_action(named(), confirm=always(True), focus=Explode())
    assert reply.result is not None and reply.result.ok, reply.message
    assert world.clicks == [CENTRE]


def test_a_coordinate_click_still_asks_for_the_handover(world):
    """27. The regression that matters: the old path is untouched."""
    own()
    asked = []

    class Watcher:
        def note_console_window(self):
            pass

        def hand_over(self, prompt):
            asked.append(prompt)
            return None

    reply = console.run_action(ExecutorAction(CLICK, "350, 420"), confirm=always(True), focus=Watcher())
    assert asked, "the coordinate click must still hand focus over"
    assert reply.result is not None and reply.result.ok


# =====================================================================================================
# SAFETY + EXECUTION (matrix 31-40)
# =====================================================================================================

def test_the_named_click_is_still_medium_and_confirmed_once(world):
    """31 + 32."""
    own()
    result = executor.execute(named(), always(True))
    assert result.ok
    description, level, confirmed = world.prompts[-1]
    assert level is RiskLevel.MEDIUM and confirmed is True
    assert description == 'click "Seven" in window "Calculator"'
    assert len(world.prompts) == 1, "activation must not ask a second time"


def test_a_low_brain_floor_cannot_soften_it_and_a_high_one_still_raises(world):
    """34 + 35."""
    own()
    assert executor.execute(named(), always(True), risk_floor=RiskLevel.LOW).ok
    assert world.prompts[-1][1] is RiskLevel.MEDIUM
    world.front = OTHER
    assert executor.execute(named(), always(True), risk_floor=RiskLevel.HIGH).ok
    assert world.prompts[-1][1] is RiskLevel.HIGH


def test_one_click_at_the_current_bounds_and_still_unverified(world):
    """36 + 39 + 40."""
    own()
    moved = (500, 100, 600, 140)
    world.matches = [element(bounds=moved)]
    result = executor.execute(named(), always(True))
    assert result.ok and world.clicks == [(550, 120)]
    assert result.outcome is Outcome.UNVERIFIED and result.verified is False


def test_a_failed_reidentification_after_activation_clicks_nothing(world):
    """38. Activation succeeded; the control went away while it happened."""
    own()
    calls = []

    def uia_find_by_name(handle, normalized_name, timeout_seconds=2.0):
        calls.append(normalized_name)
        return [element()] if len(calls) == 1 else []       # found, then gone

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(verifier_adapter, "uia_find_by_name", uia_find_by_name)
        result = executor.execute(named(), always(True))
    assert not result.ok and world.clicks == []
    assert world.activations == [WINDOW], "activation still happened exactly once"


# =====================================================================================================
# ISOLATION (matrix 41-47)
# =====================================================================================================

def test_an_offline_test_cannot_call_the_real_set_foreground_window():
    """41. Asked without the world fixture, so nothing is faked."""
    from safety_guards import PhysicalDesktopEscaped
    with pytest.raises(PhysicalDesktopEscaped):
        executor_adapter.activate_window(WINDOW)


def test_the_marker_alone_and_the_gate_alone_are_both_insufficient(monkeypatch):
    """42 + 43 + 44. The existing pair, and only the pair."""
    marked = type("Node", (), {"get_closest_marker": lambda self, name: object()})()
    bare = type("Node", (), {"get_closest_marker": lambda self, name: None})()
    monkeypatch.delenv("RUN_REAL_DESKTOP_TEST", raising=False)
    assert safety_guards.exempt(marked, safety_guards.DESKTOP_EXEMPT) is False
    monkeypatch.setenv("RUN_REAL_DESKTOP_TEST", "1")
    assert safety_guards.exempt(bare, safety_guards.DESKTOP_EXEMPT) is False
    assert safety_guards.exempt(marked, safety_guards.DESKTOP_EXEMPT) is True


def test_activation_causes_no_provider_request_and_no_memory_write(world):
    """45 + 46. Nothing on this path can reach either."""
    own()
    assert executor.execute(named(), always(True)).ok
    assert not (settings.PROJECT_ROOT / "data" / "memory.db").exists()
    imported = set()
    for node in ast.walk(ast.parse((settings.PROJECT_ROOT / "app" / "executor" / "logic.py")
                                   .read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not any(name.startswith(("app.brain", "app.memory", "anthropic", "httpx"))
                   for name in imported), imported


def test_activation_plays_no_audio(world):
    """47. The speaker is not on this path at all."""
    own()
    code = code_of(executor._bring_owned_window_forward) + code_of(executor._activate_then)
    for forbidden in ("speak", "speaker", "winmm", "play"):
        assert forbidden not in code.lower(), forbidden
