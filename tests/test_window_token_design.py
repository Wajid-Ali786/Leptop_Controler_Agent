"""
The DESIGN MODEL for the per-window ownership token. This is not the safety regression.

Everything above the last two tests models the rule locally, in this file, and touches no production
code. It was the design work that preceded the implementation, and it is kept because it states the
reasoning compactly - but a model proves nothing about what ships.

THE SHIPPED MECHANISM IS PINNED ELSEWHERE:
  tests/test_window_ownership_token.py   the whole rule, against real production code
  tests/test_hwnd_reuse_safety.py        the confirmed attack, flipped from close to refuse

The rule that was modelled here and then built:

    at verified open   SetPropW(hwnd, KEY, token)  for each handle in the group
    at close           a handle counts as ours only if it is in the recorded group
                       AND GetPropW(hwnd, KEY) == that group's token

Why that could work, from Microsoft's own documentation (quoted in the report):
  * SetProp "Adds a new entry or changes an existing entry in the property list of the SPECIFIED WINDOW" -
    the list belongs to the window, not to the number.
  * When a window is destroyed with properties still attached, "the system will call RemoveProp on your
    behalf" (The Old New Thing, 2023-10-30). The list does not outlive the window object.
  * "SetProp is subject to the restrictions of User Interface Privilege Isolation (UIPI). A process can
    only call this function on a window belonging to a process of lesser or equal integrity level."

The model below is built around the ONE distinction the production store is missing: a window OBJECT is
not its HWND number. Objects own their property list; numbers can be handed to a new object.

Nothing real is created, tagged, read or closed. The two production assertions at the end read source
only.
"""
import ast
import inspect
import secrets
import textwrap
from dataclasses import dataclass, field

import pytest

KEY = "AIDesktopCompanion.WindowOwnership"      # §5 option A: one stable namespaced key
OUR_INTEGRITY = 2                                # medium, as a non-elevated companion process runs


# --- the model -----------------------------------------------------------------------------------------

@dataclass
class WindowObject:
    """A window OBJECT. `props` is part of the object, so it is created empty and dies with it."""
    handle: int
    title: str = "Untitled - Notepad"
    class_name: str = "Notepad"
    pid: int = 4321
    created: float = 1000.0
    integrity: int = OUR_INTEGRITY
    props: dict = field(default_factory=dict)


class Desktop:
    def __init__(self):
        self.by_handle: dict[int, WindowObject] = {}

    def create(self, handle: int, **kwargs) -> WindowObject:
        """A NEW object. It may reuse a number a destroyed object had; it never inherits its properties."""
        window = WindowObject(handle, **kwargs)
        self.by_handle[handle] = window
        return window

    def destroy(self, handle: int) -> None:
        """The object goes, and its property list with it - the system removes what is left attached."""
        self.by_handle.pop(handle, None)

    def set_prop(self, handle: int, key: str, value: int) -> bool:
        window = self.by_handle.get(handle)
        if window is None:
            return False
        if window.integrity > OUR_INTEGRITY:
            return False                      # UIPI: documented ERROR_ACCESS_DENIED (5)
        window.props[key] = value
        return True

    def get_prop(self, handle: int, key: str):
        window = self.by_handle.get(handle)
        return None if window is None else window.props.get(key)


@dataclass(frozen=True)
class OwnedGroup:
    handles: frozenset
    token: int


def new_token() -> int:
    """Pointer-width, non-zero, unpredictable, process-local, never persisted (§4)."""
    return secrets.randbits(62) | 1


def register(desktop: Desktop, handles, token: int) -> OwnedGroup | None:
    """§3/§6. Record ONLY the handles that were successfully tagged. If none could be tagged, there is no
    ownership record at all - the open still happened, but close_app will refuse it."""
    tagged = frozenset(h for h in handles if desktop.set_prop(h, KEY, token))
    return OwnedGroup(tagged, token) if tagged else None


def ours(desktop: Desktop, group: OwnedGroup | None) -> frozenset:
    """The close targets. Both clauses are required: recorded number AND matching token on the object."""
    if group is None:
        return frozenset()
    return frozenset(h for h in group.handles if desktop.get_prop(h, KEY) == group.token)


def would_close(desktop: Desktop, group: OwnedGroup | None) -> frozenset:
    """What WM_CLOSE would reach. Refusing is the empty set."""
    return ours(desktop, group)


@pytest.fixture
def desktop():
    return Desktop()


def opened(desktop, *handles, **kwargs):
    """An open the verifier confirmed, then registered."""
    for handle in handles:
        desktop.create(handle, **kwargs)
    return register(desktop, frozenset(handles), new_token())


# --- 14.1 the baseline still works ---------------------------------------------------------------------

def test_a_stable_tagged_window_still_closes(desktop):
    group = opened(desktop, 1001)
    assert would_close(desktop, group) == {1001}


# --- 14.2 THE CONFIRMED DEFECT, under the token rule --------------------------------------------------

def test_a_number_reused_by_a_stranger_is_refused(desktop):
    """The exact chronology of §7, and the one case that must flip from close to refuse.

    Today: ownership holds {1001}, the stranger holds 1001, production closes it.
    With the token: the stranger is a NEW object, so its property list is empty."""
    group = opened(desktop, 1001)
    desktop.destroy(1001)
    desktop.create(1001)                                  # a stranger receives the same number
    assert desktop.get_prop(1001, KEY) is None, "a new object starts with an empty property list"
    assert would_close(desktop, group) == frozenset(), "refused: no WM_CLOSE"


# --- 14.3 SAME-process reuse: the case process identity could NOT fix ---------------------------------

def test_same_process_same_title_same_class_reuse_is_refused(desktop):
    """§8. Same pid, same creation time, same executable, same title, same class, same number - and a
    different window object. (pid, creation_time) returns "owned" here; the token does not, because the
    property belonged to the object that died.

    This is the whole reason the token is stronger than process identity."""
    group = opened(desktop, 1001)
    before = desktop.by_handle[1001]
    desktop.destroy(1001)
    after = desktop.create(1001, title=before.title, class_name=before.class_name,
                           pid=before.pid, created=before.created)
    assert (after.pid, after.created, after.title, after.class_name, after.handle) == (
        before.pid, before.created, before.title, before.class_name, before.handle), "indistinguishable"
    assert would_close(desktop, group) == frozenset()


# --- 14.4 different-process reuse ---------------------------------------------------------------------

def test_a_number_reused_by_another_process_is_refused(desktop):
    group = opened(desktop, 1001)
    desktop.destroy(1001)
    desktop.create(1001, pid=9999, created=5000.0)
    assert would_close(desktop, group) == frozenset()


# --- 14.5 the user closes it by hand, then the number comes back -------------------------------------

def test_manual_destruction_then_reuse_is_refused(desktop):
    """The real session's shape: nobody polls, so the record stays armed for as long as the gap lasts -
    fifteen minutes, in the transcript. The token makes the length of the gap irrelevant."""
    group = opened(desktop, 1001)
    desktop.destroy(1001)                                  # the user closed it
    for _ in range(10):                                    # time passes; nothing looks
        pass
    desktop.create(1001)
    assert would_close(desktop, group) == frozenset()


# --- 14.6 the tag is removed or overwritten -----------------------------------------------------------

def test_a_removed_tag_is_refused(desktop):
    group = opened(desktop, 1001)
    del desktop.by_handle[1001].props[KEY]
    assert would_close(desktop, group) == frozenset()


def test_a_tag_overwritten_by_another_companion_process_is_refused(desktop):
    """§5, the one collision a single stable key allows: a second companion process tags the same window.
    It can only do so for a window it opened, and the mismatch fails closed either way - which is why the
    simpler stable key is not less safe than a process-unique one."""
    group = opened(desktop, 1001)
    desktop.set_prop(1001, KEY, new_token())               # the other process's token
    assert would_close(desktop, group) == frozenset()


# --- 14.8 a wrong token ------------------------------------------------------------------------------

def test_a_different_groups_token_does_not_validate_this_group(desktop):
    """Each verified open gets its own token, so group A's tag can never speak for group B."""
    first = opened(desktop, 1001)
    second = opened(desktop, 1002)
    assert first.token != second.token
    assert would_close(desktop, OwnedGroup(frozenset({1002}), first.token)) == frozenset()


# --- 14.9 a correct token on a handle that is not in the group ----------------------------------------

def test_a_correctly_tagged_window_outside_the_recorded_group_is_not_a_target(desktop):
    """Both clauses are required. Even a window carrying our exact token is not closed unless its number
    was recorded - so the token can never widen the target set beyond the group."""
    group = opened(desktop, 1001)
    desktop.create(2002)
    desktop.set_prop(2002, KEY, group.token)
    assert would_close(desktop, group) == {1001}, "2002 is tagged but was never recorded"


# --- 14.10/14.11 groups: partial tagging, partial survival --------------------------------------------

def test_a_group_closes_the_members_that_still_carry_the_token(desktop):
    """§9 A then E: a frame and its content window are both tagged; one is replaced. The survivor is
    still ours and is still closed; the replacement is not a target."""
    group = opened(desktop, 1001, 1002)
    assert group.handles == {1001, 1002}
    desktop.destroy(1002)
    desktop.create(1002)                                   # a new object with that number
    assert would_close(desktop, group) == {1001}


def test_an_untagged_sibling_never_becomes_a_target_because_a_sibling_is_tagged(desktop):
    """§9 B and §10, THE MANDATORY ONE. Tagging 1002 fails (say it is elevated). It must not be closed
    merely because 1001 beside it is tagged - so only successfully tagged handles are ever recorded."""
    desktop.create(1001)
    desktop.create(1002, integrity=OUR_INTEGRITY + 1)      # UIPI blocks the tag
    group = register(desktop, frozenset({1001, 1002}), new_token())
    assert group.handles == {1001}, "the untagged sibling is not in the record"
    assert would_close(desktop, group) == {1001}
    assert 1002 not in would_close(desktop, group)


def test_a_group_whose_tagged_members_have_all_gone_is_refused(desktop):
    """§9 D: this is the churn case, and it stays fail-safe."""
    group = opened(desktop, 1001, 1002)
    desktop.destroy(1001)
    desktop.destroy(1002)
    desktop.create(3001)                                   # the app recreated its window elsewhere
    assert would_close(desktop, group) == frozenset()


# --- 14.7 tagging fails outright ----------------------------------------------------------------------

def test_an_open_whose_window_cannot_be_tagged_is_not_registered_as_closable(desktop):
    """§6. The launch happened - we must not pretend otherwise - but nothing is recorded, so close_app
    refuses rather than falling back to the raw handle."""
    desktop.create(1001, integrity=OUR_INTEGRITY + 1)
    group = register(desktop, frozenset({1001}), new_token())
    assert group is None, "no ownership record at all"
    assert would_close(desktop, group) == frozenset()


def test_a_failed_tag_never_degrades_to_raw_handle_ownership(desktop):
    """The failure mode that would reintroduce the defect: recording the handle anyway."""
    desktop.create(1001, integrity=OUR_INTEGRITY + 1)
    assert register(desktop, frozenset({1001}), new_token()) is None
    assert would_close(desktop, None) == frozenset()


# --- 14.16/14.17 what must not confer ownership ------------------------------------------------------

def test_conversational_context_cannot_supply_a_token(desktop):
    """§14.16. The rule's only inputs are the recorded group and the property read back off the object.
    "The last thing I did was open notepad" has nowhere to enter."""
    source = inspect.getsource(ours) + inspect.getsource(register)
    for forbidden in ("previous_action", "TurnContext", "restated", "alias"):
        assert forbidden not in source
    assert would_close(desktop, None) == frozenset()


def test_a_new_process_owns_nothing_and_cannot_guess_a_token(desktop):
    """§14.17. Tokens are process-local and never persisted, so a later run holds no record - and a fresh
    token never matches one left on a surviving window."""
    group = opened(desktop, 1001)
    restarted = OwnedGroup(frozenset({1001}), new_token())   # a new process, same window still open
    assert would_close(desktop, restarted) == frozenset()
    assert would_close(desktop, group) == {1001}, "the original process is unaffected"


# --- 14.18 the token is not user-visible and not loggable -------------------------------------------

def test_the_token_is_unpredictable_non_zero_and_pointer_sized():
    """§4. Non-zero so it cannot be confused with GetProp's NULL "no such property"."""
    tokens = {new_token() for _ in range(2000)}
    assert len(tokens) == 2000, "no collisions in a large sample"
    assert all(token != 0 for token in tokens)
    assert all(0 < token < 2 ** 64 for token in tokens), "fits a HANDLE on 64-bit Windows"
    assert all(token % 2 == 1 for token in tokens), "never zero by construction"


def test_nothing_in_the_rule_puts_a_token_into_a_message():
    """The token is a sentinel, not information: no decision function formats or returns it."""
    for function in (register, ours, would_close):
        source = inspect.getsource(function)
        assert "log" not in source and "message" not in source and "f\"" not in source


# --- §10 against the REAL close path (source only, nothing executed) --------------------------------

def _function_tree(function):
    return ast.parse(textwrap.dedent(inspect.getsource(function)))


def test_production_only_ever_asks_token_verified_windows_to_close():
    """Group expansion, checked against app/executor/logic.py rather than assumed.

    _close_session_group computes two sets: `still_open`, and `relevant`, which ADDS
    verifier.hosted_windows(). If `relevant` were the close target, tagging the group would not bound
    what receives WM_CLOSE and a hosted sibling could be closed unowned.

    What this pins now: `still_open` comes from _ours_now (the token check), `_request_close` is called
    with it, and `relevant` is used only to decide what must be VERIFIED gone - and is built from `ours`,
    the handles that passed the token check, never from the whole recorded group. Waiting on a handle
    number that now belongs to someone else would never finish."""
    from app.executor import logic as executor_logic
    tree = _function_tree(executor_logic._close_session_group)

    requested = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and ast.unparse(node.func) == "_request_close"]
    assert len(requested) == 1, [ast.unparse(node) for node in requested]
    assert [ast.unparse(argument) for argument in requested[0].args] == ["name", "still_open"]

    assigned = {ast.unparse(node.targets[0]): ast.unparse(node.value)
                for node in ast.walk(tree) if isinstance(node, ast.Assign) and len(node.targets) == 1}
    assert assigned["still_open"] == "_ours_now(group, expectation)", assigned["still_open"]
    assert assigned["ours"] == "frozenset((window.handle for window in still_open))", assigned["ours"]
    assert assigned["relevant"] == "ours | verifier.hosted_windows(expectation, ours)", assigned["relevant"]

    users_of_relevant = [ast.unparse(node.func) for node in ast.walk(tree)
                         if isinstance(node, ast.Call)
                         and "relevant" in [ast.unparse(argument) for argument in node.args]]
    assert users_of_relevant == ["verifier.wait_for_windows_to_close"], users_of_relevant


def test_every_production_entry_to_the_close_mechanism_is_token_backed():
    """There are three doors to _close_session_group - close_app, window_control's close, and the group
    lookup they share. Each must consult the token; one that trusted a handle number alone would be a
    bypass, which is the shape of the original defect."""
    import inspect
    from app.executor import logic as executor_logic
    for function in (executor_logic._open_session_group, executor_logic._close_session_group,
                     executor_logic._session_group_containing):
        source = inspect.getsource(function)
        assert "_ours_now" in source or "window_token" in source, function.__name__


def test_production_registers_ownership_from_exactly_one_place():
    """So the token can be attached at one seam rather than audited across several."""
    from app.executor import logic as executor_logic
    tree = ast.parse(inspect.getsource(executor_logic))
    sites = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("_remember_opened")]
    assert len(sites) == 1, [ast.unparse(node) for node in sites]
