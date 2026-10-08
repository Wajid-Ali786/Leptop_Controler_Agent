"""
SLICE 2: `open <app>` means ENSURE THE APP IS AVAILABLE AND IN FRONT.

THE FAILURE THIS FIXES, from the owner's real Chrome session:

    > open chrome and click on M. Shakar Profile in the chrome
    chrome was started, but no new window appeared within 15 seconds

Chrome was already open. The launch executed correctly; the SUCCESS TEST was wrong - it required a
window that did not exist in `before`, and Chrome's launcher hands off to the running process and
creates none. Because the open "failed", no window was recorded, and every later command in Chrome
refused for the rest of the session.

THE FROZEN LAUNCH DECISION: if exactly one usable window already exists, do NOT launch again -
activate that one. Three cases follow from it:

  A  nothing usable open      -> launch, wait for a new window, own it exactly as before
  B  exactly one already open -> no launch, no wait, bring it forward, and it stays UNOWNED
  C  launched but no new window was proved -> look at what IS there before calling it a failure

THE OWNERSHIP BOUNDARY IS THE POINT OF THE FILE. A window that existed before the command never
receives the assistant's ownership token, whatever open_app does with it. _found_windows records only
that open_app selected it; nothing reads that record, close_app cannot reach it, and a named click
still refuses. The last section pins exactly that state, because it is what Slice 3 has to start from.
"""
import ast
import inspect
import re
import subprocess
import textwrap
from types import SimpleNamespace

import pytest

from app.executor import adapter as executor_adapter
from app.executor import emergency_stop
from app.executor import logic
from app.executor.logic import execute
from app.executor.models import CLOSE_APP, OPEN_APP, ExecutorAction, Outcome
from app.safety import logic as safety_logic
from app.verifier import adapter as verifier_adapter
from app.verifier.models import ActiveTarget, WindowInfo, WindowState
from config import settings
from tests import fake_window_props

CONFIG = (
    "safety:\n"
    '  risky_keywords: [delete, shutdown, send]\n'
    "  safe_words: [sender]\n"
    "executor:\n"
    "  apps:\n"
    "    notepad: notepad.exe\n"
    "    chrome: chrome.exe\n"
    "  max_attempts: 1\n"
    "  activation_settle_seconds: 0.2\n"
    "verifier:\n"
    "  window_timeout_seconds: 0.2\n"
    "  poll_interval_seconds: 0.01\n"
    "  app_windows:\n"
    '    notepad: "Notepad$"\n'
    '    chrome: "Chrome$"\n'
    # Needed by tests/test_found_window_click.py, which borrows this fixture to run a whole named
    # click through it. Harmless here: Slice 2's own tests never reach the observation layer.
    "observation:\n"
    "  snapshot_timeout_seconds: 2.0\n"
)
NEW_TITLES = {"notepad.exe": "Untitled - Notepad", "chrome.exe": "New Tab - Google Chrome"}
# The top-level window class and the owning executable: the structural fingerprint Slice 3 records for
# a found window and re-checks before clicking in it. Real values, so the fake cannot be kinder than
# Windows - "Chrome_WidgetWin_1" is what a real Chrome frame reports.
CLASSES = {"chrome.exe": "Chrome_WidgetWin_1", "notepad.exe": "Notepad"}
CHROME_CLASS = CLASSES["chrome.exe"]
CONSOLE = 7           # stands in for "something else is in front"
USERS_CHROME = 11     # a Chrome window the user already had open


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A fake desktop that can be launched into, activated, and read back - nothing physical.

    Knobs: `windows` (what is open now), `window_appears` (does a launch create one), `arrives` (does
    activation actually reach the foreground), `minimized`, `gone`, `refuse_tags`."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_PATH", config_path)
    calls = []
    desktop = SimpleNamespace(
        windows=[WindowInfo(CONSOLE, "AI Desktop Companion")],   # nothing matching any app
        launches=0,
        window_appears=True,
        front=CONSOLE,
        arrives=True,
        accepts=True,
        minimized=False,
        gone=False,
        refuse_tags=set(),
        activations=[],
        next_handle=1000,
        # handle -> owning executable, read back by verifier.process_name. Faked here because it is a
        # REAL cross-process Win32 read (OpenProcess + QueryFullProcessImageNameW) that is not yet
        # centrally isolated, and an offline test must not make it.
        processes={CONSOLE: "python.exe"},
    )

    def exists(handle):
        return any(w.handle == handle for w in desktop.windows)

    def launch(executable):
        calls.append(("launch", executable))
        desktop.launches += 1
        if desktop.window_appears:
            desktop.next_handle += 1
            desktop.windows.append(WindowInfo(desktop.next_handle, NEW_TITLES[executable],
                                              CLASSES[executable]))
            desktop.processes[desktop.next_handle] = executable
        return 4242

    def activate_window(handle):
        calls.append(("activate", handle))
        desktop.activations.append(handle)
        if desktop.gone:
            raise executor_adapter.WindowGoneError("the window is already closed")
        if desktop.arrives:
            desktop.front = handle
        return desktop.accepts

    def window_state(handle):
        if desktop.gone or not exists(handle):
            return None
        return WindowState(minimized=desktop.minimized, maximized=False, has_minimize_box=True,
                           has_maximize_box=True, tool_window=False, hung=False)

    def active_target():
        front = next((w for w in desktop.windows if w.handle == desktop.front), None)
        return ActiveTarget(window=front)

    def forbidden_popen(*args, **kwargs):
        raise AssertionError("a real process must not be started in this test")

    real_authorize = safety_logic.authorize

    def recording_authorize(action, confirm=None):
        calls.append(("safety", action.description, action.minimum_level))
        return real_authorize(action, confirm)

    monkeypatch.setattr(logic, "authorize", recording_authorize)
    monkeypatch.setattr(executor_adapter, "launch_app", launch)
    monkeypatch.setattr(executor_adapter, "activate_window", activate_window)
    def request_close(handle):
        calls.append(("close", handle))
        desktop.windows = [w for w in desktop.windows if w.handle != handle]
        desktop.props.pop(handle, None)          # the window OBJECT is gone, so its property is too

    monkeypatch.setattr(executor_adapter, "request_close", request_close)
    desktop.props = fake_window_props.install(monkeypatch, executor_adapter, exists=exists,
                                              refuse=desktop.refuse_tags)
    monkeypatch.setattr(verifier_adapter, "list_windows", lambda: list(desktop.windows))
    monkeypatch.setattr(verifier_adapter, "list_child_windows", lambda handle: [])
    monkeypatch.setattr(verifier_adapter, "process_image_name",
                        lambda handle: desktop.processes.get(handle))
    monkeypatch.setattr(verifier_adapter, "window_state", window_state)
    monkeypatch.setattr(verifier_adapter, "active_target", active_target)
    monkeypatch.setattr(subprocess, "Popen", forbidden_popen)
    logic.forget_session_windows()
    emergency_stop.reset("test-setup")
    yield SimpleNamespace(calls=calls, desktop=desktop)
    emergency_stop.reset("test-teardown")
    logic.forget_session_windows()


def open_app(name="chrome"):
    return execute(ExecutorAction(OPEN_APP, name))


def close_app(name="chrome"):
    return execute(ExecutorAction(CLOSE_APP, name), confirm=lambda *args: True)


def launches(world):
    return [call for call in world.calls if call[0] == "launch"]


def already_open(world, title="Google Chrome", handle=USERS_CHROME, *, process="chrome.exe",
                 class_name=CHROME_CLASS, **kwargs):
    """A window the USER already had open, matching the app's pattern, with a real-shaped fingerprint."""
    world.desktop.windows.append(WindowInfo(handle, title, class_name, **kwargs))
    world.desktop.processes[handle] = process
    return handle


def owned_handles(app="chrome"):
    with logic._session_lock:
        return [handle for group in logic._session_windows.get(app, []) for handle in group.handles]


def code_of(obj) -> str:
    """Source with docstrings stripped, so a rule about the code is never satisfied by prose."""
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
# CASE A - nothing usable open (matrix 1-4). The path that already worked, and must keep working.
# ======================================================================================================

def test_it_launches_waits_and_succeeds_as_before(world):
    """1 and 2."""
    result = open_app()
    assert result.ok and result.outcome is Outcome.DONE
    assert launches(world) == [("launch", "chrome.exe")]
    assert re.fullmatch(r"Opened chrome; its window appeared after \d+\.\ds\.", result.message), \
        result.message


def test_the_new_window_is_recorded_and_tagged_exactly_as_today(world):
    """3. Ownership is unchanged on this path: the token is attached and read back, and close works."""
    assert open_app().ok
    [handle] = owned_handles()
    assert handle in world.desktop.props and world.desktop.props[handle], "no token was attached"
    assert close_app().ok, "the window the assistant opened is closable"


def test_a_genuine_launch_failure_still_fails_with_the_existing_message(world):
    """4. The ONLY case that keeps "no new window appeared": we launched and genuinely got nothing."""
    world.desktop.window_appears = False
    result = open_app()
    assert not result.ok and result.retryable
    assert result.message == "chrome was started, but no new window appeared within 0.2 seconds."
    assert launches(world) == [("launch", "chrome.exe")]
    assert owned_handles() == []


def test_a_launcher_error_is_still_reported_cleanly(world, monkeypatch):
    """4, the other half: the launch itself refusing is unchanged."""
    def broken(executable):
        raise executor_adapter.ExecutorAdapterError("chrome.exe isn't where the config says")

    monkeypatch.setattr(executor_adapter, "launch_app", broken)
    result = open_app()
    assert not result.ok and result.message == "chrome.exe isn't where the config says"


# ======================================================================================================
# CASE B - exactly one window already open (matrix 5-9). The owner's real failure.
# ======================================================================================================

def test_an_already_open_app_is_not_launched_again(world):
    """5. THE FIX. The launcher must not be called at all."""
    already_open(world)
    result = open_app()
    assert result.ok, result.message
    assert launches(world) == [], "an app that was already open was launched again"


def test_the_existing_window_is_brought_forward(world):
    """6. "In front" means actually foregrounded, through the one activation path."""
    handle = already_open(world)
    assert open_app().ok
    assert world.desktop.activations == [handle]
    assert world.desktop.front == handle


def test_the_message_says_it_was_already_open(world):
    """7. Not "opened", and emphatically not "no new window appeared"."""
    already_open(world)
    result = open_app()
    assert result.message == ("chrome was already open, so I brought it to the front instead of "
                              "opening another.")
    assert "no new window appeared" not in result.message


def test_the_already_open_path_does_not_pay_the_window_wait(world, monkeypatch):
    """8. In case B the 15-second wait is pure loss, so it is never entered - asserted at the function,
    not by timing the test, because a clock assertion would pass on a fast machine either way."""
    def forbidden(*args, **kwargs):
        raise AssertionError("the already-open path waited for a new window")

    monkeypatch.setattr(logic.verifier, "wait_for_new_window", forbidden)
    already_open(world)
    assert open_app().ok


def test_the_found_window_is_not_tagged_and_close_still_refuses_it(world):
    """9, AND THE WHOLE POINT OF THE SLICE. Activating someone's window is not acquiring it."""
    handle = already_open(world)
    assert open_app().ok
    assert owned_handles() == [], "a pre-existing window was recorded as owned"
    assert not world.desktop.props.get(handle), "a pre-existing window was tagged"
    result = close_app()
    assert not result.ok, "close_app could close a window the user already had open"
    assert "I only close windows I opened in this session" in result.message
    assert ("close", handle) not in world.calls


# ======================================================================================================
# CASE C - launched, but no new window was proved (matrix 10-13)
# ======================================================================================================

def handoff(world, monkeypatch, *, windows=1):
    """Model a launcher that hands off to a running process and creates nothing.

    A matching window is there but CLOAKED, so it is not usable and open_app does launch. The launcher
    creates nothing; the existing window then becomes visible. Because that window was in `before`,
    wait_for_new_window can never count it - which is the exact shape of the owner's Chrome failure."""
    handles = [already_open(world, title=f"Tab {n} - Google Chrome", handle=USERS_CHROME + n,
                            cloaked=True) for n in range(windows)]

    def hands_off(executable):
        world.calls.append(("launch", executable))
        for handle in handles:
            world.desktop.windows = [w if w.handle != handle
                                     else WindowInfo(handle, w.title, w.class_name)
                                     for w in world.desktop.windows]
        return 4242

    monkeypatch.setattr(executor_adapter, "launch_app", hands_off)
    return handles


def test_a_handoff_that_creates_no_new_window_still_succeeds_if_the_app_is_there(world, monkeypatch):
    """10 and 11. The launcher runs, hands off, and creates nothing - but the app IS available, so
    reporting "no new window appeared" would be false. This is Chrome's real behaviour."""
    handoff(world, monkeypatch)
    result = open_app()
    assert launches(world) == [("launch", "chrome.exe")], "it should have tried: nothing was usable"
    assert result.ok, result.message
    assert "no new window appeared" not in result.message
    assert result.message == ("chrome is open and in front. I can't prove I'm the one who opened that "
                              "window, so I won't close it automatically.")


def test_case_c_reports_provenance_honestly_and_owns_nothing(world, monkeypatch):
    """10. "Report exactly what provenance can be proved" - which here is none."""
    [handle] = handoff(world, monkeypatch)
    assert open_app().ok
    assert owned_handles() == []
    assert not world.desktop.props.get(handle), "a window we cannot attribute to us was tagged"
    assert not close_app().ok, "close_app could reach a window of unproven provenance"


def test_nothing_afterwards_is_a_genuine_launch_failure(world):
    """12. Unchanged: the existing message survives for exactly this case."""
    world.desktop.window_appears = False
    result = open_app()
    assert not result.ok
    assert result.message == "chrome was started, but no new window appeared within 0.2 seconds."


def test_several_windows_afterwards_with_no_provenance_refuses_as_ambiguous(world, monkeypatch):
    """13. Two windows and nothing to tell them apart: refuse rather than pick."""
    handoff(world, monkeypatch, windows=2)
    result = open_app()
    assert not result.ok
    assert "2 windows open" in result.message
    assert world.desktop.activations == [], "a window was activated despite the ambiguity"


# ======================================================================================================
# AMBIGUITY - deferred, not solved (matrix 14)
# ======================================================================================================

def test_two_pre_existing_windows_refuse_and_touch_nothing(world):
    """14. States the count, activates none, launches nothing, asks nothing."""
    already_open(world, handle=USERS_CHROME)
    already_open(world, title="Mail - Google Chrome", handle=USERS_CHROME + 1)
    result = open_app()
    assert not result.ok and not result.retryable
    assert result.message == ("chrome already has 2 windows open and I didn't open any of them, so I "
                              "don't know which one you mean. I've left them all alone and opened "
                              "nothing.")
    assert launches(world) == [], "it launched anyway"
    assert world.desktop.activations == [], "it activated one anyway"
    assert owned_handles() == []


def test_the_ambiguity_refusal_asks_no_new_question(world):
    """14. Selection machinery is deferred: no clarification is invented here."""
    already_open(world, handle=USERS_CHROME)
    already_open(world, title="Mail - Google Chrome", handle=USERS_CHROME + 1)
    asked = []
    execute(ExecutorAction(OPEN_APP, "chrome"), confirm=lambda *a: asked.append(a) or True)
    assert asked == [], "open_app asked something"


def test_a_cloaked_window_does_not_count_as_already_open(world):
    """"Usable" is the same test the Verifier applies to a window that has just appeared: a window
    Windows is keeping off screen is not one a person can act in."""
    already_open(world, cloaked=True)
    assert open_app().ok
    assert launches(world) == [("launch", "chrome.exe")], "a cloaked window blocked the launch"


# ======================================================================================================
# FOREGROUND (matrix 15-17)
# ======================================================================================================

def test_activation_refused_is_available_but_not_in_front(world):
    """15. Not a plain success and not a failure.

    Outcome.NEEDS_USER, which is the closest EXISTING semantics: the action cannot finish until the
    person does something. Outcome.PARTIAL was the other candidate and is wrong - the model requires
    progress=(sent, total) counting units of work on it, which an activation has none of."""
    already_open(world)
    world.desktop.arrives = False          # SetForegroundWindow reports success; the window never gets it
    result = open_app()
    assert result.outcome is Outcome.NEEDS_USER
    assert not result.ok, "NEEDS_USER cannot be ok - app/executor/models.py enforces that"
    assert not result.retryable, "the model forbids retrying into NEEDS_USER, which is right here"
    assert result.message == ("chrome is already open, but Windows wouldn't bring it to the front. "
                              "It's running - put it in front yourself if you need it there.")
    assert "no new window" not in result.message


def test_activation_refusal_never_relaunches(world):
    """15. "Do not relaunch it merely because activation failed."""
    already_open(world)
    world.desktop.arrives = False
    open_app()
    assert launches(world) == []


def test_activation_refusal_does_not_report_the_app_as_absent(world):
    """15. The app is available, and the message has to say so."""
    already_open(world)
    world.desktop.arrives = False
    message = open_app().message
    assert "already open" in message and "running" in message


def test_the_existing_activation_path_is_reused_not_duplicated():
    """16. One mechanism, one place. Only _activate_to_front may call activate_window, and open_app
    reaches the foreground through it rather than through a second implementation."""
    source = ast.parse(code_of(logic))
    callers = sorted(node.name for node in ast.walk(source)
                     if isinstance(node, ast.FunctionDef)
                     and any(isinstance(call, ast.Call)
                             and ast.unparse(call.func) == "adapter.activate_window"
                             for call in ast.walk(node)))
    assert callers == ["_activate_to_front"], callers
    assert "_activate_to_front(window.handle)" in code_of(logic._found_and_fronted)
    assert "_activate_to_front(window.handle)" in code_of(logic._bring_window_forward)


def test_a_minimized_existing_window_is_reported_and_never_restored(world):
    """17, PINNED BEHAVIOUR. The app is available but cannot be brought to the front, and the window
    is deliberately NOT restored - un-minimising someone's window is a change to their desktop that
    nobody asked for, which is the same decision the named-click path already made."""
    already_open(world)
    world.desktop.minimized = True
    result = open_app()
    assert result.outcome is Outcome.NEEDS_USER and not result.ok
    assert result.message == ("chrome is already open but minimized, so I couldn't bring it to the "
                              "front. Bring it back up and say that again.")
    assert world.desktop.activations == [], "it tried to activate a minimized window"
    assert launches(world) == [], "it relaunched instead of saying so"


def test_a_window_that_closes_before_activation_is_not_relaunched(world):
    """The remaining foreground outcome: it was listed, then vanished. Reported, retryable, and no
    relaunch on the Executor's own initiative."""
    already_open(world)
    world.desktop.gone = True
    result = open_app()
    assert not result.ok and result.retryable
    assert result.message == "chrome's window closed before I could bring it to the front."
    assert launches(world) == []


# ======================================================================================================
# OWNERSHIP (matrix 18-20) - the boundary that must not move
# ======================================================================================================

def test_a_found_window_never_receives_the_ownership_token(world):
    """18. Whatever open_app does with a window it found, it does not tag it."""
    handle = already_open(world)
    assert open_app().ok
    assert not world.desktop.props.get(handle)
    assert executor_adapter.window_token(handle) is None


def test_close_app_cannot_close_a_window_the_user_already_had_open(world):
    """19. End to end, through the real close_app and the real safety gate."""
    handle = already_open(world)
    assert open_app().ok
    result = close_app()
    assert not result.ok
    assert ("close", handle) not in world.calls
    assert world.desktop.windows[-1].handle == handle, "the user's window is still there"


def test_the_found_registry_is_structurally_separate_from_ownership(world):
    """20. _found_windows cannot be mistaken for _session_windows: different type, no token field, and
    no ownership check reads it."""
    already_open(world)
    assert open_app().ok
    assert logic._session_windows.get("chrome", []) == []
    assert logic.found_window_handle("chrome") == USERS_CHROME
    found = logic._found_windows["chrome"]
    assert not hasattr(found, "token"), "a found window has somewhere to put a token"
    assert type(found) is not logic._OwnedWindowGroup
    assert "token" not in logic._FoundWindow.__dataclass_fields__
    # SLICE 3 REWRITE. _context_for_named_click now DOES consult the registry - that is the slice -
    # so it leaves this list. Every OWNERSHIP path still must not, and that is the enduring claim:
    # ownership is unchanged, and closing is still gated on it.
    for function in (logic._ours_now, logic._open_session_group, logic._prepare_close_app,
                     logic._prepare_window_control):
        assert "_found_windows" not in code_of(function), function.__name__
        assert "found_window_handle" not in code_of(function), function.__name__
        assert "_found_now" not in code_of(function), function.__name__


def test_ours_now_is_unchanged():
    """20. The one ownership test, untouched by this slice: both halves still required."""
    code = code_of(logic._ours_now)
    assert "verifier.find_open(expectation, group.handles)" in code
    assert "adapter.window_token(window.handle) == group.token" in code


def test_the_found_record_is_dropped_when_the_session_is_forgotten(world):
    """Session state, so it has the session's lifetime. It granted nothing, so clearing it takes
    nothing away - but a stale record outliving its reason would be misleading."""
    already_open(world)
    assert open_app().ok
    assert logic.found_window_handle("chrome") == USERS_CHROME
    logic.forget_session_windows()
    assert logic.found_window_handle("chrome") is None


# ======================================================================================================
# LATER COMMANDS (matrix 21) - THE FACT SLICE 3 NEEDS
# ======================================================================================================

def test_after_an_already_open_success_a_named_click_can_use_the_found_window(world):
    """21, INVERTED BY SLICE 3, which is the whole point of that slice.

    Slice 2 pinned the opposite: a found window could not be clicked, because _context_for_named_click
    answered "where may I click?" from _session_windows, which means OWNED. Slice 3 decided that
    ownership is the gate for CLOSING, not for clicking, so the found window is now a context.

    What did NOT change: it is not owned, it is re-checked structurally before the click, and the
    confirmation says the assistant did not open it. tests/test_found_window_click.py holds all of
    that; this one holds the chooser's answer."""
    already_open(world)
    assert open_app().ok
    context = logic._context_for_named_click("chrome")
    assert not isinstance(context, str), f"the found window is still refused: {context!r}"
    assert context.app == "chrome"
    assert context.window.handle == USERS_CHROME
    assert context.owned is False, "a found window was reported as owned"
    # The compensating guarantee - that USING this context demands a MEDIUM confirmation naming the
    # window - is deliberately NOT asserted here. A first draft asserted it by building its own
    # safety Action from the risk constant, which made the test pass even with the confirmation
    # deleted from production: a tautology, and exactly the "loosened test that proves nothing" the
    # brief warned about. It lives in tests/test_found_window_click.py, against the object production
    # actually builds:
    #   test_the_prepared_click_in_a_found_window_demands_a_medium_confirmation
    #   test_the_confirmation_names_the_window_and_says_whose_it_is
    #   test_every_existing_denial_form_still_denies_in_a_found_window
    # This test's own subject is the chooser's answer, and that is all it claims.


def test_a_window_the_assistant_opened_is_still_clickable(world):
    """21's other half, so the refusal above is scoped and not a regression: case A still works."""
    assert open_app().ok
    context = logic._context_for_named_click("chrome")
    assert not isinstance(context, str), f"an owned window stopped being clickable: {context!r}"


def test_what_a_found_record_is_worth_is_a_handle_and_nothing_more(world):
    """21. The honest limit of the representation, for Slice 3 to design against: a handle number plus
    the app it matched. Windows reuses handle numbers, so this is NOT an identity - re-checking it can
    only ask "is a window with that number open and still matching the app?", which a DIFFERENT window
    could also satisfy. Ownership's token cannot be forged that way; this can."""
    already_open(world)
    assert open_app().ok
    handle = logic.found_window_handle("chrome")
    assert handle == USERS_CHROME
    fields = {field for field in logic._FoundWindow.__dataclass_fields__}
    # Slice 3 added the structural fingerprint - executable and top-level window class - so a handle
    # NUMBER alone never decides where a click goes. Still no token: that is what ownership means and
    # a found window must never acquire it.
    assert fields == {"app", "handle", "process", "class_name"}, fields
    assert "token" not in fields


# ======================================================================================================
# SAFETY AND RISK (section 6) - unchanged
# ======================================================================================================

@pytest.mark.parametrize("setup", ["nothing_open", "already_open"])
def test_open_app_stays_low_and_asks_nothing(world, setup):
    """Section 6: bringing an existing window forward is the same activation the named-click path
    already performs inside an already-confirmed action. No confirmation, no new risk level."""
    if setup == "already_open":
        already_open(world)
    asked = []
    execute(ExecutorAction(OPEN_APP, "chrome"), confirm=lambda *a: asked.append(a) or True)
    assert asked == [], "open_app asked for a confirmation"
    [safety] = [call for call in world.calls if call[0] == "safety"]
    assert safety[1] == "open app chrome"
    from app.safety.models import RiskLevel
    assert safety[2] is RiskLevel.LOW


# ======================================================================================================
# REGRESSION (matrix 22-24)
# ======================================================================================================

def test_close_app_is_untouched_by_this_slice():
    """22. Nothing in close_app's preparer learned about found windows."""
    code = code_of(logic._prepare_close_app)
    assert "_found_windows" not in code and "found_window_handle" not in code
    assert "_open_session_group(name, expectation)" in code


def test_the_browser_commands_are_untouched():
    """23. open_browser / close_browser never went near app windows and still do not."""
    for function in (logic._prepare_open_browser, logic._prepare_close_browser):
        code = code_of(function)
        assert "_app_windows_now" not in code and "_found_and_fronted" not in code
        assert "_activate_to_front" not in code


def test_only_open_app_records_a_found_window():
    """24. One writer, so no other action can quietly start recording windows it did not open."""
    tree = ast.parse(code_of(logic))
    writers = sorted(node.name for node in ast.walk(tree)
                     if isinstance(node, ast.FunctionDef)
                     and any(isinstance(call, ast.Call)
                             and ast.unparse(call.func) in ("_remember_found", "logic._remember_found")
                             for call in ast.walk(node)))
    assert writers == ["_found_and_fronted"], writers
