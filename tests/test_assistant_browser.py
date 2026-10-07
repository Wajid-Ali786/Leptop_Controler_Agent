"""
Tests for the assistant-browser wiring - the slice that makes the proven DOM primitive reachable
from the typed console.

The property this file exists to hold:

    `click Login` goes to the DOM when the assistant's own browser is the context, to UIA when an
    owned app window is, and to NEITHER when both are live and the user did not say which. The
    owner's personal Chrome is a different thing in a different registry and can never be reached
    by any of it.

Everything is faked, including Playwright. The REAL safety gate, the REAL parser, the REAL Brain
validator and the REAL Executor seam decide every outcome.

Checks about code are made against its AST with docstrings stripped. A substring search over this
project's source has matched its own explanation of a rule seven times, so the habit is not optional.
"""
import ast
import inspect
import textwrap
import time
from types import SimpleNamespace

import pytest

import safety_guards
from app import console
from app.brain import logic as brain
from app.brain.models import (ARGS_FIELDS, ARGS_FOR_KIND, CloseBrowserArgs, OpenBrowserArgs,
                              interpretation_schema, schema_complexity)
from app.executor import adapter as executor_adapter
from app.executor import commands
from app.executor import emergency_stop
from app.executor import logic as executor
from app.executor.models import (ASSISTANT_BROWSER, CLICK_TARGET, CLOSE_APP, CLOSE_BROWSER, OPEN_APP,
                                 OPEN_BROWSER, ExecutorAction, Outcome)
from app.safety import logic as safety_logic
from app.safety.models import RiskLevel
from app.verifier import adapter as verifier_adapter
from app.verifier.models import ActiveTarget, Screen, UiaElement, WindowInfo, WindowState
from config import settings
from tests import fake_window_props

SESSION = "b" * 32
PAGE = "q" * 16
CALC_WINDOW = 9100
BUTTON_BOUNDS = (300, 400, 400, 440)
CENTRE = (350, 420)

CONFIG = (
    "safety:\n"
    '  risky_keywords: [delete, shutdown, "shut down", send]\n'
    "  safe_words: [sender]\n"
    "executor:\n"
    "  apps:\n"
    "    calculator: calc.exe\n"
    "    chrome: chrome.exe\n"
    "  max_attempts: 3\n"
    "  activation_settle_seconds: 2.0\n"
    "observation:\n"
    "  snapshot_timeout_seconds: 2.0\n"
    "  freshness_seconds: 2.0\n"
    "browser:\n"
    "  channel: chrome\n"
    "  launch_timeout_seconds: 20.0\n"
    "  query_timeout_seconds: 2.0\n"
    "verifier:\n"
    "  window_timeout_seconds: 0.2\n"
    "  poll_interval_seconds: 0.01\n"
    "  app_windows:\n"
    '    calculator: "^Calculator$"\n'
    '    chrome: "Google Chrome$"\n'
)

EXECUTOR_ADAPTER = settings.PROJECT_ROOT / "app" / "executor" / "adapter.py"


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


# =====================================================================================================
# A FAKE WORLD: a fake desktop, a fake Playwright page, and both registries
# =====================================================================================================

class FakeLocator:
    def __init__(self, controls, page):
        self._controls, self._page = controls, page

    def count(self):
        return len(self._controls)

    def nth(self, index):
        return FakeLocator([self._controls[index]], self._page)

    @property
    def _one(self):
        return self._controls[0]

    def click(self, timeout=None, **kwargs):
        assert timeout is not None
        self._page.clicks.append(self._one["name"])

    def bounding_box(self, timeout=None):
        return {"x": 1, "y": 2, "width": 30, "height": 10}

    def is_enabled(self, timeout=None):
        return True

    def is_visible(self, timeout=None):
        return True

    def get_attribute(self, name, timeout=None):
        return None

    def evaluate(self, expression, timeout=None):
        return "button"



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
    def __init__(self, controls):
        self.controls = controls
        self.frames = [object()]
        self.clicks = []
        self.waits = 0
        self.ready_after_waits = 0
        self.wait_calls = []

    @property
    def url(self):
        return "about:blank"


    # --- delayed render (added with the readiness fix) -----------------------------------------------
    def is_ready(self) -> bool:
        """Whether the controls have "rendered" yet. ready_after_waits=0 means immediately."""
        return self.waits >= self.ready_after_waits

    @property
    def visible_controls(self):
        return self.controls if self.is_ready() else []

    def get_by_role(self, role, name=None, exact=None):
        return FakeLocator([c for c in self.visible_controls if c["role"] == role
                            and name.search(" ".join(c["name"].split()))], self)


def web(name, role="button"):
    return dict(name=name, role=role)


def uia(**overrides) -> UiaElement:
    fields = dict(window_handle=CALC_WINDOW, control_type="Button", runtime_id="1-1",
                  automation_id="num7Button", class_name="Button", bounds=BUTTON_BOUNDS,
                  enabled=True, focused=False, offscreen=False, is_password=False, patterns=())
    fields.update(overrides)
    return UiaElement(**fields)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Both worlds at once: an owned Calculator window AND an assistant-browser page, each able to
    be present or absent, so the ambiguity case can actually be built."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_PATH", config_path)

    page = FakePage([web("Login"), web("Sign up")])
    desktop = SimpleNamespace(
        windows=[WindowInfo(CALC_WINDOW, "Calculator", class_name="ApplicationFrameWindow")],
        uia_matches=[uia()],
        page=page,
        launches=[],
        closed=[],
        prompts=[],
        clicks=[],
    )

    # --- the browser half: a real registry entry holding a fake page ---
    def open_session(channel, launch_timeout_seconds, headed=False):
        desktop.launches.append((channel, launch_timeout_seconds, headed))
        state = executor_adapter._BrowserSession(
            runtime=SimpleNamespace(stop=lambda: None), browser=SimpleNamespace(close=lambda: None),
            context=SimpleNamespace(close=lambda: None), pages={PAGE: page}, tokens={})
        with executor_adapter._browser_lock:
            executor_adapter._browser_sessions[SESSION] = state
        return SESSION, PAGE

    monkeypatch.setattr(executor_adapter, "browser_open_session", open_session)
    monkeypatch.setattr(executor_adapter, "dom_query", _genuine("dom_query"))
    monkeypatch.setattr(executor_adapter, "dom_page_has_frames", _genuine("dom_page_has_frames"))
    monkeypatch.setattr(executor_adapter, "dom_click", _genuine("dom_click"))
    # browser_close_session is a guarded boundary (it tears down a real browser), so the genuine
    # logic is reinstalled over the fake objects rather than stubbed - the registry really is popped.
    monkeypatch.setattr(executor_adapter, "browser_close_session", _genuine("browser_close_session"))

    # --- the desktop half ---
    def click(x, y):
        desktop.clicks.append((x, y))

    monkeypatch.setattr(executor_adapter, "click", click)
    monkeypatch.setattr(executor_adapter, "activate_window", lambda handle: True)
    monkeypatch.setattr(verifier_adapter, "uia_find_by_name",
                        lambda handle, name, timeout=2.0: [e for e in desktop.uia_matches
                                                           if e.window_handle == handle])
    monkeypatch.setattr(verifier_adapter, "uia_window_bounds", lambda handle: (0, 0, 800, 600))
    monkeypatch.setattr(verifier_adapter, "list_windows", lambda: list(desktop.windows))
    monkeypatch.setattr(verifier_adapter, "list_screens",
                        lambda: [Screen(0, 0, 1920, 1080, primary=True)])
    monkeypatch.setattr(verifier_adapter, "window_at",
                        lambda x, y: next((w for w in desktop.windows), None))
    monkeypatch.setattr(verifier_adapter, "cursor_position", lambda: CENTRE)
    monkeypatch.setattr(verifier_adapter, "active_target",
                        lambda: ActiveTarget(window=next((w for w in desktop.windows), None)))
    monkeypatch.setattr(verifier_adapter, "window_state",
                        lambda handle: WindowState(minimized=False, maximized=False,
                                                   has_minimize_box=True, has_maximize_box=True,
                                                   tool_window=False, hung=False))
    fake_window_props.install(monkeypatch, executor_adapter,
                              exists=lambda handle: any(w.handle == handle for w in desktop.windows))

    real_authorize = safety_logic.authorize

    def recording_authorize(action, confirm=None):
        decision = real_authorize(action, confirm)
        desktop.prompts.append((action.description, decision.assessment.level, decision.confirmed))
        return decision

    monkeypatch.setattr(executor, "authorize", recording_authorize)

    executor.forget_session_windows()
    with executor_adapter._browser_lock:
        executor_adapter._browser_sessions.clear()
    emergency_stop.reset("assistant-browser-test")
    yield desktop
    executor.forget_session_windows()
    with executor_adapter._browser_lock:
        executor_adapter._browser_sessions.clear()
    emergency_stop.reset("assistant-browser-test")


def _genuine(name):
    namespace = {}
    exec(compile(textwrap.dedent(code_of_named(EXECUTOR_ADAPTER, name)), "<adapter>", "exec"),
         vars(executor_adapter), namespace)
    return namespace[name]


def own_calculator():
    assert executor._remember_opened("calculator", frozenset({CALC_WINDOW}))


def open_browser(world):
    result = executor.execute(ExecutorAction(OPEN_BROWSER), always(True))
    assert result.ok, result.message
    return result


def named(control="Login", app=""):
    return ExecutorAction(CLICK_TARGET, app, control)


# =====================================================================================================
# KINDS (matrix 1-5)
# =====================================================================================================

def test_the_two_kinds_exist_with_empty_args_shapes():
    """1."""
    assert ARGS_FOR_KIND[OPEN_BROWSER] is OpenBrowserArgs
    assert ARGS_FOR_KIND[CLOSE_BROWSER] is CloseBrowserArgs
    assert ARGS_FIELDS[OPEN_BROWSER] == () and ARGS_FIELDS[CLOSE_BROWSER] == ()
    assert not OpenBrowserArgs.__dataclass_fields__ and not CloseBrowserArgs.__dataclass_fields__


def test_the_schema_cost_is_two_enum_members_and_nothing_else():
    """2. Args-free, so no wire field: the flat property count must not move."""
    counts = schema_complexity(interpretation_schema(400))
    assert counts == {"optional": 0, "unions": 0, "properties": 20}, counts
    intent = interpretation_schema(400)["properties"]["intents"]["items"]
    assert OPEN_BROWSER in intent["properties"]["kind"]["enum"]
    assert CLOSE_BROWSER in intent["properties"]["kind"]["enum"]
    assert intent["properties"]["kind"]["enum"] == list(ARGS_FOR_KIND)


def test_the_vocabulary_and_the_preparers_stay_in_sync():
    """3."""
    assert set(ARGS_FOR_KIND) == set(executor._PREPARERS)
    assert set(ARGS_FOR_KIND) == set(executor._RESOLVERS)


def test_open_chrome_still_means_the_owners_configured_chrome(world):
    """4. The regression that matters most in this slice."""
    parsed = commands.parse("open chrome")
    assert parsed.kind == OPEN_APP and parsed.target == "chrome"
    assert executor.resolve(parsed).value == "chrome"
    assert "chrome" in executor.configured_app_names()
    # and it is nothing to do with the assistant browser
    assert parsed.kind != OPEN_BROWSER
    assert executor_adapter.browser_sessions() == []


def test_voice_planning_capability_is_unchanged():
    """5."""
    from app.planner.models import ALL_KINDS, NO_HANDOVER_KINDS, TYPED_CONSOLE, VOICE_CONSOLE
    assert NO_HANDOVER_KINDS == frozenset({OPEN_APP, CLOSE_APP})
    assert OPEN_BROWSER in ALL_KINDS and CLOSE_BROWSER in ALL_KINDS
    assert TYPED_CONSOLE.allows(OPEN_BROWSER) and not VOICE_CONSOLE.allows(OPEN_BROWSER)
    assert not VOICE_CONSOLE.allows(CLOSE_BROWSER)


def test_the_prompt_teaches_the_distinction_within_its_cost_budget():
    """3 of the brief + the existing cost control."""
    assert len(brain.SYSTEM_PROMPT) < 4000, "the prompt is paid for on every request"
    assert "open_browser, close_browser" in brain.SYSTEM_PROMPT
    assert ASSISTANT_BROWSER in brain.SYSTEM_PROMPT
    assert "open_app" in brain.SYSTEM_PROMPT


# =====================================================================================================
# SESSION (matrix 6-13)
# =====================================================================================================

def test_opening_creates_exactly_one_session_with_opaque_ids(world):
    """6 + 12."""
    open_browser(world)
    sessions = executor_adapter.browser_sessions()
    assert len(sessions) == 1
    session_id, page_id = sessions[0]
    assert isinstance(session_id, str) and isinstance(page_id, str)
    for value in (session_id, page_id):
        for leak in ("Playwright", "Browser", "Page", "<", "object"):
            assert leak not in value, value


def test_the_production_path_requests_a_headed_browser(world):
    """10. Asserted, never launched: the user is the one who will navigate it."""
    open_browser(world)
    assert world.launches == [("chrome", 20.0, True)], world.launches
    # and the adapter turns that into headless=False
    assert "headless=not headed" in code_of_named(EXECUTOR_ADAPTER, "browser_open_session")


def test_closing_removes_the_session(world):
    """7."""
    open_browser(world)
    result = executor.execute(ExecutorAction(CLOSE_BROWSER), always(True))
    assert result.ok, result.message
    assert executor_adapter.browser_sessions() == []


def test_closing_with_no_session_refuses_honestly(world):
    """8."""
    result = executor.execute(ExecutorAction(CLOSE_BROWSER), always(True))
    assert not result.ok and "isn't open" in result.message
    assert world.prompts == [], "nothing to confirm when there is nothing to close"


def test_opening_twice_refuses_and_creates_no_second_session(world):
    """9. One assistant browser at a time, frozen for this slice."""
    open_browser(world)
    result = executor.execute(ExecutorAction(OPEN_BROWSER), always(True))
    assert not result.ok and "already open" in result.message
    assert len(world.launches) == 1, "no second launch"
    assert len(executor_adapter.browser_sessions()) == 1


def test_the_canonical_lifecycle_commands_cost_no_model_call(world):
    """11. Parsed deterministically, so the Brain is never asked."""
    for line, kind in (("open assistant browser", OPEN_BROWSER),
                       ("close assistant browser", CLOSE_BROWSER),
                       ("OPEN  Assistant   Browser", OPEN_BROWSER)):
        parsed = commands.parse(line)
        assert isinstance(parsed, ExecutorAction) and parsed.kind == kind, line
        assert parsed.target == ""
    route = brain.route("open assistant browser", executor.resolve)
    assert isinstance(route, brain.LocalAction), f"{route} would have cost a model call"
    assert route.action.kind == OPEN_BROWSER


def test_no_live_playwright_object_leaves_the_adapter():
    """12."""
    code = code_of_named(EXECUTOR_ADAPTER, "browser_sessions")
    assert "session.pages" in code and "_browser_sessions.items()" in code
    tree = ast.parse(textwrap.dedent(code))
    returned = [ast.unparse(n.value) for n in ast.walk(tree) if isinstance(n, ast.Return) and n.value]
    assert all("page_id" in r and "session_id" in r for r in returned), returned
    for forbidden in ("runtime", "browser", "context", "tokens"):
        assert f"session.{forbidden}" not in code, forbidden


def test_closing_the_assistant_browser_never_touches_personal_chrome(world):
    """13. Personal Chrome is a launched APPLICATION with a window token, in a different registry.
    Even with it open at the same time, close_browser cannot reach it."""
    world.windows.append(WindowInfo(9200, "Something - Google Chrome",
                                    class_name="Chrome_WidgetWin_1"))
    assert executor._remember_opened("chrome", frozenset({9200}))
    open_browser(world)

    result = executor.execute(ExecutorAction(CLOSE_BROWSER), always(True))
    assert result.ok, result.message
    assert executor_adapter.browser_sessions() == []
    # the owned personal-Chrome window record is untouched, and nothing asked it to close
    with executor._session_lock:
        assert "chrome" in executor._session_windows
    assert world.closed == [], "no window close was requested"
    code = code_of(executor._prepare_close_browser)
    for forbidden in ("request_close", "CLOSE_APP", "_session_windows", "window"):
        assert forbidden not in code, forbidden


# =====================================================================================================
# THE CONTEXT CHOOSER (matrix 14-22)
# =====================================================================================================

def test_only_an_owned_app_routes_to_uia(world):
    """14."""
    own_calculator()
    result = executor.execute(named("Seven"), always(True))
    assert result.ok, result.message
    assert world.clicks == [CENTRE], "the UIA path sends a physical click"
    assert world.page.clicks == []


def test_only_a_browser_session_routes_to_dom(world):
    """15."""
    open_browser(world)
    result = executor.execute(named("Login"), always(True))
    assert result.ok, result.message
    assert world.page.clicks == ["Login"], "the DOM path clicks in the page"
    assert world.clicks == [], "and sends no physical click"


def test_both_live_and_nothing_named_refuses_naming_both(world):
    """16. THE MANDATORY TEST. Two contexts is a question, never a quiet preference."""
    own_calculator()
    open_browser(world)
    world.prompts.clear()        # the setup's own 'open browser' authorization is not the subject

    result = executor.execute(named("Login"), always(True))

    assert not result.ok, "it must not choose for the user"
    assert world.clicks == [], "no physical click"
    assert world.page.clicks == [], "no DOM click"
    assert world.prompts == [], "and nothing was even confirmed"
    assert "calculator" in result.message, result.message
    assert ASSISTANT_BROWSER in result.message, result.message
    assert "more than one" in result.message


def test_naming_the_assistant_browser_routes_to_dom_even_with_an_app_open(world):
    """17."""
    own_calculator()
    open_browser(world)
    result = executor.execute(named("Login", ASSISTANT_BROWSER), always(True))
    assert result.ok, result.message
    assert world.page.clicks == ["Login"] and world.clicks == []


def test_naming_an_app_routes_to_uia_even_with_a_session_live(world):
    """18."""
    own_calculator()
    open_browser(world)
    result = executor.execute(named("Seven", "calculator"), always(True))
    assert result.ok, result.message
    assert world.clicks == [CENTRE] and world.page.clicks == []


def test_naming_the_assistant_browser_with_no_session_refuses(world):
    """19."""
    own_calculator()
    result = executor.execute(named("Login", ASSISTANT_BROWSER), always(True))
    assert not result.ok and world.page.clicks == [] and world.clicks == []
    assert "don't have an assistant browser open" in result.message


def test_chrome_never_routes_to_the_assistant_browser(world):
    """20. `chrome` is a configured app. It is UIA, always."""
    world.windows.append(WindowInfo(9200, "Something - Google Chrome",
                                    class_name="Chrome_WidgetWin_1"))
    assert executor._remember_opened("chrome", frozenset({9200}))
    open_browser(world)
    context = executor._context_for_named_click("chrome")
    assert isinstance(context, executor._AppWindowContext), context
    assert context.app == "chrome" and context.window.handle == 9200
    assert not isinstance(context, executor._BrowserPageContext)


def test_an_application_alias_cannot_redirect_the_reserved_selector(world):
    """21. The selector is runtime context, recognised before configuration and before Memory."""
    own_calculator()
    open_browser(world)
    asked = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(console.memory_queries, "application",
                      lambda *a, **k: asked.append(a) or "calculator")
        resolution = executor.resolve(named("Login", ASSISTANT_BROWSER))
        context = executor._context_for_named_click(ASSISTANT_BROWSER)
    assert resolution.value == ("Login", ASSISTANT_BROWSER)
    assert isinstance(context, executor._BrowserPageContext), context
    assert asked == [], "Memory was consulted about the reserved selector"
    # proven from the resolver's own order, too: the selector returns before configuration is read
    code = code_of(executor._resolve_click_target)
    assert code.index("ASSISTANT_BROWSER") < code.index("_configured_apps"), code


def test_neither_context_keeps_the_existing_refusal(world):
    """22."""
    result = executor.execute(named("Login"), always(True))
    assert not result.ok
    assert "haven't opened anything yet" in result.message


def test_two_owned_apps_keep_their_existing_wording(world):
    """3 of the brief: reuse the existing refusal for two owned apps."""
    own_calculator()
    world.windows.append(WindowInfo(9200, "Something - Google Chrome",
                                    class_name="Chrome_WidgetWin_1"))
    assert executor._remember_opened("chrome", frozenset({9200}))
    result = executor.execute(named("Login"), always(True))
    assert not result.ok and "more than one app in this session" in result.message


def test_the_two_contexts_are_different_types():
    """3 of the brief: an opaque session id can never be handled as a window handle."""
    app_fields = set(executor._AppWindowContext.__dataclass_fields__)
    page_fields = set(executor._BrowserPageContext.__dataclass_fields__)
    assert app_fields == {"app", "window"}
    assert page_fields == {"session_id", "page_id"}
    assert not app_fields & page_fields, "no field is shared, so neither can stand in for the other"
    assert "handle" not in " ".join(page_fields)


# =====================================================================================================
# ROUTING (matrix 23-25)
# =====================================================================================================

def test_the_browser_context_uses_the_one_dom_path(world):
    """23 + 25."""
    open_browser(world)
    calls = []
    real = executor._prepare_dom_click

    def recording(action, target):
        calls.append(target.name)
        return real(action, target)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(executor, "_prepare_dom_click", recording)
        result = executor.execute(named("Login"), always(True))
    assert result.ok and calls == ["Login"], calls
    assert world.page.clicks == ["Login"]


def test_the_app_context_uses_the_one_uia_path(world):
    """24 + 25."""
    own_calculator()
    calls = []
    real = executor._prepare_target_click

    def recording(action, target, observed):
        calls.append(target.name)
        return real(action, target, observed)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(executor, "_prepare_target_click", recording)
        result = executor.execute(named("Seven"), always(True))
    assert result.ok and calls == ["Seven"], calls
    assert world.clicks == [CENTRE]


def test_neither_click_path_was_duplicated():
    """25. One physical click, one DOM click, each with one caller."""
    root = settings.PROJECT_ROOT / "app"
    physical, dom = [], []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "click" and getattr(node.func.value, "id", "") == "adapter":
                    physical.append(path.relative_to(root).as_posix())
                if node.func.attr == "dom_click":
                    dom.append(path.relative_to(root).as_posix())
    assert physical == ["executor/logic.py"], physical
    assert dom == ["executor/logic.py"], dom
    assert code_of(executor._send_click).count("adapter.click") == 1
    # and the named-click preparer delegates rather than reimplementing either
    code = code_of(executor._prepare_named_click)
    assert "_prepare_dom_click" in code and "_prepare_target_click" in code
    assert "locator" not in code and "adapter.click" not in code


def test_the_hwnd_justification_is_recorded_in_the_code():
    """4 of the brief."""
    source = (settings.PROJECT_ROOT / "app" / "executor" / "logic.py").read_text(encoding="utf-8")
    assert "HWND mapping is not required for that scoped workflow" in source
    assert "Browser chrome and native window controls remain UIA territory" in source


# =====================================================================================================
# SAFETY (matrix 26-29)
# =====================================================================================================

def test_a_dom_click_through_the_console_path_is_still_medium(world):
    """26."""
    open_browser(world)
    result = executor.execute(named("Login"), always(True))
    assert result.ok
    description, level, confirmed = world.prompts[-1]
    assert level is RiskLevel.MEDIUM and confirmed is True
    assert description == 'click "Login" in the assistant browser'
    assert result.outcome is Outcome.UNVERIFIED


def test_a_uia_click_through_the_console_path_is_still_medium(world):
    """27."""
    own_calculator()
    result = executor.execute(named("Seven"), always(True))
    assert result.ok and world.prompts[-1][1] is RiskLevel.MEDIUM


@pytest.mark.parametrize("answer", [False, None, "yes", "y", "yees", "", 1])
def test_only_exact_yes_permits_either_path(world, answer):
    """26 + 27."""
    open_browser(world)
    with pytest.raises(safety_logic.ActionDeniedError):
        executor.execute(named("Login"), always(answer))
    assert world.page.clicks == []


def test_the_lifecycle_kinds_follow_the_existing_open_close_policy(world):
    """28. Opening mirrors open_app (no floor); closing mirrors close_app (MEDIUM constant)."""
    open_browser(world)
    opened = world.prompts[-1] if world.prompts else None
    assert opened is None or opened[1] < RiskLevel.MEDIUM, "opening must not newly demand a prompt"

    world.prompts.clear()
    result = executor.execute(ExecutorAction(CLOSE_BROWSER), always(True))
    assert result.ok
    assert world.prompts[-1][1] is RiskLevel.MEDIUM, "closing mirrors close_app"
    assert executor._CLOSE_RISK is RiskLevel.MEDIUM


def test_no_floor_can_lower_either_click(world):
    """29."""
    open_browser(world)
    result = executor.execute(named("Login"), always(True), risk_floor=RiskLevel.LOW)
    assert result.ok and world.prompts[-1][1] is RiskLevel.MEDIUM


# =====================================================================================================
# PRIVACY / ISOLATION (matrix 30-33)
# =====================================================================================================

def test_no_session_or_page_id_reaches_a_log(world, caplog):
    """30."""
    caplog.set_level("DEBUG")
    open_browser(world)
    executor.execute(named("Login"), always(True))
    executor.execute(ExecutorAction(CLOSE_BROWSER), always(True))

    logged = " ".join(record.getMessage() for record in caplog.records)
    for secret in (SESSION, PAGE, "Login", "about:blank"):
        assert secret not in logged, f"{secret!r} reached the logs: {logged}"


def test_the_context_for_open_browser_carries_nothing_but_its_kind():
    """30 + 6 of the brief: what the provider may be told about it."""
    from app.brain.models import previous_action_context
    context = previous_action_context(OPEN_BROWSER, SESSION)
    assert context.kind == OPEN_BROWSER and context.safe_target is None
    context = previous_action_context(CLOSE_BROWSER, SESSION)
    assert context.safe_target is None


def test_ordinary_tests_launch_no_browser():
    """31. Asked without the world fixture, so nothing is faked."""
    import sys
    from safety_guards import PhysicalBrowserEscaped
    assert "playwright" not in sys.modules
    with pytest.raises(PhysicalBrowserEscaped):
        executor_adapter.browser_open_session("chrome", 1.0, True)
    with pytest.raises(PhysicalBrowserEscaped):
        import playwright  # noqa: F401


def test_the_session_read_is_not_a_physical_boundary():
    """31. It reads this process's own dict and can escape nowhere, so it is deliberately not
    refused - otherwise every test about the CHOICE of context would have to fake it."""
    assert "browser_sessions" not in safety_guards.BROWSER_BOUNDARIES
    assert executor_adapter.browser_sessions() == []


def test_no_memory_or_desktop_state_is_created_by_the_lifecycle(world):
    """33."""
    open_browser(world)
    assert not (settings.PROJECT_ROOT / "data" / "memory.db").exists()
    code = code_of(executor._prepare_open_browser) + code_of(executor._prepare_close_browser)
    for forbidden in ("memory", "sqlite", "pyautogui", "SetForegroundWindow", "speak"):
        assert forbidden not in code.lower(), forbidden


def test_the_smoke_fixture_is_self_contained():
    """9 of the brief: a fixture for the owner's smoke, with no network and no production role."""
    fixture = settings.PROJECT_ROOT / "tests" / "fixtures" / "assistant_browser_smoke.html"
    assert fixture.exists()
    html = fixture.read_text(encoding="utf-8")
    for remote in ("http://", "https://", "//cdn", "src=\"http", "@import"):
        assert remote not in html, remote
    assert html.count("<button") == 2
    assert ">Login<" in html and ">Sign up<" in html
    assert "Clicked" in html, "a person must be able to see that it landed"
    root = settings.PROJECT_ROOT / "app"
    for path in root.rglob("*.py"):
        assert "assistant_browser_smoke" not in path.read_text(encoding="utf-8"), path
