"""
Phase 5 Slice 1: the observation foundation and local UIA target resolution.

ONE LAYER, ONE QUESTION. "Which control in this window is the one the user called X?", answered through
Windows UI Automation and nothing else. There is no fall-through hierarchy yet: DOM, OCR, vision and
coordinate fallback are later slices of the frozen hierarchy and are deliberately not stubbed here.

THE PRIVACY PROPERTY THESE TESTS EXIST TO PIN. A UIA accessible name is user-visible content, and it is
also the only way to find a target the user named. So the name is read, normalised and compared INSIDE
app/verifier/adapter.py and never crosses that boundary - `UiaElement` and `Observed` have no name field
at all. An unmatched label therefore cannot be returned, logged, persisted or sent anywhere, because it
never leaves the adapter.

Nothing real is observed. The root guard refuses the real UIA read and blocks importing pywinauto, so
every test here drives the real resolver against a fake tree.
"""
import ast
import logging
import time

import pytest

import safety_guards
from app.verifier import adapter as verifier_adapter
from app.verifier import observation
from app.verifier.models import (Ambiguous, Found, NotFound, Observed, ObservationSource, Sensitivity,
                                 Target, UiaElement, Unavailable, normalize_name)
from config import settings
from config.settings import SettingsError

CONFIG = ("observation:\n"
          "  snapshot_timeout_seconds: 2.0\n"
          "  freshness_seconds: 2.0\n")


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A fake accessibility tree: names in, structural elements out - the adapter's real contract.

    `install(...)` takes {accessible name: [elements]}, so a test can give one window two controls with
    the same label, or none, or make the whole read fail."""
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_PATH", config)
    asked = []

    class Tree:
        def __init__(self):
            self.by_name = {}
            self.error = None
            self.asked = asked

        def install(self, by_name=None, error=None):
            self.by_name = by_name or {}
            self.error = error

            def find(window_handle, normalized_name, timeout_seconds=2.0):
                asked.append((window_handle, normalized_name, timeout_seconds))
                if self.error is not None:
                    raise self.error
                return list(self.by_name.get(normalized_name, ()))

            monkeypatch.setattr(verifier_adapter, "uia_find_by_name", find)
            return self

    return Tree()


def element(**overrides):
    base = dict(window_handle=500, control_type="Button", runtime_id="42-7",
                automation_id="loginButton", class_name="Button",
                bounds=(10, 20, 110, 50), enabled=True, focused=False, offscreen=False,
                is_password=False, patterns=("Invoke",))
    return UiaElement(**{**base, **overrides})


def resolve(name="Login", window=500):
    return observation.resolve_target(Target(name=name, window_handle=window))


# --- 1-4. Types and boundaries ------------------------------------------------------------------------

def test_the_observation_sources_are_the_frozen_hierarchy_in_order():
    """1. All five layers are declared so the typed model does not change shape when they arrive - and
    so a result can always say which layer answered."""
    assert [source.value for source in ObservationSource] == ["uia", "dom", "ocr", "vision", "coordinate"]


def test_the_sensitivity_classes_are_typed_and_complete():
    """1. The privacy policy is written down once, as a type, before anything can produce the classes
    that do not exist yet."""
    assert [level.value for level in Sensitivity] == ["structural", "interaction", "content", "secret",
                                                      "pixels", "untrusted"]


def test_no_observation_type_can_carry_an_action():
    """2, and §11. Observation is evidence, not intent: there is no field anywhere that could hold an
    ExecutorAction, a plan or a risk level."""
    from app.verifier import models
    for name in ("Target", "Observed", "Found", "Ambiguous", "NotFound", "Unavailable", "UiaElement"):
        shape = getattr(models, name)
        annotations = " ".join(str(field.type) for field in shape.__dataclass_fields__.values())
        for forbidden in ("ExecutorAction", "Action", "Plan", "PlanStep", "RiskLevel", "Intent"):
            assert forbidden not in annotations, f"{name} can carry {forbidden}"


def test_the_observation_layer_imports_no_action_authority():
    """3. It cannot act, cannot plan, cannot authorise, and cannot reach the model."""
    source = (settings.PROJECT_ROOT / "app" / "verifier" / "observation.py").read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    for forbidden in ("app.executor", "app.planner", "app.safety", "app.brain", "app.memory"):
        assert not any(name.startswith(forbidden) for name in imported), f"{forbidden}: {imported}"
    assert "pywinauto" not in imported and "comtypes" not in imported
    assert "app.verifier.adapter" in imported or "app.verifier" in imported


def test_only_the_adapter_boundary_imports_the_uia_library():
    """4. pywinauto is reached from exactly one file, and lazily - importing the adapter must not load a
    UI Automation stack."""
    root = settings.PROJECT_ROOT / "app"
    importers = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("pywinauto"):
                importers.append(path.relative_to(root).as_posix())
            elif isinstance(node, ast.Import) and any(a.name.startswith("pywinauto") for a in node.names):
                importers.append(path.relative_to(root).as_posix())
    assert importers == ["verifier/adapter.py"], importers

    module = ast.parse((root / "verifier" / "adapter.py").read_text(encoding="utf-8"))
    top_level = {alias.name for node in module.body if isinstance(node, ast.Import)
                 for alias in node.names}
    top_level |= {node.module or "" for node in module.body if isinstance(node, ast.ImportFrom)}
    assert not any(name.startswith("pywinauto") for name in top_level), "the import is not lazy"


# --- 5-12. Local name matching ------------------------------------------------------------------------

def test_a_user_named_target_resolves_to_one_control(tree):
    """5."""
    tree.install({"login": [element()]})
    outcome = resolve("Login")
    assert isinstance(outcome, Found), outcome
    assert outcome.observed.source is ObservationSource.UIA
    assert outcome.observed.automation_id == "loginButton"


@pytest.mark.parametrize("typed", ["login", "LOGIN", "Login", "LoGiN"])
def test_matching_ignores_case(tree, typed):
    """6."""
    tree.install({"login": [element()]})
    assert isinstance(resolve(typed), Found)


@pytest.mark.parametrize("typed", ["  Login  ", "Login\t", " log  in "])
def test_matching_normalises_whitespace(tree, typed):
    """7. Surrounding and repeated whitespace collapse; "log  in" becomes "log in"."""
    tree.install({"login": [element()], "log in": [element()]})
    assert isinstance(resolve(typed), Found)


@pytest.mark.parametrize("typed", ["Log", "gin", "Log in now"])
def test_a_substring_does_not_match(tree, typed):
    """8. Exact normalised equality, so "Log" is not "Login" - guessing at a control is worse than
    asking."""
    tree.install({"login": [element()]})
    assert isinstance(resolve(typed), NotFound), typed


@pytest.mark.parametrize("typed", ["Loggin", "Logn", "Log-in", "Login…"])
def test_a_near_spelling_does_not_match(tree, typed):
    """9. No fuzzy matching, no edit distance, no model judgement."""
    tree.install({"login": [element()]})
    assert isinstance(resolve(typed), NotFound), typed


def test_two_controls_with_the_same_name_are_ambiguous(tree):
    """10. Nothing is chosen - not the first, not the topmost, not the enabled one."""
    tree.install({"login": [element(runtime_id="1-1", bounds=(0, 0, 50, 20)),
                            element(runtime_id="2-2", bounds=(0, 60, 50, 80))]})
    outcome = resolve("Login")
    assert isinstance(outcome, Ambiguous)
    assert len(outcome.candidates) == 2
    assert {candidate.runtime_id for candidate in outcome.candidates} == {"1-1", "2-2"}
    assert "guess" in outcome.message


def test_nothing_matching_is_not_found(tree):
    """11."""
    tree.install({"save": [element()]})
    outcome = resolve("Login")
    assert isinstance(outcome, NotFound)
    assert "Login" in outcome.message, "the user's own words may be echoed back"


def test_an_unreadable_tree_is_unavailable(tree):
    """12. What a window with no accessibility tree looks like - the signal a later slice escalates on,
    reported rather than guessed around."""
    tree.install(error=verifier_adapter.VerifierAdapterError("no accessibility tree"))
    outcome = resolve("Login")
    assert isinstance(outcome, Unavailable)
    assert "couldn't read" in outcome.reason


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_an_empty_target_name_is_refused_without_looking(tree, bad):
    tree.install({"login": [element()]})
    assert isinstance(observation.resolve_target(Target(name=bad, window_handle=500)), NotFound)
    assert tree.asked == [], "it read the screen anyway"


@pytest.mark.parametrize("window", [0, -1, None])
def test_no_window_is_unavailable_without_looking(tree, window):
    tree.install({"login": [element()]})
    assert isinstance(observation.resolve_target(Target(name="Login", window_handle=window)), Unavailable)
    assert tree.asked == []


# --- 13-18. Privacy -----------------------------------------------------------------------------------

def test_an_unmatched_name_cannot_be_returned_because_it_never_leaves_the_adapter(tree):
    """13, and the structural reason it holds: UiaElement and Observed have no name field, so there is
    nothing for a result to carry even if a caller wanted it."""
    assert "name" not in UiaElement.__dataclass_fields__
    assert "name" not in Observed.__dataclass_fields__
    tree.install({"save": [element()], "delete account": [element()], "login": [element()]})
    outcome = resolve("Login")
    text = repr(outcome)
    for other in ("save", "Save", "delete", "Delete account"):
        assert other.lower() not in text.lower(), f"{other!r} leaked into the result"


def test_the_adapter_compares_names_and_returns_none_of_them(tree):
    """13's other half: the adapter is asked with the NORMALISED name and answers with structure."""
    tree.install({"login": [element()]})
    resolve("  LogIn ")
    assert tree.asked == [(500, "login", 2.0)], tree.asked


def test_no_accessible_name_is_logged(tree, caplog):
    """14, and §10. Logs carry the layer, the resolution and a count - never a label."""
    tree.install({"login": [element()], "save": [element()]})
    with caplog.at_level(logging.DEBUG):
        resolve("Login")
        tree.install({"login": [element(), element(runtime_id="9-9")]})
        resolve("Login")
        tree.install({})
        resolve("Delete Account")
    written = "\n".join(record.getMessage() for record in caplog.records)
    for label in ("login", "Login", "save", "Delete Account", "delete account"):
        assert label not in written, f"{label!r} was logged"
    assert "source=uia" in written and "escalation=false" in written
    assert "resolution=found" in written and "resolution=ambiguous" in written
    assert "resolution=not_found" in written


def uia_code() -> str:
    """The Phase 5 UIA functions as CODE, with docstrings and comments stripped.

    Scoped and stripped on purpose: the adapter is an old file whose other functions legitimately
    mention "chrome.exe" in an example and whose prose explains what is never read. A substring search
    over the whole file matches that prose rather than any behaviour - the mistake this test exists to
    avoid making."""
    module = ast.parse((settings.PROJECT_ROOT / "app" / "verifier" / "adapter.py")
                       .read_text(encoding="utf-8"))
    uia = [node for node in module.body
           if isinstance(node, ast.FunctionDef)
           and (node.name.startswith("uia_") or node.name.startswith("_uia_")
                or node.name == "_is_password")]
    assert uia, "the UIA functions moved"
    for node in uia:                      # drop every docstring before unparsing
        for inner in ast.walk(node):
            body = getattr(inner, "body", None)
            if not (isinstance(body, list) and body and isinstance(body[0], ast.Expr)):
                continue
            first = body[0].value
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                body.pop(0)
    return chr(10).join(ast.unparse(node) for node in uia)


def test_no_value_or_document_text_is_ever_requested():
    """15. The adapter reads structure and names, never a VALUE. Note the distinction this test has to
    make: asking whether a Value pattern is AVAILABLE is structural metadata and is allowed; asking it
    for its value is not."""
    code = uia_code()
    for forbidden in ("GetCurrentPropertyValue", "CurrentValue", "GetValuePattern", "GetTextPattern",
                      "get_value", "window_text", "texts()", "legacy_properties", "DocumentRange"):
        assert forbidden not in code, f"the adapter reads {forbidden}"
    assert "CurrentIsValuePatternAvailable" in code, "availability metadata is still read"
    assert "CurrentIsTextPatternAvailable" in code


def test_a_password_controls_value_is_never_read(tree):
    """16 and 17, and §5. The structure is evidence; the value is never read, and no flag can change
    that in this phase - there is no code path that asks for it."""
    tree.install({"password": [element(control_type="Edit", is_password=True, automation_id="pwd")]})
    outcome = resolve("Password")
    assert isinstance(outcome, Found)
    assert outcome.observed.is_password is True, "the structural flag is kept for later refusals"
    assert outcome.observed.automation_id == "pwd"
    assert "value" not in Observed.__dataclass_fields__ and "text" not in Observed.__dataclass_fields__
    source = (settings.PROJECT_ROOT / "app" / "verifier" / "adapter.py").read_text(encoding="utf-8")
    assert "CurrentIsPassword" in source, "it detects a password control"
    assert "CurrentPassword" not in source


def test_no_observation_reaches_a_brain_request(tree):
    """18, and §0C. Nothing observed is put into a provider request: the Brain's request builders take
    the user's text and at most a PreviousActionContext, and neither the Brain nor the observation layer
    imports the other."""
    from app.brain import logic as brain
    tree.install({"login": [element(automation_id="secretButtonId")]})
    found = resolve("Login")
    built = brain.interpretation_request("click the login button", None)
    assert "secretButtonId" not in built and "42-7" not in built
    brain_source = (settings.PROJECT_ROOT / "app" / "brain" / "logic.py").read_text(encoding="utf-8")
    assert "observation" not in brain_source and "app.verifier" not in brain_source
    assert isinstance(found, Found)


# --- 19-24. Structural evidence is preserved ----------------------------------------------------------

def test_the_bounding_rectangle_is_preserved(tree):
    """19. Needed by a later coordinate layer, and the first thing a moved window invalidates."""
    tree.install({"login": [element(bounds=(100, 200, 260, 240))]})
    assert resolve().observed.bounds == (100, 200, 260, 240)


@pytest.mark.parametrize("field, value", [("enabled", False), ("focused", True), ("offscreen", True)])
def test_state_flags_are_preserved(tree, field, value):
    """20, 21 and 22, and §6. This slice performs no action, so the flags that a later action must
    refuse or recover on have to survive the resolution."""
    tree.install({"login": [element(**{field: value})]})
    assert getattr(resolve().observed, field) is value


def test_supported_patterns_are_preserved(tree):
    """23. Structural metadata - which patterns exist, never what they would return."""
    tree.install({"login": [element(patterns=("Invoke", "Toggle"))]})
    assert resolve().observed.patterns == ("Invoke", "Toggle")


def test_the_identity_to_re_resolve_by_is_preserved(tree):
    """24. UIA elements go stale silently, so the identity matters more than the rectangle."""
    tree.install({"login": [element(runtime_id="7-13-9", automation_id="loginButton",
                                    class_name="Button", control_type="Button")]})
    observed = resolve().observed
    assert (observed.runtime_id, observed.automation_id, observed.class_name, observed.control_type) == \
        ("7-13-9", "loginButton", "Button", "Button")
    assert observed.window_handle == 500


def test_missing_identity_degrades_rather_than_failing(tree):
    """Not every control has an automation id or a runtime id. A missing one is not a reason to refuse
    to find a button."""
    tree.install({"login": [element(runtime_id="", automation_id="", bounds=None)]})
    outcome = resolve()
    assert isinstance(outcome, Found)
    assert outcome.observed.runtime_id == "" and outcome.observed.bounds is None


# --- 25-26. Freshness ---------------------------------------------------------------------------------

def test_a_just_taken_observation_is_fresh(tree, monkeypatch):
    """25."""
    tree.install({"login": [element()]})
    monkeypatch.setattr(observation, "_now", lambda: 1000.0)
    observed = resolve().observed
    assert observed.observed_at == 1000.0
    assert observation.is_fresh(observed) is True


def test_an_old_observation_is_stale(tree, monkeypatch):
    """26. freshness_seconds is 2.0 in this test's config."""
    tree.install({"login": [element()]})
    monkeypatch.setattr(observation, "_now", lambda: 1000.0)
    observed = resolve().observed
    assert observation.is_fresh(observed, now=1001.9) is True
    assert observation.is_fresh(observed, now=1002.5) is False, "it should be stale"


def test_an_unreadable_freshness_setting_fails_closed(tree, tmp_path, monkeypatch):
    """A broken setting makes an observation stale, never fresh."""
    tree.install({"login": [element()]})
    observed = resolve().observed
    broken = tmp_path / "broken.yaml"
    broken.write_text("observation:\n  snapshot_timeout_seconds: 2.0\n  freshness_seconds: 0\n",
                      encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_PATH", broken)
    assert observation.is_fresh(observed) is False


def test_nothing_polls_continuously():
    """§7. Freshness is asked; it is not watched."""
    source = (settings.PROJECT_ROOT / "app" / "verifier" / "observation.py").read_text(encoding="utf-8")
    for forbidden in ("while True", "time.sleep", "Thread", "threading"):
        assert forbidden not in source, f"observation.py contains {forbidden}"


# --- 27-34. Test isolation ----------------------------------------------------------------------------

def test_an_ordinary_test_cannot_perform_a_real_uia_read():
    """27 and 28. The real adapter function is replaced by a refuser, so a test that forgot to fake it
    finds out loudly instead of reading the user's screen."""
    with pytest.raises(safety_guards.PhysicalDesktopEscaped) as raised:
        verifier_adapter.uia_find_by_name(500, "login")
    assert "REAL SCREEN CONTENT" in str(raised.value)
    assert "login" not in str(raised.value), "the refusal echoed the name it was asked about"


def test_an_ordinary_test_cannot_import_the_uia_library():
    """27's stronger half: pywinauto is blocked at import, so no UI Automation stack can even load."""
    with pytest.raises(safety_guards.PhysicalDesktopEscaped):
        __import__("pywinauto")


@pytest.mark.parametrize("name", safety_guards.VERIFIER_CONTENT_READS)
def test_every_content_read_is_refused_in_an_ordinary_test(name):
    """29, and the Phase 4 gap this closes. read_text returns whatever is in the window that happens to
    be in front; selection and text_length describe it."""
    with pytest.raises(safety_guards.PhysicalDesktopEscaped):
        getattr(verifier_adapter, name)(500, "x")


def test_the_structural_reads_are_redirected_rather_than_refused():
    """The deliberate other half of the policy: structure redirects, content refuses. These say nothing
    about the user, and hundreds of tests need them."""
    assert verifier_adapter.modifier_keys_down() == []
    for name in ("list_windows", "active_target", "cursor_position"):
        assert not isinstance(getattr(verifier_adapter, name), type(None))
    assert "modifier_keys_down" not in safety_guards.VERIFIER_CONTENT_READS


def test_explicit_fakes_still_work(tree):
    """30. A test that is ABOUT one of these replaces it in its own body, which runs after the guard."""
    tree.install({"login": [element()]})
    assert isinstance(resolve(), Found)


class Node:
    def __init__(self, *markers):
        self.markers = set(markers)

    def get_closest_marker(self, name):
        return object() if name in self.markers else None


@pytest.mark.parametrize("marker, gate", sorted(safety_guards.DESKTOP_EXEMPT.items()))
def test_the_marker_alone_does_not_grant_real_observation(marker, gate, monkeypatch):
    """31."""
    monkeypatch.delenv(gate, raising=False)
    assert safety_guards.exempt(Node(marker), safety_guards.DESKTOP_EXEMPT) is False


@pytest.mark.parametrize("marker, gate", sorted(safety_guards.DESKTOP_EXEMPT.items()))
def test_the_gate_alone_does_not_grant_real_observation(marker, gate, monkeypatch):
    """32."""
    monkeypatch.setenv(gate, "1")
    assert safety_guards.exempt(Node(), safety_guards.DESKTOP_EXEMPT) is False


@pytest.mark.parametrize("marker, gate", sorted(safety_guards.DESKTOP_EXEMPT.items()))
def test_marker_and_gate_together_are_the_only_exemption(marker, gate, monkeypatch):
    """33, and §9: the existing real-desktop pair, with no new marker category invented."""
    monkeypatch.setenv(gate, "1")
    assert safety_guards.exempt(Node(marker), safety_guards.DESKTOP_EXEMPT) is True


def test_no_new_marker_category_was_created():
    """34's companion: the drift test has nothing new to reconcile."""
    from tests.conftest import OPT_IN_GATES
    categories = {**safety_guards.DESKTOP_EXEMPT, **safety_guards.AUDIO_EXEMPT,
                  **safety_guards.MICROPHONE_EXEMPT, **safety_guards.MODEL_EXEMPT,
                  **safety_guards.PROVIDER_EXEMPT, **safety_guards.DATABASE_EXEMPT}
    for invented in ("real_observation", "real_uia", "real_screen"):
        assert invented not in categories
    assert set(safety_guards.DESKTOP_EXEMPT) <= set(OPT_IN_GATES)


def test_the_guard_is_one_system_not_a_parallel_one():
    """§9. The observation guard uses the same sentinel, the same exemption map and the same refuser
    shape as the executor's - it is a policy table, not a second framework."""
    import inspect
    source = inspect.getsource(safety_guards.install_observation_guard)
    assert "DESKTOP_EXEMPT" in source
    assert "RefuseLibraries" in source
    assert "PhysicalDesktopEscaped" in source


# --- Later layers are declared, not stubbed -----------------------------------------------------------

def test_only_the_uia_layer_exists_so_far():
    """§15. The enum names all five layers, but no DOM, OCR, vision or coordinate acquisition exists -
    and nothing here calls a provider."""
    source = (settings.PROJECT_ROOT / "app" / "verifier" / "observation.py").read_text(encoding="utf-8")
    code = uia_code()
    for absent in ("playwright", "screenshot", "ocr", "tesseract", "vision", "anthropic", "httpx"):
        assert absent not in source.lower(), f"observation.py mentions {absent}"
        assert absent not in code.lower(), f"the UIA code mentions {absent}"
    assert "ObservationSource.UIA" in source
    for later in ("ObservationSource.DOM", "ObservationSource.OCR", "ObservationSource.VISION",
                  "ObservationSource.COORDINATE"):
        assert later not in source, f"{later} is used before its layer exists"


def test_no_app_specific_code_was_added():
    """§12. The examples motivated the architecture; the resolver is generic."""
    observation_source = (settings.PROJECT_ROOT / "app" / "verifier" / "observation.py").read_text(
        encoding="utf-8")
    for body in (observation_source.lower(), uia_code().lower()):
        for app in ("chrome", "notepad", "vscode", "explorer", "profile picker", "save as"):
            assert app not in body, f"the new code special-cases {app}"
