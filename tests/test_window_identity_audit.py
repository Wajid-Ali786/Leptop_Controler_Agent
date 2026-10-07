"""
Window-identity audit (Slice 3B real-session failure).

THE FACT THIS FILE EXPLAINS. In ONE voice process, a Brain-planned open succeeded at 22:52:13 with
"1 window(s) in its group", and an explicit close 15 minutes 34 seconds later said "I only close windows
I opened in this session. 1 notepad window is open, but I didn't open it". No process boundary.

Ownership is a set of numeric HWNDs, and verifier.find_open() requires `w.handle in handles`. So if the
stored handle stops existing while a matching window still does, that exact sentence is the only thing
the code can say. These tests record what the current code does for every identity transition a real
window can go through, against a fake desktop. Nothing real is launched, closed or observed.

This is an AUDIT: it changes no policy and fixes nothing. Each test names the behaviour it found.
"""
import pytest

from app.executor import logic as executor_logic
from app.executor.models import CLOSE_APP, OPEN_APP, ExecutorAction
from tests.test_executor_close_app import (USERS_NOTEPAD, FakeDesktop, close_app,  # noqa: F401
                                            open_app, users_notepad, world)

REFUSAL = "I only close windows I opened in this session"


def owned(name="notepad"):
    return [set(group.handles) for group in executor_logic._session_windows.get(name, [])]


def notepads(world):
    return [handle for handle, window in world.desktop.windows.items() if "Notepad" in window.title]


def replace(world, handle, title="Untitled - Notepad", class_name="Notepad"):
    """A window with the same title appears under a DIFFERENT numeric handle, and the old one is gone.

    This is the transition the audit is about: the app is still there, its identity as the code records
    it is not."""
    world.desktop.destroy(handle)
    return world.desktop.add(title, class_name)


# --- 1. The baseline: a stable handle ------------------------------------------------------------------

def test_a_stable_handle_closes_normally(world):
    open_app("notepad")
    assert len(owned()) == 1
    result = close_app("notepad")
    assert result.ok, result.message
    assert owned() == []


# --- 2. The handle is replaced ------------------------------------------------------------------------

def test_a_replaced_handle_produces_exactly_the_real_refusal(world):
    """THE REPRODUCTION. The owned handle disappears and a same-titled window appears under a new one.

    FOUND: the code refuses, with the real session's sentence word for word - including "1 notepad
    window is open", because exactly one matching window exists and it is not the one on record."""
    open_app("notepad")
    [original] = [handle for group in owned() for handle in group]
    world.desktop.destroy(USERS_NOTEPAD.handle)      # leave exactly one notepad, as in the log
    replace(world, original)
    assert len(notepads(world)) == 1, "one notepad window is open, as the real session reported"

    result = close_app("notepad")
    assert not result.ok
    assert REFUSAL in result.message
    assert "1 notepad window is open, but I didn't open it" in result.message, result.message
    assert owned() == [], "and the stale group is forgotten"


def test_the_replacement_window_is_never_closed(world):
    """The safety property holds through the failure: an unrecognised window is left alone."""
    open_app("notepad")
    [original] = [handle for group in owned() for handle in group]
    newcomer = replace(world, original)
    close_app("notepad")
    closed = [handle for kind, handle in world.calls if kind == "close"]
    assert newcomer not in closed and original not in closed


def test_a_handle_that_merely_changes_title_still_belongs_to_us(world):
    """Not every change is an identity change. The handle is what ownership is, so a retitled window -
    a different file opened in the same Notepad - is still ours, as long as it still matches."""
    open_app("notepad")
    [handle] = [h for group in owned() for h in group]
    world.desktop.update(handle, title="shopping list - Notepad")
    result = close_app("notepad")
    assert result.ok, result.message


def test_a_handle_that_stops_matching_the_pattern_is_lost(world):
    """FOUND: find_open() requires the title pattern too, so a window renamed out of the pattern is
    treated as gone even though the handle still exists."""
    open_app("notepad")
    users_notepad(world)          # a stranger, so losing ours degrades to a refusal not "already closed"
    [handle] = [h for group in owned() for h in group]
    world.desktop.update(handle, title="something else entirely")
    result = close_app("notepad")
    assert not result.ok
    assert REFUSAL in result.message or "already closed" in result.message


# --- 3. Frame and content transitions, for a grouped (Store-style) app ---------------------------------
# calculator is the fake's Store app and opens TWO windows - which matches the real log exactly:
# "calculator window appeared after 1.33s (2 window(s) in its group)".

def test_a_grouped_app_records_both_handles(world):
    open_app("calculator")
    assert len(owned("calculator")) == 1
    assert len(owned("calculator")[0]) == 2, "a frame and its content window"


def test_one_of_two_handles_replaced_keeps_ownership(world):
    """A: the frame survives, the content window is replaced. FOUND: ownership SURVIVES - find_open
    needs only one handle of the group to still be present and matching."""
    open_app("calculator")
    group = sorted(owned("calculator")[0])
    world.desktop.destroy(group[0])
    result = close_app("calculator")
    assert result.ok, result.message


def test_the_other_one_of_two_replaced_also_keeps_ownership(world):
    """B: the mirror case."""
    open_app("calculator")
    group = sorted(owned("calculator")[0])
    world.desktop.destroy(group[1])
    result = close_app("calculator")
    assert result.ok, result.message


def test_all_handles_replaced_loses_ownership(world):
    """C: FOUND: when no recorded handle remains, ownership is gone - the same failure mode as the
    single-window case. A group is only as durable as its most durable handle."""
    open_app("calculator")
    group = sorted(owned("calculator")[0])
    for handle in group:
        world.desktop.destroy(handle)
    world.desktop.add("Calculator", "ApplicationFrameWindow")
    result = close_app("calculator")
    assert not result.ok
    assert REFUSAL in result.message, result.message


def test_a_transient_handle_seen_first_and_then_gone(world):
    """D: the verifier returns on the FIRST acceptable poll, so a splash or frame that is replaced a
    moment later is what gets recorded. FOUND: if that transient handle is the only one recorded,
    ownership is lost as soon as it goes."""
    open_app("notepad")
    [transient] = [h for group in owned() for h in group]
    world.desktop.destroy(transient)
    settled = world.desktop.add("Untitled - Notepad", "Notepad")
    assert settled not in [h for group in owned() for h in group]
    result = close_app("notepad")
    assert not result.ok and REFUSAL in result.message


def test_a_window_hidden_and_reshown_under_a_new_handle_is_lost(world):
    """E: FOUND: same mechanism. Cloaking alone is survivable (the handle persists); a new handle is not."""
    open_app("notepad")
    [handle] = [h for group in owned() for h in group]
    world.desktop.update(handle, cloaked=True)
    assert close_app("notepad").ok, "a cloaked but still-present handle is still ours"

    open_app("notepad")
    [handle] = [h for group in owned() for h in group]
    replace(world, handle)
    assert not close_app("notepad").ok


# --- 6/7. A stranger alongside ------------------------------------------------------------------------

def test_a_stranger_and_a_stable_owned_window(world):
    """6: only the owned handle is asked to close."""
    open_app("notepad")
    stranger = world.desktop.add("Untitled - Notepad", "Notepad")   # after the open, since Slice 2
    ours = [h for group in owned() for h in group]
    assert close_app("notepad").ok
    closed = [handle for kind, handle in world.calls if kind == "close"]
    assert stranger not in closed and set(closed) <= set(ours)


def test_a_stranger_and_an_owned_window_whose_handle_changed(world):
    """7: THE SAFETY QUESTION. When our handle is lost, does the stranger become a target?

    FOUND: no. The refusal is returned and nothing is closed. Losing ownership degrades to refusing,
    never to closing something else - which is the behaviour that must survive any future fix."""
    open_app("notepad")
    stranger = world.desktop.add("Untitled - Notepad", "Notepad")   # after the open, since Slice 2
    [ours] = [h for group in owned() for h in group]
    newcomer = replace(world, ours)
    result = close_app("notepad")
    assert not result.ok and REFUSAL in result.message
    closed = [handle for kind, handle in world.calls if kind == "close"]
    assert closed == [], "nothing was closed - not the stranger, not the replacement"
    assert stranger in world.desktop.windows and newcomer in world.desktop.windows


# --- 8. Canonicalisation: the simpler explanation, ruled out -------------------------------------------

@pytest.mark.parametrize("opened, closed", [("notepad", "Notepad"), ("Notepad", "notepad"),
                                            ("NOTEPAD", "  notepad  "), ("  Notepad  ", "NotePad")])
def test_the_ownership_key_is_the_canonical_alias_whatever_the_target_looked_like(world, opened, closed):
    """The real log showed BOTH actions labelled 'Notepad' with a capital N, because that is what the
    Brain wrote. This proves the key is resolve()'s canonical value, so capitalisation and padding
    cannot be the cause - the open and the close agree on the alias every time."""
    assert executor_logic.execute(ExecutorAction(OPEN_APP, opened)).ok
    assert list(executor_logic._session_windows) == ["notepad"], "one canonical key"
    result = executor_logic.execute(ExecutorAction(CLOSE_APP, closed), confirm=lambda a, b: True)
    assert result.ok, result.message


def test_the_expectation_is_built_from_the_same_canonical_name(world):
    """Both preparers call verifier.expect_window(name) with resolve()'s value, so the title pattern is
    identical on both sides too."""
    import ast
    import inspect
    for function in (executor_logic._prepare_open_app, executor_logic._prepare_close_app):
        tree = ast.parse(inspect.getsource(function))
        calls = {ast.unparse(node) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        assert any(call == "verifier.expect_window(name)" for call in calls), calls
        assert "resolve(action)" in {ast.unparse(node) for node in ast.walk(tree)
                                     if isinstance(node, ast.Call)}


# --- 9/10. The safety invariants, unchanged -----------------------------------------------------------

def test_losing_ownership_never_widens_to_the_app_name(world):
    """Even with the stale group forgotten and exactly one matching window present, the app name alone
    buys nothing."""
    open_app("notepad")
    [handle] = [h for group in owned() for h in group]
    world.desktop.destroy(USERS_NOTEPAD.handle)
    replace(world, handle)
    for _ in range(3):
        assert not close_app("notepad").ok
    assert [call for call in world.calls if call[0] == "close"] == []


def test_a_new_session_still_refuses_an_old_window(world):
    """The process-scoped fact from the previous audit is unchanged and still correct - it is simply not
    what happened in the real session."""
    open_app("notepad")
    executor_logic.forget_session_windows()
    assert not close_app("notepad").ok


# --- 5. Verifier timing: one acceptable poll is enough ------------------------------------------------

def test_the_verifier_returns_on_the_first_acceptable_poll(world):
    """So the recorded group is whatever the topology looked like at that instant, and is never
    refreshed afterwards. FOUND by reading the code and asserted here: there is no settling period and
    no re-read of the group between the open and the close."""
    import ast
    import inspect

    from app.verifier import logic as verifier
    source = inspect.getsource(verifier.wait_for_new_window)
    assert "if shown:" in source and "return VerificationResult(True" in source
    # nothing re-reads the stored group after the open
    tree = ast.parse(inspect.getsource(executor_logic))
    writers = [ast.unparse(node) for node in ast.walk(tree)
               if isinstance(node, ast.Call) and "_remember_opened" in ast.unparse(node.func)]
    assert len(writers) == 1, writers


def test_nothing_refreshes_a_recorded_group_after_the_open(world):
    """Behavioural companion: a second window appearing later is NOT added to the owned group."""
    open_app("notepad")
    before = owned()
    world.desktop.add("Untitled - Notepad", "Notepad")
    world.desktop.list_windows()
    assert owned() == before, "the group recorded at open time is the whole of ownership, forever"
