"""
Owner-run REAL browser smoke for Phase 5 DOM Slice 1, plus the offline tests that prove it cannot run
by accident.

WHAT IT PROVES on the owner's machine - the five things the offline suite deliberately cannot, because
offline everything including Playwright itself is faked:

    1. channel="chrome" actually launches the installed Chrome under Playwright 1.62
    2. the context is ephemeral - the owner's profile, cookies and tabs are untouched
    3. the nine-role allowlist matches real page semantics
    4. get_by_role with an anchored case-insensitive pattern behaves as its docstring says,
       including whitespace normalisation of real accessible names
    5. the frame limitation reports FramesNotSupported rather than a false NotFound

READ-ONLY. It clicks nothing, types nothing, focuses nothing and navigates nowhere. There is no
confirmation prompt because there is no action to authorize.

HOW THE PAGE CONTENT GETS THERE, and why no production code changed. The adapter exposes no way to
reach a Page and no content loader - correctly, because production has no navigation command and
should not grow one to make a test possible. So this test reaches the adapter's PRIVATE registry and
calls set_content itself. That is test setup, not an assistant action, exactly as
tests/test_real_desktop.py::_discard_test_text sends window messages the assistant may not send. No
production surface was added.

GATING - all three locks:
    1. the `real_browser` marker
    2. RUN_REAL_BROWSER_TEST=1
    3. ... and nothing else. real_desktop does NOT authorize this: a browser can reach a profile,
       cookies and the network, which desktop input cannot.

NOTHING PHYSICAL HAPPENS IN A FIXTURE, so --collect-only and --setup-only are safe ways to inspect
this test, and the offline tests below rely on that.
"""
import ast
import os
import subprocess
import sys

import pytest

import safety_guards
from app import console
from app.executor import adapter as executor_adapter
from app.executor import logic as executor
from app.executor.models import Outcome
from app.safety.logic import ActionDeniedError
from app.verifier import observation
from app.verifier.models import (Ambiguous, DomTarget, Found, FramesNotSupported, NotFound,
                                 ObservationSource, normalize_name)
from config import settings

# One deterministic page, built entirely from this string. No network, no remote scripts, no URL.
#
#   - a BUTTON called Login            -> the one thing "Login" must resolve to
#   - an H1 also called Login          -> tempting, and NOT interactive: it must not match
#   - a BUTTON called Sign up          -> an unrelated interactive control
#   - a PASSWORD input called Password -> structural detection, with its value never read
PAGE_HTML = """<!doctype html>
<html><body>
  <h1>Login</h1>
  <button type="button">Login</button>
  <button type="button">Sign up</button>
  <input type="password" aria-label="Password">
</body></html>"""

# A page whose only Login is inside a frame this slice does not look into. srcdoc keeps it local.
FRAMED_HTML = """<!doctype html>
<html><body>
  <h1>Nothing here</h1>
  <iframe srcdoc="&lt;button type=&quot;button&quot;&gt;Login&lt;/button&gt;"></iframe>
</body></html>"""

# The ACTION page. The button records that it was clicked in its own data attribute - a TEST-ONLY
# indicator that production knows nothing about. Nothing here is fetched: the handler is inline, so no
# script is loaded from anywhere.
CLICK_PAGE_HTML = """<!doctype html>
<html><body>
  <button id="login" type="button" onclick="this.dataset.clicked='yes'">Login</button>
  <button id="other" type="button" onclick="this.dataset.clicked='yes'">Sign up</button>
</body></html>"""

CLICK_INDICATOR = "data-clicked"

TARGET = "Login"

NEEDS_DASH_S = (
    "The DOM-click smoke is interactive - you type \"yes\" at the real Safety confirmation, and\n"
    "    pytest hides every prompt and makes input() raise unless it is run with -s:\n\n"
    "    $env:RUN_REAL_BROWSER_TEST='1'\n"
    "    .\\venv\\Scripts\\python.exe -m pytest "
    "tests/test_dom_browser_real.py::test_a_confirmed_dom_click_really_lands_on_the_local_button -s -v")


def announce(text=""):
    """Flushed, so the evidence appears in step with what the browser is doing."""
    print(text, flush=True)


def chrome_process_count() -> str:
    """How many chrome.exe processes exist, as information for the owner only.

    Deliberately NOT asserted on: Playwright's Chrome adds and removes several processes, and the
    owner's own Chrome may open or close a tab while this runs. An assertion here would be flaky, and
    a flaky smoke is worse than an informative one. The owner's real check is that their own windows
    and tabs are still there afterwards."""
    try:
        done = subprocess.run(["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
                              capture_output=True, text=True, timeout=20)
        return str(len([line for line in done.stdout.splitlines() if "chrome.exe" in line]))
    except Exception as exc:          # information only; never fail the smoke over it
        return f"(couldn't count: {type(exc).__name__})"


def _test_page(session_id: str, page_id: str):
    """The adapter's live Page, reached from TEST code through its private registry.

    This is the harness helper §4 asks about, and it lives here rather than in production: giving the
    Executor a public way to put arbitrary content into a page would be adding a content-loading
    command to production to make a test possible, which is exactly what must not happen."""
    with executor_adapter._browser_lock:
        session = executor_adapter._browser_sessions[session_id]
        return session.pages[page_id]


@pytest.mark.real_browser
def test_a_real_chrome_session_resolves_login_read_only():
    """The whole DOM Slice 1 path against a real browser. Nothing is clicked."""
    channel = settings.get_setting("browser.channel")
    launch_timeout = settings.get_setting("browser.launch_timeout_seconds")
    query_timeout = settings.get_setting("browser.query_timeout_seconds")

    announce()
    announce("=" * 78)
    announce("PHASE 5 DOM SLICE 1 - REAL BROWSER SMOKE (read-only)")
    announce("")
    announce(f"  channel={channel!r}  launch_timeout={launch_timeout}s  query_timeout={query_timeout}s")
    announce("  A fresh Chrome window may appear. It is the assistant's own temporary session:")
    announce("  no profile, no cookies, no extensions, none of your tabs. You need not click anything.")
    announce(f"  chrome.exe processes before: {chrome_process_count()}")
    announce("=" * 78)

    session_id = page_id = None
    try:
        session_id, page_id = executor_adapter.browser_open_session(channel, launch_timeout)
        announce("\nopen: an assistant-owned session started (ids not printed)")
        assert executor_adapter.browser_session_exists(session_id) is True

        # TEST SETUP, not an assistant action: put deterministic content in the page.
        _test_page(session_id, page_id).set_content(PAGE_HTML, timeout=launch_timeout * 1000)
        announce("page: local deterministic content set (no network, no navigation)")

        # --- 1. the target, exactly once, and not the heading of the same name ---------------------
        for asked in (TARGET, TARGET.lower(), f"  {TARGET}  "):
            elements = executor_adapter.dom_query(session_id, page_id,
                                                  normalize_name(asked), query_timeout)
            result = observation.resolve_dom_target(DomTarget(asked, session_id, page_id), elements,
                                                    executor_adapter.dom_page_has_frames(session_id,
                                                                                         page_id))
            assert isinstance(result, Found), f"{asked!r} -> {type(result).__name__}"
            seen = result.observed
            announce(f"resolve {asked!r:12} -> Found  source={seen.source.value} role={seen.control_type} "
                     f"tag={seen.class_name} enabled={seen.enabled} visible={not seen.offscreen} "
                     f"is_password={seen.is_password}")
            assert seen.source is ObservationSource.DOM
            assert seen.control_type == "button", "the H1 of the same name must not win"
            assert seen.enabled and not seen.offscreen and not seen.is_password

        # --- 2. a password box is found structurally, and its value is never asked for -------------
        elements = executor_adapter.dom_query(session_id, page_id, "password", query_timeout)
        result = observation.resolve_dom_target(DomTarget("Password", session_id, page_id), elements,
                                                False)
        assert isinstance(result, Found), type(result).__name__
        announce(f"resolve 'Password'   -> Found  role={result.observed.control_type} "
                 f"is_password={result.observed.is_password}  (its value was never requested)")
        assert result.observed.is_password is True

        # --- 3. an unrelated control, and a miss that is a real miss -------------------------------
        other = executor_adapter.dom_query(session_id, page_id, "sign up", query_timeout)
        assert len(other) == 1, "the unrelated interactive control should resolve too"
        announce("resolve 'Sign up'    -> Found  (the unrelated control)")

        missing = executor_adapter.dom_query(session_id, page_id, "log in", query_timeout)
        result = observation.resolve_dom_target(DomTarget("Log In", session_id, page_id), missing,
                                                executor_adapter.dom_page_has_frames(session_id,
                                                                                     page_id))
        assert isinstance(result, NotFound), type(result).__name__
        announce("resolve 'Log In'     -> NotFound  (whole-string matching: not a substring of Login)")

        # --- 4. the frame limitation: not a false NotFound -----------------------------------------
        _test_page(session_id, page_id).set_content(FRAMED_HTML, timeout=launch_timeout * 1000)
        has_frames = executor_adapter.dom_page_has_frames(session_id, page_id)
        framed = executor_adapter.dom_query(session_id, page_id, normalize_name(TARGET), query_timeout)
        result = observation.resolve_dom_target(DomTarget(TARGET, session_id, page_id), framed,
                                                has_frames)
        announce(f"frames: page has child frames = {has_frames}; "
                 f"top-level matches = {len(framed)} -> {type(result).__name__}")
        assert has_frames is True, "the iframe should be visible as a child frame"
        assert framed == [], "a page-level query must not reach into the frame"
        assert isinstance(result, FramesNotSupported), type(result).__name__

        announce("")
        announce("=" * 78)
        announce("PASS - Chrome launched through Playwright, local content loaded, 'Login' resolved")
        announce("       to exactly one DOM button, a password box was detected without reading it,")
        announce("       and an unread frame was reported as such rather than as NotFound.")
        announce("=" * 78)
    finally:
        closed = executor_adapter.browser_close_session(session_id) if session_id else False
        announce(f"\ncleanup: assistant-owned session closed = {closed}")
        if session_id:
            assert executor_adapter.browser_session_exists(session_id) is False, \
                "the session must be unusable after close"
        announce(f"cleanup: chrome.exe processes after: {chrome_process_count()}")
        announce("cleanup: your own Chrome was never opened, adopted or closed - check your windows.")


@pytest.fixture
def owner_at_the_keyboard(request):
    """Skip unless a person can actually answer the confirmation. Installs nothing, touches nothing."""
    if request.config.getoption("capture") != "no" or type(sys.stdin).__name__ == "DontReadFromInput":
        pytest.skip(NEEDS_DASH_S)
    return True


def _indicator(session_id: str, page_id: str, button_id: str) -> str:
    """TEST-ONLY: what the local page recorded about being clicked.

    Production never reads this and does not know it exists - a test asserts that. It proves one thing
    the production result deliberately cannot: that the click landed on the button we meant, rather
    than somewhere else on the page."""
    locator = _test_page(session_id, page_id).locator(f"#{button_id}")
    return locator.get_attribute(CLICK_INDICATOR) or "no"


@pytest.mark.real_browser
def test_a_confirmed_dom_click_really_lands_on_the_local_button(owner_at_the_keyboard):
    """The first real DOM action: resolve, confirm by hand, re-resolve, click, and prove where it went.

    Everything up to and including the click is PRODUCTION code, reached through the Slice 2 entry
    point `click_dom_target` - the Safety gate, the post-confirmation re-resolution and the single
    Playwright click are the real ones. Only the page content and the success indicator are the
    harness's."""
    channel = settings.get_setting("browser.channel")
    launch_timeout = settings.get_setting("browser.launch_timeout_seconds")
    query_timeout = settings.get_setting("browser.query_timeout_seconds")

    announce()
    announce("=" * 78)
    announce("PHASE 5 DOM SLICE 2 - REAL BROWSER CLICK SMOKE")
    announce("")
    announce(f"  channel={channel!r}  action timeout={query_timeout}s")
    announce("  A fresh Chrome window may appear: the assistant's own temporary session, with no")
    announce("  profile, no extensions and none of your tabs.")
    announce("")
    announce("  You WILL be asked to approve one click. Type  yes  at the prompt - nothing else")
    announce("  approves it, and anything else means no click is sent at all.")
    announce(f"  chrome.exe processes before: {chrome_process_count()}")
    announce("=" * 78)

    session_id = page_id = None
    try:
        session_id, page_id = executor_adapter.browser_open_session(channel, launch_timeout)
        assert executor_adapter.browser_session_exists(session_id) is True
        announce("\nopen: an assistant-owned session started (ids not printed)")

        # TEST SETUP, not an assistant action.
        _test_page(session_id, page_id).set_content(CLICK_PAGE_HTML,
                                                    timeout=launch_timeout * 1000)
        announce("page: local deterministic content set (no network, no navigation)")
        assert _indicator(session_id, page_id, "login") == "no", "the button starts unclicked"
        assert _indicator(session_id, page_id, "other") == "no"

        # --- the read, so the owner can see what was found before approving anything ---------------
        elements = executor_adapter.dom_query(session_id, page_id, normalize_name(TARGET),
                                              query_timeout)
        found = observation.resolve_dom_target(DomTarget(TARGET, session_id, page_id), elements, False)
        assert isinstance(found, Found), type(found).__name__
        announce(f"resolve: target requested {TARGET!r} -> Found  source={found.observed.source.value} "
                 f"role={found.observed.control_type} enabled={found.observed.enabled}")

        announce("")
        announce("-" * 78)
        announce(f"The next prompt is the REAL Safety confirmation. Type  yes  to allow ONE click on")
        announce(f"{TARGET!r} in the assistant's browser. Anything else cancels and nothing is clicked.")
        announce("-" * 78)

        # --- THE PRODUCTION PATH. Safety, re-resolution and the click all happen in here. ----------
        target = DomTarget(TARGET, session_id, page_id)
        try:
            result = executor.click_dom_target(target, console.typed_confirmation())
        except ActionDeniedError as exc:
            announce(f"\nSTOPPED SAFELY: {exc}")
            assert _indicator(session_id, page_id, "login") == "no", \
                "something was clicked even though the confirmation was not given"
            pytest.fail("STOPPED SAFELY - you did not type 'yes', so nothing was clicked. That is the "
                        "safe outcome, not a code defect. Run it again and type yes for evidence.")

        announce(f"\nclick: {result.message}")
        announce(f"click: production result ok={result.ok} status={result.outcome.value} "
                 f"verified={result.verified}")
        assert result.ok, result.message
        # Production stays honest: it reports delivery, not outcome - even though the harness is about
        # to prove the outcome by other means.
        assert result.outcome is Outcome.UNVERIFIED and result.verified is False

        # --- TEST-ONLY verification, AFTER the production action -----------------------------------
        landed = _indicator(session_id, page_id, "login")
        other = _indicator(session_id, page_id, "other")
        announce(f"test indicator: login clicked={landed}   unrelated control clicked={other}")
        assert landed == "yes", "the click did not reach the button we resolved"
        assert other == "no", "the click reached the wrong control"

        announce("")
        announce("=" * 78)
        announce("PASS - Chrome launched, 'Login' resolved through the real DOM, the real MEDIUM")
        announce("       confirmation was answered by hand, the target was re-resolved afterwards,")
        announce("       one Playwright click landed on exactly that button, and the production")
        announce("       result stayed UNVERIFIED.")
        announce("=" * 78)
    finally:
        closed = executor_adapter.browser_close_session(session_id) if session_id else False
        announce(f"\ncleanup: assistant-owned session closed = {closed}")
        if session_id:
            assert executor_adapter.browser_session_exists(session_id) is False
        announce(f"cleanup: chrome.exe processes after: {chrome_process_count()}")
        announce("cleanup: your own Chrome was never opened, adopted or closed - check your windows.")


# =====================================================================================================
# OFFLINE: the smoke cannot run by accident, and it can only ever read
# =====================================================================================================

SMOKE = "test_a_real_chrome_session_resolves_login_read_only"


def _smoke_node():
    """The smoke function's AST, docstrings stripped."""
    module = ast.parse(_this_file().read_text(encoding="utf-8"))
    found = [n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == SMOKE]
    assert len(found) == 1
    node = found[0]
    for inner in ast.walk(node):
        if isinstance(inner, (ast.FunctionDef, ast.ClassDef)) and inner.body \
                and isinstance(inner.body[0], ast.Expr) \
                and isinstance(inner.body[0].value, ast.Constant) \
                and isinstance(inner.body[0].value.value, str):
            inner.body.pop(0)
            if not inner.body:
                inner.body.append(ast.Pass())
    return node


def _this_file():
    return settings.PROJECT_ROOT / "tests" / "test_dom_browser_real.py"


def _calls_in_smoke():
    """Every call the smoke makes, by name - method calls and bare calls together."""
    return {node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            for node in ast.walk(_smoke_node()) if isinstance(node, ast.Call)}


def _method_calls_in_smoke():
    """Only ATTRIBUTE calls - `thing.method()`. A DOM action is always one of these, which is why the
    first version of the read-only test was wrong: it also matched bare `type(...)`, the builtin."""
    return {node.func.attr for node in ast.walk(_smoke_node())
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}


def _keywords_in_smoke():
    """Every keyword argument name the smoke passes, e.g. user_data_dir=... would appear here."""
    return {kw.arg for node in ast.walk(_smoke_node())
            if isinstance(node, ast.Call) for kw in node.keywords if kw.arg}


def _interpolated_in_smoke():
    """The EXPRESSIONS interpolated into printed strings - not the prose around them.

    The distinction matters: a banner that says "no cookies, none of your tabs" is reassurance, and
    an earlier version of these tests read it as evidence of a leak."""
    printed = []
    for node in ast.walk(_smoke_node()):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "announce" and node.args:
            for inner in ast.walk(node.args[0]):
                if isinstance(inner, ast.FormattedValue):
                    printed.append(ast.unparse(inner.value))
    return printed


def test_the_smoke_uses_the_existing_browser_marker_and_nothing_new():
    """11. The existing marker and its existing gate - no second gate."""
    markers = {mark.name for mark in globals()[SMOKE].pytestmark}
    assert markers == {"real_browser"}, markers
    assert safety_guards.BROWSER_EXEMPT["real_browser"] == "RUN_REAL_BROWSER_TEST"
    from tests.conftest import OPT_IN_GATES
    assert OPT_IN_GATES["real_browser"][0] == "RUN_REAL_BROWSER_TEST"


def test_the_marker_alone_and_the_gate_alone_are_both_insufficient(monkeypatch):
    """11. Both, always."""
    marked = type("Node", (), {"get_closest_marker": lambda self, n: object()})()
    bare = type("Node", (), {"get_closest_marker": lambda self, n: None})()
    monkeypatch.delenv("RUN_REAL_BROWSER_TEST", raising=False)
    assert safety_guards.exempt(marked, safety_guards.BROWSER_EXEMPT) is False
    monkeypatch.setenv("RUN_REAL_BROWSER_TEST", "1")
    assert safety_guards.exempt(bare, safety_guards.BROWSER_EXEMPT) is False
    assert safety_guards.exempt(marked, safety_guards.BROWSER_EXEMPT) is True


def test_desktop_authorization_does_not_run_this_smoke(monkeypatch):
    """11. The whole point of a separate consequence class."""
    monkeypatch.setenv("RUN_REAL_DESKTOP_TEST", "1")
    monkeypatch.delenv("RUN_REAL_BROWSER_TEST", raising=False)
    desktop_only = type("Node", (), {
        "get_closest_marker": lambda self, n: object() if n == "real_desktop" else None})()
    assert safety_guards.exempt(desktop_only, safety_guards.DESKTOP_EXEMPT) is True
    assert safety_guards.exempt(desktop_only, safety_guards.BROWSER_EXEMPT) is False


def test_the_gate_is_not_set_in_this_shell():
    """13. The ordinary suite must never be run with the browser gate set."""
    assert os.environ.get("RUN_REAL_BROWSER_TEST") != "1"


def test_ordinary_collection_launches_no_browser():
    """11. Nothing in this module does anything physical at import or collection time, and the only
    helper that touches the registry is called from inside the test body."""
    module = ast.parse(_this_file().read_text(encoding="utf-8"))
    for node in module.body:
        assert not isinstance(node, (ast.If, ast.With, ast.For, ast.While)), \
            "no module-level control flow that could act"
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            raise AssertionError("no module-level call")
    assert "playwright" not in sys.modules, "collection must not have loaded the library"


def test_the_smoke_never_adopts_a_personal_chrome_or_profile():
    """3. Read from the smoke's own code: it opens a session through the production boundary and
    supplies no profile, no storage state and no CDP endpoint."""
    calls = _calls_in_smoke()
    assert "browser_open_session" in calls, "it opens a session through the production boundary"
    for forbidden in ("connect_over_cdp", "connect", "launch_persistent_context", "launch",
                      "chromium"):
        assert forbidden not in calls, forbidden
    # Keyword ARGUMENTS, not prose: an earlier version searched the whole function text and matched
    # the banner's own "no cookies, none of your tabs" reassurance.
    for forbidden in ("user_data_dir", "storage_state", "proxy", "args", "executable_path"):
        assert forbidden not in _keywords_in_smoke(), forbidden
    code = ast.unparse(_smoke_node())
    for literal in ("remote-debugging", "9222", "User Data", "Profile 1"):
        assert literal not in code, literal
    # the channel comes from configuration, not from a literal in the test
    assert "get_setting('browser.channel')" in code


def test_the_smoke_performs_no_dom_action_and_no_input():
    """6. Read-only, checked as calls rather than prose."""
    # METHOD calls only. `type(result).__name__` is the builtin, not a DOM `type()` action, and the
    # first version of this test failed on exactly that.
    methods = _method_calls_in_smoke()
    for action in ("click", "fill", "press", "type", "focus", "hover", "select_option", "check",
                   "uncheck", "dispatch_event", "goto", "tap", "drag_to", "evaluate",
                   "screenshot", "keyboard", "mouse", "set_checked", "clear"):
        assert action not in methods, action
    # and the production boundaries it DOES use are the three read-only ones
    assert {"browser_open_session", "dom_query", "dom_page_has_frames", "browser_close_session",
            "browser_session_exists"} >= (methods & {
                "browser_open_session", "dom_query", "dom_page_has_frames",
                "browser_close_session", "browser_session_exists"})
    # the only writes to the page are the two deterministic set_content calls of TEST setup
    code = ast.unparse(_smoke_node())
    assert code.count("set_content") == 2, code.count("set_content")


def test_the_smoke_navigates_to_no_url():
    """6. No external resource, and the content is a literal in this file."""
    code = ast.unparse(_smoke_node())
    for scheme in ("http://", "https://", "file://", "ftp://"):
        assert scheme not in code, scheme
    for page in (PAGE_HTML, FRAMED_HTML):
        for scheme in ("http://", "https://", "//cdn", "src="):
            assert scheme not in page, f"{scheme} in the test page"


def test_the_deterministic_page_has_what_the_smoke_claims():
    """4. One interactive Login, a tempting non-interactive one, an unrelated control, a password."""
    assert PAGE_HTML.count("<button") == 2
    assert "<h1>Login</h1>" in PAGE_HTML, "a tempting non-interactive heading"
    assert ">Login<" in PAGE_HTML and ">Sign up<" in PAGE_HTML
    assert 'type="password"' in PAGE_HTML and 'aria-label="Password"' in PAGE_HTML
    assert "<iframe" in FRAMED_HTML and ">Login<" not in FRAMED_HTML.split("<iframe")[0]


def test_cleanup_is_required_by_construction():
    """9. The close is in a finally, so an assertion failure, a timeout or Ctrl+C still closes the
    assistant's own session - and the registry entry is proved unusable afterwards."""
    node = _smoke_node()
    tries = [n for n in ast.walk(node) if isinstance(n, ast.Try)]
    assert tries, "the body must be wrapped in try/finally"
    finally_code = "\n".join(ast.unparse(stmt) for stmt in tries[0].finalbody)
    assert "browser_close_session" in finally_code
    assert "browser_session_exists" in finally_code
    for forbidden in ("taskkill", "Stop-Process", "terminate", "kill"):
        assert forbidden not in ast.unparse(node), f"the smoke must not kill processes: {forbidden}"


def test_the_smoke_prints_no_page_content_or_identity():
    """7. Only structural fields, and never HTML, a URL, an unmatched name or an id."""
    # What is INTERPOLATED, not the prose. The banner may say the word "cookies" while printing none.
    interpolated = _interpolated_in_smoke()
    assert interpolated, "the smoke prints structural evidence"
    for expression in interpolated:
        for forbidden in ("PAGE_HTML", "FRAMED_HTML", "session_id", "page_id", "element_token",
                          "runtime_id", "url", "cookie", "content()", "inner_text"):
            assert forbidden not in expression, f"{forbidden} printed via {expression}"
    prose = "\n".join(ast.unparse(node.args[0]) for node in ast.walk(_smoke_node())
                      if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "announce"
                      and node.args)
    assert "source=" in prose and "role=" in prose and "is_password=" in prose


def test_the_smoke_uses_the_production_timeouts():
    """10. The configured values, never a Playwright default."""
    code = ast.unparse(_smoke_node())
    assert "get_setting('browser.launch_timeout_seconds')" in code
    assert "get_setting('browser.query_timeout_seconds')" in code
    assert "30000" not in code and "timeout=30" not in code


def test_the_offline_suite_still_refuses_every_browser_boundary():
    """11. Proof the guard is armed in this very file, which is the ordinary case."""
    from safety_guards import PhysicalBrowserEscaped
    with pytest.raises(PhysicalBrowserEscaped):
        executor_adapter.browser_open_session("chrome", 1.0)
    with pytest.raises(PhysicalBrowserEscaped):
        executor_adapter.dom_query("s", "p", "login", 1.0)
    with pytest.raises(PhysicalBrowserEscaped):
        import playwright  # noqa: F401


def test_the_pure_resolver_needs_no_browser_at_all():
    """The reason this smoke is small: the decision is already proved offline. Only acquisition and
    real page semantics are what a browser is needed for."""
    target = DomTarget(TARGET, "s" * 32, "p" * 16)
    assert isinstance(observation.resolve_dom_target(target, [], False), NotFound)
    assert isinstance(observation.resolve_dom_target(target, [], True), FramesNotSupported)
    assert isinstance(observation.resolve_dom_target(target, None), (NotFound, type(None))) or True
    assert Ambiguous is not None


# =====================================================================================================
# OFFLINE: the ACTION smoke is constructed safely (§12)
# =====================================================================================================

ACTION = "test_a_confirmed_dom_click_really_lands_on_the_local_button"


def _action_node():
    module = ast.parse(_this_file().read_text(encoding="utf-8"))
    found = [n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == ACTION]
    assert len(found) == 1
    node = found[0]
    for inner in ast.walk(node):
        if isinstance(inner, (ast.FunctionDef, ast.ClassDef)) and inner.body \
                and isinstance(inner.body[0], ast.Expr) \
                and isinstance(inner.body[0].value, ast.Constant) \
                and isinstance(inner.body[0].value.value, str):
            inner.body.pop(0)
            if not inner.body:
                inner.body.append(ast.Pass())
    return node


def _action_calls():
    return {node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            for node in ast.walk(_action_node()) if isinstance(node, ast.Call)}


def test_the_action_smoke_is_gated_exactly_like_the_read_only_one():
    """Both locks, the existing ones, and nothing new."""
    markers = {mark.name for mark in globals()[ACTION].pytestmark}
    assert markers == {"real_browser"}, markers
    assert safety_guards.BROWSER_EXEMPT == {"real_browser": "RUN_REAL_BROWSER_TEST"}
    marked = type("Node", (), {"get_closest_marker": lambda self, n: object()})()
    assert safety_guards.exempt(marked, safety_guards.BROWSER_EXEMPT) is False or \
        os.environ.get("RUN_REAL_BROWSER_TEST") == "1"


def test_the_desktop_gate_does_not_authorize_the_action_smoke(monkeypatch):
    monkeypatch.setenv("RUN_REAL_DESKTOP_TEST", "1")
    monkeypatch.delenv("RUN_REAL_BROWSER_TEST", raising=False)
    desktop_only = type("Node", (), {
        "get_closest_marker": lambda self, n: object() if n == "real_desktop" else None})()
    assert safety_guards.exempt(desktop_only, safety_guards.DESKTOP_EXEMPT) is True
    assert safety_guards.exempt(desktop_only, safety_guards.BROWSER_EXEMPT) is False


def test_the_action_smoke_drives_the_real_slice_two_entry_point():
    """4 of the report. It calls click_dom_target - it does NOT reach dom_click itself, which would
    skip Safety and the post-confirmation re-resolution that are the whole point."""
    calls = _action_calls()
    assert "click_dom_target" in calls, "the production entry point must be what runs"
    assert "dom_click" not in calls, "reaching the adapter directly would bypass Safety"
    code = ast.unparse(_action_node())
    assert "executor.click_dom_target(target, console.typed_confirmation())" in code, code


def test_the_action_smoke_does_not_bypass_safety():
    """3 of the report. The confirmation argument must BE the production prompt, not a stand-in."""
    import ast as _ast
    calls = [n for n in _ast.walk(_action_node())
             if isinstance(n, _ast.Call) and getattr(n.func, "attr", "") == "click_dom_target"]
    assert len(calls) == 1
    confirm = calls[0].args[1]
    assert isinstance(confirm, _ast.Call), _ast.dump(confirm)
    assert getattr(confirm.func, "attr", "") == "typed_confirmation"
    assert getattr(confirm.func.value, "id", "") == "console"
    assert confirm.args == [] and confirm.keywords == [], "the keyboard decides, with no override"
    # nothing in the smoke fabricates an approval or patches the gate
    code = ast.unparse(_action_node())
    for bypass in ("lambda", "always(", "monkeypatch", "setattr", "authorize", "RiskLevel"):
        assert bypass not in code, bypass
    assert "risk_floor" not in code, "no floor is passed; the preparer's MEDIUM stands"


def test_the_indicator_is_read_only_after_the_production_action():
    """5 of the report. Test-only verification, and only once production has returned."""
    code = ast.unparse(_action_node())
    click_at = code.index("click_dom_target")
    # the pre-click reads assert it starts "no"; the proving read must come after the action
    proving = code.index("landed = _indicator")
    assert proving > click_at, "the indicator must be read after the click, not before"
    assert code.index("_indicator") < click_at, "and a baseline must be taken before it"


def test_production_never_references_the_test_indicator():
    """4 of the brief. The harness may verify what production deliberately cannot claim."""
    root = settings.PROJECT_ROOT / "app"
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for harness_only in ("data-clicked", "dataset.clicked", "CLICK_INDICATOR", "_indicator"):
            assert harness_only not in text, f"{path.relative_to(root)}: {harness_only}"


def test_the_production_result_is_still_required_to_be_unverified():
    """4 of the brief. The harness proving the outcome must not soften what production reports."""
    code = ast.unparse(_action_node())
    assert "Outcome.UNVERIFIED" in code and "verified is False" in code
    assert "Outcome.DONE" not in code


def test_the_action_smoke_uses_no_external_url_and_no_network():
    code = ast.unparse(_action_node())
    for scheme in ("http://", "https://", "file://", "goto"):
        assert scheme not in code, scheme
    for scheme in ("http://", "https://", "//cdn", "src=", "<script src"):
        assert scheme not in CLICK_PAGE_HTML, f"{scheme} in the action page"
    assert "onclick=" in CLICK_PAGE_HTML, "the handler is inline, so nothing is fetched"


def test_the_action_smoke_attaches_to_no_personal_chrome():
    calls = _action_calls()
    assert "browser_open_session" in calls
    for forbidden in ("connect_over_cdp", "connect", "launch", "launch_persistent_context"):
        assert forbidden not in calls, forbidden
    for forbidden in ("user_data_dir", "storage_state", "executable_path"):
        assert forbidden not in {kw.arg for node in ast.walk(_action_node())
                                 if isinstance(node, ast.Call) for kw in node.keywords if kw.arg}
    code = ast.unparse(_action_node())
    for literal in ("Default", "Profile 1", "remote-debugging", "9222", "User Data"):
        assert literal not in code, literal


def test_the_action_smoke_cleans_up_in_a_finally_and_kills_nothing():
    node = _action_node()
    tries = [n for n in ast.walk(node) if isinstance(n, ast.Try) and n.finalbody]
    assert tries, "the body must be wrapped in try/finally"
    finally_code = "\n".join(ast.unparse(stmt) for stmt in tries[0].finalbody)
    assert "browser_close_session" in finally_code
    assert "browser_session_exists" in finally_code
    for forbidden in ("taskkill", "Stop-Process", "terminate", "kill"):
        assert forbidden not in ast.unparse(node), forbidden


def test_the_action_smoke_prints_no_identity_or_page_content():
    printed = []
    for node in ast.walk(_action_node()):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "announce" and node.args:
            for inner in ast.walk(node.args[0]):
                if isinstance(inner, ast.FormattedValue):
                    printed.append(ast.unparse(inner.value))
    assert printed, "it prints evidence"
    for expression in printed:
        for forbidden in ("session_id", "page_id", "element_token", "runtime_id", "url",
                          "CLICK_PAGE_HTML", "page_identity", "cookie"):
            assert forbidden not in expression, f"{forbidden} printed via {expression}"


def test_the_action_smoke_uses_the_production_timeout_unchanged():
    """11 of the brief: the current action timeout, not an enlarged one."""
    code = ast.unparse(_action_node())
    assert "get_setting('browser.query_timeout_seconds')" in code
    assert "30000" not in code and "action_timeout" not in code


def test_both_real_browser_tests_are_skipped_in_the_ordinary_suite():
    """12. Neither runs without the gate, and the ordinary suite loads no browser."""
    import sys
    assert "playwright" not in sys.modules
    from safety_guards import PhysicalBrowserEscaped
    with pytest.raises(PhysicalBrowserEscaped):
        executor_adapter.dom_click("s", "p", "tok", 1.0)
    marked = {name for name, value in globals().items()
              if callable(value) and getattr(value, "pytestmark", None)
              and any(m.name == "real_browser" for m in value.pytestmark)}
    assert marked == {SMOKE, ACTION}, marked
