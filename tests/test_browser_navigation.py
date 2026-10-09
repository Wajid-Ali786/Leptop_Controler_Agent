"""
SLICE 5: navigate the assistant's OWN browser to a web address the user typed.

WHY. The assistant browser is the only reliable clicking surface in this project, and until now it
could act only on pages the owner put there by hand. This makes

    open assistant browser
    open this site https://example.com
    click Login

one conversation - subject to the context-chooser limit the third command runs into, which is pinned
below rather than designed around.

WHAT IS NEW IS EGRESS. The provider path has used the network since Phase 3; this is the first way for
the assistant to load an ARBITRARY website. So the address is checked twice, by two different owners:

    app/planner/logic._url_provenance  - did it come from the USER, or did the model invent it?
    app/executor/logic._navigable_url  - is the scheme one we touch at all? Checked in the preparer,
                                         so a refused address never reaches a function that can open a
                                         socket.

AND A PAGE GAINS NOTHING BY BEING LOADED: no page text, title, address or cookie is read back, none of
it reaches the Brain, and the DOM click path still resolves only the control name the user gave.
"""
import ast
import inspect
import textwrap
from types import SimpleNamespace

import pytest

from app.brain.models import ARGS_FIELDS, ARGS_FOR_KIND, Intent, NavigateArgs, Understood
from app.executor import adapter as executor_adapter
from app.executor import emergency_stop, logic
from app.executor.logic import execute
from app.executor.models import CLICK_TARGET, NAVIGATE, ExecutorAction, Outcome
from app.planner import logic as planner
from app.planner.models import TYPED_CONSOLE, VOICE_CONSOLE, PlanRefusal
from app.safety import logic as safety_logic
from app.safety.models import RiskLevel
from config import settings

URL = "https://example.com"
SESSION, PAGE = "s1", "p1"

CONFIG = (
    "safety:\n"
    '  risky_keywords: [delete, shutdown, send]\n'
    "  safe_words: [sender]\n"
    "executor:\n"
    "  apps: {chrome: chrome.exe}\n"
    "  max_attempts: 1\n"
    "browser:\n"
    "  channel: chrome\n"
    "  launch_timeout_seconds: 5.0\n"
    "  query_timeout_seconds: 2.0\n"
    "  navigate_timeout_seconds: 3.0\n"
    "verifier:\n"
    "  window_timeout_seconds: 0.2\n"
    "  poll_interval_seconds: 0.01\n"
    '  app_windows: {chrome: "Chrome$"}\n'
)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A fake assistant browser. Nothing here can reach a network or a real browser process."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_PATH", config_path)
    state = SimpleNamespace(sessions=[(SESSION, PAGE)], navigations=[], outcome=executor_adapter.NAVIGATED,
                            error=None, prompts=[], clicks=[], page_token="tok-1")

    def browser_navigate(session_id, page_id, url, navigate_timeout_seconds):
        state.navigations.append((session_id, page_id, url, navigate_timeout_seconds))
        if state.error is not None:
            raise state.error
        return state.outcome

    monkeypatch.setattr(executor_adapter, "browser_sessions", lambda: list(state.sessions))
    monkeypatch.setattr(executor_adapter, "browser_navigate", browser_navigate)
    emergency_stop.reset("test-setup")
    yield state
    emergency_stop.reset("test-teardown")


def navigate(url=URL):
    return ExecutorAction(NAVIGATE, "", "", url)


def confirming(state, answer=True):
    def confirm(action, assessment):
        state.prompts.append((action.description, assessment.level))
        return answer
    return confirm


def run(state, url=URL, answer=True):
    return execute(navigate(url), confirming(state, answer))


def plan(url, user_text, frontend=TYPED_CONSOLE):
    """One navigate intent through the real Planner, with the user's own words."""
    understood = Understood(intents=(Intent(NAVIGATE, NavigateArgs(url), why="you asked",
                                            risk_floor=RiskLevel.LOW),), restated="open that site")
    return planner.build_plan(understood, frontend, logic.resolve, user_text)


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


# ======================================================================================================
# NAVIGATION (matrix 1-9)
# ======================================================================================================

@pytest.mark.parametrize("url", ["https://example.com", "https://example.com/a/b?c=1#d"])
def test_a_live_session_and_an_https_url_navigates(world, url):
    """1."""
    result = run(world, url)
    assert result.ok and result.outcome is Outcome.UNVERIFIED, result.message
    assert world.navigations == [(SESSION, PAGE, url, 3.0)]
    # honest about what it proves: the page was not read, so nothing claims what is on it
    assert "can't check what the page contains" in result.message


def test_an_http_url_navigates(world):
    """2. Not only https: a local or intranet page is a legitimate destination."""
    assert run(world, "http://example.com").ok
    assert world.navigations[0][2] == "http://example.com"


def test_no_session_refuses_and_says_to_open_one(world):
    """3."""
    world.sessions = []
    result = run(world)
    assert not result.ok
    assert "isn't open" in result.message and "open assistant browser" in result.message
    assert world.navigations == [], "it navigated with no session"
    assert world.prompts == [], "it asked for a confirmation it could not act on"


@pytest.mark.parametrize("url", ["file:///C:/Windows/win.ini", "javascript:alert(1)",
                                 "data:text/html,<b>x", "about:blank", "ftp://example.com",
                                 "chrome://settings", "vbscript:x"])
def test_only_http_and_https_are_opened(world, url):
    """4, 5, 6. file:// reads the disk; javascript: and data: execute in the page; the rest are
    browser-internal. Each refuses WITHOUT reaching the browser."""
    result = run(world, url)
    assert not result.ok
    assert "only open http and https" in result.message
    assert world.navigations == [], f"{url} reached the browser"


@pytest.mark.parametrize("url", ["", "   ", "not a url", "https://", "http:///nohost", "://x"])
def test_a_malformed_address_refuses_without_reaching_the_browser(world, url):
    """7."""
    result = run(world, url)
    assert not result.ok
    assert world.navigations == []


def test_validation_happens_before_any_browser_call(world):
    """8, STRUCTURALLY. The scheme check must come before the session lookup and before the adapter,
    so an address we will not touch never reaches a function that can open a socket."""
    code = code_of(logic._prepare_navigate)
    assert code.index("_navigable_url(action.url)") < code.index("adapter.browser_sessions()")
    assert code.index("_navigable_url(action.url)") < code.index("adapter.browser_navigate")
    # and the validator itself cannot reach anything
    pure = code_of(logic._navigable_url)
    for forbidden in ("adapter", "browser", "verifier", "requests", "socket", "urlopen"):
        assert forbidden not in pure, forbidden


def test_a_timeout_never_claims_the_page_loaded(world):
    """9. goto() may navigate and then run out of time waiting for the load event, so the page may be
    PARTLY there. Saying "loaded" would be a lie the next click acts on."""
    world.outcome = executor_adapter.NAVIGATE_TIMEOUT
    result = run(world)
    assert result.outcome is Outcome.UNVERIFIED
    assert "didn't finish loading" in result.message
    assert "Part of the page may be there" in result.message
    assert "can't tell you it loaded" in result.message
    assert "Look at the browser before clicking" in result.message


def test_a_timeout_leaves_the_session_usable(world):
    """9. "Preserve the session if it is still usable" - it is not closed or forgotten."""
    world.outcome = executor_adapter.NAVIGATE_TIMEOUT
    run(world)
    assert executor_adapter.browser_sessions() == [(SESSION, PAGE)]
    assert run(world).ok, "a second navigation after a timeout still works"


def test_a_browser_error_is_reported_and_claims_nothing(world):
    world.error = executor_adapter.BrowserError("the browser could not open that address")
    result = run(world)
    assert not result.ok and "couldn't navigate" in result.message


def test_one_navigation_per_action(world):
    """"One navigation per action; no redirect-following logic of our own.\""""
    run(world)
    assert len(world.navigations) == 1
    code = code_of(logic._prepare_navigate)
    assert code.count("adapter.browser_navigate") == 1
    for invented in ("redirect", "while True", "for _ in range", "retry"):
        assert invented not in code, invented


def test_the_timeout_comes_from_configuration(world):
    """Rule 3: every setting lives in config.yaml. And it is its OWN setting - see the note there."""
    run(world)
    assert world.navigations[0][3] == 3.0
    assert "browser.navigate_timeout_seconds" in code_of(logic._navigate_timeout)
    assert "query_timeout" not in code_of(logic._prepare_navigate)


# ======================================================================================================
# URL PROVENANCE (matrix 10-12)
# ======================================================================================================

def test_an_address_i_supplied_is_allowed():
    """10, the allowed half."""
    built = plan(URL, f"open this site {URL}")
    assert not isinstance(built, PlanRefusal), built
    [step] = built.steps
    assert step.action.url == URL


def test_a_scheme_the_model_completed_is_still_mine():
    """10. Typing "example.com" and the model adding https:// is COMPLETING a scheme for a host I
    supplied - not inventing a domain."""
    built = plan(URL, "open this site example.com")
    assert not isinstance(built, PlanRefusal), built


def test_an_address_i_did_not_supply_is_refused():
    """10, THE POINT. The model returning a different site than the one I typed."""
    built = plan("https://evil.example.net", f"open this site {URL}")
    assert isinstance(built, PlanRefusal)
    assert "address you've given me yourself" in built.message
    # the refusal does not echo the address that failed: it is the one thing here that was not mine
    assert "evil.example.net" not in built.message


def test_a_website_named_without_an_address_is_never_guessed():
    """11. "open the BBC website" must not become a navigation to a domain the model composed."""
    for guess in ("https://bbc.co.uk", "https://www.bbc.com", "http://bbc.com"):
        built = plan(guess, "open the bbc website")
        assert isinstance(built, PlanRefusal), guess


def test_an_observed_page_link_cannot_become_a_navigation_source():
    """12. A link read off a page is not in what I typed, so it cannot be navigated to. Nothing in the
    DOM path can reach navigation either: no observed URL exists to pass on."""
    built = plan("https://tracker.example.net/clicked", "click Login")
    assert isinstance(built, PlanRefusal)
    # and structurally: the navigate preparer takes its address from the ACTION, never from a page
    # The address comes from the ACTION and from nowhere else. Named precisely: an earlier draft
    # forbade "page_", which matched this function's own `page_id` - a legitimate session coordinate,
    # and the twelfth time in this project a substring has matched something innocent.
    code = code_of(logic._prepare_navigate)
    assert "action.url" in code
    tree = ast.parse(code)
    reads = {ast.unparse(node) for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "action.url" in reads
    assert not any(read.endswith(".url") and read != "action.url" for read in reads), reads
    for forbidden in ("dom_query", "dom_click", "observation", "href", "_page_identity"):
        assert forbidden not in code, forbidden


def test_the_provenance_check_runs_before_a_plan_exists():
    """The check is in the Planner, so a refused address never becomes a step the user is shown."""
    code = code_of(planner._step)
    assert code.index("_url_provenance") < code.index("ExecutorAction(")


def test_a_caller_that_supplies_no_text_gets_no_navigation():
    """Fail closed. build_plan's user_text defaults to empty, so a caller that cannot supply the
    user's words cannot navigate at all - rather than navigating unchecked."""
    assert isinstance(plan(URL, ""), PlanRefusal)
    assert inspect.signature(planner.build_plan).parameters["user_text"].default == ""


# ======================================================================================================
# THE LOOP AND CONTEXT (matrix 13-17)
# ======================================================================================================

def test_the_same_session_and_page_stay_the_click_target(world):
    """13. Navigation does not re-open or replace anything: the DOM target is the session it was."""
    assert run(world).ok
    assert world.navigations[0][:2] == (SESSION, PAGE)
    assert executor_adapter.browser_sessions() == [(SESSION, PAGE)]


def test_navigating_twice_leaves_the_second_page_as_the_target(world):
    """14."""
    assert run(world, "https://one.example.com").ok
    assert run(world, "https://two.example.com").ok
    assert [nav[2] for nav in world.navigations] == ["https://one.example.com",
                                                     "https://two.example.com"]
    assert world.navigations[-1][:2] == (SESSION, PAGE)


def test_navigation_after_closing_the_browser_refuses(world):
    """15."""
    world.sessions = []
    assert not run(world).ok
    assert world.navigations == []


def test_the_assistant_browser_alone_is_reached_by_an_unqualified_click(world, monkeypatch):
    """16. With no app window opened in this session, the browser is the only candidate."""
    logic.forget_session_windows()
    context = logic._context_for_named_click("")
    assert not isinstance(context, str), context
    assert (context.session_id, context.page_id) == (SESSION, PAGE)


def test_with_personal_chrome_also_available_an_unqualified_click_refuses(world, monkeypatch):
    """17, AND THE AUDIT RESULT, pinned rather than designed around.

    The existing ambiguity rule is unchanged: a live assistant browser beside an available Chrome is
    TWO candidates, so an unqualified click refuses and names the reserved selector as the way out.
    Navigation does not and must not weaken that. So the three-command workflow reads as advertised
    ONLY while the assistant browser is the only context; with Chrome also in play the third command
    has to be `click Login in assistant browser`."""
    from app.verifier.models import WindowInfo
    logic.forget_session_windows()
    logic._remember_found("chrome", WindowInfo(11, "Google Chrome", "Chrome_WidgetWin_1"))
    try:
        context = logic._context_for_named_click("")
        assert isinstance(context, str), f"the ambiguity rule was weakened: {context!r}"
        assert "more than one" in context.lower()
        assert "assistant browser" in context, "the way out must be named"
    finally:
        logic.forget_session_windows()


def test_naming_the_assistant_browser_still_reaches_it(world):
    """17's other half: the reserved selector works, which is what makes the refusal actionable."""
    logic.forget_session_windows()
    context = logic._context_for_named_click("assistant browser")
    assert not isinstance(context, str), context
    assert (context.session_id, context.page_id) == (SESSION, PAGE)


# ======================================================================================================
# RISK AND SAFETY (matrix 18-22)
# ======================================================================================================

def test_navigation_is_medium_and_the_reason_is_ownership_independent(world):
    """18. MEDIUM, and the reason says why - not that the address is suspect."""
    assert logic._NAVIGATE_RISK is RiskLevel.MEDIUM
    assert run(world).ok
    [(description, level)] = world.prompts
    assert level is RiskLevel.MEDIUM
    reason = logic._NAVIGATE_RISK_REASON
    assert "hasn't been seen yet" in reason
    assert "next click would act on whatever loaded" in reason


def test_the_confirmation_shows_the_full_address(world):
    """19. Reading the address before it loads is the whole point of asking, so it is shown in full -
    scheme, host, path and query."""
    url = "https://example.com/login?next=%2Fdashboard"
    assert run(world, url).ok
    [(description, _level)] = world.prompts
    assert url in description, description
    assert description == f"navigate the assistant browser to {url}"


@pytest.mark.parametrize("answer", [False, None, "yes", "y", 1, ""])
def test_a_denial_navigates_nothing(world, answer):
    """20. The existing yes-only contract, unchanged: only a literal True permits."""
    with pytest.raises(safety_logic.ActionDeniedError):
        run(world, answer=answer)
    assert world.navigations == []


def test_a_command_at_the_confirmation_cancels_and_is_queued(world):
    """21. Slice 1's rule, still intact on a brand-new kind."""
    from app import console
    pending = console.PendingCommand()
    screen = []
    script = iter(["close assistant browser"])
    confirm = console._confirm(lambda prompt: next(script), screen.append, pending)
    with pytest.raises(safety_logic.ActionDeniedError):
        execute(navigate(), confirm)
    assert world.navigations == []
    assert pending.take() == "close assistant browser"
    assert any(URL in line for line in screen), "the prompt still showed the address"


def test_the_emergency_stop_navigates_nothing(world):
    """22. Checked before the navigation and again after it."""
    emergency_stop.trigger("test")
    try:
        with pytest.raises(emergency_stop.EmergencyStopError):
            run(world)
        assert world.navigations == []
    finally:
        emergency_stop.reset("test")
    code = code_of(logic._prepare_navigate)
    assert code.count("emergency_stop.check()") == 2, "one checkpoint before, one after"


# ======================================================================================================
# PRIVACY (matrix 23-25)
# ======================================================================================================

def test_the_address_is_not_the_action_target(world):
    """24. log_label is what the Executor's log and plan_summary() report, and plan_summary travels to
    the model inside a ReplanRequest. The address is in `url`, so `target` stays empty and neither
    sees it."""
    action = navigate()
    assert action.target == ""
    assert action.log_label == ""
    assert URL not in action.description
    assert URL not in repr(action)


def test_the_address_is_not_written_to_the_log(world, caplog):
    """24, behaviourally, through the real result log."""
    import logging
    with caplog.at_level(logging.DEBUG):
        assert run(world).ok
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert URL not in text, text
    assert "example.com" not in text, text
    assert "navigate" in text, "something was logged - just not the address"


def test_what_is_logged_is_metadata_only(world):
    """7 of the report: the log messages this kind can produce, named here so they cannot drift into
    carrying an address."""
    code = code_of(logic._prepare_navigate)
    messages = [node.value for node in ast.walk(ast.parse(code))
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
                and node.value.startswith("navigate:")]
    assert sorted(messages) == sorted([
        "navigate: refused the address's scheme",
        "navigate: the browser refused",
        "navigate: timed out; the load was not confirmed",
        "navigate: the browser reported the navigation complete",
    ]), messages
    for message in messages:
        assert "{" not in message, f"{message!r} interpolates something"


def test_no_page_content_can_reach_a_provider_payload(world):
    """23. The navigate path reads nothing off the page, so there is nothing to send. The adapter's
    page fingerprint stays inside the adapter, as the DOM slices established."""
    code = code_of(logic._prepare_navigate)
    for forbidden in ("page_identity", "title", "content", "inner_text", "cookies", "url()"):
        assert forbidden not in code, forbidden
    adapter_code = (settings.PROJECT_ROOT / "app" / "executor" / "adapter.py").read_text(encoding="utf-8")
    navigate_src = adapter_code.split("def browser_navigate")[1].split("\ndef ")[0]
    for forbidden in ("page.title", "page.content", "inner_text", "page.url", "cookies"):
        assert forbidden not in navigate_src, forbidden


def test_trusted_user_input_and_an_observed_url_are_different_things():
    """25. The distinction, pinned: a URL the user typed is checked against their own words and may be
    navigated to; a URL read off a page is never in those words, and there is no code path that could
    hand one to navigation in the first place."""
    assert planner._url_provenance(URL, f"go to {URL}") is None
    assert planner._url_provenance(URL, "click the first link") is not None
    assert "user_text" in inspect.signature(planner.build_plan).parameters
    # no observation or DOM type carries a URL out of the adapter
    from app.verifier.models import DomElement
    assert not any("url" in field for field in DomElement.__dataclass_fields__)


# ======================================================================================================
# SCHEMA AND CAPABILITY (matrix 26-29)
# ======================================================================================================

def test_the_schema_cost_is_one_required_property():
    """26. 21 properties, and the counts that matter did not move at all."""
    from app.brain.models import interpretation_schema
    from tests.test_brain_provider import schema_complexity, DOCUMENTED_OPTIONAL_LIMIT, \
        DOCUMENTED_UNION_LIMIT
    counts = schema_complexity(interpretation_schema(250))
    assert counts == {"optional": 0, "unions": 0, "properties": 21}, counts
    assert counts["optional"] <= DOCUMENTED_OPTIONAL_LIMIT
    assert counts["unions"] <= DOCUMENTED_UNION_LIMIT
    assert ARGS_FIELDS[NAVIGATE] == ("url",)


def test_a_url_does_not_ride_in_a_field_that_means_something_else():
    """26. `text` means "what to type" and `app` means "a configured application name" (40 chars).
    Carrying an address in either would make the schema lie about the field."""
    from app.brain.models import interpretation_schema
    props = interpretation_schema(250)["properties"]["intents"]["items"]["properties"]
    assert "for navigate" in props["url"]["description"]
    assert "navigate" not in props["text"]["description"]
    assert "navigate" not in props["app"]["description"]


def test_the_three_tables_agree():
    """27."""
    assert set(ARGS_FOR_KIND) == set(logic._PREPARERS) == set(logic._RESOLVERS)
    assert NAVIGATE in ARGS_FOR_KIND


def test_voice_cannot_plan_a_navigation():
    """28. A spoken address must not become a page load this slice."""
    assert not VOICE_CONSOLE.allows(NAVIGATE)
    assert TYPED_CONSOLE.allows(NAVIGATE)
    built = plan(URL, f"open this site {URL}", frontend=VOICE_CONSOLE)
    assert isinstance(built, PlanRefusal)


def test_the_system_prompt_is_inside_the_raised_budget():
    """29. The limit was raised once, deliberately, to 4400 - see app/brain/logic.py's note."""
    from app.brain import logic as brain_logic
    assert len(brain_logic.SYSTEM_PROMPT) < 4400
    assert "never invent, complete or guess an address" in brain_logic.SYSTEM_PROMPT.lower()
    assert "needs_clarification" in brain_logic.SYSTEM_PROMPT.split("- navigate:")[1].split("\n")[0] \
        or "needs_clarification" in brain_logic.SYSTEM_PROMPT


# ======================================================================================================
# ISOLATION (matrix 30-32)
# ======================================================================================================

def test_the_navigation_primitive_is_centrally_guarded():
    """32, and 30/31 by consequence: an ordinary offline test that reached it would be refused by the
    repository-root guard, not by a fixture this file happens to install."""
    import safety_guards
    assert "browser_navigate" in safety_guards.BROWSER_BOUNDARIES


def test_an_unguarded_navigation_attempt_is_refused(monkeypatch):
    """31, proved by firing the guard rather than by assuming it. This test does NOT install the fake
    adapter, so the autouse browser guard is still in place over the real primitive."""
    import safety_guards
    with pytest.raises(safety_guards.PhysicalBrowserEscaped) as raised:
        executor_adapter.browser_navigate(SESSION, PAGE, URL, 1.0)
    assert "browser_navigate" in str(raised.value)


def test_this_file_reaches_no_network(world):
    """30. The only egress-capable function is replaced, and the real one is guarded above."""
    assert run(world).ok
    assert world.navigations, "the fake was used"
    code = code_of(logic._prepare_navigate)
    for forbidden in ("urlopen", "requests", "socket", "httpx", "aiohttp"):
        assert forbidden not in code, forbidden


# ======================================================================================================
# REGRESSION (matrix 33-35)
# ======================================================================================================

def test_the_dom_click_path_is_unchanged():
    """33."""
    code = code_of(logic._prepare_dom_click)
    for name in ("_navigable_url", "browser_navigate", "_navigate_timeout", "action.url"):
        assert name not in code, name


def test_personal_chrome_cannot_be_navigated(world):
    """34. "Assistant browser ONLY" - navigating personal Chrome needs the address bar, which is the
    parked Slice 4 problem. There is no app name on this kind at all."""
    assert ARGS_FIELDS[NAVIGATE] == ("url",)
    assert "app" not in ARGS_FIELDS[NAVIGATE]
    code = code_of(logic._prepare_navigate)
    for forbidden in ("_context_for_named_click", "_session_windows", "_found_windows",
                      "activate", "expect_window"):
        assert forbidden not in code, forbidden
    # it acts only on the adapter's own session registry
    assert "adapter.browser_sessions()" in code


def test_open_and_close_ownership_is_unchanged():
    """35."""
    assert "url" not in code_of(logic._prepare_close_app)
    assert "_navigable_url" not in code_of(logic._prepare_open_app)
    assert "_found" not in code_of(logic._ours_now)


def test_the_single_session_rule_is_unchanged(world):
    """6 of the brief: one assistant browser at a time, and navigation does not open one."""
    code = code_of(logic._prepare_navigate)
    assert "browser_open_session" not in code
    world.sessions = [(SESSION, PAGE), ("s2", "p2")]
    result = run(world)
    assert not result.ok and "more than one" in result.message
    assert world.navigations == []



# ======================================================================================================
# BL-1, SECOND ROUND: the milestone moves to `commit`, on the owner's measurements
#
# MEASURED on the owner's machine (facts):
#     main document only (PowerShell):  herokuapp 1.23s   smebluepages 12.27s   wikipedia 1.15s
#     DOMContentLoaded (warm Chrome):   herokuapp 30.94s  smebluepages 15.35s
#     assistant browser:                herokuapp and smebluepages both timed out at 20s,
#                                       wikipedia and example.com fine
#
# So `domcontentloaded` did NOT fix it, and a document that arrives in 1.2s whose DOMContentLoaded
# lands at 31s is a page with a blocked parse, not a page that is slow to reach.
#
# THE FAKE BELOW MODELS THAT MEASURED SHAPE, and the point of these tests is the pair the brief
# requires: a control that IS available soon after commit must be clickable, and a control that is NOT
# must be refused rather than claimed. The second is the safety case for choosing commit at all.
# ======================================================================================================

from tests.test_dom_observation import FakeLocator, FakePage, control as dom_control  # noqa: E402


class ClickingLocator(FakeLocator):
    """FakeLocator plus click(). The DOM Slice 1 fake is deliberately READ-ONLY - it has no click, so
    an observation test cannot act by accident - and these two tests need the whole commit -> query ->
    click path. Subclassed rather than changing the shared fake."""

    def click(self, timeout=None, **kwargs):
        assert timeout is not None, "the click must be bounded"
        self._page.clicks.append(self._one["name"])

    def nth(self, index):
        return ClickingLocator([self._controls[index]], self._page)

    def or_(self, other):
        return ClickingLocator(self._controls + other._controls, self._page)

    @property
    def first(self):
        return ClickingLocator(self._controls[:1], self._page)


class Response:
    """What goto() returns: the main resource response. Playwright does NOT throw for a 404."""

    def __init__(self, status=200):
        self.status = status


class MeasuredPage(FakePage):
    """A page with BOTH halves: Playwright's navigation milestones and role scanning.

    `goto` behaves like the real one - a milestone beyond the budget raises with "Timeout" in the
    message - so a test can ask what each milestone would have done to the SAME page. The inherited
    `ready_after_waits` is how a control that has not been parsed yet is modelled: at commit the DOM
    may legitimately be empty, which is the risk commit carries and the thing that must be tested.
    """

    MILESTONES = ("commit", "domcontentloaded", "load", "networkidle")

    def __init__(self, controls, *, commit_ms=1_230, dcl_ms=30_940, load_ms=120_000,
                 status=200, unreachable=False, ready_after_waits=0, frames=1):
        super().__init__(controls, frames=frames)
        # inherited from FakePage and set after construction, as that class expects
        self.ready_after_waits = ready_after_waits
        self.clicks = []
        self.when = {"commit": commit_ms, "domcontentloaded": dcl_ms, "load": load_ms,
                     "networkidle": load_ms + 500}
        self.status = status
        self.unreachable = unreachable
        self.gotos = []

    def get_by_role(self, role, name=None, exact=None):
        found = super().get_by_role(role, name=name, exact=exact)
        return ClickingLocator(found._controls, self)

    def goto(self, url, timeout=None, wait_until=None):
        self.gotos.append((url, timeout, wait_until))
        if self.unreachable:
            raise RuntimeError("net::ERR_NAME_NOT_RESOLVED at " + url)
        assert wait_until in self.MILESTONES, f"unknown milestone {wait_until!r}"
        if self.when[wait_until] > (timeout or 0):
            raise RuntimeError(f"Timeout {timeout:.0f}ms exceeded.")
        return Response(self.status)


@pytest.fixture
def measured(monkeypatch):
    """Install a MeasuredPage in the real registry, with the genuine navigate/query/click reinstalled.

    This file is ABOUT those three functions, so they are read from the adapter's source the way the
    DOM slices' tests do. The page is pure Python: no Playwright, no socket, nothing real."""
    from tests.test_dom_observation import EXECUTOR_ADAPTER, code_of_named
    import textwrap

    def genuine(name):
        namespace = {}
        source = textwrap.dedent(code_of_named(EXECUTOR_ADAPTER, name))
        exec(compile(source, "<adapter>", "exec"), vars(executor_adapter), namespace)
        return namespace[name]

    for name in ("browser_navigate", "dom_query", "dom_click", "dom_page_has_frames"):
        monkeypatch.setattr(executor_adapter, name, genuine(name))

    def install(page):
        state = executor_adapter._BrowserSession(
            runtime=SimpleNamespace(stop=lambda: None),
            browser=SimpleNamespace(close=lambda: None),
            context=SimpleNamespace(close=lambda: None),
            pages={PAGE: page}, tokens={})
        with executor_adapter._browser_lock:
            executor_adapter._browser_sessions[SESSION] = state
        return page

    yield install
    with executor_adapter._browser_lock:
        executor_adapter._browser_sessions.pop(SESSION, None)


HEROKUAPP = "https://the-internet.herokuapp.com/add_remove_elements/"
SMEBLUEPAGES = "https://www.smebluepages.com/"


def test_the_measured_shape_is_modelled_and_the_old_milestones_would_still_fail(measured):
    """The fake's premise, asserted before it is used as evidence. The SAME page, the SAME 20-second
    budget, and the answer differs by milestone exactly as the owner's numbers say it should."""
    page = MeasuredPage([dom_control("Add Element")], commit_ms=1_230, dcl_ms=30_940)
    for milestone in ("domcontentloaded", "load"):
        with pytest.raises(RuntimeError, match="Timeout"):
            page.goto(HEROKUAPP, timeout=20_000, wait_until=milestone)
    assert page.goto(HEROKUAPP, timeout=20_000, wait_until="commit").status == 200


def test_herokuapp_navigates_at_commit(measured):
    """BL-1's first failing URL, at its measured shape: document 1.23s, DOMContentLoaded 30.94s."""
    page = measured(MeasuredPage([dom_control("Add Element")], commit_ms=1_230, dcl_ms=30_940))
    assert executor_adapter.browser_navigate(SESSION, PAGE, HEROKUAPP, 20.0) \
        == executor_adapter.NAVIGATED
    assert page.gotos[0][2] == "commit"


def test_smebluepages_navigates_at_commit(measured):
    """BL-1's second failing URL: a slow SERVER (12.27s for the document) rather than a blocked parse.
    Commit is bounded by the document, so it fits inside the existing 20-second budget."""
    page = measured(MeasuredPage([dom_control("Login")], commit_ms=12_270, dcl_ms=15_350))
    assert executor_adapter.browser_navigate(SESSION, PAGE, SMEBLUEPAGES, 20.0) \
        == executor_adapter.NAVIGATED
    assert page.gotos[0][2] == "commit"


# --- THE PAIR THE BRIEF REQUIRES -------------------------------------------------------------------

def test_commit_then_a_control_that_is_there_navigates_and_clicks(measured):
    """CASE 1. The document commits quickly, the target becomes available quickly, and
    DOMContentLoaded is delayed by blocking resources -> navigation AND the click both work."""
    page = measured(MeasuredPage([dom_control("Add Element")], commit_ms=1_230, dcl_ms=30_940,
                                 ready_after_waits=0))
    assert executor_adapter.browser_navigate(SESSION, PAGE, HEROKUAPP, 20.0) \
        == executor_adapter.NAVIGATED
    found = executor_adapter.dom_query(SESSION, PAGE, "add element", 2.0)
    assert len(found) == 1, found
    assert executor_adapter.dom_click(SESSION, PAGE, found[0].element_token, 2.0) \
        == executor_adapter.DOM_CLICKED
    assert page.clicks == ["Add Element"]


def test_commit_then_a_control_that_is_not_there_yet_refuses_and_clicks_nothing(measured):
    """CASE 2, AND THE SAFETY CASE FOR CHOOSING COMMIT AT ALL. The document commits quickly, but
    parsing delays the target beyond the click's readiness bound.

    The assistant must NOT claim the control is available and must NOT click a stale target. It
    refuses: dom_query returns nothing, so there is no token to click and nothing is clicked."""
    page = measured(MeasuredPage([dom_control("Add Element")], commit_ms=1_230, dcl_ms=30_940,
                                 ready_after_waits=99))   # never parsed inside one bounded wait
    assert executor_adapter.browser_navigate(SESSION, PAGE, HEROKUAPP, 20.0) \
        == executor_adapter.NAVIGATED
    found = executor_adapter.dom_query(SESSION, PAGE, "add element", 2.0)
    assert found == [], "a control that has not parsed was reported as available"
    assert page.clicks == [], "nothing may be clicked"
    assert page.waits == 1, "it did wait once, within its own bound, before refusing"


def test_the_click_readiness_deadline_starts_at_the_click_not_at_commit(measured):
    """The brief's correction, pinned: the click's bound is spent when the CLICK is attempted. Two
    separate attempts each get their own bounded wait, which is why a page that keeps parsing can
    succeed on a later attempt rather than being lost by the navigation."""
    page = measured(MeasuredPage([dom_control("Add Element")], commit_ms=1_230, ready_after_waits=2))
    assert executor_adapter.browser_navigate(SESSION, PAGE, HEROKUAPP, 20.0) \
        == executor_adapter.NAVIGATED
    assert executor_adapter.dom_query(SESSION, PAGE, "add element", 2.0) == []   # wait 1
    found = executor_adapter.dom_query(SESSION, PAGE, "add element", 2.0)        # wait 2 -> ready
    assert len(found) == 1, "a second attempt gets its own deadline against a further-parsed page"


# --- the guarantees that must survive the change ---------------------------------------------------

def test_an_unreachable_address_still_fails_honestly_at_commit(measured):
    """Playwright throws for an unreachable server whatever the milestone, and the message is not a
    timeout message - so it is a BrowserError, not NAVIGATED and not a timeout."""
    measured(MeasuredPage([], unreachable=True))
    with pytest.raises(executor_adapter.BrowserError):
        executor_adapter.browser_navigate(SESSION, PAGE, "https://no-such-host.invalid", 20.0)


@pytest.mark.parametrize("status", [404, 500, 403])
def test_an_http_error_page_is_still_a_navigation_at_commit(measured, status):
    """commit means "the network response is received", so the response - and its status - exists.
    Playwright does not throw for a valid status, so a 404 is still a page that loaded."""
    measured(MeasuredPage([], commit_ms=500, status=status))
    assert executor_adapter.browser_navigate(SESSION, PAGE, URL, 20.0) \
        == executor_adapter.NAVIGATED


def test_a_document_that_never_arrives_is_still_a_timeout_within_the_bound(measured):
    """The budget is untouched and still bites: a server that accepts the connection and never
    responds exhausts it, and is reported as a timeout rather than as a load."""
    page = measured(MeasuredPage([], commit_ms=60_000))
    assert executor_adapter.browser_navigate(SESSION, PAGE, URL, 20.0) \
        == executor_adapter.NAVIGATE_TIMEOUT
    assert page.gotos[0][1] == 20_000, "the configured bound reached Playwright, in milliseconds"


def test_the_navigation_budget_was_not_raised():
    """The brief's constraint, and the measurements' own conclusion: commit is bounded by the document
    request, so no bigger number is needed. 20.0 is unchanged."""
    text = (settings.PROJECT_ROOT / "config" / "config.yaml").read_text(encoding="utf-8")
    assert "navigate_timeout_seconds: 20.0" in text


def test_the_click_readiness_bound_was_not_raised():
    """EXPLICITLY NOT CHANGED, and the report says why: no measurement exists of how long a control
    takes to parse AFTER commit, and 2.0 -> some number chosen from DOMContentLoaded timings would be
    a guess wearing a number. The owner's smoke produces the evidence a new value would need."""
    text = (settings.PROJECT_ROOT / "config" / "config.yaml").read_text(encoding="utf-8")
    assert "query_timeout_seconds: 2.0" in text
    from tests.test_dom_observation import EXECUTOR_ADAPTER, code_of_named
    matches = code_of_named(EXECUTOR_ADAPTER, "_dom_matches")
    assert "deadline = time.monotonic() + max(0.0, float(timeout_ms)) / 1000.0" in matches
    assert "_wait_for_any_role(page, wanted, remaining_ms)" in matches


def test_exactly_one_milestone_is_chosen_and_it_is_commit():
    source = (settings.PROJECT_ROOT / "app" / "executor" / "adapter.py").read_text(encoding="utf-8")
    code = " ".join(line.split("#")[0] for line in source.splitlines())
    assert code.count("wait_until=") == 1
    assert 'wait_until="commit"' in code
    for rejected in ('wait_until="load"', 'wait_until="domcontentloaded"', "networkidle"):
        assert rejected not in code, rejected
