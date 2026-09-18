"""
Measure how fast the emergency stop really is on THIS machine (docs/step4 Section 4, Phase 1).

    python scripts/measure_emergency_stop.py              # the synthetic-input scenarios
    python scripts/measure_emergency_stop.py --trials 3   # a quick check
    python scripts/measure_emergency_stop.py --physical   # YOU press the hotkey; nothing is injected
    python scripts/measure_emergency_stop.py --elevated   # the same, with an elevated window in front

It opens ONE Notepad through the assistant's own pipeline, types into it, scrolls it, and closes it
again. It touches nothing else: no other window, no clipboard, no browser. Your mouse pointer is
moved for the scroll scenario and put back afterwards.

Three latencies are reported, and two of them are measured with SYNTHETIC input (SendInput), so they
do NOT include the physical keyboard and driver path:

    A  synthetic key press  ->  WM_HOTKEY received by the listener      (SYNTHETIC)
    B  emergency_stop.trigger() returned  ->  the action actually stopped
    C  synthetic key press  ->  the action actually stopped             (SYNTHETIC)

A physical press can't be timestamped from Python, so --physical proves only that pressing the keys
by hand really triggers the stop. That is REACHABILITY evidence, never a timing measurement.

Since 18 September 2026 a run also judges itself against the thresholds the project owner approved
(docs/step4 Section 4) and exits 1 if any of them is missed. If a run fails, report the numbers and
stop: never loosen a threshold, and never tune the code to chase one.
"""
import argparse
import ctypes
import random
import statistics
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.executor import emergency_stop, hotkey  # noqa: E402
from app.executor.emergency_stop import ActionInterruptedError, EmergencyStopError  # noqa: E402
from app.executor.logic import execute_with_recovery  # noqa: E402
from app.executor.models import CLICK, CLOSE_APP, OPEN_APP, SCROLL, TYPE_TEXT, ExecutorAction  # noqa: E402
from app.logging_setup import setup_logging  # noqa: E402
from app.verifier import logic as verifier  # noqa: E402

TYPED_TEXT = "emergency stop measurement " * 30   # 810 harmless characters
LINES = 200
SCROLL_TARGET = "down 20"
WM_SETTEXT, EM_SETMODIFY, WM_VSCROLL, WM_CLOSE = 0x000C, 0x00B9, 0x0115, 0x0010
SB_TOP = 6
VK = {0x0002: 0x11, 0x0001: 0x12, 0x0004: 0x10, 0x0008: 0x5B}  # MOD_CONTROL/ALT/SHIFT/WIN -> virtual key
INPUT_KEYBOARD, KEYEVENTF_KEYUP = 1, 0x0002


# --- The harness's own Windows calls (test code, not the assistant) -------------------------------

class _KeyboardInput(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _MouseInput(ctypes.Structure):  # only so the union has Windows' real size
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _InputUnion(ctypes.Union):
    _fields_ = [("ki", _KeyboardInput), ("mi", _MouseInput)]


class _Input(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _InputUnion)]


def _user32():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int]
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = wintypes.LPARAM
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SwitchToThisWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
    return user32


def press_hotkey(chosen) -> float:
    """Send the registered combination with SendInput and return the moment it was sent. SYNTHETIC
    input: this is the harness standing in for a person, and it skips the real keyboard path."""
    keys = [VK[flag] for flag in (0x0002, 0x0001, 0x0004, 0x0008) if chosen.modifiers & flag] + [chosen.virtual_key]
    events = [(key, False) for key in keys] + [(key, True) for key in reversed(keys)]
    user32 = _user32()
    inputs = (_Input * len(events))()
    for item, (key, key_up) in zip(inputs, events):
        item.type = INPUT_KEYBOARD
        item.ki.wVk = key
        item.ki.dwFlags = KEYEVENTF_KEYUP if key_up else 0
    sent_at = time.perf_counter()
    user32.SendInput(len(events), inputs, ctypes.sizeof(_Input))
    return sent_at


def set_text(field, text=""):
    """Put text into the measurement's own Notepad (harness code, not the assistant). The buffer is kept
    alive in a local until SendMessage returns - a temporary can be freed before Windows reads it, and
    the message then quietly does nothing."""
    buffer = ctypes.create_unicode_buffer(text)
    user32 = _user32()
    user32.SendMessageW(field, WM_SETTEXT, 0, ctypes.cast(buffer, ctypes.c_void_p).value)
    user32.SendMessageW(field, EM_SETMODIFY, 0, 0)
    return verifier.field_text_length(field)


def text_length(field):
    return verifier.field_text_length(field)


def scroll_to_top(field):
    _user32().SendMessageW(field, WM_VSCROLL, SB_TOP, 0)


def text_area_centre(handle):
    user32 = _user32()
    rect = wintypes.RECT()
    user32.GetClientRect(handle, ctypes.byref(rect))
    point = wintypes.POINT((rect.right - rect.left) // 2, (rect.bottom - rect.top) // 2)
    user32.ClientToScreen(handle, ctypes.byref(point))
    return point.x, point.y


# --- Trials ----------------------------------------------------------------------------------------

class Trial:
    """One measured stop."""

    def __init__(self, number):
        self.number = number
        self.a = self.b = self.c = None
        self.sent_before = self.sent_after = None
        self.outcome = ""
        self.continued = False
        self.retried = False
        self.note = ""

    def row(self):
        def ms(value):
            return f"{value * 1000:8.1f}" if value is not None else "       -"
        return (f"  {self.number:>3}  A {ms(self.a)}  B {ms(self.b)}  C {ms(self.c)}  "
                f"progress {str(self.sent_before):>9}  after {str(self.sent_after):>9}  "
                f"{self.outcome:<9} {'INPUT CONTINUED' if self.continued else ''}"
                f"{' RETRIED' if self.retried else ''} {self.note}")


def arm(chosen, delay, holder):
    """Press the hotkey `delay` seconds from now, from another thread, and record when."""
    def fire():
        holder["sent_at"] = press_hotkey(chosen)
    timer = threading.Timer(delay, fire)
    timer.daemon = True
    timer.start()
    return timer


def wait_for_press(presses_before, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = hotkey.status()
        if state.presses > presses_before:
            return state
        time.sleep(0.0005)
    return None


def measure_idle(chosen, trials):
    """A: how long the listener takes to see a press when nothing is running."""
    rows = []
    for number in range(1, trials + 1):
        emergency_stop.reset("measurement")
        trial = Trial(number)
        before = hotkey.status().presses
        sent_at = press_hotkey(chosen)
        state = wait_for_press(before)
        if state is None:
            trial.note = "NO PRESS SEEN"
        else:
            trial.a = state.received_at - sent_at
            trial.b = 0.0
            trial.c = trial.a
            trial.outcome = "stopped" if emergency_stop.is_stopped() else "NOT STOPPED"
        rows.append(trial)
    emergency_stop.reset("measurement")
    return rows


def measure_typing(chosen, trials, field):
    rows = []
    for number in range(1, trials + 1):
        emergency_stop.reset("measurement")
        time.sleep(0.3)  # let the previous trial's last keystrokes land before clearing
        set_text(field, "")
        trial = Trial(number)
        if text_length(field):
            trial.note = "FIELD NOT EMPTY AT START"
        retries = []
        holder = {}
        before = hotkey.status().presses
        timer = arm(chosen, random.uniform(0.3, 0.9), holder)
        try:
            result = execute_with_recovery(ExecutorAction(TYPE_TEXT, TYPED_TEXT),
                                           confirm=lambda action, assessment: True,
                                           offer_retry=lambda outcome: retries.append(outcome) or True)
            returned_at = time.perf_counter()
            trial.outcome = result.outcome.value
            trial.note = "NOT INTERRUPTED"
        except ActionInterruptedError as stopped:
            returned_at = time.perf_counter()
            trial.outcome = stopped.result.outcome.value
            trial.sent_before = stopped.result.progress
        except EmergencyStopError:
            returned_at = time.perf_counter()
            trial.outcome = "stopped"
            trial.note = "before the first character"
        timer.cancel()
        state = wait_for_press(before, 2.0)
        if state and "sent_at" in holder:
            trial.a = state.received_at - holder["sent_at"]
            trial.b = returned_at - state.triggered_at
            trial.c = returned_at - holder["sent_at"]
        # "Input continued" means the window ended up with MORE characters than the Executor admits
        # sending. A character sent just before the stop can still be in Windows' queue, so the length
        # at the moment of the stop and 0.5 s later are both recorded.
        sent = trial.sent_before[0] if trial.sent_before else 0
        length_at_stop = text_length(field)
        time.sleep(0.5)
        final = text_length(field)
        trial.sent_after = (length_at_stop, final)
        trial.continued = final is not None and final > sent
        trial.retried = bool(retries)
        rows.append(trial)
    emergency_stop.reset("measurement")
    set_text(field, "")
    return rows


def measure_scrolling(chosen, trials, window, field):
    rows = []
    filled = set_text(field, "\r\n".join(f"emergency stop measurement line {n}" for n in range(1, LINES + 1)))
    if not filled:
        raise SystemExit("the measurement's Notepad couldn't be filled, so there is nothing to scroll")
    x, y = text_area_centre(field)
    _user32().SetCursorPos(x, y)
    for number in range(1, trials + 1):
        emergency_stop.reset("measurement")
        scroll_to_top(field)
        start_position = verifier.scroll_state(field)
        trial = Trial(number)
        retries = []
        holder = {}
        before = hotkey.status().presses
        timer = arm(chosen, random.uniform(0.15, 0.5), holder)
        try:
            result = execute_with_recovery(ExecutorAction(SCROLL, SCROLL_TARGET),
                                           confirm=lambda action, assessment: True,
                                           offer_retry=lambda outcome: retries.append(outcome) or True)
            returned_at = time.perf_counter()
            trial.outcome = result.outcome.value
            trial.note = "NOT INTERRUPTED"
        except ActionInterruptedError as stopped:
            returned_at = time.perf_counter()
            trial.outcome = stopped.result.outcome.value
            trial.sent_before = stopped.result.progress
        except EmergencyStopError:
            returned_at = time.perf_counter()
            trial.outcome = "stopped"
            trial.note = "before the first notch"
        timer.cancel()
        state = wait_for_press(before, 2.0)
        if state and "sent_at" in holder:
            trial.a = state.received_at - holder["sent_at"]
            trial.b = returned_at - state.triggered_at
            trial.c = returned_at - holder["sent_at"]
        at_stop = verifier.scroll_state(field)
        time.sleep(0.5)
        later = verifier.scroll_state(field)
        # Scroll positions: where it started, where it was when the stop landed, and 0.5 s later.
        trial.sent_after = (start_position.position if start_position else None,
                            at_stop.position if at_stop else None, later.position if later else None)
        trial.continued = bool(at_stop and later and at_stop.position != later.position)
        trial.retried = bool(retries)
        rows.append(trial)
    emergency_stop.reset("measurement")
    set_text(field, "")
    return rows


def measure_one_shot(chosen, trials, window):
    """A stop pressed before the action's last checkpoint: nothing may be sent at all."""
    rows = []
    x, y = text_area_centre(window.handle)
    for number in range(1, trials + 1):
        emergency_stop.reset("measurement")
        trial = Trial(number)
        retries = []
        holder = {}
        pointer_before = verifier.cursor_position()
        before = hotkey.status().presses

        def confirm(action, assessment):
            holder["sent_at"] = press_hotkey(chosen)
            wait_for_press(before, 2.0)
            return True
        try:
            result = execute_with_recovery(ExecutorAction(CLICK, f"{x + 3}, {y + 3}"), confirm=confirm,
                                           offer_retry=lambda outcome: retries.append(outcome) or True)
            returned_at = time.perf_counter()
            trial.outcome = result.outcome.value
            trial.note = "NOT STOPPED"
        except EmergencyStopError:
            returned_at = time.perf_counter()
            trial.outcome = "stopped"
        state = hotkey.status()
        if state.presses > before and "sent_at" in holder:
            trial.a = state.received_at - holder["sent_at"]
            trial.b = returned_at - state.triggered_at
            trial.c = returned_at - holder["sent_at"]
        trial.sent_before = (0, 1)
        pointer_now = verifier.cursor_position()
        trial.continued = pointer_now != pointer_before  # the click would have moved the pointer
        trial.sent_after = pointer_now
        trial.retried = bool(retries)
        rows.append(trial)
    emergency_stop.reset("measurement")
    return rows


# --- Reporting ---------------------------------------------------------------------------------------

def percentile(values, fraction):
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(fraction * len(ordered) + 0.5)) - 1))
    return ordered[index]


def report(key, name, rows, note=""):
    print(f"\n=== {name} ({len(rows)} trials) {note}")
    for trial in rows:
        print(trial.row())
    for label in ("a", "b", "c"):
        values = [getattr(trial, label) for trial in rows if getattr(trial, label) is not None]
        if not values:
            continue
        print(f"  {label.upper()}: min {min(values) * 1000:.1f} ms | median {statistics.median(values) * 1000:.1f} ms"
              f" | p95 {percentile(values, 0.95) * 1000:.1f} ms | max {max(values) * 1000:.1f} ms")
    continued = [t.number for t in rows if t.continued]
    retried = [t.number for t in rows if t.retried]
    missed = [t.number for t in rows if t.note]
    print(f"  input continued after the stop: {continued or 'never'}")
    print(f"  action retried after the stop:  {retried or 'never'}")
    if missed:
        print(f"  trials needing a look: {[(t.number, t.note) for t in rows if t.note]}")
    return Scenario(key, name, rows)


# --- Acceptance: the approved Phase 1 thresholds ---------------------------------------------------
# Approved by the project owner on 18 September 2026, after reading the first measurement. They judge
# the SYNTHETIC path only - SendInput -> WM_HOTKEY -> the existing emergency stop -> the Executor stops
# - and make no claim about physical keyboard or driver latency. A failing run is reported as it is:
# the thresholds are not adjusted to make a run pass, and the code is not tuned to chase a number.

HARD_MAX_C_MS = 100.0         # rule 1: EVERY timed trial of EVERY scenario
TYPICAL_MEDIAN_C_MS = 25.0    # rule 2: the two 20-trial scenarios only
TYPICAL_P95_C_MS = 50.0
# rule 3: idle and one-shot keep the hard maximum and are deliberately given no p95 requirement -
# 5 one-shot trials are far too few to support one.
TYPICAL_SCENARIOS = ("typing", "scrolling")


class Check:
    """One approved threshold, measured. `value` is in milliseconds, or None if nothing was timed."""

    def __init__(self, label, value, limit):
        self.label, self.value, self.limit = label, value, limit
        self.passed = value is not None and value <= limit

    def line(self):
        measured = f"{self.value:7.1f} ms" if self.value is not None else "      - ms"
        return (f"      {self.label:<10} {measured}  <=  {self.limit:6.1f} ms   "
                f"{'PASS' if self.passed else 'FAIL'}")


class Scenario:
    """One scenario's trials, and how they measure against the thresholds that apply to it."""

    def __init__(self, key, name, rows):
        self.key, self.name, self.rows = key, name, rows

    @property
    def timings(self):
        """Every trial's end-to-end latency C, in milliseconds."""
        return sorted(trial.c * 1000 for trial in self.rows if trial.c is not None)

    def checks(self):
        values = self.timings
        if not values:
            return [Check("max C", None, HARD_MAX_C_MS)]
        checks = [Check("max C", max(values), HARD_MAX_C_MS)]
        if self.key in TYPICAL_SCENARIOS:  # rule 2
            checks.append(Check("median C", statistics.median(values), TYPICAL_MEDIAN_C_MS))
            checks.append(Check("p95 C", percentile(values, 0.95), TYPICAL_P95_C_MS))
        return checks

    def continued(self):
        """Rule 4: trials where input carried on after the stop took effect (for the one-shot
        scenario, where anything at all was sent)."""
        return [trial.number for trial in self.rows if trial.continued]

    def retried(self):
        """Rule 4: trials where a stopped action was offered for retry."""
        return [trial.number for trial in self.rows if trial.retried]

    def untimed(self):
        """Trials where no stop was measured at all. Not one of the four rules - it means the run
        itself didn't observe what it claims to, so the result can't be called a pass."""
        return [trial.number for trial in self.rows if trial.c is None]

    def failures(self):
        reasons = [f"{self.name}: {check.label} "
                   f"{'not measured' if check.value is None else f'{check.value:.1f} ms'} "
                   f"exceeds the {check.limit:.1f} ms threshold"
                   for check in self.checks() if not check.passed]
        if self.continued():
            reasons.append(f"{self.name}: input continued after the stop in trial(s) {self.continued()}")
        if self.retried():
            reasons.append(f"{self.name}: a stopped action was retried in trial(s) {self.retried()}")
        if self.untimed():
            reasons.append(f"{self.name}: no stop was measured in trial(s) {self.untimed()}")
        return reasons


class Verdict:
    """The whole acceptance run: every scenario, and whether all four approved rules held."""

    def __init__(self, scenarios):
        self.scenarios = list(scenarios)

    @property
    def failures(self):
        return [reason for scenario in self.scenarios for reason in scenario.failures()]

    @property
    def passed(self):
        return bool(self.scenarios) and not self.failures

    @property
    def trials(self):
        return sum(len(scenario.rows) for scenario in self.scenarios)


def print_acceptance(verdict):
    """The pass/fail report: trial count, min/median/p95/max, every threshold, and the two
    non-timing rules, per scenario and then overall."""
    print("\n=== Acceptance against the approved Phase 1 thresholds (SYNTHETIC input)")
    print(f"  C = synthetic key press -> the action actually stopped. "
          f"Thresholds: every trial <= {HARD_MAX_C_MS:.0f} ms; "
          f"median <= {TYPICAL_MEDIAN_C_MS:.0f} ms and p95 <= {TYPICAL_P95_C_MS:.0f} ms "
          f"for {' and '.join(TYPICAL_SCENARIOS)}.")
    for scenario in verdict.scenarios:
        values = scenario.timings
        stats = (f"min {min(values):.1f} | median {statistics.median(values):.1f} | "
                 f"p95 {percentile(values, 0.95):.1f} | max {max(values):.1f} ms" if values
                 else "nothing timed")
        print(f"\n  {scenario.name} - {len(scenario.rows)} trials, {len(values)} timed")
        print(f"      C: {stats}")
        for check in scenario.checks():
            print(check.line())
        print(f"      input continued after the stop: {scenario.continued() or 'never'}")
        print(f"      stopped action retried:         {scenario.retried() or 'never'}")
        if scenario.untimed():
            print(f"      NO STOP MEASURED in trial(s):   {scenario.untimed()}")
    print(f"\n=== RESULT: {'PASS' if verdict.passed else 'FAIL'} "
          f"over {verdict.trials} trials in {len(verdict.scenarios)} scenarios")
    for reason in verdict.failures:
        print(f"  FAILED: {reason}")
    if not verdict.passed:
        print("  Report these numbers as they are. Do not loosen a threshold and do not tune the code "
              "to chase one.")
    print("  These thresholds judge the synthetic path only; they make no claim about physical "
          "keyboard latency.")
    return verdict


# --- Physical checks (a person presses the keys) --------------------------------------------------

def physical_check(chosen, elevated=False, seconds=60):
    where = ("Bring an ELEVATED window to the front (e.g. a Notepad you started with 'Run as "
             "administrator'), then press") if elevated else "Press"
    print(f"\n=== {'Elevated-window observation' if elevated else 'Physical hotkey check'}")
    print(f"  {where} {chosen.name} on the keyboard. Waiting up to {seconds}s... (Ctrl+C to skip)")
    emergency_stop.reset("measurement")
    before = hotkey.status().presses
    deadline = time.monotonic() + seconds
    front = None
    while time.monotonic() < deadline:
        state = hotkey.status()
        if state.presses > before:
            stop = emergency_stop.status()
            print(f"  RECEIVED: WM_HOTKEY arrived, emergency stop triggered by {stop.source!r}")
            print(f"  window in front at the press: {front}")
            emergency_stop.reset("measurement")
            return True, front
        try:
            target = verifier.active_target()
            if target.window:
                front = (f"{target.window.title!r} class={target.window.class_name!r} "
                         f"exe={verifier.process_name(target.window.handle)}")
        except Exception as exc:  # reading the desktop must never end the observation
            front = f"(couldn't read the active window: {type(exc).__name__})"
        time.sleep(0.1)
    print(f"  NOT RECEIVED within {seconds}s. Window in front when the wait ended: {front}")
    return False, front


# --- Main ---------------------------------------------------------------------------------------------

def bring_to_front(handle, seconds=2.0):
    """HARNESS code standing in for the person switching windows - the assistant never activates a
    window. Only ever called on the Notepad this run opened itself."""
    user32 = _user32()
    user32.SetForegroundWindow(handle)
    deadline = time.monotonic() + seconds
    switched = False
    while time.monotonic() < deadline:
        target = verifier.active_target()
        if target.window is not None and target.window.handle == handle:
            return True
        if not switched:  # Windows can refuse SetForegroundWindow while another app owns the front
            user32.SwitchToThisWindow(handle, True)
            switched = True
        time.sleep(0.1)
    return False


def open_notepad(seconds=10.0):
    """Open the measurement's own Notepad and get it to the front.

    Nothing is measured against a window that isn't the active one (typing would land somewhere else),
    and never against a Notepad this run didn't open: the handles present beforehand are remembered.
    A run that can't get there closes the Notepad it opened rather than leaving it behind."""
    expectation = verifier.expect_window("notepad")
    before = verifier.snapshot_windows(expectation)
    result = execute_with_recovery(ExecutorAction(OPEN_APP, "notepad"))
    print(f"  {result.message}")
    if not result.ok:
        raise SystemExit("couldn't open the Notepad this measurement types into")
    opened = verifier.snapshot_windows(expectation) - before
    deadline = time.monotonic() + seconds
    while True:
        target = verifier.active_target()
        if (target.window is not None and target.window.class_name == "Notepad" and target.control_handle
                and (not opened or target.window.handle in opened)):
            return target.window, target.control_handle
        if opened:
            bring_to_front(next(iter(opened)))
        if time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    close_notepad(None, None)
    raise SystemExit(f"the Notepad this measurement opened didn't come to the front within {seconds:.0f}s, "
                     f"so nothing was measured (its window was closed again).")


def close_notepad(window, field):
    if field:
        set_text(field, "")
    try:
        result = execute_with_recovery(ExecutorAction(CLOSE_APP, "notepad"), confirm=lambda action, assessment: True)
        print(f"  {result.message}")
    except Exception as exc:
        print(f"  close_app failed during cleanup: {exc!r}")
    if window and verifier.window_state(window.handle) is not None:
        _user32().PostMessageW(window.handle, WM_CLOSE, 0, 0)  # backstop: only the window this script opened
        print("  cleanup posted a close request to the measurement's own Notepad")


DEFAULT_COUNTS = {"idle": 20, "typing": 20, "scrolling": 20, "one-shot": 5}


def acceptance_run(counts=None):
    """THE acceptance path: run every scenario and judge it against the approved thresholds.

    Starts and stops the hotkey listener itself, opens and closes its own Notepad, and always puts the
    mouse pointer back. Returns a Verdict; it never adjusts a threshold and never retries a failure.
    The opt-in real-desktop test calls this, so there is one measurement system, not two."""
    counts = dict(counts or DEFAULT_COUNTS)
    emergency_stop.reset("measurement")
    state = hotkey.start()
    chosen = hotkey.configured()
    print(f"Emergency-stop hotkey: {state.hotkey} - {state.state}{': ' + state.reason if state.reason else ''}")
    if not state.active:
        hotkey.stop()
        raise RuntimeError("the hotkey isn't registered, so there is nothing to measure")
    try:
        pointer_before = verifier.cursor_position()
        print(f"\nPointer at {pointer_before}; opening the measurement's own Notepad...")
        window, field = open_notepad()
        scenarios = []
        try:
            scenarios.append(report("idle", "Idle press (A only)",
                                    measure_idle(chosen, counts["idle"]), "SYNTHETIC input"))
            scenarios.append(report("typing", "Type Text (810 characters)",
                                    measure_typing(chosen, counts["typing"], field), "SYNTHETIC input"))
            scenarios.append(report("scrolling", f"Scroll ({SCROLL_TARGET}, {LINES} lines)",
                                    measure_scrolling(chosen, counts["scrolling"], window, field), "SYNTHETIC input"))
            scenarios.append(report("one-shot", "One-shot click, stopped before sending",
                                    measure_one_shot(chosen, counts["one-shot"], window), "SYNTHETIC input"))
        finally:
            _user32().SetCursorPos(*pointer_before)
            print(f"\nPointer restored to {verifier.cursor_position()}")
            close_notepad(window, field)
        print("\n  A and C exclude the physical keyboard path; run --physical to confirm a real press works.")
        return print_acceptance(Verdict(scenarios))
    finally:
        hotkey.stop()
        emergency_stop.reset("measurement")


def main(argv=()):
    parser = argparse.ArgumentParser(description="Measure the emergency stop's real response time and "
                                                 "judge it against the approved thresholds.")
    parser.add_argument("--trials", type=int, default=0, help="override the trial count for every scenario")
    parser.add_argument("--physical", action="store_true", help="wait for a real key press instead of measuring")
    parser.add_argument("--elevated", action="store_true", help="the same, with an elevated window in front")
    args = parser.parse_args(list(argv))
    setup_logging()
    emergency_stop.reset("measurement")

    if args.physical or args.elevated:
        state = hotkey.start()
        print(f"Emergency-stop hotkey: {state.hotkey} - {state.state}"
              f"{': ' + state.reason if state.reason else ''}")
        try:
            if not state.active:
                raise SystemExit("the hotkey isn't registered, so there is nothing to measure")
            physical_check(hotkey.configured(), elevated=args.elevated)
            return 0
        finally:
            hotkey.stop()
            emergency_stop.reset("measurement")
    try:
        verdict = acceptance_run({name: args.trials for name in DEFAULT_COUNTS} if args.trials else None)
    except RuntimeError as exc:
        raise SystemExit(str(exc))
    return 0 if verdict.passed else 1  # a failing run must be visible to whatever ran it


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
