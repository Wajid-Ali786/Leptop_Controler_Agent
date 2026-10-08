"""
Tests for Phase 5 DOM Slice 1 - the assistant-owned browser session and read-only DOM resolution.

The properties this file exists to hold:

    the live Playwright objects never leave app/executor/adapter.py; the user's own browser profile
    is unreachable; nothing read off a page is returned, logged or sent anywhere; and an ordinary
    test cannot start a browser, read a real page or reach the network.

EVERYTHING HERE IS FAKED, INCLUDING PLAYWRIGHT ITSELF. Unlike the UIA slices there is no owner smoke
behind this file, so a green run here says the local substrate is right - it says NOTHING about whether
channel="chrome" launches on this machine, whether the role allowlist matches real page semantics, or
whether get_by_role behaves as assumed. Those need a gated real-browser run. The report says so.

Checks about code are made against its AST with docstrings and comments stripped. A substring search
over this project's source has matched its own explanation of a rule five times; the habit is not
optional here.
"""
import ast
import inspect
import textwrap
import time
from types import SimpleNamespace

import pytest

import safety_guards
from app.executor import adapter as executor_adapter
from app.verifier import observation
from app.verifier.models import (Ambiguous, DomElement, DomTarget, Found, FramesNotSupported,
                                 NotFound, ObservationSource, Unavailable)
from config import settings

SESSION = "s" * 32
PAGE = "p" * 16


def element(**overrides) -> DomElement:
    fields = dict(session_id=SESSION, page_id=PAGE, role="button", tag="button",
                  element_token="tok", enabled=True, visible=True, bounds=(10, 20, 110, 60),
                  is_password=False, observed_at=time.time())
    fields.update(overrides)
    return DomElement(**fields)


def target(name="Login") -> DomTarget:
    return DomTarget(name, SESSION, PAGE)


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
    """One named function's code from the FILE - needed because the browser guard replaces the
    adapter's boundary functions with its refuser, so inspect.getsource() would return that."""
    module = ast.parse(path.read_text(encoding="utf-8"))
    found = [n for n in ast.walk(module) if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(found) == 1, f"{name} defined {len(found)} times"
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


def _imports(path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            yield node.module or ""


# =====================================================================================================
# A FAKE PLAYWRIGHT - deterministic, and the only "page" any ordinary test sees
# =====================================================================================================

class FakeLocator:
    """Stands in for a Playwright Locator over a fixed list of fake controls."""

    def __init__(self, controls, page):
        self._controls = controls
        self._page = page

    def count(self):
        return len(self._controls)

    def nth(self, index):
        return FakeLocator([self._controls[index]], self._page)

    @property
    def _one(self):
        assert len(self._controls) == 1
        return self._controls[0]

    def bounding_box(self, timeout=None):
        self._page.timeouts.append(timeout)
        return self._one.get("box")

    def is_enabled(self, timeout=None):
        return self._one.get("enabled", True)

    def is_visible(self, timeout=None):
        return self._one.get("visible", True)

    def get_attribute(self, name, timeout=None):
        return self._one.get("attrs", {}).get(name)

    def evaluate(self, expression, timeout=None):
        return self._one.get("tag", "button")



    # --- delayed render (added with the readiness fix) -----------------------------------------------
    def or_(self, other):
        """Playwright's Locator.or_: match either. The production matcher combines all the allowed
        roles with this so it can wait ONCE across all of them."""
        combined = FakeLocator(self._controls + other._controls, self._page)
        return combined

    @property
    def first(self):
        return FakeLocator(self._controls[:1], self._page)

    def wait_for(self, state=None, timeout=None):
        """The one bounded readiness wait. Records that it happened, and how long it was allowed."""
        assert timeout is not None, "the readiness wait must be bounded"
        assert state == "attached", f"unexpected wait state {state!r}"
        self._page.wait_calls.append(timeout)
        self._page.waits += 1
        if not self._page.is_ready():
            raise RuntimeError("Timeout waiting for locator")


class FakePage:
    """A page of fake controls. `get_by_role` applies the SAME rule the real one documents: a regex
    `name` matches the accessible name, and whitespace in that name is already normalised."""

    def __init__(self, controls, frames=1):
        self.controls = controls
        self.frames = [object()] * frames
        self.timeouts = []
        self.forbidden = []
        self.url_reads = 0
        self.waits = 0
        self.ready_after_waits = 0
        self.wait_calls = []


    # --- delayed render (added with the readiness fix) -----------------------------------------------
    def is_ready(self) -> bool:
        """Whether the controls have "rendered" yet. ready_after_waits=0 means immediately."""
        return self.waits >= self.ready_after_waits

    @property
    def visible_controls(self):
        return self.controls if self.is_ready() else []

    def get_by_role(self, role, name=None, exact=None):
        assert exact is None, "exact must not be passed: Playwright ignores it for a pattern"
        assert hasattr(name, "search"), "the name must be a compiled pattern, not a string"
        matched = [c for c in self.visible_controls
                   if c["role"] == role and name.search(" ".join(c["name"].split()))]
        return FakeLocator(matched, self)

    # Anything a page could expose that this slice must never touch. Reaching one fails the test.
    def content(self):
        self.forbidden.append("content")
        raise AssertionError("the DOM layer must not read page HTML")

    def inner_text(self, *a, **k):
        self.forbidden.append("inner_text")
        raise AssertionError("the DOM layer must not read page text")

    @property
    def url(self):
        """Readable since the DOM action slice: the adapter fingerprints it to notice that the page
        changed between a confirmation and a click. It is a ONE-WAY digest, and the address itself is
        never kept, returned or logged - which the tests below assert directly."""
        self.url_reads += 1
        return "https://private.example.com/account?token=secret"


def control(name, role="button", **extra):
    base = dict(name=name, role=role, tag="button", box={"x": 10, "y": 20, "width": 100, "height": 40},
                enabled=True, visible=True, attrs={})
    base.update(extra)
    return base


@pytest.fixture
def session(monkeypatch):
    """A live session in the REAL registry, holding fake Playwright objects.

    The guard has replaced the adapter's boundary functions, so the genuine dom_query is reinstalled
    here - this file is ABOUT it - while nothing real is ever started."""
    page = FakePage([control("Login")])
    real_dom_query = type(executor_adapter).__dict__ if False else None
    monkeypatch.setattr(executor_adapter, "dom_query", _genuine("dom_query"))
    monkeypatch.setattr(executor_adapter, "dom_page_has_frames", _genuine("dom_page_has_frames"))
    runtime = SimpleNamespace(stop=lambda: page.timeouts.append("stopped"))
    browser = SimpleNamespace(close=lambda: page.timeouts.append("browser closed"))
    context = SimpleNamespace(close=lambda: page.timeouts.append("context closed"))
    state = executor_adapter._BrowserSession(runtime=runtime, browser=browser, context=context,
                                             pages={PAGE: page}, tokens={})
    with executor_adapter._browser_lock:
        executor_adapter._browser_sessions[SESSION] = state
    yield page
    with executor_adapter._browser_lock:
        executor_adapter._browser_sessions.pop(SESSION, None)


def _genuine(name):
    """The adapter's real function, read from the module's source rather than the patched attribute."""
    namespace = {}
    source = textwrap.dedent(code_of_named(EXECUTOR_ADAPTER, name))
    exec(compile(source, "<adapter>", "exec"), vars(executor_adapter), namespace)
    return namespace[name]


# =====================================================================================================
# ARCHITECTURE (matrix 1-7)
# =====================================================================================================

def test_only_the_executor_adapter_imports_playwright():
    """1. And it is the file that ACTS - not the Verifier's, because Playwright can click."""
    root = settings.PROJECT_ROOT / "app"
    # ast.walk finds a function-level import too, which is exactly what a lazy import is - so the
    # claim here is about WHICH FILE, and laziness is asserted by its own test below.
    importers = sorted({path.relative_to(root).as_posix() for path in root.rglob("*.py")
                        for module in _imports(path) if module.split(".")[0] == "playwright"})
    assert importers == ["executor/adapter.py"], importers


def test_the_playwright_import_is_lazy():
    """2. Importing the adapter must not load a browser stack."""
    module = ast.parse(EXECUTOR_ADAPTER.read_text(encoding="utf-8"))
    top_level = {alias.name for node in module.body if isinstance(node, ast.Import)
                 for alias in node.names}
    top_level |= {node.module or "" for node in module.body if isinstance(node, ast.ImportFrom)}
    assert not any(name.startswith("playwright") for name in top_level), top_level
    import sys
    assert "playwright" not in sys.modules, "nothing in this suite may have loaded it"


@pytest.mark.parametrize("folder", ["verifier", "brain", "planner", "memory", "listener", "speaker"])
def test_no_other_module_reaches_playwright(folder):
    """3 + 4."""
    root = settings.PROJECT_ROOT / "app" / folder
    for path in root.rglob("*.py"):
        assert not any(m.split(".")[0] == "playwright" for m in _imports(path)), path.name


def test_no_core_model_can_hold_a_live_playwright_object():
    """5. Checked over the annotations of every model in the observation module."""
    from app.verifier import models
    for name in dir(models):
        kind = getattr(models, name)
        fields = getattr(kind, "__dataclass_fields__", None)
        if not fields:
            continue
        for field in fields.values():
            annotation = str(field.type)
            for forbidden in ("Browser", "BrowserContext", "Page", "Locator", "ElementHandle",
                              "Playwright", "object"):
                assert forbidden not in annotation, f"{name}.{field.name}: {annotation}"


def test_the_live_registry_exists_only_in_the_executor_adapter():
    """6 + 7. The registry and the private session type are defined in one file, and no other
    production file names them."""
    root = settings.PROJECT_ROOT / "app"
    holders = []
    for path in root.rglob("*.py"):
        text = code_of_module_text(path)
        if "_browser_sessions" in text or "_BrowserSession" in text:
            holders.append(path.relative_to(root).as_posix())
    assert holders == ["executor/adapter.py"], holders


def code_of_module_text(path) -> str:
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


def test_executor_logic_holds_no_live_browser_state():
    """7, updated by the DOM action slice.

    Slice 1 could make the strongest possible claim - logic had no browser code at all - because there
    was nothing to act with. Slice 2 gives it the action, so it now CALLS the adapter's boundaries.
    The claim that endures, and the one that was always the point, is that it holds no LIVE state: no
    registry, no session record, and no Playwright object. Opaque ids only."""
    text = code_of_module_text(settings.PROJECT_ROOT / "app" / "executor" / "logic.py")
    for forbidden in ("_browser_sessions", "_BrowserSession", "_DomLocatorDescription",
                      "sync_playwright", "new_context", "get_by_role", "_page_identity"):
        assert forbidden not in text, forbidden
    # what it may do is ask the adapter, by opaque id
    assert "adapter.dom_query" in text and "adapter.dom_click" in text
    imported = set()
    for node in ast.walk(ast.parse((settings.PROJECT_ROOT / "app" / "executor" / "logic.py")
                                   .read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not any(name.startswith("playwright") for name in imported), imported


# =====================================================================================================
# GATING (matrix 8-13)
# =====================================================================================================

def test_an_ordinary_test_cannot_import_playwright():
    """8."""
    from safety_guards import PhysicalBrowserEscaped
    with pytest.raises(PhysicalBrowserEscaped):
        import playwright  # noqa: F401
    with pytest.raises(PhysicalBrowserEscaped):
        import playwright.sync_api  # noqa: F401


@pytest.mark.parametrize("name", safety_guards.BROWSER_BOUNDARIES)
def test_an_ordinary_test_cannot_reach_a_browser_boundary(name):
    """9. Every boundary, centrally refused."""
    from safety_guards import PhysicalBrowserEscaped
    with pytest.raises(PhysicalBrowserEscaped):
        getattr(executor_adapter, name)(SESSION, PAGE, "login", 1.0)


def test_the_marker_alone_and_the_gate_alone_are_both_insufficient(monkeypatch):
    """10 + 11 + 12."""
    marked = type("Node", (), {"get_closest_marker": lambda self, n: object()})()
    bare = type("Node", (), {"get_closest_marker": lambda self, n: None})()
    monkeypatch.delenv("RUN_REAL_BROWSER_TEST", raising=False)
    assert safety_guards.exempt(marked, safety_guards.BROWSER_EXEMPT) is False
    monkeypatch.setenv("RUN_REAL_BROWSER_TEST", "1")
    assert safety_guards.exempt(bare, safety_guards.BROWSER_EXEMPT) is False
    assert safety_guards.exempt(marked, safety_guards.BROWSER_EXEMPT) is True


def test_the_desktop_gate_does_not_grant_browser_access(monkeypatch):
    """13. The whole reason for a separate consequence class: a browser can reach a profile, cookies
    and the network, and permission to move the mouse was never permission for that."""
    monkeypatch.setenv("RUN_REAL_DESKTOP_TEST", "1")
    monkeypatch.delenv("RUN_REAL_BROWSER_TEST", raising=False)
    desktop_marked = type("Node", (), {
        "get_closest_marker": lambda self, n: object() if n == "real_desktop" else None})()
    assert safety_guards.exempt(desktop_marked, safety_guards.DESKTOP_EXEMPT) is True
    assert safety_guards.exempt(desktop_marked, safety_guards.BROWSER_EXEMPT) is False
    assert set(safety_guards.BROWSER_EXEMPT) & set(safety_guards.DESKTOP_EXEMPT) == set()
    assert "RUN_REAL_BROWSER_TEST" not in safety_guards.DESKTOP_EXEMPT.values()


def test_the_browser_gate_is_registered_centrally():
    from tests.conftest import OPT_IN_GATES
    assert OPT_IN_GATES["real_browser"][0] == "RUN_REAL_BROWSER_TEST"
    assert safety_guards.BROWSER_EXEMPT["real_browser"] == "RUN_REAL_BROWSER_TEST"


# =====================================================================================================
# SESSION (matrix 14-23)
# =====================================================================================================

def test_a_session_is_owned_only_because_the_registry_has_it(session):
    """15. Never a title, never a URL, never a process name."""
    assert executor_adapter.browser_session_exists(SESSION) is True
    assert executor_adapter.browser_session_exists("not-a-session") is False
    code = code_of_named(EXECUTOR_ADAPTER, "browser_session_exists")
    for forbidden in ("title", "url", "name", "process"):
        assert forbidden not in code, forbidden


def test_identifiers_are_opaque_and_random():
    """14. Read from the source, since the boundary itself is guarded."""
    code = code_of_named(EXECUTOR_ADAPTER, "browser_open_session")
    assert "secrets.token_hex" in code
    for forbidden in ("title", "url", "user_data_dir", "storage_state", "launch_persistent_context"):
        assert forbidden not in code, forbidden


def test_the_launch_is_the_installed_chrome_and_non_persistent():
    """21 + 22 + 23. The binding privacy decision, read off the code that implements it."""
    code = code_of_named(EXECUTOR_ADAPTER, "browser_open_session")
    assert "channel=channel" in code, "the installed browser, so no Chromium download"
    assert "new_context()" in code, "non-persistent: Playwright's own throwaway profile"
    for forbidden in ("launch_persistent_context", "user_data_dir", "storage_state", "cookies",
                      "Default", "Profile 1"):
        assert forbidden not in code, f"the user's own profile must be unreachable: {forbidden}"
    assert settings.get_setting("browser.channel") == "chrome"


def test_an_unknown_session_or_page_is_refused_never_guessed(session):
    """16 + 17."""
    query = _genuine("dom_query")
    with pytest.raises(executor_adapter.BrowserError):
        query("no-such-session", PAGE, "login", 1.0)
    with pytest.raises(executor_adapter.BrowserError):
        query(SESSION, "no-such-page", "login", 1.0)


def test_closing_removes_the_session_from_use(session):
    """18 + 19."""
    assert executor_adapter.browser_close_session.__name__
    close = _genuine("browser_close_session")
    assert close(SESSION) is True
    assert executor_adapter.browser_session_exists(SESSION) is False
    assert close(SESSION) is False, "closing twice is safe and says there was nothing to close"
    with pytest.raises(executor_adapter.BrowserError):
        _genuine("dom_query")(SESSION, PAGE, "login", 1.0)


def test_close_removes_the_entry_before_tearing_anything_down():
    """18. A caller racing with close must not find a session whose objects are being closed."""
    code = code_of_named(EXECUTOR_ADAPTER, "browser_close_session")
    assert code.index("_browser_sessions.pop") < code.index("_abandon"), code


def test_a_partial_launch_leaves_no_usable_entry():
    """20. The registry is written only after everything has been built."""
    code = code_of_named(EXECUTOR_ADAPTER, "browser_open_session")
    assert code.index("_abandon") < code.index("_browser_sessions[session_id]"), code
    assert code.index("except Exception") < code.index("secrets.token_hex"), \
        "ids are minted only after the launch succeeded"


# =====================================================================================================
# MATCHING (matrix 24-35)
# =====================================================================================================

@pytest.mark.parametrize("asked", ["Login", "login", "LOGIN", "  Login  "])
def test_the_name_matches_whole_string_case_insensitively(session, asked):
    """24 + 25 + 26."""
    from app.verifier.models import normalize_name
    found = _genuine("dom_query")(SESSION, PAGE, normalize_name(asked), 1.0)
    assert len(found) == 1, asked


@pytest.mark.parametrize("asked", ["Log In", "Log", "gin", "Logins", "Logn", "Log-in"])
def test_neither_substring_nor_near_spelling_matches(session, asked):
    """27 + 28 + 29. The rule Playwright's own default would have broken: its default name match is a
    case-insensitive SUBSTRING, so "Log" would have matched "Login". An anchored pattern is used."""
    from app.verifier.models import normalize_name
    assert _genuine("dom_query")(SESSION, PAGE, normalize_name(asked), 1.0) == []


def test_matches_are_combined_across_the_allowed_roles(session):
    """30 + 32. Two things called Login are two things, button or link."""
    session.controls = [control("Login", role="button"), control("Login", role="link")]
    found = _genuine("dom_query")(SESSION, PAGE, "login", 1.0)
    assert len(found) == 2
    assert {e.role for e in found} == {"button", "link"}
    result = observation.resolve_dom_target(target(), found)
    assert isinstance(result, Ambiguous) and len(result.candidates) == 2


def test_the_role_allowlist_is_small_fixed_and_real():
    """8 of the report. Every role is one Playwright actually accepts - checked against the installed
    package's own AriaRole list, by reading it rather than importing the library."""
    roles = _playwright_aria_roles()
    assert set(executor_adapter.DOM_ROLES) <= roles, set(executor_adapter.DOM_ROLES) - roles
    assert executor_adapter.DOM_ROLES == ("button", "link", "checkbox", "radio", "textbox",
                                          "combobox", "option", "menuitem", "tab")
    # interactive only: a heading or paragraph called Login is not something to click
    for not_interactive in ("heading", "paragraph", "banner", "main", "article", "table"):
        assert not_interactive not in executor_adapter.DOM_ROLES


def _playwright_aria_roles() -> set:
    structures = (settings.PROJECT_ROOT / "venv" / "Lib" / "site-packages" / "playwright" /
                  "_impl" / "_api_structures.py")
    if not structures.exists():
        pytest.skip("playwright source not found next to this checkout")
    for node in ast.walk(ast.parse(structures.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "AriaRole":
            return {s.value for s in node.value.slice.elts}
    pytest.skip("AriaRole literal not found")


def test_non_interactive_text_is_never_returned_as_a_control(session):
    """35."""
    session.controls = [control("Login", role="heading"), control("Login", role="paragraph")]
    assert _genuine("dom_query")(SESSION, PAGE, "login", 1.0) == []


def test_nothing_matched_in_the_top_document_is_not_found(session):
    """33."""
    session.controls = []
    found = _genuine("dom_query")(SESSION, PAGE, "login", 1.0)
    has_frames = _genuine("dom_page_has_frames")(SESSION, PAGE)
    assert has_frames is False
    result = observation.resolve_dom_target(target(), found, has_frames)
    assert isinstance(result, NotFound)


def test_a_miss_with_child_frames_is_not_reported_as_not_found(session):
    """34. "I didn't find it" and "I didn't look everywhere" are different claims."""
    session.controls = []
    session.frames = [object(), object()]        # main frame plus one child
    found = _genuine("dom_query")(SESSION, PAGE, "login", 1.0)
    has_frames = _genuine("dom_page_has_frames")(SESSION, PAGE)
    assert has_frames is True
    result = observation.resolve_dom_target(target(), found, has_frames)
    assert isinstance(result, FramesNotSupported)
    assert "frames I can't read yet" in result.reason


def test_the_query_passes_a_pattern_and_never_the_exact_flag(session):
    """8 of the report, enforced by the fake: Playwright documents `exact` as ignored for a pattern
    and case-SENSITIVE otherwise, so passing it would be either useless or wrong."""
    found = _genuine("dom_query")(SESSION, PAGE, "login", 1.0)  # the fake asserts both conditions
    assert session.forbidden == [], session.forbidden
    assert "example.com" not in repr(found)
    code = code_of_named(EXECUTOR_ADAPTER, "_dom_matches")
    assert "re.IGNORECASE" in code and "re.escape" in code
    assert "exact=" not in code


# =====================================================================================================
# PRIVACY (matrix 36-44)
# =====================================================================================================

def test_the_returned_element_has_no_name_or_content_field():
    """36 + 37."""
    fields = set(DomElement.__dataclass_fields__)
    assert fields == {"session_id", "page_id", "role", "tag", "element_token", "enabled", "visible",
                      "bounds", "is_password", "observed_at"}
    for forbidden in ("name", "text", "html", "url", "href", "value", "selector", "locator",
                      "cookie", "storage"):
        assert not any(forbidden in f for f in fields), forbidden


def test_an_unmatched_name_is_never_returned(session):
    """38. Three controls, one asked for: only the match comes back, and it carries no label."""
    session.controls = [control("Login"), control("Delete account"), control("Sign up")]
    found = _genuine("dom_query")(SESSION, PAGE, "login", 1.0)
    assert len(found) == 1
    rendered = repr(found)
    for leaked in ("Login", "Delete account", "Sign up"):
        assert leaked not in rendered, leaked


def test_no_value_password_cookie_or_storage_is_ever_requested():
    """39 + 40 + 41. Checked against the stripped code of every DOM read."""
    code = chr(10).join(code_of_named(EXECUTOR_ADAPTER, name) for name in
                        ("dom_query", "_dom_element", "dom_page_has_frames"))
    for forbidden in ("input_value", "text_content", "inner_text", "inner_html", "content()",
                      "cookies", "local_storage", "session_storage", "storage_state",
                      "all_text_contents", "evaluate_handle"):
        assert forbidden not in code, forbidden
    # It reads the type ATTRIBUTE to know whether it is a password box, and never a value. Checked
    # against ast.unparse output, which renders literals SINGLE-quoted; the first version of this
    # assertion looked for double quotes and failed for that reason alone.
    assert "get_attribute('type'" in code, code
    for value_read in ("get_attribute('value'", "input_value", "text_content", "all_text_contents"):
        assert value_read not in code, value_read
    # the one evaluate() call may read nothing but the tag name
    evaluated = [node.args[0].value for node in ast.walk(ast.parse(code))
                 if isinstance(node, ast.Call)
                 and getattr(node.func, "attr", "") == "evaluate"
                 and node.args and isinstance(node.args[0], ast.Constant)]
    assert evaluated == ["node => node.tagName"], evaluated


def test_a_password_box_is_found_structurally_and_its_value_never_read(session):
    """12 of the brief. Clicking a box is how a person starts typing in it."""
    session.controls = [control("Password", role="textbox", tag="input",
                                attrs={"type": "password"})]
    found = _genuine("dom_query")(SESSION, PAGE, "password", 1.0)
    assert len(found) == 1 and found[0].is_password is True
    assert session.forbidden == [], session.forbidden
    assert "example.com" not in repr(found)


def test_the_page_apis_that_would_leak_content_are_never_touched(session):
    """37 + 41. The fake raises if the implementation reaches for HTML or page text.

    The URL is deliberately NOT in that set any more: since the DOM action slice the adapter reads it
    to fingerprint the page. What is asserted instead is the thing that always mattered - that the
    address does not come back out."""
    session.controls = [control("Login")]
    found = _genuine("dom_query")(SESSION, PAGE, "login", 1.0)
    _genuine("dom_page_has_frames")(SESSION, PAGE)
    assert session.forbidden == [], session.forbidden

    rendered = repr(found)
    for secret in ("private.example.com", "token=secret", "https://"):
        assert secret not in rendered, f"{secret!r} came back from the adapter"
    for element in found:
        for value in vars(element).values():
            assert "example.com" not in str(value), value


def test_no_name_url_or_domain_reaches_the_logs(session, caplog):
    """42 + 43."""
    caplog.set_level("DEBUG")
    session.controls = [control("Login"), control("Login", role="link")]
    found = _genuine("dom_query")(SESSION, PAGE, "login", 1.0)
    observation.resolve_dom_target(target(), found, False)
    session.controls = []
    observation.resolve_dom_target(target(), [], True)

    logged = " ".join(record.getMessage() for record in caplog.records)
    for secret in ("Login", "login", "http", "example.com", SESSION, PAGE, "tok"):
        assert secret not in logged, f"{secret!r} reached the logs: {logged}"
    assert "source=dom" in logged and "candidates=" in logged


def test_no_observation_content_can_reach_the_provider():
    """44. The Brain cannot be reached from the Executor or from observation, in either direction."""
    for path in (settings.PROJECT_ROOT / "app" / "executor" / "adapter.py",
                 settings.PROJECT_ROOT / "app" / "executor" / "logic.py",
                 settings.PROJECT_ROOT / "app" / "verifier" / "observation.py"):
        imported = {m.split(".")[0] for m in _imports(path)}
        assert "anthropic" not in imported and "httpx" not in imported and "httpx2" not in imported
        assert not any(m.startswith("app.brain") for m in _imports(path))


# =====================================================================================================
# PURE RESOLUTION (matrix 45-50)
# =====================================================================================================

def test_the_dom_resolver_is_pure():
    """45. No Playwright, no IO, no input, no logging of content - and no mutation."""
    # Checked as CALLS in the AST, not as substrings. The first version forbade the word "page", which
    # matched the perfectly legitimate `page_id` - the same crude-matching mistake this file warns
    # about at the top, made while writing the test meant to guard against it.
    tree = ast.parse(textwrap.dedent(inspect.getsource(observation.resolve_dom_target)))
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            called.add(node.func.attr if isinstance(node.func, ast.Attribute)
                       else getattr(node.func, "id", ""))
    allowed = {"isinstance", "tuple", "len", "strip", "bool", "_log_dom", "_observed_from_dom",
               "NotFound", "Unavailable", "Found", "Ambiguous", "FramesNotSupported"}
    assert called <= allowed, called - allowed
    for acting in ("get_by_role", "click", "goto", "evaluate", "bounding_box", "count", "fill"):
        assert acting not in called, acting
    # and the module it lives in cannot reach the library at all
    source = code_of_module_text(settings.PROJECT_ROOT / "app" / "verifier" / "observation.py")
    assert "playwright" not in source


@pytest.mark.parametrize("elements, expected", [
    ([element()], Found),
    ([element(), element(role="link")], Ambiguous),
    ([], NotFound),
])
def test_the_three_ordinary_outcomes(elements, expected):
    """46 + 47 + 48."""
    assert isinstance(observation.resolve_dom_target(target(), elements), expected)


@pytest.mark.parametrize("bad", [DomTarget("Login", "", PAGE), DomTarget("Login", SESSION, "")])
def test_a_missing_session_or_page_is_unavailable(bad):
    """49."""
    assert isinstance(observation.resolve_dom_target(bad, [element()]), Unavailable)


def test_unreadable_evidence_is_unavailable():
    """49."""
    assert isinstance(observation.resolve_dom_target(target(), None), Unavailable)


@pytest.mark.parametrize("bad", [None, "Login", 7, DomTarget("", SESSION, PAGE),
                                 DomTarget("   ", SESSION, PAGE)])
def test_a_missing_or_empty_target_is_refused(bad):
    result = observation.resolve_dom_target(bad, [element()])
    assert isinstance(result, (NotFound, Unavailable)), result


def test_elements_from_another_session_are_ignored():
    """The evidence must belong to the page that was asked about."""
    other = element(session_id="other" * 6)
    assert isinstance(observation.resolve_dom_target(target(), [other]), NotFound)


def test_the_result_keeps_its_dom_provenance_and_carries_no_action():
    """50 + the hierarchy's own requirement that a result says which layer answered."""
    found = observation.resolve_dom_target(target(), [element()])
    assert found.observed.source is ObservationSource.DOM
    assert found.observed.window_handle == 0, "a page is not a window, and this slice maps neither"
    from app.verifier.models import Observed
    for field in Observed.__dataclass_fields__.values():
        assert "ExecutorAction" not in str(field.type)


# =====================================================================================================
# TIMEOUTS / ISOLATION (matrix 51-58)
# =====================================================================================================

def test_every_browser_call_is_bounded_by_configuration():
    """51 + 52. No reliance on Playwright's own 30-second default."""
    launch = code_of_named(EXECUTOR_ADAPTER, "browser_open_session")
    query = code_of_named(EXECUTOR_ADAPTER, "dom_query")
    assert "timeout=" in launch and "launch_timeout_seconds" in launch
    assert "set_default_timeout" in launch, "so an unbounded call cannot slip through later"
    assert "timeout" in query and "query_timeout_seconds" in query
    assert settings.get_setting("browser.launch_timeout_seconds") > 0
    assert settings.get_setting("browser.query_timeout_seconds") > 0


def test_the_query_passes_its_timeout_to_every_element_read(session):
    """52. Including the per-element reads, which is where a default would otherwise apply."""
    _genuine("dom_query")(SESSION, PAGE, "login", 1.0)
    assert session.timeouts and all(t == 1000.0 for t in session.timeouts if t != "stopped"), \
        session.timeouts


def test_no_timeout_is_hardcoded_in_the_browser_code():
    """52."""
    for name in ("browser_open_session", "dom_query"):
        code = code_of_named(EXECUTOR_ADAPTER, name)
        assert "30000" not in code and "30.0" not in code


def test_the_ordinary_suite_reaches_no_browser_and_no_network(session):
    """53 + 54 + 55. The fake page is the only page, and the real boundaries are refused."""
    import sys
    assert "playwright" not in sys.modules
    from safety_guards import PhysicalBrowserEscaped
    with pytest.raises(PhysicalBrowserEscaped):
        executor_adapter.browser_open_session("chrome", 1.0)
    code = code_of_module_text(EXECUTOR_ADAPTER)
    # SUPERSEDED BY USABILITY SLICE 5, which added navigation deliberately. This used to assert the
    # adapter contained no navigation at all - true while DOM Slice 1 only READ pages. The enduring
    # claim is narrower and stronger: navigation exists in exactly ONE function, that function is in
    # the central guard, and no OTHER kind of outbound request was added alongside it.
    assert code.count("page.goto") == 1, "navigation must live in exactly one place"
    assert code.count(".goto(") == 1
    navigate_src = code.split("def browser_navigate")[1].split("def ")[0]
    assert "page.goto" in navigate_src, "the one navigation is not in browser_navigate"
    from safety_guards import BROWSER_BOUNDARIES
    assert "browser_navigate" in BROWSER_BOUNDARIES
    for other in ("request.get", "api_request", "urlopen", "requests.", "httpx"):
        assert other not in code, f"a second kind of outbound request appeared: {other}"


def test_no_desktop_audio_or_memory_action_is_on_this_path():
    """58 + 46/47 of the brief."""
    code = (code_of_named(EXECUTOR_ADAPTER, "dom_query")
            + code_of_named(EXECUTOR_ADAPTER, "browser_open_session")
            + code_of(observation.resolve_dom_target))
    for forbidden in ("pyautogui", "SetForegroundWindow", "winmm", "speak", "sqlite", "memory"):
        assert forbidden not in code, forbidden
    assert not (settings.PROJECT_ROOT / "data" / "memory.db").exists()


# =====================================================================================================
# READINESS - the confirmed defect: snapshot matching produced a transient false NotFound
# =====================================================================================================
# The owner's console run found 'Login' absent, then found the identical target seconds later. The
# cause was not case: production normalises, so both attempts passed the byte-identical string to the
# matcher. The cause was that `locator.count()` is an instantaneous snapshot and nothing waited.
#
# These tests model what no fake could model before: NOT READY, THEN READY.

CONFIGURED_QUERY_MS = 2000.0          # browser.query_timeout_seconds (2.0) in milliseconds


def test_a_target_that_renders_late_is_found_before_the_deadline(session):
    """1. The defect, reproduced and fixed: empty on the first scan, present after the one wait."""
    session.ready_after_waits = 1                 # nothing renders until the page has been waited on
    assert session.visible_controls == [], "the first scan must see an empty page"

    found = _genuine("dom_query")(SESSION, PAGE, "login", 2.0)

    assert len(found) == 1, "a late-rendering control must still be found"
    assert session.waits == 1, "it waited, once"
    result = observation.resolve_dom_target(target(), found)
    assert isinstance(result, Found)


def test_a_target_that_never_renders_is_not_found_after_the_bounded_deadline(session):
    """2. Patience is bounded: it still gives up, and with the existing message."""
    session.ready_after_waits = 99                # it is never going to appear
    found = _genuine("dom_query")(SESSION, PAGE, "login", 2.0)

    assert found == []
    assert session.waits == 1, "it waited once and then stopped - no unbounded retrying"
    result = observation.resolve_dom_target(target(), found, False)
    assert isinstance(result, NotFound)
    assert result.message == "I couldn't find anything called 'Login' on that page."


def test_one_shared_deadline_is_used_not_one_per_role(session):
    """3. Nine roles, ONE wait. Worst case stays about the configured timeout, not nine times it."""
    session.ready_after_waits = 99
    _genuine("dom_query")(SESSION, PAGE, "login", 2.0)

    assert len(session.wait_calls) == 1, f"one wait across all roles, got {len(session.wait_calls)}"
    assert len(executor_adapter.DOM_ROLES) == 9, "the premise: there are nine roles"
    only = session.wait_calls[0]
    assert 0 < only <= CONFIGURED_QUERY_MS, only
    # and the deadline is counted once, before anything is scanned
    code = code_of_named(EXECUTOR_ADAPTER, "_dom_matches")
    assert code.count("deadline = ") == 1, code
    assert "for role in DOM_ROLES" not in code, "the per-role scan belongs to _scan_roles"


def test_a_target_that_is_already_there_never_waits(session):
    """4. The ready case must not become slower."""
    session.ready_after_waits = 0
    found = _genuine("dom_query")(SESSION, PAGE, "login", 2.0)

    assert len(found) == 1
    assert session.waits == 0 and session.wait_calls == [], "nothing should have waited"


def test_ambiguity_still_returns_ambiguous_after_a_late_render(session):
    """5. Readiness does not change what two matches mean."""
    session.controls = [control("Login"), control("Login", role="link")]
    session.ready_after_waits = 1
    found = _genuine("dom_query")(SESSION, PAGE, "login", 2.0)

    assert len(found) == 2
    result = observation.resolve_dom_target(target(), found)
    assert isinstance(result, Ambiguous) and len(result.candidates) == 2


@pytest.mark.parametrize("asked", ["Login", "login", "LOGIN", "  Login  "])
def test_case_and_whitespace_handling_is_unchanged_by_the_fix(session, asked):
    """6 + 7. The semantics the fix must not touch, re-checked through the waiting path."""
    from app.verifier.models import normalize_name
    session.ready_after_waits = 1
    found = _genuine("dom_query")(SESSION, PAGE, normalize_name(asked), 2.0)
    assert len(found) == 1, asked


@pytest.mark.parametrize("asked", ["Log In", "Log", "gin", "Logins", "Log-in"])
def test_whole_string_matching_is_unchanged_by_the_fix(session, asked):
    """8. Waiting must not become a second chance for a near miss."""
    from app.verifier.models import normalize_name
    session.ready_after_waits = 1
    found = _genuine("dom_query")(SESSION, PAGE, normalize_name(asked), 2.0)
    assert found == [], asked
    assert session.waits == 1, "it waited, and still refused - the wait is not a looser rule"


def test_the_matcher_has_no_raw_sleep_and_no_unbounded_wait():
    """10. Playwright does the waiting, bounded; this adapter polls nothing and sleeps never."""
    matcher = code_of_named(EXECUTOR_ADAPTER, "_dom_matches")
    waiter = code_of_named(EXECUTOR_ADAPTER, "_wait_for_any_role")
    for forbidden in ("sleep", "while ", "for attempt", "range("):
        assert forbidden not in matcher, f"{forbidden} in _dom_matches"
        assert forbidden not in waiter, f"{forbidden} in _wait_for_any_role"
    assert "timeout=max(1.0, remaining_ms)" in waiter, "the wait must be bounded by what is left"
    assert "wait_for(state='attached'" in waiter.replace('"', "'")
    assert "or_(" in waiter, "the roles are combined so there is one wait, not nine"


def test_the_query_and_the_action_still_share_one_matcher():
    """2 of the brief: the fix applies to both, because there is still only one implementation."""
    for name in ("dom_query", "dom_click"):
        assert "_dom_matches" in code_of_named(EXECUTOR_ADAPTER, name), name
    root = settings.PROJECT_ROOT / "app"
    definers = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef) and node.name in ("_dom_matches", "_scan_roles"):
                definers.append(f"{path.relative_to(root).as_posix()}:{node.name}")
    assert sorted(definers) == ["executor/adapter.py:_dom_matches",
                                "executor/adapter.py:_scan_roles"], definers


def test_the_fakes_can_now_model_not_ready_then_ready(session):
    """The test-side half of the fix, stated as a test: a fake that is always ready cannot express
    the state that failed, which is why 4051 tests missed it."""
    session.ready_after_waits = 1
    assert session.is_ready() is False and session.visible_controls == []
    session.waits = 1
    assert session.is_ready() is True and session.visible_controls == session.controls
