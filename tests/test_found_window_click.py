"""
SLICE 3: PER-CAPABILITY AUTHORIZATION. Ownership stops being the gate for CLICKING.

    close_app, window_control close  -> strict ownership, UNCHANGED
    click_target                     -> named-or-found window + a confirmation that NAMES it
                                        + the structural re-check below

WHY. The owner spends the day in Chrome, and Chrome is a window the assistant can never own: Slice 2's
frozen launch rule means a running Chrome is reused, never relaunched, so no ownership token is ever
attached. Ownership-as-click-gate therefore prevented every click in the one application that matters,
while preventing nothing: the owner did the click by hand, which moves the risk somewhere that cannot
be confirmed or logged. type_text - the HIGHER-risk action - has worked on exactly this model since
Phase 1: foreground window, confirmation naming it, pre-send re-check.

THE CONFIRMATION IS NOW THE AUTHORIZATION, so it is a tested guarantee here rather than a comment. It
always names the window, and it says plainly whether the assistant opened it or only found it.

WHAT DID NOT MOVE: a found window still cannot be closed, _ours_now is unchanged and still requires
both of its clauses, and the DOM path is untouched.
"""
import ast
import inspect
import textwrap

import pytest

from app.executor import adapter as executor_adapter
from app.executor import emergency_stop, logic
from app.executor.logic import execute
from app.executor.models import CLICK_TARGET, CLOSE_APP, OPEN_APP, ExecutorAction, Outcome
from app.safety import logic as safety_logic
from app.safety.models import RiskLevel
from app.verifier import adapter as verifier_adapter
from app.verifier.models import WindowInfo
from tests.test_open_app_available import (CHROME_CLASS, CONSOLE, USERS_CHROME, already_open,
                                           close_app, code_of, handoff, launches, open_app,
                                           owned_handles, world)  # noqa: F401

CONTROL = "Login"
POINT = (400, 300)


# --- the UIA half of the fake desktop, which test_open_app_available.py does not need -----------------

@pytest.fixture
def clickable(world, monkeypatch):
    """Add control resolution and clicking to the Slice 2 desktop, so a whole named click can run."""
    state = world.desktop
    state.control_name = "login"          # normalised, as the verifier reports it
    state.clicks = []
    state.prompts = []
    state.on_top = None                   # which window is at the click point; None = the target
    state.duplicate = False

    def uia_find_by_name(handle, normalized_name, timeout_seconds=2.0):
        if normalized_name != state.control_name:
            return []
        element = _element(handle)
        return [element, _element(handle)] if state.duplicate else [element]

    def click(x, y):
        state.clicks.append((x, y))
        state.pointer = (x, y)

    monkeypatch.setattr(verifier_adapter, "uia_find_by_name", uia_find_by_name)
    monkeypatch.setattr(verifier_adapter, "uia_window_bounds", lambda handle: (0, 0, 800, 600))
    monkeypatch.setattr(verifier_adapter, "list_screens",
                        lambda: [_screen()])
    monkeypatch.setattr(verifier_adapter, "cursor_position", lambda: state.pointer)
    monkeypatch.setattr(verifier_adapter, "window_at",
                        lambda x, y: next((w for w in state.windows
                                           if w.handle == (state.on_top if state.on_top is not None
                                                           else state.front)), None))
    monkeypatch.setattr(executor_adapter, "click", click)
    state.pointer = POINT
    return state


def _screen():
    from app.verifier.models import Screen
    return Screen(left=0, top=0, right=1920, bottom=1080, primary=True)


def _element(handle):
    """A control whose bounds put its centre at POINT - the real shape, built from the real model."""
    from app.verifier.models import UiaElement
    x, y = POINT
    return UiaElement(window_handle=handle, control_type="Button", runtime_id="login-1",
                      automation_id="login-btn", class_name="Button",
                      bounds=(x - 20, y - 10, x + 20, y + 10), enabled=True, focused=False,
                      offscreen=False, is_password=False, patterns=("Invoke",))


def named(control=CONTROL, app="chrome"):
    return ExecutorAction(CLICK_TARGET, app, control)


def recording_confirm(desktop, answer=True):
    def confirm(action, assessment):
        desktop.prompts.append((action.description, assessment.level))
        return answer
    return confirm


def click(desktop, control=CONTROL, app="chrome", answer=True):
    return execute(named(control, app), recording_confirm(desktop, answer))


def own_chrome(world):
    """Give the session a genuinely OWNED chrome window, the Slice 2 way: nothing open, then launch."""
    assert open_app("chrome").ok
    [handle] = owned_handles("chrome")
    return handle


def find_chrome(world):
    """Give the session a FOUND chrome window: one already open, then open_app reuses it."""
    handle = already_open(world)
    assert open_app("chrome").ok
    assert owned_handles("chrome") == [], "this is supposed to be unowned"
    return handle


# =====================================================================================================
# CLICKING (matrix 1-6)
# =====================================================================================================

def test_a_click_in_an_owned_window_works_exactly_as_today(clickable, world):
    """1."""
    handle = own_chrome(world)
    result = click(clickable)
    assert result.ok and result.outcome is Outcome.UNVERIFIED, result.message
    assert clickable.clicks == [POINT]
    assert len(clickable.prompts) == 1
    description, level = clickable.prompts[0]
    assert level is RiskLevel.MEDIUM
    assert description == 'click "Login" in the chrome window I opened ("New Tab - Google Chrome")'
    assert clickable.front == handle, "the owned window was brought forward"


def test_a_click_in_a_found_window_works_behind_a_confirmation_naming_it(clickable, world):
    """2. THE SLICE. The window the owner already had open is now clickable."""
    handle = find_chrome(world)
    result = click(clickable)
    assert result.ok, result.message
    assert clickable.clicks == [POINT]
    description, level = clickable.prompts[0]
    assert level is RiskLevel.MEDIUM, "the risk level did not change"
    assert description == 'click "Login" in a chrome window I did NOT open ("Google Chrome")'
    assert clickable.front == handle


def test_a_found_window_is_used_when_it_is_the_only_candidate_and_no_app_is_named(clickable, world):
    """2b. Without this the owner would have to say "in chrome" on every single command."""
    find_chrome(world)
    result = click(clickable, app="")
    assert result.ok, result.message
    assert clickable.clicks == [POINT]
    assert "did NOT open" in clickable.prompts[0][0]


def test_a_found_window_beside_another_candidate_refuses_as_ambiguous(clickable, world, monkeypatch):
    """2c. A found window does NOT get to skip the ambiguity rule."""
    find_chrome(world)
    monkeypatch.setattr(executor_adapter, "browser_sessions", lambda: [("s1", "p1")])
    result = click(clickable, app="")
    assert not result.ok
    assert clickable.clicks == [], "it clicked despite two candidates"
    assert clickable.prompts == [], "it asked for a confirmation despite two candidates"
    assert "more than one" in result.message.lower(), result.message


def test_two_found_apps_with_nothing_named_also_refuse(clickable, world):
    """2c, without a browser in play: two apps open_app has fronted is still ambiguous."""
    find_chrome(world)
    already_open(world, title="Untitled - Notepad", handle=50, process="notepad.exe",
                 class_name="Notepad")
    assert open_app("notepad").ok
    result = click(clickable, app="")
    assert not result.ok and clickable.clicks == [] and clickable.prompts == []
    assert "More than one app is available to click in" in result.message, result.message
    assert "chrome" in result.message and "notepad" in result.message


def test_a_window_that_is_neither_owned_nor_found_still_refuses(clickable, world):
    """3. A window merely being visible has never made it a target, and still does not."""
    already_open(world)            # on the desktop, but open_app was never asked for it
    result = click(clickable)
    assert not result.ok
    assert clickable.clicks == [] and clickable.prompts == []
    assert "I don't have a window of chrome to click in" in result.message, result.message


def test_an_owned_window_is_preferred_over_a_found_one(clickable, world):
    """A found window must never quietly displace one the token proves. Both exist here."""
    owned = own_chrome(world)
    already_open(world)
    logic._remember_found("chrome", WindowInfo(USERS_CHROME, "Google Chrome", CHROME_CLASS))
    context = logic._context_for_named_click("chrome")
    assert not isinstance(context, str), context
    assert context.owned is True and context.window.handle == owned


@pytest.mark.parametrize("setup, expected", [
    ("owned", 'click "Login" in the chrome window I opened ("New Tab - Google Chrome")'),
    ("found", 'click "Login" in a chrome window I did NOT open ("Google Chrome")'),
])
def test_the_confirmation_names_the_window_and_says_whose_it_is(clickable, world, setup, expected):
    """4 and 5. From the prompt alone the owner can tell which kind of window they are authorizing."""
    own_chrome(world) if setup == "owned" else find_chrome(world)
    assert click(clickable).ok
    description, _level = clickable.prompts[0]
    assert description == expected
    assert ("I opened" in description) is (setup == "owned")
    assert ("did NOT open" in description) is (setup == "found")


@pytest.mark.parametrize("setup", ["owned", "found"])
def test_the_result_names_the_window_it_acted_in(clickable, world, setup):
    """6. Provenance stays VISIBLE now that it is no longer a gate."""
    own_chrome(world) if setup == "owned" else find_chrome(world)
    result = click(clickable)
    assert result.ok
    assert "Clicked \"Login\" in " in result.message, result.message
    assert ("I did NOT open" in result.message) is (setup == "found"), result.message
    assert ("window I opened" in result.message) is (setup == "owned"), result.message


def test_the_prepared_click_in_a_found_window_demands_a_medium_confirmation(clickable, world):
    """THE PRICE OF THE LOOSENING, asserted against the object PRODUCTION builds rather than against
    the risk constant. _prepare_named_click returns the safety Action the gate will classify, so
    deleting the confirmation from that line makes this fail - which is what makes the ownership gate's
    removal provably paid for."""
    find_chrome(world)
    prepared = logic._prepare_named_click(named())
    assert isinstance(prepared, logic._Prepared), prepared
    gate = prepared.safety_action
    assert gate is not None, "the click was prepared with no safety action at all"
    assert gate.minimum_level is RiskLevel.MEDIUM, gate
    assert "did NOT open" in gate.description, gate.description
    assert safety_logic.assess(gate).level is RiskLevel.MEDIUM


def test_the_prepared_click_in_an_owned_window_demands_the_same_confirmation(clickable, world):
    """...and an owned window is gated identically, so the two paths cannot drift apart."""
    own_chrome(world)
    gate = logic._prepare_named_click(named()).safety_action
    assert gate.minimum_level is RiskLevel.MEDIUM
    assert "I opened" in gate.description, gate.description


def test_the_window_title_never_reaches_the_log(clickable, world, caplog):
    """The title is shown so a person can RECOGNISE the window; it is still never written to disk."""
    import logging
    find_chrome(world)
    with caplog.at_level(logging.INFO):
        assert click(clickable).ok
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "Google Chrome" not in text, text
    assert CONTROL not in text, "the user's control word reached the log"


# =====================================================================================================
# THE RE-CHECK (matrix 7-10)
# =====================================================================================================

def test_a_handle_that_no_longer_exists_refuses(clickable, world):
    """7. Checked after the confirmation, before anything is activated or clicked."""
    handle = find_chrome(world)

    def vanishing(action, assessment):
        clickable.prompts.append((action.description, assessment.level))
        clickable.windows = [w for w in clickable.windows if w.handle != handle]
        return True

    result = execute(named(), vanishing)
    assert not result.ok
    assert clickable.clicks == [], "it clicked into a window that had gone"
    assert "isn't the same one any more" in result.message, result.message


def test_a_handle_that_no_longer_belongs_to_the_app_refuses(clickable, world):
    """8. The same number, now owned by a different program."""
    handle = find_chrome(world)

    def repurposed(action, assessment):
        clickable.prompts.append((action.description, assessment.level))
        clickable.processes[handle] = "excel.exe"
        return True

    result = execute(named(), repurposed)
    assert not result.ok and clickable.clicks == []
    assert "isn't the same one any more" in result.message


def test_the_same_handle_with_a_different_window_class_refuses(clickable, world):
    """9. THE FINGERPRINT. The handle exists, the program matches, the title still matches the app's
    pattern - and it is a different KIND of window, so it is not the one that was authorized."""
    handle = find_chrome(world)

    def reclassed(action, assessment):
        clickable.prompts.append((action.description, assessment.level))
        clickable.windows = [WindowInfo(w.handle, w.title, "Chrome_WidgetWin_2")
                             if w.handle == handle else w for w in clickable.windows]
        return True

    result = execute(named(), reclassed)
    assert not result.ok and clickable.clicks == []
    assert "isn't the same one any more" in result.message


def test_a_control_that_no_longer_resolves_uniquely_refuses(clickable, world):
    """10. The existing re-identification, unchanged: two matches is not a preference."""
    find_chrome(world)

    def duplicated(action, assessment):
        clickable.prompts.append((action.description, assessment.level))
        clickable.duplicate = True
        return True

    result = execute(named(), duplicated)
    assert not result.ok and clickable.clicks == []


def test_the_registry_being_re_pointed_during_the_confirmation_refuses(clickable, world):
    """FOUND BY MUTATION. `window.handle != context.window.handle` in _found_still_valid survived
    being deleted, because nothing exercised it: _found_now reads the SAME record the context came
    from, so the two normally agree by construction.

    They stop agreeing if _found_windows is re-pointed between the confirmation and the click - which
    a second open_app in a multi-step plan does. The confirmation named ONE window, so a different one
    must not be clicked, and that is now tested rather than merely written."""
    find_chrome(world)
    other = already_open(world, title="Mail - Google Chrome", handle=USERS_CHROME + 5)

    def repoint(action, assessment):
        clickable.prompts.append((action.description, assessment.level))
        logic._remember_found("chrome", WindowInfo(other, "Mail - Google Chrome", CHROME_CLASS))
        return True

    result = execute(named(), repoint)
    assert not result.ok, "it clicked in a window the confirmation did not name"
    assert clickable.clicks == []
    assert "isn't the same one any more" in result.message, result.message


def test_the_re_check_reads_four_independent_facts():
    """What the re-check PROVES, held at the source so the list cannot quietly shrink."""
    code = code_of(logic._found_now)
    assert "verifier.find_open(expectation, frozenset({found.handle}))" in code   # exists + app pattern
    assert "window.class_name != found.class_name" in code                        # same kind of window
    assert "verifier.process_name(window.handle) != found.process" in code        # same program
    assert "not found.process" in code, "an unreadable executable must fail closed"
    assert ".title" not in code, "the title must not become the identity proof"


def test_an_owned_window_does_not_pay_for_the_found_re_check(clickable, world):
    """The re-check is for found windows only: an owned window's token cannot be inherited by another
    window, so there is nothing for a fingerprint to add."""
    own_chrome(world)
    calls = []
    real = logic._found_still_valid

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(logic, "_found_still_valid",
                      lambda action, context: calls.append(context) or real(action, context))
        assert click(clickable).ok
    assert calls == [], "the owned path ran the found-window re-check"


def test_the_re_check_runs_before_anything_is_activated(clickable, world):
    """Order matters: a window that failed the check must not be brought to the front either."""
    code = code_of(logic._activate_then)
    assert code.index("_found_still_valid") < code.index("_bring_window_forward")
    handle = find_chrome(world)

    def vanishing(action, assessment):
        clickable.prompts.append((action.description, assessment.level))
        clickable.windows = [w for w in clickable.windows if w.handle != handle]
        return True

    clickable.activations.clear()
    execute(named(), vanishing)
    assert clickable.activations == [], "it activated a window that had failed the re-check"


# =====================================================================================================
# CLOSING - UNCHANGED (matrix 11-14)
# =====================================================================================================

def test_close_app_on_a_found_window_still_refuses(clickable, world):
    """11. Clickable does NOT mean closable. This is the per-capability split, at its sharpest."""
    handle = find_chrome(world)
    assert click(clickable).ok, "the premise: it IS clickable"
    result = close_app("chrome")
    assert not result.ok
    assert "I only close windows I opened in this session" in result.message
    assert ("close", handle) not in world.calls


def test_close_app_on_an_owned_window_still_works(clickable, world):
    """12."""
    own_chrome(world)
    assert close_app("chrome").ok


def test_window_control_close_still_requires_ownership():
    """13. Untouched by this slice, and it must not learn about found windows."""
    code = code_of(logic._prepare_window_control)
    assert "_found_windows" not in code and "_found_now" not in code
    assert "found_window_handle" not in code


def test_ours_now_is_unchanged_and_still_both_clauses():
    """14. It stopped being the CLICK gate. It did not become weaker."""
    code = code_of(logic._ours_now)
    assert "verifier.find_open(expectation, group.handles)" in code
    assert "adapter.window_token(window.handle) == group.token" in code
    assert "_found" not in code, "ownership learned about found windows"


def test_only_the_click_path_reads_the_found_registry():
    """The per-capability split, structurally: exactly which functions may consult it."""
    tree = ast.parse(code_of(logic))
    readers = sorted(node.name for node in ast.walk(tree)
                     if isinstance(node, ast.FunctionDef)
                     and any(name in ast.unparse(node)
                             for name in ("_found_windows", "_found_now"))
                     and node.name not in ("_remember_found", "_forget_found", "found_window_handle",
                                           "forget_session_windows", "_found_now"))
    assert readers == ["_context_for_named_click", "_found_still_valid"], readers


# =====================================================================================================
# DENIAL (matrix 15-16)
# =====================================================================================================

def test_a_literal_true_permits_a_click_in_a_found_window(clickable, world):
    """15. Half one, unchanged from the existing contract."""
    find_chrome(world)
    assert click(clickable, answer=True).ok
    assert clickable.clicks == [POINT]


@pytest.mark.parametrize("answer", [False, None, "yes", "y", 1, ""])
def test_every_existing_denial_form_still_denies_in_a_found_window(clickable, world, answer):
    """15. Half two: only a literal True permits, and a found window gets no softer gate than an owned
    one - it is the same gate object, asked the same way. The denial still RAISES, which is how the
    safety gate has always reported it; this slice did not change that either."""
    find_chrome(world)
    with pytest.raises(safety_logic.ActionDeniedError):
        click(clickable, answer=answer)
    assert clickable.clicks == []


def test_the_safety_gate_itself_was_not_touched():
    """15, structurally: this slice changed no part of app/safety/."""
    gate = code_of(safety_logic.authorize)
    assert "is True" in gate
    assert "_found" not in gate and "owned" not in gate


def test_a_denial_in_a_found_window_clicks_nothing(clickable, world):
    """16, with Slice 1's rule: a command typed at the confirmation denies, and nothing happens."""
    find_chrome(world)
    from app import console
    script = iter(["close notepad"])
    screen = []
    pending = console.PendingCommand()
    confirm = console._confirm(lambda prompt: next(script), screen.append, pending)
    with pytest.raises(safety_logic.ActionDeniedError):
        execute(named(), confirm)
    assert clickable.clicks == []
    assert pending.take() == "close notepad", "Slice 1's handoff stopped working"
    # and the prompt the console showed named the window it was about to click in
    assert any("did NOT open" in line for line in screen), screen


# =====================================================================================================
# REGRESSION (matrix 17-18)
# =====================================================================================================

def test_the_dom_click_path_is_untouched():
    """17. A page is not a window: the DOM path has no window handle and no provenance to report."""
    code = code_of(logic._prepare_dom_click)
    for name in ("_found_windows", "_found_now", "_found_still_valid", "owned", "_describe_window"):
        assert name not in code, name


@pytest.mark.parametrize("function", ["_prepare_type_text", "_prepare_shortcut", "_prepare_scroll",
                                      "_prepare_refresh"])
def test_the_other_actions_are_untouched_this_slice(function):
    """18. Slice 4's job, not this one. None of them learned about found windows."""
    code = code_of(getattr(logic, function))
    for name in ("_found_windows", "_found_now", "_found_still_valid", "_context_for_named_click"):
        assert name not in code, f"{function} reads {name}"


def test_the_click_risk_level_did_not_move():
    assert logic._TARGET_CLICK_RISK is RiskLevel.MEDIUM


def test_the_bare_click_target_primitive_keeps_its_old_wording():
    """The gated real-UIA entry point has no context, so it has no provenance to report and its
    confirmation is byte-identical to before this slice."""
    code = code_of(logic._describe_window)
    assert 'if context is None' in code
    window = WindowInfo(1, "Calculator", "ApplicationFrameWindow")
    assert logic._describe_window(window, None) == 'window "Calculator"'
    assert logic._describe_window(WindowInfo(1, "", ""), None) == "a window with no readable title"
