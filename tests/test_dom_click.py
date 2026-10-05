"""
Tests for Phase 5 DOM Slice 2 - the one authorized DOM click.

The property this file exists to hold:

    the click happens only after the user has typed yes, only after the target has been found AGAIN
    on the same page, only when it is still exactly one control of the same kind - and the result
    never claims more than that the element received the click.

Everything is faked, including Playwright. The REAL safety gate, the REAL observation rules and the
REAL Executor seam decide every outcome; only the browser is a stand-in. What that cannot prove is
listed in the report: that a real locator.click() lands, and that a real page's identity and
actionability behave as assumed.

Checks about code are made against its AST with docstrings and comments stripped. A substring search
over this project's source has matched its own explanation of a rule seven times now, so the habit is
not optional.
"""
import ast
import inspect
import textwrap
import time
from types import SimpleNamespace

import pytest

import safety_guards
from app.executor import adapter as executor_adapter
from app.executor import emergency_stop
from app.executor import logic as executor
from app.executor.emergency_stop import EmergencyStopError
from app.executor.models import CLICK_TARGET, Outcome
from app.safety import logic as safety_logic
from app.safety.models import RiskLevel
from app.verifier.models import DomTarget, normalize_name
from config import settings

SESSION = "s" * 32
PAGE = "p" * 16
TARGET = "Login"

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
    """One function's code from the FILE - the guard replaces the adapter's boundaries, so
    inspect.getsource() on them would return the guard's refuser instead."""
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


def genuine(name):
    """The adapter's real function, rebuilt from source so the guard's refuser is bypassed for the
    function under test - and only for it."""
    namespace = {}
    exec(compile(textwrap.dedent(code_of_named(EXECUTOR_ADAPTER, name)), "<adapter>", "exec"),
         vars(executor_adapter), namespace)
    return namespace[name]


# =====================================================================================================
# A FAKE PLAYWRIGHT PAGE - the only page any ordinary test sees
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
        assert len(self._controls) == 1
        return self._controls[0]

    def click(self, timeout=None, **kwargs):
        assert timeout is not None, "every Playwright call must carry an explicit timeout"
        assert not kwargs, f"the click must be the ordinary action, not {sorted(kwargs)}"
        self._page.clicks.append((self._one["name"], self._one["role"], timeout))

    def bounding_box(self, timeout=None):
        return self._one.get("box")

    def is_enabled(self, timeout=None):
        return self._one.get("enabled", True)

    def is_visible(self, timeout=None):
        return self._one.get("visible", True)

    def get_attribute(self, name, timeout=None):
        return self._one.get("attrs", {}).get(name)

    def evaluate(self, expression, timeout=None):
        return self._one.get("tag", "button")

    # Anything that would be a forbidden way to click. Reaching one fails the test.
    def dispatch_event(self, *a, **k):
        raise AssertionError("dispatch_event is not how this clicks")

    def press(self, *a, **k):
        raise AssertionError("press is not how this clicks")

    def fill(self, *a, **k):
        raise AssertionError("the DOM layer does not type")

    def focus(self, *a, **k):
        raise AssertionError("the DOM layer does not focus")


class FakePage:
    def __init__(self, controls, frames=1, url="about:blank"):
        self.controls = controls
        self.frames = [object()] * frames
        self._url = url
        self.clicks = []

    @property
    def url(self):
        return self._url

    def navigate_to(self, url):
        """TEST helper: pretend the page became a different one."""
        self._url = url

    def get_by_role(self, role, name=None, exact=None):
        assert exact is None, "exact must not be passed: Playwright ignores it for a pattern"
        assert hasattr(name, "fullmatch"), "the name must be a compiled pattern"
        return FakeLocator([c for c in self.controls if c["role"] == role
                            and name.fullmatch(" ".join(c["name"].split()))], self)

    def content(self):
        raise AssertionError("the DOM layer must not read page HTML")

    def inner_text(self, *a, **k):
        raise AssertionError("the DOM layer must not read page text")


def control(name, role="button", **extra):
    base = dict(name=name, role=role, tag="button", enabled=True, visible=True, attrs={},
                box={"x": 1, "y": 2, "width": 30, "height": 10})
    base.update(extra)
    return base


@pytest.fixture
def world(monkeypatch):
    """A live session holding a fake page, with the genuine query and action reinstalled."""
    page = FakePage([control(TARGET)])
    monkeypatch.setattr(executor_adapter, "dom_query", genuine("dom_query"))
    monkeypatch.setattr(executor_adapter, "dom_page_has_frames", genuine("dom_page_has_frames"))
    monkeypatch.setattr(executor_adapter, "dom_click", genuine("dom_click"))

    state = executor_adapter._BrowserSession(
        runtime=SimpleNamespace(stop=lambda: None), browser=SimpleNamespace(close=lambda: None),
        context=SimpleNamespace(close=lambda: None), pages={PAGE: page}, tokens={})
    with executor_adapter._browser_lock:
        executor_adapter._browser_sessions[SESSION] = state

    prompts = []
    real_authorize = safety_logic.authorize

    def recording_authorize(action, confirm=None):
        decision = real_authorize(action, confirm)
        prompts.append((action.description, decision.assessment.level, decision.confirmed))
        return decision

    monkeypatch.setattr(executor, "authorize", recording_authorize)
    emergency_stop.reset("dom-click-test")
    desktop = SimpleNamespace(page=page, prompts=prompts, session=state)
    yield desktop
    with executor_adapter._browser_lock:
        executor_adapter._browser_sessions.pop(SESSION, None)
    emergency_stop.reset("dom-click-test")


def target(name=TARGET, session=SESSION, page=PAGE) -> DomTarget:
    return DomTarget(name, session, page)


# =====================================================================================================
# ARCHITECTURE (matrix 1-6)
# =====================================================================================================

def test_the_dom_action_lives_only_in_the_executor_adapter():
    """1 + 2 + 3."""
    root = settings.PROJECT_ROOT / "app"
    definers, callers = [], []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef) and node.name == "dom_click":
                definers.append(path.relative_to(root).as_posix())
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "dom_click":
                callers.append(path.relative_to(root).as_posix())
    assert definers == ["executor/adapter.py"], definers
    assert callers == ["executor/logic.py"], callers
    for folder in ("verifier", "brain", "planner"):
        for path in (root / folder).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "playwright" not in text, f"{folder}/{path.name}"
            assert "dom_click" not in text, f"{folder}/{path.name} must not click"


def test_no_live_playwright_object_escapes_the_adapter():
    """4. The action takes an opaque token, not a locator, and returns a string outcome."""
    code = code_of_named(EXECUTOR_ADAPTER, "dom_click")
    tree = ast.parse(textwrap.dedent(code))
    signature = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    assert [a.arg for a in signature.args.args] == ["session_id", "page_id", "element_token",
                                                    "action_timeout_seconds"]
    returned = {ast.unparse(n.value) for n in ast.walk(tree) if isinstance(n, ast.Return) and n.value}
    for value in returned:
        assert value.startswith("DOM_"), f"only outcome constants may be returned, not {value}"


def test_dom_click_is_centrally_guarded():
    """5."""
    assert "dom_click" in safety_guards.BROWSER_BOUNDARIES
    from safety_guards import PhysicalBrowserEscaped
    with pytest.raises(PhysicalBrowserEscaped):
        executor_adapter.dom_click(SESSION, PAGE, "tok", 1.0)


def test_the_desktop_gate_does_not_authorize_a_dom_click(monkeypatch):
    """6."""
    monkeypatch.setenv("RUN_REAL_DESKTOP_TEST", "1")
    monkeypatch.delenv("RUN_REAL_BROWSER_TEST", raising=False)
    desktop_only = type("Node", (), {
        "get_closest_marker": lambda self, n: object() if n == "real_desktop" else None})()
    assert safety_guards.exempt(desktop_only, safety_guards.DESKTOP_EXEMPT) is True
    assert safety_guards.exempt(desktop_only, safety_guards.BROWSER_EXEMPT) is False


def test_no_new_action_kind_was_added():
    """The Brain's vocabulary must not widen for this slice."""
    from app.brain.models import ARGS_FOR_KIND
    assert set(ARGS_FOR_KIND) == set(executor._PREPARERS)
    assert "dom" not in " ".join(ARGS_FOR_KIND)


# =====================================================================================================
# ORDER (matrix 7-13)
# =====================================================================================================

def test_the_whole_order_is_resolve_confirm_reresolve_click(world):
    """7 + 10 + 11. Recorded: the page is read once to resolve, the user answers, then it is read
    AGAIN before anything is clicked."""
    order = []
    real_query, real_click = executor_adapter.dom_query, executor_adapter.dom_click

    def query(*a, **k):
        order.append("query")
        return real_query(*a, **k)

    def click(*a, **k):
        order.append("click")
        return real_click(*a, **k)

    def confirm(action, assessment):
        order.append("confirm")
        return True

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(executor_adapter, "dom_query", query)
        patch.setattr(executor_adapter, "dom_click", click)
        result = executor.click_dom_target(target(), confirm)
    assert result.ok, result.message
    assert order == ["query", "confirm", "click"], order
    assert len(world.page.clicks) == 1


def test_nothing_is_clicked_before_the_confirmation(world):
    """8."""
    seen = []

    def confirm(action, assessment):
        seen.append(list(world.page.clicks))
        return True

    executor.click_dom_target(target(), confirm)
    assert seen == [[]], "something was clicked before the user answered"


@pytest.mark.parametrize("answer", [False, None, "yes", "y", 1, "", "yees", "click Login"])
def test_a_non_yes_answer_clicks_nothing(world, answer):
    """9 + 38."""
    with pytest.raises(safety_logic.ActionDeniedError):
        executor.click_dom_target(target(), always(answer))
    assert world.page.clicks == []


def test_the_stop_is_checked_immediately_before_and_after_the_action():
    """12 + 13. Read from the code, because a blocking Playwright call cannot be interrupted part
    way - before and after is the honest guarantee, and it must actually be there."""
    code = code_of(executor._prepare_dom_click)
    assert code.count("emergency_stop.check()") == 2, code
    tree = ast.parse(textwrap.dedent(inspect.getsource(executor._prepare_dom_click)))
    tries = [n for n in ast.walk(tree) if isinstance(n, ast.Try)]
    after = [t for t in tries if t.finalbody and "emergency_stop.check()" in
             "\n".join(ast.unparse(stmt) for stmt in t.finalbody)]
    assert after, "the check after the action must be in a finally, so a failure cannot skip it"


def test_a_stop_before_the_action_prevents_the_click(world):
    """12."""
    emergency_stop.trigger("before")
    with pytest.raises(EmergencyStopError):
        executor.click_dom_target(target(), always(True))
    assert world.page.clicks == []


def test_a_stop_pressed_during_the_confirmation_prevents_the_click(world):
    """12."""
    def confirm(action, assessment):
        emergency_stop.trigger("mid-confirmation")
        return True

    with pytest.raises(EmergencyStopError):
        executor.click_dom_target(target(), confirm)
    assert world.page.clicks == []


def test_a_stop_pressed_while_the_click_ran_is_reported_afterwards(world):
    """13. The limitation stated honestly: the click cannot be interrupted, so the stop is noticed
    immediately after it returns - which is what stops anything else continuing."""
    real_click = executor_adapter.dom_click

    def click(*a, **k):
        outcome = real_click(*a, **k)
        emergency_stop.trigger("during the click")
        return outcome

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(executor_adapter, "dom_click", click)
        with pytest.raises(EmergencyStopError):
            executor.click_dom_target(target(), always(True))
    assert len(world.page.clicks) == 1, "the click had already been delivered - that is the limitation"


# =====================================================================================================
# RE-IDENTIFICATION (matrix 14-21)
# =====================================================================================================

def test_the_same_unique_element_proceeds(world):
    """14."""
    result = executor.click_dom_target(target(), always(True))
    assert result.ok, result.message
    assert [(name, role) for name, role, _ in world.page.clicks] == [(TARGET, "button")]


def test_an_element_that_disappears_after_the_confirmation_clicks_nothing(world):
    """15."""
    def confirm(action, assessment):
        world.page.controls = []
        return True

    result = executor.click_dom_target(target(), confirm)
    assert not result.ok and world.page.clicks == []
    assert "wasn't there any more" in result.message


def test_a_duplicate_appearing_after_the_confirmation_refuses(world):
    """16."""
    def confirm(action, assessment):
        world.page.controls = [control(TARGET), control(TARGET, role="link")]
        return True

    result = executor.click_dom_target(target(), confirm)
    assert not result.ok and world.page.clicks == []
    assert "more than one" in result.message


def test_a_role_change_after_the_confirmation_refuses(world):
    """17. One control still answers to the name, but it is a different kind of thing."""
    def confirm(action, assessment):
        world.page.controls = [control(TARGET, role="link")]
        return True

    result = executor.click_dom_target(target(), confirm)
    assert not result.ok and world.page.clicks == []
    assert "same kind of control" in result.message


def test_a_page_change_after_the_confirmation_refuses(world):
    """18. The page is not the one that was confirmed, so nothing is clicked on it."""
    def confirm(action, assessment):
        world.page.navigate_to("about:srcdoc")
        return True

    result = executor.click_dom_target(target(), confirm)
    assert not result.ok and world.page.clicks == []
    assert "page changed while you were answering" in result.message


def test_a_target_that_moves_into_a_frame_refuses(world):
    """19."""
    def confirm(action, assessment):
        world.page.controls = []
        world.page.frames = [object(), object()]
        return True

    result = executor.click_dom_target(target(), confirm)
    assert not result.ok and world.page.clicks == []
    assert "frames I can't read yet" in result.message


def test_a_stale_session_or_page_clicks_nothing(world):
    """20 + 21."""
    for bad in (target(session="no-such-session"), target(page="no-such-page")):
        result = executor.click_dom_target(bad, always(True))
        assert not result.ok and world.page.clicks == []
    # and a token minted for one page cannot be used on another
    click = genuine("dom_click")
    executor_adapter.dom_query(SESSION, PAGE, "login", 1.0)
    token = next(iter(world.session.tokens))
    with pytest.raises(executor_adapter.BrowserError):
        click(SESSION, "other-page", token, 1.0)


def test_an_unknown_token_is_refused_never_guessed(world):
    """21. A token this adapter did not mint resolves to nothing, and nothing is clicked."""
    click = genuine("dom_click")
    with pytest.raises(executor_adapter.BrowserError):
        click(SESSION, PAGE, "not-a-token", 1.0)
    assert world.page.clicks == []


def test_no_element_handle_is_held_across_the_confirmation():
    """5 of the brief. The token stands for a DESCRIPTION, re-run after the answer - so the thing
    carried across the confirmation cannot detach."""
    description = code_of_named(EXECUTOR_ADAPTER, "_DomLocatorDescription") if False else None
    record = next(n for n in ast.walk(ast.parse(EXECUTOR_ADAPTER.read_text(encoding="utf-8")))
                  if isinstance(n, ast.ClassDef) and n.name == "_DomLocatorDescription")
    fields = [n.target.id for n in record.body if isinstance(n, ast.AnnAssign)]
    assert fields == ["page_id", "role", "name", "page_identity"], fields
    for forbidden in ("locator", "handle", "element", "selector"):
        assert not any(forbidden in f for f in fields), forbidden
    assert "element_handle" not in code_of_named(EXECUTOR_ADAPTER, "dom_click")


# =====================================================================================================
# MATCHING (matrix 22-27)
# =====================================================================================================

@pytest.mark.parametrize("asked", [TARGET, "login", "LOGIN", "  Login  "])
def test_the_name_matches_whole_string_case_insensitively(world, asked):
    """22 + 23 + 24."""
    result = executor.click_dom_target(target(name=asked), always(True))
    assert result.ok, f"{asked!r}: {result.message}"
    assert len(world.page.clicks) == 1


@pytest.mark.parametrize("asked", ["Log In", "Log", "gin", "Logins", "Log-in"])
def test_neither_substring_nor_near_spelling_clicks_anything(world, asked):
    """25 + 26."""
    result = executor.click_dom_target(target(name=asked), always(True))
    assert not result.ok and world.page.clicks == []


def test_first_match_guessing_is_impossible(world):
    """27. Two matches is a refusal at BOTH stages - before the confirmation and after it."""
    world.page.controls = [control(TARGET), control(TARGET, role="link")]
    result = executor.click_dom_target(target(), always(True))
    assert not result.ok and world.page.clicks == []
    assert "not going to guess" in result.message
    assert world.prompts == [], "nothing should have been confirmed"


def test_the_query_and_the_action_share_one_matching_implementation():
    """22-27. Two implementations could disagree about what the name means, and the disagreement
    would appear between the confirmation and the click."""
    for name in ("dom_query", "dom_click"):
        assert "_dom_matches" in code_of_named(EXECUTOR_ADAPTER, name), name
    matcher = code_of_named(EXECUTOR_ADAPTER, "_dom_matches")
    assert "re.IGNORECASE" in matcher and "re.escape" in matcher
    assert "exact=" not in matcher
    for guess in ("nth_child", "first", "last", "css=", "xpath="):
        assert guess not in matcher, guess


# =====================================================================================================
# ACTION (matrix 28-35)
# =====================================================================================================

def test_the_successful_path_clicks_exactly_once_with_a_bounded_timeout(world):
    """28 + 33."""
    result = executor.click_dom_target(target(), always(True))
    assert result.ok
    assert len(world.page.clicks) == 1
    _name, _role, timeout = world.page.clicks[0]
    assert timeout == settings.get_setting("browser.query_timeout_seconds") * 1000


def test_the_click_is_the_ordinary_locator_action_and_nothing_else():
    """29 + 30 + 31 + 32. Checked as CALLS in the adapter's action code."""
    code = code_of_named(EXECUTOR_ADAPTER, "dom_click")
    tree = ast.parse(textwrap.dedent(code))
    methods = {n.func.attr for n in ast.walk(tree)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "click" in methods, "it does click"
    for forbidden in ("dispatch_event", "evaluate", "evaluate_handle", "press", "fill", "type",
                      "focus", "hover", "tap", "goto", "mouse", "keyboard", "screenshot"):
        assert forbidden not in methods, forbidden
    # no physical-coordinate conversion anywhere on this path
    for pixels in ("bounding_box", "SetForegroundWindow", "pyautogui", "cursor_position",
                   "device_pixel_ratio"):
        assert pixels not in code, pixels
    # and actionability checks are never bypassed
    keywords = {kw.arg for n in ast.walk(tree) if isinstance(n, ast.Call) for kw in n.keywords}
    assert "force" not in keywords, "force would skip the hit-target check"
    assert "no_wait_after" not in keywords and "trial" not in keywords


def test_no_forbidden_click_mechanism_is_reachable_at_runtime(world):
    """29 + 30 + 31. The fake raises if the implementation reaches for one; a whole flow trips none."""
    result = executor.click_dom_target(target(), always(True))
    assert result.ok, result.message


def test_a_playwright_failure_is_reported_honestly(world):
    """34. It says the click could not be delivered - it does not claim it was."""
    def exploding_click(*a, **k):
        raise RuntimeError("element is not visible")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(FakeLocator, "click", exploding_click)
        result = executor.click_dom_target(target(), always(True))
    assert not result.ok
    assert "couldn't click" in result.message and "could not be delivered" in result.message
    for overclaim in ("Clicked", "did it", "succeeded"):
        assert overclaim not in result.message, overclaim


def test_a_successful_click_is_unverified_not_done(world):
    """35 + 13 of the brief. The element received the click. That is all it proves."""
    result = executor.click_dom_target(target(), always(True))
    assert result.ok and result.outcome is Outcome.UNVERIFIED and result.verified is False
    assert "can't check what the click did" in result.message
    for overclaim in ("logged in", "saved", "sent", "submitted", "worked"):
        assert overclaim not in result.message, overclaim


# =====================================================================================================
# SAFETY (matrix 36-40)
# =====================================================================================================

def test_a_dom_click_is_medium_and_confirmed_once(world):
    """36 + 37 + 40."""
    result = executor.click_dom_target(target(), always(True))
    assert result.ok
    assert len(world.prompts) == 1, "one confirmation, not two"
    description, level, confirmed = world.prompts[0]
    assert level is RiskLevel.MEDIUM and confirmed is True
    assert description == 'click "Login" in the assistant browser'


def test_dom_precision_cannot_lower_the_risk(world):
    """39."""
    result = executor.click_dom_target(target(), always(True), risk_floor=RiskLevel.LOW)
    assert result.ok and world.prompts[-1][1] is RiskLevel.MEDIUM
    assert executor._DOM_CLICK_RISK is RiskLevel.MEDIUM


def test_a_higher_floor_still_raises(world):
    result = executor.click_dom_target(target(), always(True), risk_floor=RiskLevel.HIGH)
    assert result.ok and world.prompts[-1][1] is RiskLevel.HIGH


def test_global_safety_behaviour_is_untouched():
    """8 of the brief: the gate itself is not altered for this slice."""
    assert safety_logic.CONFIRMATION_REQUIRED_AT is RiskLevel.MEDIUM
    assert "dom" not in code_of(safety_logic.authorize).lower()
    assert "browser" not in code_of(safety_logic.assess).lower()


# =====================================================================================================
# PRIVACY (matrix 41-47)
# =====================================================================================================

def test_the_confirmation_name_comes_from_the_caller_not_the_page(world):
    """41 + 42. The page calls it "Login"; the prompt would say whatever the CALLER asked for."""
    world.page.controls = [control("Login")]
    executor.click_dom_target(DomTarget("  login  ", SESSION, PAGE), always(True))
    assert world.prompts[-1][0] == 'click "login" in the assistant browser', world.prompts[-1][0]
    source = code_of(executor._prepare_dom_click)
    assert "target.name" in source
    for from_page in ("element.name", "accessible_name", "inner_text", "text_content"):
        assert from_page not in source, from_page


def test_no_url_selector_or_page_content_is_returned_or_logged(world, caplog):
    """43 + 44. The page identity is a one-way digest, and even that stays in the adapter."""
    caplog.set_level("DEBUG")
    world.page._url = "https://private.example.com/account?token=secret"
    result = executor.click_dom_target(target(), always(True))
    assert result.ok

    logged = " ".join(record.getMessage() for record in caplog.records)
    shown = result.message + " " + logged
    for secret in ("private.example.com", "token=secret", "https://", "about:blank", "css=", "xpath="):
        assert secret not in shown, f"{secret!r} leaked: {shown}"
    assert TARGET not in logged, "the user's word must not reach the logs"
    assert "dom target: clicked" in logged


def test_the_page_identity_never_leaves_the_adapter():
    """43. The digest is compared inside dom_click; no function returns or logs it."""
    identity = code_of_named(EXECUTOR_ADAPTER, "_page_identity")
    assert "sha256" in identity and "hexdigest" in identity
    click = code_of_named(EXECUTOR_ADAPTER, "dom_click")
    assert "_page_identity(page) != description.page_identity" in click
    # the one thing returned for a mismatch is a constant, not the identity
    assert "return DOM_PAGE_CHANGED" in click
    root = settings.PROJECT_ROOT / "app"
    for path in root.rglob("*.py"):
        if path.name == "adapter.py" and "executor" in path.parts:
            continue
        assert "_page_identity" not in path.read_text(encoding="utf-8"), path


def test_no_form_value_password_cookie_or_storage_is_touched():
    """45 + 47."""
    code = "\n".join(code_of_named(EXECUTOR_ADAPTER, name)
                     for name in ("dom_click", "_dom_matches", "_page_identity"))
    for forbidden in ("input_value", "text_content", "inner_text", "inner_html", "content()",
                      "cookies", "local_storage", "session_storage", "storage_state",
                      "get_attribute('value'", "fill("):
        assert forbidden not in code, forbidden


def test_no_observation_content_can_reach_the_provider():
    """46."""
    for path in (EXECUTOR_ADAPTER, settings.PROJECT_ROOT / "app" / "executor" / "logic.py"):
        imported = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
            elif isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
        assert not any(name.startswith(("app.brain", "anthropic", "httpx")) for name in imported)


# =====================================================================================================
# ISOLATION (matrix 48-56)
# =====================================================================================================

def test_an_ordinary_test_cannot_perform_a_real_dom_click_or_load_playwright():
    """48 + 52 + 53."""
    import sys
    from safety_guards import PhysicalBrowserEscaped
    assert "playwright" not in sys.modules
    for name in safety_guards.BROWSER_BOUNDARIES:
        with pytest.raises(PhysicalBrowserEscaped):
            getattr(executor_adapter, name)(SESSION, PAGE, "tok", 1.0)
    with pytest.raises(PhysicalBrowserEscaped):
        import playwright  # noqa: F401


def test_the_marker_alone_and_the_gate_alone_are_both_insufficient(monkeypatch):
    """49 + 50 + 51."""
    marked = type("Node", (), {"get_closest_marker": lambda self, n: object()})()
    bare = type("Node", (), {"get_closest_marker": lambda self, n: None})()
    monkeypatch.delenv("RUN_REAL_BROWSER_TEST", raising=False)
    assert safety_guards.exempt(marked, safety_guards.BROWSER_EXEMPT) is False
    monkeypatch.setenv("RUN_REAL_BROWSER_TEST", "1")
    assert safety_guards.exempt(bare, safety_guards.BROWSER_EXEMPT) is False
    assert safety_guards.exempt(marked, safety_guards.BROWSER_EXEMPT) is True


def test_no_desktop_audio_screenshot_or_memory_action_is_on_this_path(world):
    """56."""
    code = code_of(executor._prepare_dom_click) + code_of(executor.click_dom_target)
    for forbidden in ("pyautogui", "SetForegroundWindow", "activate_window", "winmm", "speak",
                      "screenshot", "sqlite", "memory"):
        assert forbidden not in code, forbidden
    executor.click_dom_target(target(), always(True))
    assert not (settings.PROJECT_ROOT / "data" / "memory.db").exists()
