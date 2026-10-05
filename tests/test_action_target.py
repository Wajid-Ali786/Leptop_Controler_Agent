"""
Tests for Phase 5 Slice 2 - re-identifying a UIA target and handing it to the EXISTING click path
(app/verifier/observation.py reidentify(), app/executor/logic.py click_target()).

Nothing real is touched. The UIA tree is a list of fake elements, the desktop is fake, and the
Executor's click is recorded - but the REAL safety gate, the REAL observation rules and the REAL
executor pipeline decide every outcome, so a test that passes says something about production.

Two habits this file keeps on purpose, both learned the hard way in this project:
  - a rule about code is checked against the code's AST with docstrings and comments STRIPPED, never
    against its prose. A substring search finds the sentence explaining a rule and calls it a violation
    (or, worse, finds nothing and calls that compliance). That mistake has been made four times here.
  - a claim about an identifier is checked against the dependency, not against a belief about it. The
    forbidden pywinauto action names below are all asserted to EXIST in the installed pywinauto, so this
    file cannot drift into forbidding methods that were never real.
"""
import ast
import inspect
import pathlib
import sys
import time
import warnings
from types import SimpleNamespace

import pytest

from app.executor import adapter, emergency_stop, logic
from app.executor.emergency_stop import EmergencyStopError
from app.executor.models import CLICK, Outcome
from app.safety import logic as safety_logic
from app.safety.models import RiskLevel
from app.verifier import adapter as verifier_adapter
from app.verifier import observation
from app.verifier.models import (ActionTarget, Ambiguous, NotEligible, NotFound, Observed,
                                 ObservationSource, Screen, Stale, Target, UiaElement, Unavailable,
                                 WindowInfo)
from config import settings

CONFIG = (
    "safety:\n"
    '  risky_keywords: [delete, shutdown, "shut down", send]\n'
    "  safe_words: [sender]\n"
    "executor:\n"
    "  apps:\n"
    "    notepad: notepad.exe\n"
    "  max_attempts: 3\n"
    "observation:\n"
    "  snapshot_timeout_seconds: 2.0\n"
    "  freshness_seconds: 2.0\n"
    "verifier:\n"
    "  window_timeout_seconds: 0.2\n"
    "  poll_interval_seconds: 0.01\n"
    "  app_windows:\n"
    '    notepad: "Notepad$"\n'
)

WINDOW = 4100                    # the window every test works in
WINDOW_BOUNDS = (0, 0, 800, 600)
BUTTON_BOUNDS = (300, 400, 400, 440)   # centre (350, 420)
CENTRE = (350, 420)


def element(**overrides) -> UiaElement:
    """The control under test: an ordinary enabled button with an automation id."""
    fields = dict(window_handle=WINDOW, control_type="Button", runtime_id="42-7", automation_id="loginBtn",
                  class_name="Button", bounds=BUTTON_BOUNDS, enabled=True, focused=False, offscreen=False,
                  is_password=False, patterns=("Invoke",))
    fields.update(overrides)
    return UiaElement(**fields)


def observed(**overrides) -> Observed:
    """What Slice 1 would have returned for that control, through the real conversion."""
    observed_at = overrides.pop("observed_at", time.time())
    return observation._observed(element(**overrides), observed_at)


def always(answer):
    return lambda *args: answer


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A fake window with one fake control in it, and a recorded Executor click."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_PATH", config_path)

    desktop = SimpleNamespace(
        windows=[WindowInfo(WINDOW, "Untitled - Notepad")],
        matches=[element()],          # what UIA reports for the user's word, right now
        window_bounds=WINDOW_BOUNDS,
        uia_error=None,               # set to a message to make the window unreadable
        on_top=WINDOW,                # which window is actually in front at the clicked point
        pointer=None,                 # where the pointer ends up; None means "wherever it clicked"
        clicks=[],
        prompts=[],
    )

    def uia_find_by_name(window_handle, normalized_name, timeout_seconds=2.0):
        if desktop.uia_error:
            raise verifier_adapter.VerifierAdapterError(desktop.uia_error)
        return [e for e in desktop.matches if e.window_handle == window_handle]

    def uia_window_bounds(window_handle):
        if desktop.uia_error:
            raise verifier_adapter.VerifierAdapterError(desktop.uia_error)
        return desktop.window_bounds

    def window_at(x, y):
        return next((w for w in desktop.windows if w.handle == desktop.on_top), None)

    def click(x, y):
        desktop.clicks.append((x, y))
        desktop.pointer = desktop.pointer or (x, y)

    monkeypatch.setattr(verifier_adapter, "uia_find_by_name", uia_find_by_name)
    monkeypatch.setattr(verifier_adapter, "uia_window_bounds", uia_window_bounds)
    monkeypatch.setattr(verifier_adapter, "list_windows", lambda: list(desktop.windows))
    monkeypatch.setattr(verifier_adapter, "list_screens", lambda: [Screen(0, 0, 1920, 1080, primary=True)])
    monkeypatch.setattr(verifier_adapter, "window_at", window_at)
    monkeypatch.setattr(verifier_adapter, "cursor_position", lambda: desktop.pointer or (0, 0))
    monkeypatch.setattr(adapter, "click", click)

    real_authorize = safety_logic.authorize

    def recording_authorize(action, confirm=None):
        decision = real_authorize(action, confirm)       # the REAL gate, unchanged
        desktop.prompts.append((action.description, decision.assessment.level, decision.confirmed))
        return decision

    monkeypatch.setattr(logic, "authorize", recording_authorize)
    emergency_stop.reset("test")
    yield desktop
    emergency_stop.reset("test")


def target(name="Login") -> Target:
    return Target(name, WINDOW)


def _strip_prose(tree):
    """Drop every docstring. A body left empty gets a `pass`, or the result will not unparse - which is
    how a class whose body is only a docstring broke this the first time."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)) \
                and node.body and isinstance(node.body[0], ast.Expr) \
                and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
            node.body.pop(0)
            if not node.body and not isinstance(node, ast.Module):
                node.body.append(ast.Pass())
    return tree


def code_of(obj) -> str:
    """A function's source with every docstring and comment removed, so a rule about what the code DOES
    is never satisfied or broken by prose that merely describes it."""
    import textwrap
    return ast.unparse(_strip_prose(ast.parse(textwrap.dedent(inspect.getsource(obj)))))


# =====================================================================================================
# READ-ONLY UIA ARCHITECTURE (matrix 1-4)
# =====================================================================================================

# The real pywinauto action surface, read off the installed package rather than guessed from prose.
# `check` and `uncheck` are deliberately NOT here: pywinauto 0.6.9 has no such methods (the real ones
# are `toggle` and `check_button`), and forbidding a name that does not exist would look like a rule
# while enforcing nothing. Every name below is asserted to exist in the dependency.
FORBIDDEN_UIA_ACTIONS = (
    "click", "click_input", "double_click", "double_click_input", "right_click", "right_click_input",
    "press_mouse_input", "release_mouse_input", "move_mouse_input", "drag_mouse_input",
    "wheel_mouse_input", "type_keys", "send_keystrokes", "send_keys", "set_focus", "invoke", "select",
    "expand", "collapse", "toggle", "check_button", "set_edit_text", "set_text", "set_window_text",
    "set_value", "menu_select", "scroll", "minimize", "maximize", "restore", "move_window", "close",
    "check", "uncheck",   # common_controls.py, the win32 backend - real methods, just not UIA ones
    # the pattern accessors: reaching one hands out a COM interface that can act
    "iface_invoke", "iface_toggle", "iface_selection", "iface_selection_item",
    "iface_expand_collapse", "iface_scroll", "iface_scroll_item",
)


def pywinauto_source_folder():
    """pywinauto's installed source, found WITHOUT importing it - the offline guard blocks that import,
    and rightly: loading it would load a UI Automation stack. Reading the files loads nothing."""
    for entry in sys.path:
        folder = pathlib.Path(entry) / "pywinauto"
        if (folder / "__init__.py").exists():
            return folder
    pytest.skip("pywinauto source not found on sys.path")


def pywinauto_defined_names():
    """Every name pywinauto defines as a function, method or module-level assignment."""
    names = set()
    for path in pywinauto_source_folder().rglob("*.py"):
        try:
            with warnings.catch_warnings():     # pywinauto's own files carry invalid escapes
                warnings.simplefilter("ignore")
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return names


def test_every_forbidden_name_is_a_real_pywinauto_api():
    """The forbidden list is anchored to the dependency, so it cannot rot into forbidding fiction.

    This test earned its place immediately. The first draft of the list was taken from the UIA
    backend's classes alone, which has no `check`/`uncheck` - but pywinauto's win32 backend defines
    both, in controls/common_controls.py. They are on the list now. Forbidding a name that does not
    exist looks like a rule while enforcing nothing; missing one that does is worse."""
    defined = pywinauto_defined_names()
    missing = [name for name in FORBIDDEN_UIA_ACTIONS if name not in defined]
    assert missing == [], f"not real pywinauto APIs, so forbidding them proves nothing: {missing}"
    assert len(FORBIDDEN_UIA_ACTIONS) == len(set(FORBIDDEN_UIA_ACTIONS))


def _verifier_production_files():
    folder = settings.PROJECT_ROOT / "app" / "verifier"
    return sorted(folder.glob("*.py"))


# Everything the adapter is allowed to read off a UI Automation element. An allow-list, not a
# deny-list: a new pywinauto release can add an acting method, and a deny-list would not know about it,
# whereas anything not on this list fails the test the moment it is used.
UIA_READS_ALLOWED = frozenset({
    # structure and identity
    "descendants", "element", "runtime_id", "automation_id", "class_name", "control_type",
    # geometry
    "rectangle", "left", "top", "right", "bottom",
    # state
    "enabled", "has_keyboard_focus", "is_offscreen",
    # password: whether it IS one, never what is in it
    "is_password", "IsPassword", "CurrentIsPassword",
    # which patterns a control supports - availability flags, never a pattern's value
    "CurrentIsInvokePatternAvailable", "CurrentIsValuePatternAvailable",
    "CurrentIsTextPatternAvailable", "CurrentIsTogglePatternAvailable",
    "CurrentIsSelectionPatternAvailable", "CurrentIsExpandCollapsePatternAvailable",
    # the accessible name, compared inside the adapter and never returned
    "name",
})

# The only functions in the project that ever hold a pywinauto element.
UIA_FUNCTIONS = ("uia_find_by_name", "uia_window_bounds", "_uia_name", "_uia_element", "_is_password",
                 "_uia_patterns")


def _uia_function_nodes():
    tree = ast.parse((settings.PROJECT_ROOT / "app" / "verifier" / "adapter.py")
                     .read_text(encoding="utf-8"))
    found = {node.name: node for node in ast.walk(tree)
             if isinstance(node, ast.FunctionDef) and node.name in UIA_FUNCTIONS}
    assert set(found) == set(UIA_FUNCTIONS), f"expected all of {UIA_FUNCTIONS}, found {sorted(found)}"
    return found


def test_the_verifier_reads_only_allowed_properties_off_a_uia_element():
    """1 + 2. The Verifier may OBSERVE through UI Automation. Only the Executor may ACT.

    An allow-list on the element, checked in the AST of the six functions that ever hold one. The first
    draft of this test was a deny-list of action names over the whole module, and it flagged
    `emergency_stop.check()` five times - a bare attribute name says nothing about whose attribute it
    is, which is the AST form of the substring mistake this file warns about at the top.

    Attribute names reached through getattr() with a literal are included: that is how the password and
    pattern flags are read, and a rule that getattr() slips past is not a rule."""
    offenders = []
    for name, node in _uia_function_nodes().items():
        for inner in ast.walk(node):
            if isinstance(inner, ast.Attribute) and isinstance(inner.value, ast.Name) \
                    and inner.value.id in ("element", "element_info", "rectangle", "root"):
                if inner.attr not in UIA_READS_ALLOWED:
                    offenders.append(f"{name}: .{inner.attr}")
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) \
                    and inner.func.id == "getattr" and len(inner.args) >= 2 \
                    and isinstance(inner.args[1], ast.Constant):
                if inner.args[1].value not in UIA_READS_ALLOWED:
                    offenders.append(f"{name}: getattr(..., {inner.args[1].value!r})")
            for literal in [n.value for n in ast.walk(inner)
                            if isinstance(n, ast.Constant) and isinstance(n.value, str)]:
                if literal in FORBIDDEN_UIA_ACTIONS:
                    offenders.append(f"{name}: the string {literal!r}")
    assert offenders == [], f"the Verifier must never act through UI Automation: {offenders}"
    assert not (UIA_READS_ALLOWED & set(FORBIDDEN_UIA_ACTIONS)), "the two lists must not overlap"


def test_no_verifier_file_names_a_uia_action_outside_a_comment():
    """1. The crude check, kept but scoped to where it means something: a pywinauto element only ever
    exists inside those six adapter functions, so an action name appearing in the code of ANY OTHER
    verifier function cannot be a UIA call - but it can be in observation.py, which must stay clean of
    acting vocabulary entirely. Prose is stripped first."""
    module = code_of_module(settings.PROJECT_ROOT / "app" / "verifier" / "observation.py")
    named = [name for name in FORBIDDEN_UIA_ACTIONS
             if f".{name}(" in module or f" {name}(" in module]
    assert named == [], f"observation.py must not act: {named}"


def code_of_module(path) -> str:
    """The same, for a whole file."""
    return ast.unparse(_strip_prose(ast.parse(path.read_text(encoding="utf-8"))))


def test_the_verifier_reads_only_the_element_info_class():
    """2. pywinauto has a read-only half and an acting half. The Verifier may reach only the read-only
    one - UIAElementInfo, which defines no action method at all - and never a control WRAPPER, which is
    where click_input, type_keys, invoke and set_focus live.

    Read from pywinauto's source, so the claim is about the installed dependency and still imports
    nothing."""
    element_info = pywinauto_source_folder() / "uia_element_info.py"
    defined = {node.name for node in ast.walk(ast.parse(element_info.read_text(encoding="utf-8")))
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert defined, "uia_element_info.py defines nothing - the check would pass for the wrong reason"
    assert [n for n in defined if n in FORBIDDEN_UIA_ACTIONS] == []

    imported = set()
    for path in _verifier_production_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("pywinauto"):
                imported |= {f"{node.module}.{alias.name}" for alias in node.names}
            elif isinstance(node, ast.Import):
                imported |= {a.name for a in node.names if a.name.startswith("pywinauto")}
    assert imported == {"pywinauto.uia_element_info.UIAElementInfo"}, imported


def test_the_physical_click_still_belongs_to_the_executor():
    """3. There is ONE click in the project, in the Executor's adapter, and one caller of it."""
    root = settings.PROJECT_ROOT / "app"
    callers = []
    for path in root.rglob("*.py"):
        tree = ast.parse(code_of_module(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "click" and isinstance(node.func.value, ast.Name) \
                    and node.func.value.id == "adapter":
                callers.append(path.relative_to(root).as_posix())
    assert callers == ["executor/logic.py"], callers
    assert code_of(logic._send_click).count("adapter.click") == 1
    # and both click paths go through that one function
    assert code_of(logic._prepare_click).count("_send_click") == 1
    assert code_of(logic._prepare_target_click).count("_send_click") == 1


def test_observation_models_still_carry_no_executor_action():
    """4. Slice 1's rule, re-checked for the new types: evidence can never carry an action."""
    for kind in (ActionTarget, Stale, NotEligible):
        annotations = {str(f.type) for f in kind.__dataclass_fields__.values()}
        for forbidden in ("ExecutorAction", "RiskLevel", "Action", "SafetyDecision", "Callable"):
            assert not any(forbidden in a for a in annotations), f"{kind.__name__} can carry {forbidden}"
    imported = set()
    for node in ast.walk(ast.parse((settings.PROJECT_ROOT / "app" / "verifier" / "models.py")
                                   .read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not any(name.startswith(("app.executor", "app.safety")) for name in imported), imported


# =====================================================================================================
# RE-IDENTIFICATION (matrix 5-11)
# =====================================================================================================

def test_the_same_control_is_reidentified(world):
    """5."""
    found = observation.reidentify(target(), observed())
    assert isinstance(found, ActionTarget)
    assert found.point == CENTRE and found.bounds == BUTTON_BOUNDS
    assert found.identity == observation._IDENTITY_WITH_AUTOMATION_ID


def test_a_changed_runtime_id_alone_does_not_refuse(world):
    """6. The audited policy, applied: pywinauto documents runtime_id as "may be different from run to
    run", so a changed runtime id is NOT evidence of a different control. Refusing on it would make this
    feature fail at random for a reason the platform never promised to avoid."""
    world.matches = [element(runtime_id="99-1")]      # same control, new runtime id
    found = observation.reidentify(target(), observed(runtime_id="42-7"))
    assert isinstance(found, ActionTarget), getattr(found, "reason", found)


def test_runtime_id_is_not_what_decides_identity():
    """6. Proved against the code, not the comment: runtime_id is absent from the identity decision."""
    assert "runtime_id" not in code_of(observation._identity_match)
    assert "automation_id" in code_of(observation._identity_match)


def test_a_matching_runtime_id_cannot_rescue_a_different_control(world):
    """6. The converse, which is the one that matters for safety: the stronger evidence wins."""
    world.matches = [element(automation_id="cancelBtn")]
    found = observation.reidentify(target(), observed(automation_id="loginBtn"))
    assert isinstance(found, Stale)


def test_a_different_control_type_is_stale(world):
    """6."""
    world.matches = [element(control_type="Edit", automation_id="")]
    assert isinstance(observation.reidentify(target(), observed(automation_id="")), Stale)


def test_a_control_without_an_automation_id_names_its_weaker_evidence(world):
    """6. Many native controls expose no automation id. That match is genuinely weaker, so it is named
    rather than quietly treated as equivalent."""
    world.matches = [element(automation_id="")]
    found = observation.reidentify(target(), observed(automation_id=""))
    assert isinstance(found, ActionTarget)
    assert found.identity == observation._IDENTITY_STRUCTURAL_ONLY


def test_the_window_being_gone_is_a_refusal(world):
    """7."""
    world.uia_error = "the window could not be read (COMError)"
    found = observation.reidentify(target(), observed())
    assert isinstance(found, Unavailable) and "couldn't read that window" in found.reason


def test_the_element_being_gone_is_a_refusal(world):
    """8."""
    world.matches = []
    found = observation.reidentify(target(), observed())
    assert isinstance(found, NotFound) and "isn't in that window any more" in found.message


def test_two_current_matches_refuse_without_guessing(world):
    """9. Nothing is chosen, and the candidates carry no labels."""
    world.matches = [element(automation_id="a"), element(automation_id="b", bounds=(10, 10, 60, 30))]
    found = observation.reidentify(target(), observed())
    assert isinstance(found, Ambiguous) and len(found.candidates) == 2
    assert "not going to guess" in found.message
    assert not any(hasattr(candidate, "name") for candidate in found.candidates)


def test_a_stale_observation_is_still_reidentified_the_same_way(world):
    """10. Staleness changes nothing about HOW re-identification happens - it is always required, so
    there is no path that acts on a stale observation and no path that skips the re-read."""
    old = observed(observed_at=time.time() - 3600)
    assert observation.is_fresh(old) is False
    found = observation.reidentify(target(), old)
    assert isinstance(found, ActionTarget)
    assert found.reidentified_at > old.observed_at


def test_a_fresh_observation_is_reidentified_too(world):
    """11. Freshness is not a licence to skip the re-read: the click path calls reidentify() with no
    freshness condition in front of it at all."""
    fresh = observed()
    assert observation.is_fresh(fresh) is True
    source = code_of(logic._prepare_target_click)
    assert "reidentify" in source
    assert "is_fresh" not in source, "re-identification must not be conditional on freshness"


def test_the_executor_reidentifies_after_the_confirmation_not_before(world):
    """11. The required order: observe, resolve, Safety, confirmation, re-identify, act."""
    order = []

    def confirm(action, assessment):
        order.append("confirm")
        return True

    real = observation.reidentify
    def recording(t, o):
        order.append("reidentify")
        return real(t, o)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(logic.observation, "reidentify", recording)
        def recorded_click(x, y):
            order.append("click")
            world.clicks.append((x, y))
            world.pointer = (x, y)

        patch.setattr(adapter, "click", recorded_click)
        result = logic.click_target(target(), observed(), confirm)
    assert result.ok, result.message
    assert order == ["confirm", "reidentify", "click"]


def test_the_bounds_shown_to_the_user_are_never_the_bounds_clicked(world):
    """11 + 20. The control moves between the observation and the answer; the click follows it."""
    moved = (500, 100, 600, 140)         # centre (550, 120)
    world.matches = [element(bounds=moved)]
    result = logic.click_target(target(), observed(bounds=BUTTON_BOUNDS), always(True))
    assert result.ok, result.message
    assert world.clicks == [(550, 120)]
    assert CENTRE not in world.clicks


# =====================================================================================================
# ELIGIBILITY (matrix 12-16)
# =====================================================================================================

def test_an_enabled_onscreen_control_is_action_ready(world):
    """12."""
    assert isinstance(observation.reidentify(target(), observed()), ActionTarget)


@pytest.mark.parametrize("overrides, expect", [
    (dict(enabled=False), "greyed out"),                       # 13
    (dict(offscreen=True), "out of view"),                     # 14
    (dict(bounds=None), "can't tell where"),                   # 15
    (dict(bounds=(300, 400, 300, 440)), "can't tell where"),   # 15: no width
    (dict(bounds=(300, 400, 400, 400)), "can't tell where"),   # 15: no height
    (dict(bounds=(400, 440, 300, 400)), "can't tell where"),   # 15: inverted
])
def test_an_ineligible_control_is_refused(world, overrides, expect):
    """13-15. Every one of these refuses; none of them is adjusted into something clickable."""
    world.matches = [element(**overrides)]
    found = observation.reidentify(target(), observed(**overrides))
    assert isinstance(found, NotEligible), found
    assert expect in found.reason


def test_a_point_outside_the_current_window_is_refused(world):
    """16. The control claims a position its own window does not contain. The evidence contradicts
    itself, and the one thing not to do is click where it points."""
    world.matches = [element(bounds=(1000, 900, 1100, 940))]
    world.window_bounds = WINDOW_BOUNDS
    found = observation.reidentify(target(), observed(bounds=(1000, 900, 1100, 940)))
    assert isinstance(found, NotEligible) and "outside its own window" in found.reason


def test_an_unreadable_window_rectangle_does_not_block_a_good_target(world):
    """16. UIA exposing no rectangle for the window is missing evidence, not contradictory evidence, so
    the containment rule simply does not apply - it must not refuse every click instead."""
    world.window_bounds = None
    assert isinstance(observation.reidentify(target(), observed()), ActionTarget)


def test_a_target_in_another_window_is_refused(world):
    """The target and the observation must agree about which window they are talking about."""
    found = observation.reidentify(Target("Login", 999), observed())
    assert isinstance(found, Unavailable) and "different window" in found.reason


@pytest.mark.parametrize("bad", [None, "Login", 7, Target("", WINDOW), Target("   ", WINDOW)])
def test_a_missing_or_empty_target_is_refused(world, bad):
    found = observation.reidentify(bad, observed())
    assert isinstance(found, (Unavailable, NotFound)), found
    assert world.clicks == []


def test_a_non_uia_observation_is_refused(world):
    """Only the layer that exists may be re-identified. A later layer's evidence is not silently
    accepted by a function that only knows how to re-read a UIA tree."""
    from dataclasses import replace
    found = observation.reidentify(target(), replace(observed(), source=ObservationSource.OCR))
    assert isinstance(found, Unavailable) and "ocr" in found.reason


# =====================================================================================================
# PROVENANCE (matrix 17-20)
# =====================================================================================================

def test_the_point_is_the_centre_of_the_current_bounds(world):
    """17."""
    world.matches = [element(bounds=(100, 200, 140, 260))]
    found = observation.reidentify(target(), observed(bounds=(100, 200, 140, 260)))
    assert found.point == (120, 230)
    assert found.bounds == (100, 200, 140, 260)


def test_the_result_keeps_its_uia_provenance(world):
    """18 + 19. A point backed by UIA evidence is not the Phase 5 coordinate fallback, and must never
    be relabelled as one - that layer does not exist yet, and when it does it will be weaker."""
    found = observation.reidentify(target(), observed())
    assert found.source is ObservationSource.UIA
    assert found.source is not ObservationSource.COORDINATE
    assert "COORDINATE" not in code_of(observation._action_target)


def test_the_coordinate_layer_is_still_unimplemented():
    """19. Declared in the frozen hierarchy, produced by nothing.

    DOM was removed from this list when layer 2 was built; the point of the test is unchanged, and it
    is the coordinate fallback in particular that must never be reached for while a higher layer
    works."""
    module = code_of_module(settings.PROJECT_ROOT / "app" / "verifier" / "observation.py")
    for later in ("ObservationSource.OCR", "ObservationSource.VISION",
                  "ObservationSource.COORDINATE"):
        assert later not in module, f"{later} is a later slice"
    # the UIA bridge in particular must still never relabel its own evidence
    assert "COORDINATE" not in code_of(observation._action_target)


def test_the_target_name_can_only_come_from_the_user(world):
    """33 + privacy. The name in the bridge is the one the USER gave, carried through from their Target.
    It is never taken from the matched element - which has no name field to take it from."""
    found = observation.reidentify(Target("  Login  ", WINDOW), observed())
    assert found.target_name == "  Login  "               # the user's own words, untouched
    assert not hasattr(element(), "name")
    assert "name" not in {f.name for f in Observed.__dataclass_fields__.values()}
    source = code_of(observation._action_target)
    assert "target.name" in source
    assert "current.name" not in source and "element.name" not in source


def test_the_identity_label_names_evidence_and_never_its_values(world):
    """The audit trail says HOW strongly a control was matched without repeating anything read off the
    screen: no automation id value, no class name value, no label."""
    found = observation.reidentify(target(), observed())
    for value in ("loginBtn", "Button", "42-7"):
        assert value not in found.identity


# =====================================================================================================
# SAFETY (matrix 21-24)
# =====================================================================================================

def test_a_uia_click_is_medium_risk_and_is_confirmed(world):
    """21 + 22. Precision is not permission: knowing exactly which button is under the pointer says
    nothing about what that button does."""
    result = logic.click_target(target(), observed(), always(True))
    assert result.ok, result.message
    (description, level, confirmed) = world.prompts[-1]
    assert level is RiskLevel.MEDIUM and confirmed is True
    assert logic._TARGET_CLICK_RISK is RiskLevel.MEDIUM
    assert logic._TARGET_CLICK_RISK >= logic._CLICK_RISK


def test_uia_precision_cannot_lower_the_risk(world):
    """21. Not even an explicit LOW advisory floor from the Brain can soften it."""
    result = logic.click_target(target(), observed(), always(True), risk_floor=RiskLevel.LOW)
    assert world.prompts[-1][1] is RiskLevel.MEDIUM
    assert result.ok


def test_a_risky_word_in_the_users_own_target_can_only_raise(world):
    """21. The keyword rule still reads the description, and the floor can never lower what it finds."""
    result = logic.click_target(Target("Delete", WINDOW), observed(), always(True))
    assert world.prompts[-1][1] >= RiskLevel.MEDIUM
    assert result.ok


@pytest.mark.parametrize("answer", [False, None, "yes", "y", 1, "", object()])
def test_anything_other_than_true_cancels(world, answer):
    """23. The gate already requires exactly True; this proves the new entry point did not weaken it."""
    with pytest.raises(safety_logic.ActionDeniedError):
        logic.click_target(target(), observed(), always(answer))
    assert world.clicks == []


def test_no_confirmation_method_denies(world):
    """23."""
    with pytest.raises(safety_logic.ActionDeniedError):
        logic.click_target(target(), observed())
    assert world.clicks == []


def test_a_broken_confirmation_denies(world):
    """23."""
    def confirm(action, assessment):
        raise RuntimeError("the prompt broke")

    with pytest.raises(safety_logic.ActionDeniedError):
        logic.click_target(target(), observed(), confirm)
    assert world.clicks == []


def test_a_command_typed_instead_of_yes_does_not_bypass_the_confirmation(world):
    """24. A Safety confirmation is not a place to issue a new command. The gate sees something that is
    not True and denies; it does not defer, queue or re-interpret it."""
    for typed in ("click Login", "open notepad", "cancel", "stop"):
        with pytest.raises(safety_logic.ActionDeniedError):
            logic.click_target(target(), observed(), always(typed))
    assert world.clicks == []


def test_the_confirmation_names_the_users_word_and_the_window(world):
    """9 of the brief. The prompt may identify the target by the user's OWN name. Nothing read off the
    screen is in it: the labels that made the match never left the verifier's adapter."""
    logic.click_target(target(), observed(), always(True))
    description = world.prompts[-1][0]
    assert description == 'click "Login" in window "Untitled - Notepad"'


def test_the_confirmation_survives_a_window_with_no_title(world):
    world.windows = [WindowInfo(WINDOW, "")]
    logic.click_target(target(), observed(), always(True))
    assert world.prompts[-1][0] == 'click "Login" in a window with no readable title'


def test_the_gate_is_asked_before_anything_is_read_again(world):
    """The confirmation is not shown AFTER the work: nothing touches UIA before the gate answers."""
    reads = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(verifier_adapter, "uia_find_by_name",
                      lambda *a, **k: reads.append("read") or [element()])

        def confirm(action, assessment):
            assert reads == [], "UIA was re-read before the user answered"
            return True

        logic.click_target(target(), observed(), confirm)
    assert reads == ["read"]


# =====================================================================================================
# EXECUTION (matrix 25-29)
# =====================================================================================================

def test_a_successful_flow_clicks_exactly_once(world):
    """25 + 28."""
    result = logic.click_target(target(), observed(), always(True))
    assert result.ok and world.clicks == [CENTRE]
    assert result.action.kind == CLICK


def test_the_click_is_unverified_not_done(world):
    """11 of the brief. Clicking Save does not prove a file was saved. Re-identification proves WHERE
    the click landed, never what it achieved."""
    result = logic.click_target(target(), observed(), always(True))
    assert result.outcome is Outcome.UNVERIFIED
    assert result.ok is True and result.verified is False
    assert "can't check what the click did" in result.message


@pytest.mark.parametrize("overrides, nothing", [
    (dict(enabled=False), "disabled"),
    (dict(offscreen=True), "offscreen"),
    (dict(bounds=None), "no bounds"),
])
def test_an_ineligible_target_never_reaches_the_executor(world, overrides, nothing):
    """27."""
    world.matches = [element(**overrides)]
    result = logic.click_target(target(), observed(**overrides), always(True))
    assert not result.ok and world.clicks == [], nothing


@pytest.mark.parametrize("break_it, expect", [
    (dict(matches=[]), "isn't in that window any more"),
    (dict(matches=[element(automation_id="other")]), "isn't the one I found"),
    (dict(uia_error="gone"), "couldn't read that window"),
])
def test_a_failed_reidentification_never_reaches_the_executor(world, break_it, expect):
    """26."""
    for key, value in break_it.items():
        setattr(world, key, value)
    result = logic.click_target(target(), observed(), always(True))
    assert not result.ok and world.clicks == []
    assert expect in result.message


def test_two_matches_after_the_confirmation_refuse_rather_than_click(world):
    """26 + 9. The dialog grew a second Login while the user was reading the prompt."""
    world.matches = [element(automation_id="a"), element(automation_id="b")]
    result = logic.click_target(target(), observed(), always(True))
    assert not result.ok and world.clicks == []
    assert "not going to guess" in result.message


def test_an_overlapping_window_refuses_the_click(world):
    """UIA reports where a control IS, not whether anything is in front of it. IsOffscreen is not set
    merely because another window covers a control, so a click aimed at a covered button would land in
    whatever is on top. Not in the brief's list - added because the gap is real."""
    world.windows = [WindowInfo(WINDOW, "Untitled - Notepad"), WindowInfo(5200, "Something Else")]
    world.on_top = 5200
    result = logic.click_target(target(), observed(), always(True))
    assert not result.ok and world.clicks == []
    assert "in front of" in result.message


def test_a_point_off_every_screen_refuses(world):
    world.matches = [element(bounds=(5000, 5000, 5100, 5040))]
    world.window_bounds = (4000, 4000, 6000, 6000)
    result = logic.click_target(target(), observed(bounds=(5000, 5000, 5100, 5040)), always(True))
    assert not result.ok and world.clicks == []
    assert "isn't on any screen" in result.message


def test_a_point_in_a_fail_safe_corner_refuses(world):
    world.matches = [element(bounds=(0, 0, 1, 1))]
    result = logic.click_target(target(), observed(bounds=(0, 0, 1, 1)), always(True))
    assert not result.ok and world.clicks == []
    assert "emergency stop" in result.message


def test_a_window_that_closed_before_the_confirmation_refuses(world):
    """The pre-confirmation check: there is no point asking the user about a window that is gone."""
    world.windows = []
    result = logic.click_target(target(), observed(), always(True))
    assert not result.ok and world.clicks == [] and world.prompts == []
    assert "isn't open any more" in result.message


def test_no_pywinauto_action_method_is_ever_called(world):
    """29. Nothing in a whole successful flow touches the acting half of pywinauto - proved by running
    it with every forbidden name installed as a tripwire on the fake element."""
    class Tripwire(UiaElement):
        pass

    tripped = []

    def forbid(name):
        def boom(*args, **kwargs):
            tripped.append(name)
            raise AssertionError(f"the Verifier called pywinauto's {name}()")
        return boom

    for name in FORBIDDEN_UIA_ACTIONS:
        setattr(Tripwire, name, property(lambda self, n=name: forbid(n)()))
    world.matches = [Tripwire(**{f.name: getattr(element(), f.name)
                                 for f in UiaElement.__dataclass_fields__.values()})]
    result = logic.click_target(target(), observed(), always(True))
    assert result.ok, result.message
    assert tripped == []


def test_the_pointer_check_still_applies(world):
    """The shared physical click keeps its after-the-fact check for both kinds of click."""
    world.pointer = (10, 10)          # the pointer ended up somewhere else
    result = logic.click_target(target(), observed(), always(True))
    assert not result.ok
    assert "may have landed somewhere else" in result.message


def test_the_coordinate_click_messages_are_unchanged(world):
    """The messages were tuned during Phase 1 acceptance. Sharing one physical click between the two
    paths must not have reworded the old one by a single character."""
    from app.executor.models import ExecutorAction
    result = logic.execute(ExecutorAction(CLICK, "350, 420"), always(True))
    assert result.message == "Clicked at (350, 420). I can't check what the click did."
    assert result.outcome is Outcome.UNVERIFIED


# =====================================================================================================
# PRIVACY (matrix 30-33)
# =====================================================================================================

def test_no_accessible_name_is_returned_or_logged(world, caplog):
    """30 + 32. The only names in play are the user's own. Nothing read off the screen is returned,
    and the logs carry counts and layer names only."""
    caplog.set_level("DEBUG")
    world.matches = [element(automation_id="a"), element(automation_id="b")]
    observation.reidentify(target(), observed())
    world.matches = [element()]
    logic.click_target(target(), observed(), always(True))

    logged = " ".join(record.getMessage() for record in caplog.records)
    for secret in ("Login", "loginBtn", "Untitled - Notepad", "Notepad"):
        assert secret not in logged, f"{secret!r} reached the logs: {logged}"
    assert "source=uia" in logged and "candidates=" in logged


def test_no_value_document_or_password_content_is_read(world):
    """31. Checked against the stripped code of the UIA reads, because this project has four times
    written a substring assertion that matched its own explanation of the rule."""
    reads = code_of(verifier_adapter.uia_find_by_name) + code_of(verifier_adapter.uia_window_bounds) \
        + code_of(observation.reidentify) + code_of(observation._action_target)
    for forbidden in ("CurrentValue", "GetValue", "ValuePattern", "TextPattern", "DocumentRange",
                      "CurrentPassword", "GetSelection", "rich_text", "window_text"):
        assert forbidden not in reads, forbidden


def test_a_password_control_keeps_its_structure_and_never_its_value(world):
    """31 + 5 of the brief. A password field can still be found and clicked - clicking a box is how a
    person starts typing in it. Its VALUE is never read, under any flag, because nothing asks."""
    world.matches = [element(is_password=True)]
    found = observation.reidentify(target("Password"), observed(is_password=True))
    assert isinstance(found, ActionTarget)
    assert not any("value" in f.name or "text" in f.name
                   for f in ActionTarget.__dataclass_fields__.values())


def test_nothing_observed_can_reach_the_provider():
    """32. The observation layer still imports no Brain and no provider client, so there is no path from
    a screen read to a prompt."""
    for name in ("observation.py", "models.py", "adapter.py", "logic.py"):
        source = (settings.PROJECT_ROOT / "app" / "verifier" / name).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            module = node.module if isinstance(node, ast.ImportFrom) else None
            if isinstance(node, ast.Import):
                module = ",".join(a.name for a in node.names)
            assert not (module or "").startswith(("app.brain", "anthropic")), f"{name}: {module}"


def test_the_bridge_carries_no_screen_content(world):
    """30. Every field of the bridge is a number, a provenance label, the user's own word, or the name
    of a kind of evidence. There is nowhere to put a label, a value or a tree."""
    found = observation.reidentify(target(), observed())
    assert set(ActionTarget.__dataclass_fields__) == {
        "source", "window_handle", "bounds", "point", "target_name", "reidentified_at", "identity"}
    assert found.target_name == "Login"


# =====================================================================================================
# EMERGENCY STOP + ISOLATION (matrix 34-39)
# =====================================================================================================

def test_the_emergency_stop_blocks_the_click_before_anything_happens(world):
    """34."""
    emergency_stop.trigger("test")
    with pytest.raises(EmergencyStopError):
        logic.click_target(target(), observed(), always(True))
    assert world.clicks == [] and world.prompts == []


def test_the_emergency_stop_during_reidentification_aborts(world):
    """35. The stop is checked again after the gate and before the re-identification, which is the one
    step here that is allowed to take a moment."""
    def confirm(action, assessment):
        emergency_stop.trigger("mid-confirmation")      # pressed while the prompt was up
        return True

    with pytest.raises(EmergencyStopError):
        logic.click_target(target(), observed(), confirm)
    assert world.clicks == []


def test_the_stop_is_checked_before_the_reidentification_read(world):
    """35."""
    reads = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(verifier_adapter, "uia_find_by_name",
                      lambda *a, **k: reads.append("read") or [element()])
        emergency_stop.trigger("already stopped")
        with pytest.raises(EmergencyStopError):
            logic.click_target(target(), observed(), always(True))
    assert reads == [], "UIA was re-read after the emergency stop"


def test_there_is_only_one_stop_mechanism():
    """10 of the brief. The new path reuses emergency_stop and defines no second one."""
    source = code_of(logic._prepare_target_click) + code_of(logic.click_target)
    assert source.count("emergency_stop.check()") == 2
    assert "observation" in code_of(logic._prepare_target_click)
    assert "emergency" not in code_of_module(settings.PROJECT_ROOT / "app" / "verifier" / "observation.py")


def test_an_offline_test_cannot_perform_real_uia():
    """36. The guard is real: the genuine adapter functions are refused, and the library is blocked at
    import. Asked WITHOUT the world fixture, so nothing is faked."""
    from safety_guards import PhysicalDesktopEscaped
    for name in ("uia_find_by_name", "uia_window_bounds"):
        with pytest.raises(PhysicalDesktopEscaped):
            getattr(verifier_adapter, name)(WINDOW, "login")
    with pytest.raises(PhysicalDesktopEscaped):
        import pywinauto  # noqa: F401


def test_an_offline_test_cannot_perform_a_real_click():
    """37."""
    from safety_guards import PhysicalDesktopEscaped
    with pytest.raises(PhysicalDesktopEscaped):
        adapter.click(10, 10)


def test_the_new_uia_read_is_centrally_guarded():
    """36. Registered in the one place, not guarded by this test file remembering to."""
    import safety_guards
    assert "uia_window_bounds" in safety_guards.VERIFIER_CONTENT_READS
    assert "uia_find_by_name" in safety_guards.VERIFIER_CONTENT_READS
