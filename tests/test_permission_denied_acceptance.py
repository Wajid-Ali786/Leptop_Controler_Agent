"""
Phase 1 acceptance: what happens when this NON-ELEVATED assistant acts on a window belonging to an
ELEVATED application (docs/step4 Section 4 test cases: "Permission denied | An action requiring admin
rights -> confirmation/refusal per Section 3's Windows permissions note, not a crash", with
docs/step1 Section 3: the assistant should not run elevated, and some restrictions are Windows' to
enforce, not ours to work around).

The real pipeline runs: the real Executor validates, the real safety gate authorizes, and
app/executor/adapter.py really posts WM_SYSCOMMAND/SC_MAXIMIZE across the Windows integrity boundary.
Nothing is mocked and no production code is patched - the adapter's own result is the evidence.

WINDOW_CONTROL "maximize" is the chosen action because it is the only Phase 1 action whose adapter
sends a WINDOW MESSAGE, which UIPI refuses with an error. Every SendInput-based action (click, type,
scroll, refresh) is dropped silently by Windows with nothing to detect, and close is excluded because
close only ever touches a window group this session opened.

Preconditions are proved by reading BOTH tokens directly, never inferred from an access failure:

    GetWindowThreadProcessId -> OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) ->
    OpenProcessToken(TOKEN_QUERY) -> GetTokenInformation(TokenElevation, TokenIntegrityLevel)

Measured on this machine (18-19 September 2026): a non-elevated Medium process CAN read an elevated
Notepad's token this way - elevated=True, integrity High (RID 0x3000).

It needs a Notepad YOU started with "Run as administrator", so it is gated behind its own opt-in on
top of the desktop one, and never runs unattended:

    $env:RUN_REAL_DESKTOP_TEST='1'; $env:RUN_ELEVATED_TEST='1'
    pytest tests/test_permission_denied_acceptance.py -m real_elevated -v -s

The test types nothing, moves nothing, closes nothing, creates nothing and reads no window titles: it
reads two tokens and performs exactly ONE maximize attempt. It never automates UAC or the secure
desktop, and it never elevates itself.
"""
import ctypes
import time
from ctypes import wintypes

import pytest

from app.executor import emergency_stop
from app.executor.logic import execute_with_recovery
from app.executor.models import WINDOW_CONTROL, ExecutorAction, Outcome
from app.verifier import logic as verifier

TOKEN_QUERY = 0x0008
TOKEN_ELEVATION, TOKEN_INTEGRITY_LEVEL = 20, 25
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000  # the only process right this test asks for
# SECURITY_MANDATORY_*_RID (winnt.h). Medium is an ordinary process; High is what elevation gives.
LEVELS = {0x0000: "Untrusted", 0x1000: "Low", 0x2000: "Medium", 0x2100: "Medium Plus",
          0x3000: "High", 0x4000: "System", 0x5000: "Protected"}
MEDIUM_RID, HIGH_RID = 0x2000, 0x3000
WAIT_SECONDS = 60

# What the real Executor says in each case. These are matched, not produced, by the test.
DENIED_MESSAGE = ("I couldn't maximize the window: it runs with administrator rights, and the "
                  "assistant doesn't run elevated.")
REFUSED_PREFIX = "I couldn't maximize the window: Windows refused the request (error "
IGNORED_PREFIX = "I asked the window to maximize, but it didn't maximize within"

EXPLICIT_DENIAL, SAFE_DEGRADATION, ACCEPTANCE_FAIL = ("EXPLICIT PERMISSION DENIAL PASS",
                                                      "SAFE DEGRADATION ONLY", "ACCEPTANCE FAIL")


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]


def _apis():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
                                             ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetSidSubAuthorityCount.argtypes = [wintypes.LPVOID]
    advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    advapi32.GetSidSubAuthority.argtypes = [wintypes.LPVOID, wintypes.DWORD]
    advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsZoomed.argtypes = [wintypes.HWND]
    user32.IsWindow.argtypes = [wintypes.HWND]
    return kernel32, advapi32, user32


KERNEL32, ADVAPI32, USER32 = _apis()


class TokenFacts:
    """What a process's own token says. `problem` is set when it couldn't be read, and the test then
    reports INVALID SETUP rather than guessing."""

    def __init__(self, elevated=None, rid=None, exe=None, pid=None, problem=None):
        self.elevated, self.rid, self.exe, self.pid, self.problem = elevated, rid, exe, pid, problem

    @property
    def level(self):
        return LEVELS.get(self.rid, "unknown") if self.rid is not None else "unreadable"

    def describe(self):
        if self.problem:
            return f"UNREADABLE - {self.problem}"
        return (f"{'ELEVATED' if self.elevated else 'not elevated'}, "
                f"integrity {self.level} (RID 0x{self.rid:04X})")


def _read_token(token):
    """(elevated, integrity RID) or a problem string."""
    elevation, needed = wintypes.DWORD(), wintypes.DWORD()
    if not ADVAPI32.GetTokenInformation(token, TOKEN_ELEVATION, ctypes.byref(elevation),
                                        ctypes.sizeof(elevation), ctypes.byref(needed)):
        return None, None, f"GetTokenInformation(TokenElevation) failed (error {ctypes.get_last_error()})"
    size = wintypes.DWORD()
    ADVAPI32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(size))
    buffer = ctypes.create_string_buffer(size.value or 1)
    if not ADVAPI32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, buffer, size, ctypes.byref(size)):
        return None, None, f"GetTokenInformation(TokenIntegrityLevel) failed (error {ctypes.get_last_error()})"
    label = ctypes.cast(buffer, ctypes.POINTER(SID_AND_ATTRIBUTES)).contents
    count = ADVAPI32.GetSidSubAuthorityCount(label.Sid).contents.value
    return bool(elevation.value), ADVAPI32.GetSidSubAuthority(label.Sid, count - 1).contents.value, None


def own_token():
    token = wintypes.HANDLE()
    if not ADVAPI32.OpenProcessToken(KERNEL32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        return TokenFacts(problem=f"OpenProcessToken on this process failed (error {ctypes.get_last_error()})")
    try:
        elevated, rid, problem = _read_token(token)
        return TokenFacts(elevated=elevated, rid=rid, problem=problem)
    finally:
        KERNEL32.CloseHandle(token)


def window_token(handle):
    """The token of the process owning `handle`, read with the minimum rights that can do it."""
    pid = wintypes.DWORD()
    USER32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
    if not pid.value:
        return TokenFacts(problem="the window has no process id")
    ctypes.set_last_error(0)
    process = KERNEL32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not process:
        return TokenFacts(pid=pid.value,
                          problem=f"OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) failed "
                                  f"(error {ctypes.get_last_error()})")
    try:
        name = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(512)
        exe = (name.value.rsplit("\\", 1)[-1]
               if KERNEL32.QueryFullProcessImageNameW(process, 0, name, ctypes.byref(size)) else None)
        token = wintypes.HANDLE()
        ctypes.set_last_error(0)
        if not ADVAPI32.OpenProcessToken(process, TOKEN_QUERY, ctypes.byref(token)):
            return TokenFacts(pid=pid.value, exe=exe,
                              problem=f"OpenProcessToken(TOKEN_QUERY) failed (error {ctypes.get_last_error()})")
        try:
            elevated, rid, problem = _read_token(token)
            return TokenFacts(elevated=elevated, rid=rid, exe=exe, pid=pid.value, problem=problem)
        finally:
            KERNEL32.CloseHandle(token)
    finally:
        KERNEL32.CloseHandle(process)


def wait_for_elevated_notepad(seconds=WAIT_SECONDS):
    """Wait for YOU to bring an elevated Notepad to the front. The test never activates a window."""
    print(f"\n  Bring the Notepad you started with 'Run as administrator' to the FRONT now. "
          f"Waiting up to {seconds}s...")
    deadline, last = time.monotonic() + seconds, None
    while time.monotonic() < deadline:
        try:
            window = verifier.active_target().window
        except verifier.VerifierUnavailableError:
            window = None
        if window is not None and window.class_name == "Notepad":
            facts = window_token(window.handle)
            if facts.elevated:
                return window, facts
            last = facts
        time.sleep(0.2)
    return None, last


def invalid_setup(reason):
    print(f"\nVERDICT\n  INVALID SETUP - {reason}")
    pytest.fail(f"INVALID SETUP: {reason}. This run is NOT permission-denied evidence.")


@pytest.mark.real_desktop
@pytest.mark.real_elevated
def test_acting_on_an_elevated_window_is_refused_clearly_and_never_reported_as_done():
    emergency_stop.reset("permission-denied-acceptance")

    mine = own_token()
    if mine.problem:
        invalid_setup(f"this process's own token couldn't be read ({mine.problem})")
    if mine.elevated:
        invalid_setup("this test process is ELEVATED - it must run non-elevated to test the boundary")

    window, facts = wait_for_elevated_notepad()
    if window is None:
        invalid_setup("no ELEVATED Notepad was in front within the wait" +
                      (f" (last Notepad seen: {facts.describe()})" if facts else ""))
    if facts.problem:
        invalid_setup(f"the target's token couldn't be read ({facts.problem})")
    if not facts.elevated:
        invalid_setup(f"the Notepad in front is {facts.describe()}, not elevated")
    if mine.rid is not None and facts.rid is not None and facts.rid <= mine.rid:
        invalid_setup(f"the target's integrity ({facts.level}) isn't higher than this process's ({mine.level})")
    if mine.rid != MEDIUM_RID:
        print(f"  NOTE: this process is {mine.level}, not Medium - the boundary is still real "
              f"because the target is higher.")

    state = verifier.window_state(window.handle)
    if state is None:
        invalid_setup("the elevated Notepad disappeared before the test acted")
    if state.maximized:
        invalid_setup("the elevated Notepad is ALREADY maximized - the Executor would return done "
                      "without sending anything. Restore it and run again")

    print("\nPRECONDITIONS")
    print(f"  test process:        {mine.describe()}")
    print(f"  target hwnd:         {window.handle}")
    print(f"  target PID:          {facts.pid}")
    print(f"  target exe:          {facts.exe}")
    print(f"  target token:        {facts.describe()}")
    print(f"  target maximized:    no (has_maximize_box={state.has_maximize_box}, "
          f"tool_window={state.tool_window}, hung={state.hung})")

    # --- exactly one maximize attempt, through the real pipeline ---
    prompts, offers = [], []
    print("\nACTION\n  WINDOW_CONTROL maximize (real Executor -> validation -> Safety -> adapter)")
    result = execute_with_recovery(
        ExecutorAction(WINDOW_CONTROL, "maximize"),
        confirm=lambda action, assessment: prompts.append((action.description, assessment)) or False,
        offer_retry=lambda outcome: offers.append(outcome) or True)
    zoomed_after = bool(USER32.IsZoomed(window.handle))
    still_there = bool(USER32.IsWindow(window.handle))
    print(f"  safety:              {'no confirmation requested (LOW)' if not prompts else prompts}")

    # The adapter's own outcome IS the evidence: production is not patched and no second message is
    # sent, so the raw PostMessageW result is read back from the message the real adapter produced.
    if result.message == DENIED_MESSAGE:
        windows_result, verdict = ("PostMessageW returned FALSE, GetLastError = 5 (ERROR_ACCESS_DENIED), "
                                   "mapped by the existing adapter"), EXPLICIT_DENIAL
    elif result.message.startswith(REFUSED_PREFIX):
        # FALSE, but with some error other than 5 - reported as-is rather than read as a denial
        windows_result, verdict = (f"PostMessageW returned FALSE with a different error - {result.message}",
                                   ACCEPTANCE_FAIL)
    elif result.message.startswith(IGNORED_PREFIX):
        windows_result, verdict = ("PostMessageW reported success; the elevated window never changed state",
                                   SAFE_DEGRADATION)
    elif result.outcome is Outcome.DONE:
        windows_result, verdict = ("PostMessageW reported success and Windows MAXIMIZED the elevated window",
                                   ACCEPTANCE_FAIL)
    else:
        windows_result, verdict = ("not determinable from the Executor's message", ACCEPTANCE_FAIL)

    print(f"\nWINDOWS RESULT\n  {windows_result}")
    print("\nEXECUTOR RESULT")
    print(f"  outcome:             {result.outcome.value}")
    print(f"  ok:                  {result.ok}")
    print(f"  retryable:           {result.retryable}")
    print(f"  message:             {result.message}")
    print(f"  Verifier reached:    {'no - the adapter refused before anything could be verified' if verdict == EXPLICIT_DENIAL else 'yes - the request was sent and the state was read back'}")
    print(f"  retry offered:       {'yes' if offers else 'no'}")
    print(f"\nTARGET AFTER\n  same hwnd:           {still_there}\n  maximized:           {zoomed_after}")
    print(f"\nVERDICT\n  {verdict}")

    # --- invariants that must hold whatever Windows did ---
    assert not result.ok, "an action Windows refused was reported as successful"
    assert result.outcome is not Outcome.DONE, "an action against an elevated window reported done"
    assert not result.retryable, "a permission failure was marked retryable"
    assert offers == [], "recovery offered to retry an action Windows refused"
    assert prompts == [], f"maximize is LOW risk and must not ask for confirmation: {prompts}"
    assert still_there, "the elevated Notepad disappeared during the test"

    if verdict == ACCEPTANCE_FAIL:
        pytest.fail(f"ACCEPTANCE FAIL - {windows_result}. test process: {mine.describe()}; "
                    f"target: {facts.describe()}; maximized afterwards: {zoomed_after}. "
                    f"Executor said: {result.message}"
                    + (" Your elevated Notepad was left maximized; the test cannot undo that across the "
                       "integrity boundary - please restore it by hand." if zoomed_after else ""))
    if verdict == SAFE_DEGRADATION:
        pytest.fail(f"SAFE DEGRADATION ONLY - Windows accepted the message and silently dropped it, so "
                    f"there is no explicit permission denial to record. The Executor still failed safely: "
                    f"{result.message} This needs the project owner's decision, not a code change.")

    assert result.message == DENIED_MESSAGE
    assert not zoomed_after, "the elevated window maximized despite reporting a denial"
    print("\n  Close the elevated Notepad yourself when you're done - this test never closes it.")
