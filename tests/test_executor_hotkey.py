"""
Tests for the global emergency-stop hotkey (app/executor/hotkey.py).

No real hotkey is registered here: Windows' RegisterHotKey, its message loop and WM_QUIT are faked,
so the tests run offline and can't take a key combination away from the machine running them. The
REAL emergency stop, the REAL Executor pipeline and the REAL safety gate decide everything else -
a press does one thing and one thing only, emergency_stop.trigger("global-hotkey").

Real registration and the latency measurements live in scripts/measure_emergency_stop.py.
"""
import ast
import logging
import queue
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import console
from app.console import Status, handle_command
from app.executor import adapter, emergency_stop, hotkey
from app.executor import logic as executor_logic
from app.executor.emergency_stop import ActionInterruptedError, TypingInterruptedError
from app.executor.hotkey import ACTIVE, FAILED, STOPPED, UNAVAILABLE, Hotkey, HotkeyRefusal
from app.executor.models import SCROLL, TYPE_TEXT, ExecutorAction, Outcome
from app.verifier import adapter as verifier_adapter
from app.verifier.models import ActiveTarget, ControlInfo, ScrollState, WindowInfo
from config import settings

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x0008, 0x4000
BACKSPACE = 0x08
NOTEPAD = WindowInfo(500, "Untitled - Notepad", "Notepad")

CONFIG = (
    "safety:\n"
    "  risky_keywords: [delete, shutdown, send, close]\n"
    "  safe_words: [sender]\n"
    "console:\n"
    "  focus_handover_seconds: 0.5\n"
    "  focus_settle_seconds: 0.05\n"
    "  poll_interval_seconds: 0.01\n"
    "executor:\n"
    "  apps:\n"
    "    notepad: notepad.exe\n"
    "  max_attempts: 3\n"
    "  max_type_characters: 1000\n"
    "  typing_interval_seconds: 0.01\n"
    "  max_scroll_notches: 20\n"
    "  max_unclassified_notches: 3\n"
    "  scroll_interval_seconds: 0.01\n"
    '  emergency_stop_hotkey: "Ctrl+Alt+Backspace"\n'
    "verifier:\n"
    "  window_timeout_seconds: 0.3\n"
    "  poll_interval_seconds: 0.01\n"
    "  text_settle_seconds: 0.05\n"
    "  scroll_settle_seconds: 0.05\n"
    "  max_read_characters: 100000\n"
    "  app_windows:\n"
    '    notepad: "Notepad$"\n'
)


class FakeWindows:
    """Stands in for RegisterHotKey and the thread's message queue."""

    def __init__(self):
        self.calls = []
        self.registered = {}
        self.messages = queue.Queue()
        self.register_error = None
        self.message_error = None
        self.thread_id = 4242
        self.posted = True

    def current_thread_id(self):
        return self.thread_id

    def create_message_queue(self):
        self.calls.append(("queue",))

    def register_hotkey(self, hotkey_id, modifiers, virtual_key):
        self.calls.append(("register", hotkey_id, modifiers, virtual_key))
        if self.register_error:
            raise self.register_error
        self.registered[hotkey_id] = (modifiers, virtual_key)

    def unregister_hotkey(self, hotkey_id):
        self.calls.append(("unregister", hotkey_id))
        return self.registered.pop(hotkey_id, None) is not None

    def wait_for_hotkey_message(self):
        if self.message_error:
            error, self.message_error = self.message_error, None
            raise error
        return self.messages.get()

    def post_quit_to_thread(self, thread_id):
        self.calls.append(("post_quit", thread_id))
        if self.posted:
            self.messages.put((adapter.QUIT_MESSAGE, None))
        return self.posted

    # --- what the "keyboard" does ---
    def press(self, hotkey_id=hotkey.HOTKEY_ID):
        self.messages.put((adapter.HOTKEY_MESSAGE, hotkey_id))

    def other_message(self):
        self.messages.put((adapter.OTHER_MESSAGE, None))


@pytest.fixture
def world(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_PATH", config_path)
    windows = FakeWindows()
    for name in ("current_thread_id", "create_message_queue", "register_hotkey", "unregister_hotkey",
                 "wait_for_hotkey_message", "post_quit_to_thread"):
        monkeypatch.setattr(adapter, name, getattr(windows, name))
    emergency_stop.reset("test-setup")
    yield SimpleNamespace(windows=windows, config_path=config_path)
    hotkey.stop()
    emergency_stop.reset("test-teardown")


def wait_until(check, seconds=2.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.005)
    return check()


# --- The configured combination ------------------------------------------------------------------

def test_the_configured_hotkey_is_the_one_registered(world):
    state = hotkey.start()
    assert state.state == ACTIVE and state.hotkey == "Ctrl+Alt+Backspace"
    assert ("register", hotkey.HOTKEY_ID, MOD_NOREPEAT | MOD_CONTROL | MOD_ALT, BACKSPACE) in world.windows.calls
    assert ("queue",) in world.windows.calls  # the message queue exists before anything can post to it


@pytest.mark.parametrize("text, name, modifiers, key", [
    ("Ctrl+Alt+Backspace", "Ctrl+Alt+Backspace", MOD_CONTROL | MOD_ALT, BACKSPACE),
    ("ctrl + alt + backspace", "Ctrl+Alt+Backspace", MOD_CONTROL | MOD_ALT, BACKSPACE),
    ("ALT+CTRL+BACKSPACE", "Ctrl+Alt+Backspace", MOD_CONTROL | MOD_ALT, BACKSPACE),
    ("Ctrl+Alt+Shift+Escape", "Ctrl+Alt+Shift+Esc", MOD_CONTROL | MOD_ALT | MOD_SHIFT, 0x1B),
    ("Win+Pause", "Win+Pause", MOD_WIN, 0x13),
    ("Ctrl+Shift+F12", "Ctrl+Shift+F12", MOD_CONTROL | MOD_SHIFT, 0x7B),
])
def test_combinations_that_may_be_used(text, name, modifiers, key):
    parsed = hotkey.parse(text)
    assert parsed == Hotkey(name, MOD_NOREPEAT | modifiers, key)


@pytest.mark.parametrize("text, part", [
    ("", "must name a key combination"),
    ("   ", "must name a key combination"),
    (None, "must name a key combination"),
    ("Backspace", "needs at least one of Ctrl, Alt, Shift or Win"),
    ("Ctrl+Alt+Delete", "reserved by Windows"),
    ("Ctrl+Shift+Esc", "reserved by Windows"),
    ("Win+L", "reserved by Windows"),
    ("ctrl+alt+del", "reserved by Windows"),
    ("Ctrl+Alt+A", "I don't know a key called 'A'"),     # a character key can never be the stop
    ("Ctrl+Alt", "needs exactly one key"),
    ("Ctrl+Alt+Backspace+Esc", "needs exactly one key"),
    ("Ctrl+Ctrl+Backspace", "names the same modifier twice"),
    ("Ctrl++Backspace", "join key names with +"),
])
def test_combinations_that_are_refused(text, part):
    refusal = hotkey.parse(text)
    assert isinstance(refusal, HotkeyRefusal) and part in refusal.message


def test_a_badly_configured_hotkey_leaves_the_app_working(world, caplog):
    world.config_path.write_text(CONFIG.replace('"Ctrl+Alt+Backspace"', '"Ctrl+Alt+Delete"'), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        state = hotkey.start()
    assert state.state == UNAVAILABLE and "reserved by Windows" in state.reason
    assert world.windows.calls == [] and "No emergency-stop hotkey" in caplog.text
    assert hotkey.status().state == UNAVAILABLE


def test_a_missing_setting_is_reported_not_raised(world):
    world.config_path.write_text(CONFIG.replace('  emergency_stop_hotkey: "Ctrl+Alt+Backspace"\n', ""),
                                 encoding="utf-8")
    state = hotkey.start()
    assert state.state == UNAVAILABLE and hotkey.SETTING in state.reason
    assert hotkey.configured_name() == "(not configured)"


# --- A press does one thing ----------------------------------------------------------------------

def test_a_press_triggers_the_one_emergency_stop(world):
    hotkey.start()
    assert not emergency_stop.is_stopped()
    world.windows.press()
    assert wait_until(emergency_stop.is_stopped)
    assert emergency_stop.status().source == "global-hotkey"
    assert wait_until(lambda: hotkey.status().presses == 1)


def test_repeated_presses_are_harmless_and_keep_the_first_stop(world):
    hotkey.start()
    world.windows.press()
    assert wait_until(emergency_stop.is_stopped)
    first = emergency_stop.status().triggered_at
    for _ in range(5):
        world.windows.press()
    assert wait_until(lambda: hotkey.status().presses == 6)
    assert emergency_stop.is_stopped() and emergency_stop.status().triggered_at == first
    assert emergency_stop.status().source == "global-hotkey"
    assert hotkey.status().state == ACTIVE  # still watching


def test_only_our_hotkey_id_triggers_anything(world):
    hotkey.start()
    world.windows.press(hotkey_id=99)  # someone else's hotkey
    world.windows.other_message()      # any other message
    assert wait_until(lambda: len(world.windows.calls) >= 2)
    time.sleep(0.05)
    assert not emergency_stop.is_stopped() and hotkey.status().presses == 0


def test_the_press_records_when_it_happened_for_measuring(world):
    """On the high-resolution clock: time.monotonic() moves in ~15.6 ms steps on Windows, which is
    coarser than the latency being measured."""
    hotkey.start()
    before = time.perf_counter()
    world.windows.press()
    assert wait_until(lambda: hotkey.status().presses == 1)
    state = hotkey.status()
    assert before <= state.received_at <= state.triggered_at <= time.perf_counter()
    assert state.received_wall and abs(state.received_wall - emergency_stop.status().triggered_at) < 1


# --- Lifecycle -------------------------------------------------------------------------------------

def test_the_listener_is_a_daemon_thread_that_cannot_keep_the_app_alive(world):
    hotkey.start()
    threads = [t for t in threading.enumerate() if t.name == "emergency-stop-hotkey"]
    assert len(threads) == 1 and threads[0].daemon


def test_stopping_gives_the_hotkey_back_exactly_once(world):
    hotkey.start()
    hotkey.stop()
    assert wait_until(lambda: world.windows.registered == {})
    assert [call for call in world.windows.calls if call[0] == "unregister"] == [("unregister", hotkey.HOTKEY_ID)]
    assert ("post_quit", world.windows.thread_id) in world.windows.calls
    assert hotkey.status().state == STOPPED


def test_starting_twice_registers_once(world):
    first = hotkey.start()
    second = hotkey.start()
    assert first.state == second.state == ACTIVE
    assert len([call for call in world.windows.calls if call[0] == "register"]) == 1


def test_stopping_without_starting_is_harmless(world):
    hotkey.stop()
    assert hotkey.status().state == STOPPED and world.windows.calls == []


def test_a_combination_another_program_owns_is_reported_not_fatal(world, caplog):
    world.windows.register_error = adapter.HotkeyError("another program has already registered that key combination")
    with caplog.at_level(logging.WARNING):
        state = hotkey.start()
    assert state.state == UNAVAILABLE and "another program" in state.reason
    assert "unavailable" in caplog.text
    assert [call for call in world.windows.calls if call[0] == "unregister"] == []  # nothing to give back
    assert wait_until(lambda: not any(t.name == "emergency-stop-hotkey" and t.is_alive()
                                      for t in threading.enumerate()))


def test_a_listener_failure_is_recorded_and_the_thread_stops(world, caplog):
    hotkey.start()
    world.windows.message_error = adapter.HotkeyError("Windows stopped delivering messages (error 1400)")
    world.windows.other_message()  # wakes the loop, which then hits the failure
    with caplog.at_level(logging.ERROR):
        assert wait_until(lambda: hotkey.status().state == FAILED)
    assert "stopped delivering messages" in hotkey.status().reason
    assert wait_until(lambda: not any(t.name == "emergency-stop-hotkey" and t.is_alive()
                                      for t in threading.enumerate())), "the thread must end, not spin"
    assert world.windows.registered == {}  # the hotkey was still given back


def test_a_crash_in_the_listener_is_contained(world):
    hotkey.start()
    world.windows.message_error = RuntimeError("something odd")
    world.windows.other_message()
    assert wait_until(lambda: hotkey.status().state == FAILED)
    assert hotkey.status().reason == "the listener stopped (RuntimeError)"
    hotkey.stop()  # still safe afterwards


def test_shutdown_is_safe_when_the_thread_is_already_gone(world):
    hotkey.start()
    world.windows.posted = False  # PostThreadMessage fails: the thread was already gone
    hotkey.stop()
    assert hotkey.status().state == STOPPED


def test_the_stop_flag_is_never_reset_by_the_hotkey(world):
    emergency_stop.trigger("something-else")
    hotkey.start()
    world.windows.press()
    assert wait_until(lambda: hotkey.status().presses == 1)
    assert emergency_stop.is_stopped() and emergency_stop.status().source == "something-else"


# --- A press really stops a running action ---------------------------------------------------------

class FakeDesktop:
    def __init__(self, calls):
        self.calls = calls
        self.target = ActiveTarget(NOTEPAD, 501, "Edit")
        self.scroll = ScrollState(position=10, minimum=0, maximum=199, page=20)

    def active_target(self):
        return self.target

    def modifier_keys_down(self):
        return []

    def read_text(self, handle, max_characters):
        return ""

    def list_windows(self):
        return [NOTEPAD]

    def control_chain_at(self, x, y):
        return [ControlInfo(501, "Edit"), ControlInfo(500, "Notepad")]

    def cursor_position(self):
        return (700, 400)

    def vertical_scroll(self, handle):
        return self.scroll

    def send_character(self, character):
        self.calls.append("typed")

    def send_wheel_notch(self, up):
        self.calls.append("notch")
        return True


@pytest.fixture
def desktop(world, monkeypatch):
    calls = []
    fake = FakeDesktop(calls)
    for name in ("active_target", "modifier_keys_down", "read_text", "list_windows", "control_chain_at",
                 "cursor_position", "vertical_scroll"):
        monkeypatch.setattr(verifier_adapter, name, getattr(fake, name))
    for name in ("send_character", "send_wheel_notch"):
        monkeypatch.setattr(adapter, name, getattr(fake, name))
    return SimpleNamespace(fake=fake, calls=calls)


def press_after(world, seconds=0.05):
    threading.Timer(seconds, world.windows.press).start()


def test_a_press_stops_typing_part_way_and_nothing_is_retried(world, desktop):
    hotkey.start()
    retries = []
    press_after(world)
    with pytest.raises(TypingInterruptedError) as raised:
        executor_logic.execute_with_recovery(ExecutorAction(TYPE_TEXT, "x" * 500),
                                             confirm=lambda action, assessment: True,
                                             offer_retry=lambda result: retries.append(result) or True)
    result = raised.value.result
    assert result.outcome is Outcome.PARTIAL and 0 < result.progress[0] < 500
    assert result.progress[0] == len(desktop.calls), "more characters were sent than the result admits"
    sent = len(desktop.calls)
    time.sleep(0.1)
    assert len(desktop.calls) == sent, "input continued after the stop"
    assert retries == [], "a stopped action must never be retried"
    assert emergency_stop.status().source == "global-hotkey"


def test_a_press_stops_scrolling_part_way_and_nothing_is_retried(world, desktop):
    hotkey.start()
    retries = []
    press_after(world)
    with pytest.raises(ActionInterruptedError) as raised:
        executor_logic.execute_with_recovery(ExecutorAction(SCROLL, "down 20"),
                                             confirm=lambda action, assessment: True,
                                             offer_retry=lambda result: retries.append(result) or True)
    result = raised.value.result
    assert result.outcome is Outcome.PARTIAL and 0 < result.progress[0] < 20
    assert result.progress[0] == len(desktop.calls)
    sent = len(desktop.calls)
    time.sleep(0.1)
    assert len(desktop.calls) == sent, "notches continued after the stop"
    assert retries == []


def test_a_press_during_a_confirmation_stops_the_action(world, desktop):
    hotkey.start()

    def confirm(action, assessment):
        world.windows.press()
        assert wait_until(emergency_stop.is_stopped)
        return True
    reply = handle_command("type hello", confirm=confirm)
    assert reply.status is Status.STOPPED and desktop.calls == []


def test_a_press_during_the_focus_hand_over_stops_the_action(world, desktop):
    hotkey.start()
    focus = console.FocusHandover(lambda text: None)
    desktop.fake.target = ActiveTarget(WindowInfo(100, "Console", "ConsoleWindowClass"), 101, "")
    focus.note_console_window()
    press_after(world, 0.02)
    reply = handle_command("type hello", confirm=lambda action, assessment: True, focus=focus)
    assert reply.status is Status.STOPPED and desktop.calls == []


# --- What this module may and may not do -----------------------------------------------------------

def hotkey_source():
    return ast.parse(Path(hotkey.__file__).read_text(encoding="utf-8"))


def test_the_hotkey_module_uses_only_the_adapters_hotkey_functions():
    """The companion rule to letting hotkey.py import the Executor adapter at all: it may not reach a
    single function that acts on the computer."""
    allowed = {"register_hotkey", "unregister_hotkey", "wait_for_hotkey_message", "post_quit_to_thread",
               "current_thread_id", "create_message_queue",
               "HotkeyError", "ExecutorAdapterError", "QUIT_MESSAGE", "HOTKEY_MESSAGE", "OTHER_MESSAGE"}
    used = {node.attr for node in ast.walk(hotkey_source())
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "adapter"}
    assert used <= allowed, f"hotkey.py may not use adapter.{used - allowed}"
    forbidden = {"launch_app", "request_close", "request_window_state", "click", "send_character",
                 "send_shortcut", "send_wheel_notch", "release_keys"}
    assert not used & forbidden


def test_the_hotkey_module_watches_no_other_keys():
    """RegisterHotKey is the whole point: no hook, no key polling, no keystroke ever seen."""
    source = Path(hotkey.__file__).read_text(encoding="utf-8")
    for api in ("SetWindowsHookEx", "WH_KEYBOARD", "GetAsyncKeyState", "GetKeyState", "GetKeyboardState",
                "keybd_event", "ReadConsoleInput", "SendInput"):
        assert api not in source, f"{api} must not appear in hotkey.py"
    modules = {node.module or "" for node in ast.walk(hotkey_source()) if isinstance(node, ast.ImportFrom)}
    modules |= {alias.name for node in ast.walk(hotkey_source()) if isinstance(node, ast.Import)
                for alias in node.names}
    assert not any(module.split(".")[0] == "ctypes" for module in modules), "only the adapter may use ctypes"


def test_the_hotkey_module_only_triggers_the_stop():
    used = {node.attr for node in ast.walk(hotkey_source())
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "emergency_stop"}
    assert used == {"trigger"}, f"hotkey.py may only trigger the stop, not {used}"


def test_the_console_never_starts_stops_or_fires_the_hotkey():
    used = {node.attr for node in ast.walk(ast.parse(Path(console.__file__).read_text(encoding="utf-8")))
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "hotkey"}
    assert used == {"status"}, f"the console may only read the hotkey's status, not {used}"


def test_the_console_says_what_to_press(world):
    active = hotkey.HotkeyStatus(ACTIVE, "Ctrl+Alt+Backspace")
    assert console._hotkey_line(active) == ("Emergency stop: press Ctrl+Alt+Backspace at any time - it works even "
                                            "when this window isn't in front.")
    missing = hotkey.HotkeyStatus(UNAVAILABLE, "Ctrl+Alt+Backspace", "another program has already registered it")
    line = console._hotkey_line(missing)
    assert "is NOT active" in line and "another program" in line and "Nothing can interrupt" in line
