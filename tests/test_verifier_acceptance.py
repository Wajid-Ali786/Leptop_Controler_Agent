"""
Phase 1 acceptance: a deliberately-broken action is CAUGHT by the Verifier, not reported as success
(docs/step4 Section 4, Done-when: "the Verifier correctly detects at least one deliberately-broken
action ... instead of reporting false success").

The whole real path runs: the real Executor prepares and validates, the real safety gate authorizes,
app/executor/adapter.py really posts WM_SYSCOMMAND/SC_MAXIMIZE to a real window, and the real Verifier
reads the real desktop afterwards. Nothing is mocked, faked or monkey-patched, and no production code
is touched.

What is deliberately broken is the TARGET, not the assistant: the test creates its own ordinary
top-level window whose window procedure receives SC_MAXIMIZE and then silently drops it instead of
passing it to DefWindowProc. The window advertises a maximize box, is not a tool window, and keeps its
own message pump running, so it stays a perfectly valid target right up to the moment it ignores the
request. The window therefore never maximizes, and the Verifier has to notice.

    ACTION ACTUALLY ATTEMPTED -> EXPECTED EFFECT PREVENTED -> VERIFIER OBSERVES THE REAL DESKTOP
    -> failed, never a false done

Receipt is proved by the fixture itself, not by PostMessage returning success: the window procedure
counts every SC_MAXIMIZE it receives, records whether it swallowed it or passed it on, and notes
whether Windows considered the window maximized at that moment.

The same window then runs the CONTROL case with swallowing turned off: it receives SC_MAXIMIZE again,
hands it to DefWindowProc, really maximizes, and the same action reports done. That is what proves the
earlier failure came from the deliberate break and not from an untargetable fixture.

Opt-in, like every real-desktop test:

    $env:RUN_REAL_DESKTOP_TEST='1'; pytest tests/test_verifier_acceptance.py -m real_desktop -v -s

It creates exactly one window and one thread, both its own, and removes both in finally. It sends no
keystrokes, never moves the mouse, never touches the clipboard, files, the network, the browser or any
window it did not create, and needs no elevation.

The matching offline test - the same rule against a fake desktop - is
tests/test_executor_window_control.py::test_state_that_doesnt_change_is_a_failure_not_success, and
::test_window_controls_are_never_retried already pins the no-retry policy, so nothing is duplicated here.
"""
import ctypes
import threading
import time
from ctypes import wintypes

import pytest

from app.executor import emergency_stop
from app.executor.logic import execute_with_recovery
from app.executor.models import WINDOW_CONTROL, ExecutorAction, Outcome
from app.verifier import logic as verifier

WM_DESTROY, WM_CLOSE, WM_SYSCOMMAND = 0x0002, 0x0010, 0x0112
SC_MAXIMIZE, SC_MASK = 0xF030, 0xFFF0  # the command a title-bar maximize button sends
WS_OVERLAPPEDWINDOW = 0x00CF0000       # includes WS_MAXIMIZEBOX: the window really offers maximize
SW_SHOWNORMAL, SW_RESTORE = 1, 9
IDC_ARROW, COLOR_WINDOW = 32512, 5
CLASS_NAME = "AiCompanionVerifierAcceptanceFixture"
TITLE = "AI Desktop Companion - test fixture (safe to close)"
START_TIMEOUT, STOP_TIMEOUT, ACTIVATE_TIMEOUT, RECEIPT_TIMEOUT = 10.0, 5.0, 5.0, 5.0

WNDPROC = ctypes.WINFUNCTYPE(wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HICON), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


def _user32():
    """Handles are pointer-sized, so every one of these needs its argtypes set."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = wintypes.LPARAM
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                       wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsZoomed.argtypes = [wintypes.HWND]
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = ctypes.c_int  # -1 is a real failure, so this must NOT be BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
    user32.LoadCursorW.restype = wintypes.HICON
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SwitchToThisWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
    return user32


def _module_handle():
    """This process's HINSTANCE. The restype matters: the default c_int truncates it on 64-bit Windows,
    and RegisterClassW then reads a garbage pointer."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    return kernel32.GetModuleHandleW(None)


def _resource_id(value):
    """MAKEINTRESOURCE: a low-numbered id passed where Windows expects a string pointer."""
    return ctypes.cast(ctypes.c_void_p(value), wintypes.LPCWSTR)


class StubbornWindow:
    """A real top-level window the TEST owns, which deliberately ignores SC_MAXIMIZE.

    It is an ordinary window in every other way: WS_OVERLAPPEDWINDOW (so it advertises a maximize box),
    no WS_EX_TOOLWINDOW, and its own message pump, so Windows never reports it as hung. Its window
    procedure is the evidence: it counts the SC_MAXIMIZE messages it really receives, records whether
    it swallowed each one or handed it to DefWindowProc, and notes what IsZoomed said at that moment."""

    def __init__(self):
        self.handle = None
        self.swallow = True                    # read by the window procedure; the deliberate break
        self.received = 0                      # SC_MAXIMIZE messages the window procedure really saw
        self.swallowed = 0                     # ...of those, how many were dropped instead of handled
        self.passed_on = 0                     # ...and how many went to DefWindowProc
        self.zoomed_when_received = []         # IsZoomed at the moment each one arrived
        self._changed = threading.Condition()  # so waiting for receipt needs no sleep
        self._ready = threading.Event()
        self._error = None
        self._thread = None
        self._procedure = WNDPROC(self._window_procedure)  # kept alive: Windows calls this from C
        self._atom = None
        self._class = None
        self.closed_handle = None
        self._user32 = _user32()
        self._instance = _module_handle()

    # --- the window procedure (runs on the pump thread) ---
    def _window_procedure(self, handle, message, wparam, lparam):
        if message == WM_SYSCOMMAND and (wparam & SC_MASK) == SC_MAXIMIZE:
            with self._changed:
                self.received += 1
                self.zoomed_when_received.append(bool(self._user32.IsZoomed(handle)))
                swallow = self.swallow
                if swallow:
                    self.swallowed += 1
                else:
                    self.passed_on += 1
                self._changed.notify_all()
            if swallow:
                return 0  # the deliberate break: received, understood, and deliberately not acted on
        if message == WM_DESTROY:
            self._user32.PostQuitMessage(0)
            return 0
        return self._user32.DefWindowProcW(handle, message, wparam, lparam)

    # --- lifecycle ---
    def start(self, timeout=START_TIMEOUT):
        self._thread = threading.Thread(target=self._run, name="verifier-acceptance-fixture", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError(f"the fixture window wasn't created within {timeout:g}s")
        if self._error is not None:
            raise self._error
        return self.handle

    def _run(self):
        user32 = self._user32
        instance = self._instance
        try:
            user32.UnregisterClassW(CLASS_NAME, instance)  # in case an earlier run died mid-test
            self._class = WNDCLASSW(style=0, lpfnWndProc=self._procedure, cbClsExtra=0, cbWndExtra=0,
                                    hInstance=instance, hIcon=None,
                                    hCursor=user32.LoadCursorW(None, _resource_id(IDC_ARROW)),
                                    hbrBackground=COLOR_WINDOW + 1, lpszMenuName=None, lpszClassName=CLASS_NAME)
            window_class = self._class  # kept alive for as long as the class is registered
            self._atom = user32.RegisterClassW(ctypes.byref(window_class))
            if not self._atom:
                raise OSError(f"RegisterClassW failed (error {ctypes.get_last_error()})")
            self.handle = user32.CreateWindowExW(0, CLASS_NAME, TITLE, WS_OVERLAPPEDWINDOW,
                                                 140, 140, 640, 420, None, None, instance, None)
            if not self.handle:
                raise OSError(f"CreateWindowExW failed (error {ctypes.get_last_error()})")
            user32.ShowWindow(self.handle, SW_SHOWNORMAL)
        except Exception as exc:  # the waiting test must see why, not time out
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        message = wintypes.MSG()
        while True:  # the pump: it is what keeps the window responsive (never "hung")
            got = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
            if got in (0, -1):
                return
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))

    def close(self, timeout=STOP_TIMEOUT):
        """Remove everything this fixture created. Returns a list of problems, empty when clean."""
        problems = []
        user32 = self._user32
        handle, self.handle = self.handle, None
        self.closed_handle = handle  # kept so the test can prove afterwards that it really went
        if handle and user32.IsWindow(handle):
            if user32.IsZoomed(handle):
                user32.ShowWindow(handle, SW_RESTORE)
            user32.PostMessageW(handle, WM_CLOSE, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                problems.append(f"the fixture's message pump was still running {timeout:g}s after WM_CLOSE")
        if handle and user32.IsWindow(handle):
            user32.DestroyWindow(handle)  # backstop: only ever this window
            if user32.IsWindow(handle):
                problems.append("the fixture window still exists after WM_CLOSE and DestroyWindow")
        if self._atom:
            if not user32.UnregisterClassW(CLASS_NAME, self._instance):
                problems.append(f"the fixture's window class couldn't be unregistered "
                                f"(error {ctypes.get_last_error()})")
            self._atom = None
        return problems

    # --- evidence ---
    def wait_for_receipt(self, count, timeout=RECEIPT_TIMEOUT):
        """Block until the window procedure has received at least `count` SC_MAXIMIZE messages."""
        with self._changed:
            return self._changed.wait_for(lambda: self.received >= count, timeout)

    def is_zoomed(self):
        return bool(self._user32.IsZoomed(self.handle)) if self.handle else False


def _activate(user32, handle, timeout=ACTIVATE_TIMEOUT):
    """Bring the TEST'S OWN window to the front. Windows may refuse a plain SetForegroundWindow to a
    background process, so SwitchToThisWindow is the fallback. The foreground is then polled, because
    Windows offers nothing to wait on - the one place this test polls."""
    user32.SetForegroundWindow(handle)
    deadline, switched = time.monotonic() + timeout, False
    while True:
        try:
            active = verifier.active_target().window
        except verifier.VerifierUnavailableError:
            active = None
        if active is not None and active.handle == handle:
            return True
        if not switched:
            user32.SwitchToThisWindow(handle, True)
            switched = True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


@pytest.mark.real_desktop
def test_a_deliberately_broken_maximize_is_caught_instead_of_reported_as_done():
    emergency_stop.reset("verifier-acceptance-test")
    user32 = _user32()

    held = verifier.modifiers_held()
    if held:
        pytest.fail(f"INVALID SETUP: {' and '.join(held)} held down on the keyboard - the pipeline may refuse "
                    f"the action, which would not be Verifier evidence. Let go and run it again.")

    previous_foreground = user32.GetForegroundWindow()
    fixture = StubbornWindow()
    fixture.start()
    problems = []
    try:
        assert _activate(user32, fixture.handle), (
            "INVALID SETUP: the test's own fixture window couldn't be brought to the front, so "
            "window_control had nothing to act on - this is not Verifier evidence")

        # --- the fixture is a valid target before anything is asked of it ---
        target = verifier.active_target()
        assert target.window is not None and target.window.handle == fixture.handle, \
            "the fixture window isn't the active window"
        state = verifier.window_state(fixture.handle)
        assert state is not None, "the fixture window vanished before the test started"
        assert not state.maximized and not fixture.is_zoomed(), "the fixture must start not maximized"
        assert state.has_maximize_box, "the fixture must advertise maximize, or validation would refuse it"
        assert not state.tool_window and not state.hung, "the fixture must be an ordinary responsive window"

        # --- BROKEN CASE: the window receives the request and deliberately drops it ---
        prompts, offers = [], []
        broken = execute_with_recovery(
            ExecutorAction(WINDOW_CONTROL, "maximize"),
            confirm=lambda action, assessment: prompts.append(action.description) or False,
            offer_retry=lambda result: offers.append(result) or True)
        print(f"broken case: {broken.outcome.value}: {broken.message}")

        assert prompts == [], f"maximize is LOW risk and must not ask: {prompts}"
        assert fixture.wait_for_receipt(1), "the fixture never received SC_MAXIMIZE - the action didn't reach it"
        assert fixture.received == 1 and fixture.swallowed == 1 and fixture.passed_on == 0, \
            f"expected exactly one swallowed SC_MAXIMIZE, got received={fixture.received} " \
            f"swallowed={fixture.swallowed} passed_on={fixture.passed_on}"
        assert fixture.zoomed_when_received == [False], "the window was already maximized when the request arrived"
        assert not fixture.is_zoomed(), "the window maximized anyway - the fixture didn't swallow the request"
        assert verifier.window_state(fixture.handle).maximized is False, "the Verifier reads the window as maximized"

        assert broken.outcome is Outcome.FAILED, f"expected failed, got {broken.outcome.value}"
        assert broken.outcome is not Outcome.DONE and not broken.ok, "a broken action was reported as success"
        assert broken.message.startswith("I asked the window to maximize, but it didn't maximize within"), \
            broken.message
        assert not broken.retryable, "window controls are never retryable"
        assert offers == [], "recovery offered a retry for a non-retryable verification failure"

        # --- CONTROL CASE: the same window, same action, break removed ---
        fixture.swallow = False
        assert not fixture.is_zoomed(), "the fixture should still be a normal window before the control case"
        working = execute_with_recovery(
            ExecutorAction(WINDOW_CONTROL, "maximize"),
            confirm=lambda action, assessment: prompts.append(action.description) or False,
            offer_retry=lambda result: offers.append(result) or True)
        print(f"control case: {working.outcome.value}: {working.message}")

        assert fixture.wait_for_receipt(2), "the fixture never received the second SC_MAXIMIZE"
        assert fixture.received == 2 and fixture.passed_on == 1 and fixture.swallowed == 1, \
            f"expected the second request to be passed on: received={fixture.received} " \
            f"swallowed={fixture.swallowed} passed_on={fixture.passed_on}"
        assert fixture.is_zoomed(), "the window didn't maximize even with the break removed"
        assert verifier.window_state(fixture.handle).maximized is True, "the Verifier can't see the maximized window"
        assert working.ok and working.outcome is Outcome.DONE, f"expected done, got {working.outcome.value}"
        assert prompts == [] and offers == [], f"unexpected prompt or retry offer: {prompts} {offers}"
    finally:
        problems = fixture.close()
        if previous_foreground and user32.IsWindow(previous_foreground):
            user32.SwitchToThisWindow(previous_foreground, True)  # best effort, never asserted
    assert problems == [], f"cleanup did not leave the desktop as it found it: {problems}"
    assert not user32.IsWindow(fixture.closed_handle), "the fixture window outlived the test"
