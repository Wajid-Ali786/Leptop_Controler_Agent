"""The per-window ownership token, against PRODUCTION code.

Every test in this file drives the real app/executor/logic.py - the real _remember_opened, _ours_now,
_open_session_group, _close_session_group and _session_group_containing - against the fake desktop from
tests/test_executor_close_app.py. The adapter section at the bottom drives the real
app/executor/adapter.py with ctypes.WinDLL replaced, so the exact Win32 prototypes and the read-back are
exercised rather than described.

Nothing here is a model of the rule. The model lives in tests/test_window_token_design.py, which was the
design work; a safety mechanism has to be pinned on the code that ships.

WHAT THE TOKEN IS FOR. A window handle number is not an identity: Windows gives handle numbers to new
windows, so a window the assistant never opened could be closed as one it did (reproduced, and now
refused, in tests/test_hwnd_reuse_safety.py). A Windows window property belongs to the window OBJECT and
is removed when that object is destroyed, so it can carry "the assistant opened this" in a way a number
cannot.
"""
import ctypes
import logging
from types import SimpleNamespace

import pytest

from app.executor import adapter, emergency_stop, logic
from app.executor.emergency_stop import EmergencyStopError
from app.executor.logic import execute
from app.executor.models import OPEN_APP, ExecutorAction, Outcome
from app.safety.logic import ActionDeniedError
from tests.fake_window_props import TOKEN_KEY
from tests.test_executor_close_app import close_app, open_app, world  # noqa: F401


def no(*args):
    return False

REFUSAL = "I only close windows I opened in this session"


def groups(name="notepad"):
    return list(logic._session_windows.get(name, []))


def owned_handles(name="notepad"):
    return sorted(handle for group in groups(name) for handle in group.handles)


def closed(world):
    return [handle for kind, handle in world.calls if kind == "close"]


# --- 1. The feature still works -----------------------------------------------------------------------

def test_a_tagged_window_closes(world):
    """Item 1. The open attaches a token; the close finds it and goes ahead."""
    open_app("notepad")
    [group] = groups()
    [handle] = sorted(group.handles)
    assert world.desktop.props[handle][TOKEN_KEY] == group.token, "the window itself carries the token"

    result = close_app("notepad")
    assert result.ok, result.message
    assert closed(world) == [handle]
    assert groups() == [], "a verified close forgets the group"


def test_the_open_message_is_unchanged_when_ownership_is_registered(world):
    """The normal path must read exactly as it did before the token existed."""
    result = execute(ExecutorAction(OPEN_APP, "notepad"))
    assert result.message == "Opened notepad; its window appeared after 0.0s.", result.message
    assert "close" not in result.message, "no caveat when ownership was registered"


# --- 3/4. Reuse by a window that is indistinguishable in every observable way --------------------------

def test_same_title_same_class_reuse_is_refused(world):
    """Items 3 and 4. The stranger is given the SAME handle number, the SAME title and the SAME window
    class, so nothing the Verifier can read distinguishes it from the window we opened. In a real
    tabbed host it would also share the pid, the process creation time and the executable.

    It is refused because the property list it would have had to inherit belonged to the object that
    died."""
    open_app("notepad")
    [handle] = owned_handles()
    before = world.desktop.windows[handle]
    stranger = world.desktop.reuse(handle, title=before.title, class_name=before.class_name)
    assert world.desktop.windows[stranger].title == before.title
    assert world.desktop.windows[stranger].class_name == before.class_name

    result = close_app("notepad")
    assert not result.ok and REFUSAL in result.message
    assert closed(world) == []


def test_the_ownership_test_never_consults_process_identity(world):
    """Items 3 and 4, from the other side: no PID fallback was added, so the refusal above cannot be
    coming from a process check and cannot be defeated by matching one."""
    import ast
    import inspect
    source = inspect.getsource(logic._ours_now)
    tree = ast.parse(source)
    calls = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    assert calls == {"verifier.find_open", "adapter.window_token"}, calls
    for forbidden in ("pid", "process_image_name", "creation", "image"):
        assert forbidden not in source, f"_ours_now consults {forbidden}"


# --- 6/7/8. The token itself: missing, wrong, overwritten ---------------------------------------------

def test_a_window_whose_token_was_dropped_is_refused(world):
    """Item 6. The window is alive, its handle is recorded, and the property is gone - which is what a
    replacement window looks like. Refuse."""
    open_app("notepad")
    [handle] = owned_handles()
    del world.desktop.props[handle][TOKEN_KEY]
    result = close_app("notepad")
    assert not result.ok and REFUSAL in result.message
    assert closed(world) == []


def test_a_window_carrying_a_different_token_is_refused(world):
    """Item 7."""
    open_app("notepad")
    [handle] = owned_handles()
    world.desktop.props[handle][TOKEN_KEY] = adapter.new_window_token()
    assert not close_app("notepad").ok
    assert closed(world) == []


def test_a_token_overwritten_by_another_session_is_refused(world):
    """Item 8. A second companion process can only tag windows it opened itself, so this needs handle
    reuse across two processes - and it still fails closed, which is why one fixed property key is no
    less safe than a per-process one."""
    open_app("notepad")
    [handle] = owned_handles()
    [group] = groups()
    world.desktop.props[handle][TOKEN_KEY] = group.token + 2   # another process's token, same key
    assert not close_app("notepad").ok
    assert closed(world) == []


def test_our_token_on_a_window_we_never_recorded_is_not_a_target(world):
    """Item 9. Both halves of the predicate are required: a window carrying our exact token is still not
    closed unless its handle is one we recorded. The token can never WIDEN the target set."""
    open_app("notepad")
    [handle] = owned_handles()
    [group] = groups()
    stranger = world.desktop.add("Untitled - Notepad", "Notepad")
    world.desktop.props.setdefault(stranger, {})[TOKEN_KEY] = group.token

    assert close_app("notepad").ok
    assert closed(world) == [handle], "only the recorded handle"
    assert stranger in world.desktop.windows


# --- 10. Tagging fails -------------------------------------------------------------------------------

def test_an_untaggable_window_is_opened_but_never_becomes_closable(world):
    """Item 10, and the §11 behaviour. SetPropW fails - the documented UIPI case is a window belonging to
    a process of higher integrity level. The launch really happened, so it is NOT reported as a failure;
    but no ownership is recorded, and a raw handle is never kept as a substitute."""
    world.desktop.refuse_tags.add(1001)        # the handle the fake's next notepad will get
    result = execute(ExecutorAction(OPEN_APP, "notepad"))

    assert result.ok, "the window did open; saying otherwise would be a lie"
    assert result.message == ("Opened notepad; its window appeared after 0.0s, "
                              "but I won't be able to close it automatically."), result.message
    assert groups() == [], "no ownership group at all"

    refused = close_app("notepad")
    assert not refused.ok and REFUSAL in refused.message
    assert closed(world) == []


def test_a_failed_tag_is_never_recorded_on_its_number_alone(world):
    """The regression that would reintroduce the whole defect: keeping the handle anyway."""
    world.desktop.refuse_tags.add(1001)
    execute(ExecutorAction(OPEN_APP, "notepad"))
    assert logic._session_windows.get("notepad", []) == []
    assert owned_handles() == []


# --- 13/14/15. Groups of more than one window ---------------------------------------------------------

def test_a_group_survives_on_one_tagged_window_and_closes_only_that_one(world):
    """Items 13 and 15 together. Calculator opens a frame AND a content window, both tagged. One is
    replaced by a different object holding the same number: the group is still ours through the survivor,
    and the replacement is NOT a close target."""
    open_app("calculator")
    [group] = groups("calculator")
    frame, content = sorted(group.handles)
    assert len(group.handles) == 2

    world.desktop.reuse(content, title="Calculator", class_name="Windows.UI.Core.CoreWindow")
    result = close_app("calculator")
    assert result.ok, result.message
    assert closed(world) == [frame], "only the window that still carried the token"


def test_an_untagged_sibling_is_never_closed_because_a_sibling_is_tagged(world):
    """Item 14, THE one that matters for group expansion. Paint opens two same-titled windows; tagging
    the second fails. The first is ours, the second never enters the group, and proving the first does
    not drag the second in."""
    world.desktop.refuse_tags.add(1002)
    open_app("paint")
    [group] = groups("paint")
    assert group.handles == frozenset({1001}), group.handles

    result = close_app("paint")
    assert result.ok, result.message
    assert closed(world) == [1001]
    assert 1002 in world.desktop.windows, "the window we could not prove ours is still open"


def test_a_group_with_no_tagged_window_left_is_refused_and_forgotten(world):
    """Item 15. This is the churn case, and it stays fail-safe: a replacement under a new handle has no
    token, so the automatic close refuses rather than guessing."""
    open_app("notepad")
    [handle] = owned_handles()
    world.desktop.destroy(handle)
    world.desktop.add("Untitled - Notepad", "Notepad")    # the app reopened under a new handle

    result = close_app("notepad")
    assert not result.ok and REFUSAL in result.message
    assert closed(world) == []
    assert groups() == [], "the stale group is forgotten"


# --- 16/17/18/19. Lifecycle --------------------------------------------------------------------------

def test_a_denied_confirmation_keeps_the_window_and_its_token(world):
    """Item 16. Ownership is not permission, and a refusal to confirm must not cost us the window."""
    open_app("notepad")
    [group] = groups()
    [handle] = sorted(group.handles)
    with pytest.raises(ActionDeniedError):
        close_app("notepad", confirm=no)
    assert groups() == [group], "still ours, token and all"
    assert world.desktop.props[handle][TOKEN_KEY] == group.token
    assert closed(world) == []


def test_a_close_that_does_not_verify_keeps_ownership(world):
    """Item 17. The app ignored WM_CLOSE and the window is still there, so it is still ours to try
    again."""
    open_app("notepad")
    [group] = groups()
    world.desktop.reaction = "ignore"
    result = close_app("notepad")
    assert not result.ok and result.outcome is Outcome.STILL_OPEN
    assert groups() == [group]
    assert world.desktop.props[sorted(group.handles)[0]][TOKEN_KEY] == group.token


def test_a_verified_close_removes_the_local_record(world):
    """Item 18. Windows removes the property when it destroys the window, so only our own record needs
    clearing."""
    open_app("notepad")
    [handle] = owned_handles()
    assert close_app("notepad").ok
    assert groups() == []
    assert handle not in world.desktop.windows
    assert adapter.window_token(handle) is None, "no property survives the window"


def test_an_emergency_stop_before_the_close_keeps_ownership(world):
    """Item 19. The stop happens at the checkpoint before the close request, so nothing was sent and the
    window is still legitimately ours."""
    open_app("notepad")
    [group] = groups()
    emergency_stop.trigger("token-test")
    try:
        with pytest.raises(EmergencyStopError):
            close_app("notepad")
    finally:
        emergency_stop.reset("token-test")
    assert groups() == [group]
    assert closed(world) == []


# --- 20/21. What must not confer ownership -----------------------------------------------------------

def test_a_recorded_handle_alone_cannot_authorize_a_close(world):
    """Item 20 at its root. A group is forged with the right handle and a wrong token - which is exactly
    what any "I opened notepad a moment ago" reasoning would amount to - and the close still refuses."""
    open_app("notepad")
    [group] = groups()
    forged = logic._OwnedWindowGroup(group.handles, group.token + 1)
    logic._session_windows["notepad"] = [forged]

    result = close_app("notepad")
    assert not result.ok and REFUSAL in result.message
    assert closed(world) == []


def test_a_new_session_owns_nothing_and_its_tokens_are_unrelated(world):
    """Item 21. Tokens are process-local and never persisted, so a later run holds no record - and a
    fresh token never matches one left on a window that is still open."""
    open_app("notepad")
    [handle] = owned_handles()
    logic.forget_session_windows()                     # stands in for a new process
    assert groups() == []
    assert not close_app("notepad").ok
    assert adapter.new_window_token() != world.desktop.props[handle][TOKEN_KEY]


def test_a_stranger_on_our_old_handle_is_never_reported_as_already_closed(world):
    """The group lookup must apply the token too, not just the close that follows it.

    If the lookup accepted the group on numeric membership alone, the close would then find nothing of
    its own and answer "notepad is already closed" - ok=True, no WM_CLOSE, and a false statement, because
    a Notepad window IS open and it is the user's. Found by mutation testing."""
    open_app("notepad")
    [handle] = owned_handles()
    world.desktop.reuse(handle)

    result = close_app("notepad")
    assert not result.ok, result.message
    assert result.outcome is not Outcome.ALREADY_CLOSED, result.message
    assert "already closed" not in result.message, result.message
    assert REFUSAL in result.message
    assert closed(world) == []


def test_window_control_refuses_an_active_window_that_lost_its_token(world):
    """The active window must itself be ours. Paint opens two windows, both tagged; one is replaced by a
    stranger holding the same number while the other survives.

    Without the handle-level token check the lookup would accept the group - because a sibling still
    carries the token - and window_control would close that sibling instead of the window the user is
    actually looking at. Found by mutation testing."""
    open_app("paint")
    [group] = groups("paint")
    survivor, replaced = sorted(group.handles)
    world.desktop.reuse(replaced, title="Untitled - Paint", class_name="MSPaintView")

    assert logic._session_group_containing(survivor) is not None, "the survivor is still ours"
    assert logic._session_group_containing(replaced) is None, "the active window is not ours"


def test_the_window_control_close_also_requires_the_token(world, monkeypatch):
    """The third door to the same close mechanism. window_control closes the ACTIVE window, resolving it
    with _session_group_containing - so that path must be token-backed too, or it is a bypass."""
    from app.verifier import adapter as verifier_adapter
    open_app("notepad")
    [handle] = owned_handles()
    assert logic._session_group_containing(handle) is not None, "ours while it carries the token"

    world.desktop.reuse(handle)                        # a stranger now holds that number
    monkeypatch.setattr(verifier_adapter, "list_windows", world.desktop.list_windows)
    assert logic._session_group_containing(handle) is None, "not ours any more"


# --- 22. The token never leaves the process ----------------------------------------------------------

def test_the_token_is_not_in_the_groups_repr(world):
    open_app("notepad")
    [group] = groups()
    assert str(group.token) not in repr(group), repr(group)
    assert "token" not in repr(group)
    assert str(sorted(group.handles)[0]) in repr(group), "handles are fine to show"


def test_no_token_reaches_a_message_or_a_log_line(world, caplog):
    """A full open-close cycle with logging captured at DEBUG."""
    with caplog.at_level(logging.DEBUG):
        opened = open_app("notepad")
        [group] = groups()
        token = group.token
        world.desktop.props[sorted(group.handles)[0]][TOKEN_KEY] = token  # unchanged; just read it here
        shut = close_app("notepad")
    written = "\n".join(record.getMessage() for record in caplog.records)
    for text in (opened.message, shut.message, written):
        assert str(token) not in text, text
        assert f"{token:#x}" not in text


def test_the_token_is_not_persisted_anywhere(world, tmp_path):
    """It lives in one module-level dict and nowhere else: no file, no config, no log handler."""
    open_app("notepad")
    [group] = groups()
    import json
    assert str(group.token) not in json.dumps(
        {app: [sorted(g.handles) for g in gs] for app, gs in logic._session_windows.items()})


# --- The adapter itself: exact prototypes, native width, read-back -----------------------------------

class FakeProp:
    """One user32 property function. ctypes assigns argtypes/restype onto it, so it must accept them."""

    def __init__(self, behaviour):
        self.behaviour, self.calls = behaviour, []
        self.argtypes = self.restype = None

    def __call__(self, *args):
        self.calls.append(args)
        return self.behaviour(*args)


@pytest.fixture
def fake_property_api(monkeypatch):
    """The real adapter functions, with ctypes.WinDLL replaced by an in-memory property list."""
    def install(*, set_result=1, get=None, remove=None, last_error=0):
        store = {}

        def set_prop(handle, key, value):
            if set_result:
                store[(handle, key)] = value
            return set_result

        user32 = SimpleNamespace(
            SetPropW=FakeProp(set_prop),
            GetPropW=FakeProp(get if get else (lambda handle, key: store.get((handle, key)))),
            RemovePropW=FakeProp(remove if remove else (lambda handle, key: store.pop((handle, key), None))))
        monkeypatch.setattr(adapter.sys, "platform", "win32")
        monkeypatch.setattr(adapter.ctypes, "WinDLL", lambda name, use_last_error=False: user32, raising=False)
        monkeypatch.setattr(adapter.ctypes, "get_last_error", lambda: last_error, raising=False)
        return SimpleNamespace(user32=user32, store=store)
    return install


def test_the_token_width_is_derived_from_this_process_not_hardcoded():
    """Item 12. The project targets 64-bit Windows but must not assume it: the width comes from
    ctypes.sizeof(c_void_p), so a 32-bit interpreter gets a token that still fits a HANDLE."""
    assert adapter._TOKEN_BITS == ctypes.sizeof(ctypes.c_void_p) * 8 - 2


def test_tokens_are_non_zero_unpredictable_and_fit_a_handle():
    """Non-zero matters: GetPropW returns NULL for "no such property", so a zero token would be
    indistinguishable from an untagged window."""
    tokens = {adapter.new_window_token() for _ in range(2000)}
    assert len(tokens) == 2000, "no collisions in a large sample"
    limit = 1 << (ctypes.sizeof(ctypes.c_void_p) * 8 - 1)
    assert all(0 < token < limit for token in tokens), "positive, and positive read as signed too"
    assert all(token % 2 for token in tokens), "odd, so never zero"


def test_the_adapter_declares_the_documented_prototypes(os_primitives_faked, fake_property_api):
    """SetPropW returns BOOL - not a handle - and the data is pointer-width HANDLE. Getting this wrong is
    how a token would silently truncate, so the declarations are asserted rather than trusted."""
    from ctypes import wintypes
    api = fake_property_api()
    adapter.tag_window(777, adapter.new_window_token())
    assert api.user32.SetPropW.argtypes == [wintypes.HWND, wintypes.LPCWSTR, wintypes.HANDLE]
    assert api.user32.SetPropW.restype is wintypes.BOOL
    assert api.user32.GetPropW.argtypes == [wintypes.HWND, wintypes.LPCWSTR]
    assert api.user32.GetPropW.restype is wintypes.HANDLE
    assert api.user32.RemovePropW.argtypes == [wintypes.HWND, wintypes.LPCWSTR]
    assert api.user32.RemovePropW.restype is wintypes.HANDLE
    assert wintypes.HANDLE is ctypes.c_void_p, "HANDLE must be pointer-width"


def test_a_full_width_token_round_trips_through_the_adapter(os_primitives_faked, fake_property_api):
    """Item 12. The largest token this process can generate survives set-then-get unchanged."""
    api = fake_property_api()
    token = (1 << adapter._TOKEN_BITS) | 1          # the top bit this width allows, and odd
    assert adapter.tag_window(777, token) is True
    assert adapter.window_token(777) == token
    assert list(api.store.values()) == [token]


def test_tag_window_reads_the_property_back_and_only_then_reports_success(os_primitives_faked,
                                                                          fake_property_api):
    """The read-back is what catches a prototype mistake or a truncating conversion."""
    api = fake_property_api()
    token = adapter.new_window_token()
    assert adapter.tag_window(555, token) is True
    assert [call[0] for call in api.user32.SetPropW.calls] == [555]
    assert api.user32.GetPropW.calls, "it did not just trust SetPropW"


def test_a_read_back_mismatch_fails_closed(os_primitives_faked, fake_property_api):
    """Item 11. SetPropW SUCCEEDS, but the value that comes back is different - a truncated handle, say.
    Ownership must not be registered."""
    fake_property_api(get=lambda handle, key: 12345)
    assert adapter.tag_window(555, adapter.new_window_token()) is False


def test_a_property_that_does_not_stick_fails_closed(os_primitives_faked, fake_property_api):
    """GetPropW returns NULL: the property did not persist, so the window is not ours."""
    fake_property_api(get=lambda handle, key: None)
    assert adapter.tag_window(555, adapter.new_window_token()) is False


@pytest.mark.parametrize("last_error", [5, 1400, 87])
def test_setprop_failure_returns_false_and_never_raises(os_primitives_faked, fake_property_api, last_error):
    """Error 5 is the documented UIPI case (a higher-integrity window). Every failure is handled the same
    way: False. It must not raise, because an open that worked must not be reported as a failure."""
    fake_property_api(set_result=0, last_error=last_error)
    assert adapter.tag_window(555, adapter.new_window_token()) is False


def test_window_token_is_none_for_an_untagged_window(os_primitives_faked, fake_property_api):
    fake_property_api()
    assert adapter.window_token(999) is None


def test_untag_removes_only_our_own_value(os_primitives_faked, fake_property_api):
    """Windows documents that an application "must not remove properties added by other applications or
    by the system itself", so the value is checked before anything is removed."""
    api = fake_property_api()
    token = adapter.new_window_token()
    adapter.tag_window(555, token)
    assert adapter.untag_window(555, token + 2) is False
    assert api.user32.RemovePropW.calls == [], "nothing was removed on a mismatch"
    assert adapter.untag_window(555, token) is True
    assert adapter.window_token(555) is None


def test_the_property_functions_are_inert_off_windows(os_primitives_faked, monkeypatch):
    """No exception leaks into the Executor from a non-Windows machine; ownership simply never happens."""
    monkeypatch.setattr(adapter.sys, "platform", "linux")
    assert adapter.tag_window(555, 1) is False
    assert adapter.window_token(555) is None
    assert adapter.untag_window(555, 1) is False


def test_the_property_key_is_an_internal_constant_with_no_user_data():
    """Ownership correctness must not depend on a user-editable config value."""
    from config import settings
    key = adapter._OWNERSHIP_PROPERTY
    assert isinstance(key, str) and key
    assert "AIDesktopCompanion" in key
    config = (settings.PROJECT_ROOT / "config" / "config.yaml").read_text(encoding="utf-8")
    assert key not in config, "the key is a code constant, not configuration"
