"""HWND REUSE: the confirmed false positive, and the proof that it is closed.

THE DEFECT THIS FILE RECORDS. Ownership used to be a set of integers. A numeric window handle is not a
permanent name for a window, so when the owned window was destroyed and a DIFFERENT window later
received the same number, the stored set matched it and the close path closed it - a window the
assistant never opened. That is the one outcome the ownership rule exists to prevent, and it failed
UNSAFE, unlike the churn refusal it was found next to.

THE FIX these tests now pin. A verified open attaches an unpredictable ownership token to each window as
a Windows window property, and a window counts as ours only if its handle is one we recorded AND it
still carries that group's token. A property belongs to the window OBJECT and is removed when the object
is destroyed, so a new window that inherits the number never inherits the token.

Every test above the "process identity" divider drives REAL production code - the real
_prepare_close_app, _open_session_group and _close_session_group - against the fake desktop. The
historical reproductions are kept, with the expectation flipped from what they found to what is now
required.

Below the divider, `Identity` is a pure MODEL of a (pid, creation_time) pair, kept because it records
why process identity was not sufficient and the token is. Those tests touch no production code.
"""
from dataclasses import dataclass

import pytest

from app.executor import logic as executor_logic
from tests.test_executor_close_app import USERS_NOTEPAD, FakeDesktop, close_app, open_app, world  # noqa: F401

REFUSAL = "I only close windows I opened in this session"


def owned_handles(name="notepad"):
    return [handle for group in executor_logic._session_windows.get(name, []) for handle in group.handles]


def closed_handles(world):
    return [handle for kind, handle in world.calls if kind == "close"]


# --- 1/2. The attack, against production code ---------------------------------------------------------

def test_a_reused_handle_is_refused_and_the_stranger_is_left_alone(world):
    """THE SAFETY REGRESSION. Formerly
    test_a_reused_handle_is_treated_as_owned_and_the_stranger_is_closed, which FOUND that production
    closed the stranger.

        t0 agent opens its Notepad       -> ownership stores handle 1001 + a token attached to it
        t1 that window is destroyed      -> the property goes with the object; nothing polls
        t2 a different window appears and receives handle 1001
        t3 close notepad                 -> REFUSED, and nothing is closed

    The number still matches. The token cannot, because the window that carried it no longer exists."""
    open_app("notepad")
    [handle] = owned_handles()
    assert executor_logic._session_windows["notepad"][0].handles == frozenset({handle})

    stranger = world.desktop.reuse(handle)

    result = close_app("notepad")
    assert not result.ok, result.message
    assert REFUSAL in result.message, result.message
    assert closed_handles(world) == [], "not one close request was sent"
    assert stranger in world.desktop.windows, "the window we never opened is still open"


def test_the_stale_record_is_harmless_however_long_it_survives(world):
    """Formerly test_the_exposure_window_is_from_destruction_until_the_next_lookup, which recorded that a
    group is only forgotten when a lookup happens to find it gone - so the stored number stayed armed for
    as long as the gap lasted (fifteen minutes, in the real session).

    The record still survives that long; it is no longer dangerous. The token is what is checked, and it
    died with the window, so the length of the gap stopped mattering."""
    open_app("notepad")
    [handle] = owned_handles()
    world.desktop.destroy(handle)
    assert owned_handles() == [handle], "the record is still there: no lookup has happened"

    stranger = world.desktop.reuse(handle)       # the number comes back before anything looks
    assert not close_app("notepad").ok
    assert closed_handles(world) == []
    assert stranger in world.desktop.windows


def test_an_intervening_close_attempt_still_forgets_the_stale_group(world):
    """This used to be the only thing protecting us - luck about ordering. It still behaves correctly,
    and is no longer what the safety depends on."""
    open_app("notepad")
    [handle] = owned_handles()
    world.desktop.destroy(handle)
    close_app("notepad")                      # the lookup that forgets it
    assert owned_handles() == []
    stranger = world.desktop.reuse(handle)
    result = close_app("notepad")
    assert not result.ok and REFUSAL in result.message
    assert stranger not in closed_handles(world)


def test_a_stable_window_still_closes_normally(world):
    """The fix must not cost the feature: while the window lives it carries its token and closes."""
    open_app("notepad")
    [handle] = owned_handles()
    result = close_app("notepad")
    assert result.ok, result.message
    assert closed_handles(world) == [handle]
    assert owned_handles() == []


def test_exact_hwnd_is_no_longer_trusted_once_the_window_object_is_gone(world):
    """Formerly test_exact_hwnd_is_safe_only_while_the_original_window_object_still_exists, where BOTH
    halves passed and that was the point. The first half is unchanged - while the window lives, the
    number denotes it. The second half has flipped: after destruction the number is just an integer, and
    production now says so."""
    open_app("notepad")
    assert close_app("notepad").ok, "while it lives, the window closes"

    open_app("notepad")
    [handle] = owned_handles()
    world.desktop.reuse(handle)
    assert not close_app("notepad").ok, "after destruction, the same number is refused"


def test_a_reused_handle_no_longer_singles_out_a_victim(world):
    """Formerly test_a_reused_handle_is_preferred_over_an_untouched_stranger, which found the defect hit
    exactly the window holding the recycled number. Now neither window is touched."""
    bystander = world.desktop.add("Untitled - Notepad", "Notepad")
    open_app("notepad")
    [handle] = owned_handles()
    victim = world.desktop.reuse(handle)
    close_app("notepad")
    closed = closed_handles(world)
    assert victim not in closed and bystander not in closed
    assert closed == []


def test_the_number_staying_while_the_process_changes_is_refused_too(world):
    """Formerly test_the_number_staying_while_the_process_changes_is_exactly_the_detectable_case - the
    sub-case a (pid, creation_time) check would have caught. It is refused, by the same one mechanism
    rather than by a second one."""
    open_app("notepad")
    [handle] = owned_handles()
    world.desktop.reuse(handle)
    assert not close_app("notepad").ok
    assert closed_handles(world) == []


# --- 5. A raw-handle baseline is not a historical identity either --------------------------------------

def test_a_baseline_of_raw_handles_cannot_prove_a_window_is_the_same_pre_existing_one(world):
    """A baseline recorded as {500} means "500 was in use before we launched". After 500 is destroyed and
    reissued, membership no longer means "this is that same pre-existing window".

    Kept because it is why no baseline-of-handles scheme was built: it can falsely EXCLUDE a legitimate
    replacement that receives a recycled baseline number, and falsely INCLUDE a stranger as
    pre-existing. Ownership asks the window itself instead."""
    baseline = frozenset({USERS_NOTEPAD.handle})
    world.desktop.reuse(USERS_NOTEPAD.handle)              # a different window now holds that number
    assert USERS_NOTEPAD.handle in baseline, "the baseline cannot tell; it only holds an integer"


# --- 3/4. MODEL ONLY, below here: what a strong process identity would and would not have fixed -------

@dataclass(frozen=True)
class Identity:
    pid: int
    created: float


@dataclass(frozen=True)
class Seen:
    handle: int
    identity: Identity


def decide(stored_handle: int, stored_identity, desktop, *, corroborate: bool) -> str:
    """A model of the rejected alternative: numeric match, optionally corroborated by process identity.
    Production does not work this way and never did - this exists to show why."""
    for window in desktop:
        if window.handle != stored_handle:
            continue
        if corroborate and window.identity != stored_identity:
            return "refuse"
        return "owned"
    return "refuse"


OURS = Identity(pid=4321, created=1000.0)


def test_identity_corroboration_would_have_fixed_different_process_reuse():
    """MODEL. Handle 1001 reissued to a window in ANOTHER process: strong identity refuses. So the
    cross-process half of the hole was closable that way."""
    other = [Seen(1001, Identity(pid=5555, created=2000.0))]
    assert decide(1001, OURS, other, corroborate=False) == "owned", "the old behaviour"
    assert decide(1001, OURS, other, corroborate=True) == "refuse"


def test_identity_corroboration_would_NOT_have_fixed_same_process_reuse():
    """MODEL, and the decisive limitation - the reason the token was built instead. A tabbed or
    multi-window host reissues the handle to a different logical window INSIDE the same process: same
    pid, same creation time, same executable, same class, same title pattern.

    Process identity cannot tell them apart, because the process really is the same one. The real
    same-process case is pinned against production in
    tests/test_window_ownership_token.py."""
    same_process = [Seen(1001, OURS)]
    assert decide(1001, OURS, same_process, corroborate=False) == "owned"
    assert decide(1001, OURS, same_process, corroborate=True) == "owned", (
        "found: identity corroboration does not help here")


def test_identity_proves_a_process_not_a_window():
    """MODEL. (pid, creation_time) answers "is this the same process?" and never "is this the same
    window?". Two different windows in one process are indistinguishable by it."""
    first, second = Seen(1001, OURS), Seen(1002, OURS)
    assert first.identity == second.identity and first.handle != second.handle


def test_endpoint_snapshots_cannot_separate_a_replacement_from_a_stranger():
    """MODEL. We recorded handle 1001. At close we see handle 1001 again, same process, same class,
    matching title.

    World 1: the host destroyed and recreated OUR window, reusing the number.
    World 2: our window died and an unrelated one received the number.

    The snapshots are byte-identical, with or without process identity - which is why the token is
    attached at open time rather than inferred at close time."""
    world_one = [Seen(1001, OURS)]
    world_two = [Seen(1001, OURS)]
    assert world_one == world_two
    assert decide(1001, OURS, world_one, corroborate=True) == decide(1001, OURS, world_two,
                                                                    corroborate=True)


# --- Conversational context stays irrelevant ----------------------------------------------------------

def test_previous_action_context_is_still_not_consulted_anywhere_in_this(world):
    import ast
    import inspect
    for function in (executor_logic._prepare_close_app, executor_logic._open_session_group,
                     executor_logic._owned_by_session, executor_logic._close_session_group,
                     executor_logic._ours_now):
        source = inspect.getsource(function)
        assert "previous_action" not in source and "TurnContext" not in source
        ast.parse(source)
