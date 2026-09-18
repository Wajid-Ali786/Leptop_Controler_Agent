"""
The global emergency-stop hotkey (docs/step4 Section 4): one key combination, pressed anywhere,
that stops whatever the assistant is doing - even while another application is in front and the
assistant is typing or scrolling into it.

    with hotkey.listening():   # main.py --console
        run_console()

A press does exactly ONE thing: emergency_stop.trigger("global-hotkey"). There is no second stop
mechanism - the Executor's existing checkpoints, ActionInterruptedError and TypingInterruptedError
stay authoritative, and nothing here ever resets the stop.

Why RegisterHotKey and not a keyboard hook: Windows delivers only the one registered combination
to the listener thread. No other keystroke is seen, recorded or logged, no text is captured, and
this module sends no keyboard or mouse input and performs no desktop action. It uses exactly five
functions of the Executor adapter - register, unregister, wait, post-quit and thread id - and a
rule test checks it can't reach any of the adapter's acting functions.

The listener is one daemon thread: it creates its message queue, registers the combination from
`executor.emergency_stop_hotkey`, then blocks in GetMessageW. stop() posts WM_QUIT to it, and the
hotkey is given back on the same thread that registered it. It can never keep the app alive, and a
failure (another program already owns the combination, a bad setting, Windows refusing) is reported
through status() - never raised at the caller and never fatal.
"""
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

from app.executor import adapter, emergency_stop
from config.settings import SettingsError, get_setting

SOURCE = "global-hotkey"  # what emergency_stop.status() reports after a press
HOTKEY_ID = 1             # this process registers exactly one hotkey
SETTING = "executor.emergency_stop_hotkey"
START_TIMEOUT_SECONDS = 2.0
STOP_TIMEOUT_SECONDS = 1.0

# States
ACTIVE = "active"            # registered and watching
UNAVAILABLE = "unavailable"  # never registered (taken, refused, or badly configured)
FAILED = "failed"            # it was active and then broke
STOPPED = "stopped"          # shut down normally (or never started)

_MODIFIERS = {"Ctrl": 0x0002, "Alt": 0x0001, "Shift": 0x0004, "Win": 0x0008}  # MOD_CONTROL / ALT / SHIFT / WIN
_MOD_NOREPEAT = 0x4000  # holding the keys down must not fire again and again
_ORDER = ("Ctrl", "Alt", "Shift", "Win")
# Deliberately only keys that type nothing, so the combination can't collide with typing (and with
# AltGr, which is Ctrl+Alt, on international layouts).
_KEYS = {"Backspace": 0x08, "Esc": 0x1B, "Insert": 0x2D, "Pause": 0x13,
         **{f"F{n}": 0x70 + n - 1 for n in range(1, 25)}}
_ALIASES = {"control": "Ctrl", "ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win", "windows": "Win",
            "escape": "Esc", "esc": "Esc", "backspace": "Backspace", "insert": "Insert", "pause": "Pause",
            "break": "Pause"}
# Windows keeps these for itself: RegisterHotKey would never see them, so say why rather than fail oddly.
_RESERVED = {"Ctrl+Alt+Delete", "Ctrl+Shift+Esc", "Win+L"}
_RESERVED_ALIASES = {"del": "Delete", "delete": "Delete", "l": "L"}  # only so a reserved combination is recognised

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Hotkey:
    """A combination this module may register."""
    name: str          # normalized, e.g. "Ctrl+Alt+Backspace" - safe to show and log
    modifiers: int     # MOD_* flags, including MOD_NOREPEAT
    virtual_key: int


@dataclass(frozen=True)
class HotkeyRefusal:
    """The configured combination can't be used. Nothing is registered."""
    message: str


@dataclass(frozen=True)
class HotkeyStatus:
    """What the listener is doing. `hotkey` is safe to show the user."""
    state: str
    hotkey: str = ""
    reason: str = ""                     # why it isn't active
    presses: int = 0                     # how many times the hotkey has fired
    # The last press, on time.perf_counter() - the high-resolution clock, because time.monotonic()
    # only moves in ~15.6 ms steps on Windows, which is coarser than the thing being measured.
    received_at: float | None = None     # when the press arrived
    triggered_at: float | None = None    # when the emergency stop had been triggered
    received_wall: float | None = None   # the same press on the wall clock (time.time())

    @property
    def active(self) -> bool:
        return self.state == ACTIVE


def parse(text) -> Hotkey | HotkeyRefusal:
    """The Hotkey named by `text` (e.g. "Ctrl+Alt+Backspace"), or a refusal saying why not."""
    if not isinstance(text, str) or not text.strip():
        return HotkeyRefusal(f"Setting '{SETTING}' must name a key combination, e.g. Ctrl+Alt+Backspace.")
    parts = [part.strip() for part in text.split("+")]
    if any(not part for part in parts):
        return HotkeyRefusal(f"I can't read the hotkey '{text.strip()}': join key names with +, "
                             f"e.g. Ctrl+Alt+Backspace.")
    reserved = _reserved_name(parts)  # checked first, so the reason is the real one
    if reserved:
        return HotkeyRefusal(f"{reserved} is reserved by Windows, which never passes it to a program.")
    modifiers, keys = [], []
    for part in parts:
        name = _ALIASES.get(part.lower()) or _canonical(part)
        if name is None:
            return HotkeyRefusal(f"I don't know a key called '{part}'. The emergency-stop hotkey may use "
                                 f"Ctrl, Alt, Shift and Win with one of: {', '.join(sorted(_KEYS))}.")
        (modifiers if name in _MODIFIERS else keys).append(name)
    if len(set(modifiers)) != len(modifiers):
        return HotkeyRefusal(f"'{text.strip()}' names the same modifier twice.")
    if len(keys) != 1:
        return HotkeyRefusal(f"'{text.strip()}' needs exactly one key besides Ctrl, Alt, Shift or Win.")
    if not modifiers:
        return HotkeyRefusal(f"'{text.strip()}' needs at least one of Ctrl, Alt, Shift or Win, so it can't be "
                             f"pressed by accident.")
    name = "+".join([m for m in _ORDER if m in modifiers] + keys)
    flags = _MOD_NOREPEAT
    for modifier in modifiers:
        flags |= _MODIFIERS[modifier]
    return Hotkey(name, flags, _KEYS[keys[0]])


def configured() -> Hotkey | HotkeyRefusal:
    """The configured hotkey, or a refusal. Never raises, so a bad setting can't stop the app starting."""
    try:
        return parse(get_setting(SETTING))
    except SettingsError as exc:
        return HotkeyRefusal(str(exc))
    except Exception as exc:  # an unreadable config must not crash the console
        return HotkeyRefusal(f"the emergency-stop hotkey setting couldn't be read ({type(exc).__name__})")


def configured_name() -> str:
    """What to call the hotkey on screen - the configured combination, or why there isn't one."""
    chosen = configured()
    return chosen.name if isinstance(chosen, Hotkey) else "(not configured)"


class _Listener:
    """One daemon thread: register, wait for presses, give the hotkey back."""

    def __init__(self, hotkey: Hotkey):
        self.hotkey = hotkey
        self.thread = None
        self.thread_id = None
        self.ready = threading.Event()
        self.stopping = False
        self._lock = threading.Lock()
        self._status = HotkeyStatus(STOPPED, hotkey.name)

    # --- state ---
    def status(self) -> HotkeyStatus:
        with self._lock:
            return self._status

    def _set(self, state: str, reason: str = "") -> None:
        with self._lock:
            self._status = HotkeyStatus(state, self.hotkey.name, reason, self._status.presses,
                                        self._status.received_at, self._status.triggered_at,
                                        self._status.received_wall)

    def _record_press(self, received: float, triggered: float, wall: float) -> None:
        with self._lock:
            self._status = HotkeyStatus(self._status.state, self.hotkey.name, self._status.reason,
                                        self._status.presses + 1, received, triggered, wall)

    # --- lifecycle ---
    def start(self) -> HotkeyStatus:
        self.thread = threading.Thread(target=self._run, name="emergency-stop-hotkey", daemon=True)
        self.thread.start()
        if not self.ready.wait(START_TIMEOUT_SECONDS):
            self._set(FAILED, "the listener didn't start in time")
            log.error("Emergency-stop hotkey listener didn't start within %.1fs", START_TIMEOUT_SECONDS)
        return self.status()

    def stop(self) -> None:
        self.stopping = True
        self.ready.wait(START_TIMEOUT_SECONDS)  # so the thread id and registration are settled
        thread_id = self.thread_id
        if thread_id and not adapter.post_quit_to_thread(thread_id):
            log.info("Emergency-stop hotkey listener was already gone when asked to stop")
        if self.thread is not None:
            self.thread.join(STOP_TIMEOUT_SECONDS)  # a daemon thread can never keep the app alive
            if self.thread.is_alive():
                log.warning("Emergency-stop hotkey listener didn't finish in time; leaving it to process exit")

    def _run(self) -> None:
        registered = False
        try:
            self.thread_id = adapter.current_thread_id()
            adapter.create_message_queue()  # so a WM_QUIT posted to this thread can't be lost
            adapter.register_hotkey(HOTKEY_ID, self.hotkey.modifiers, self.hotkey.virtual_key)
            registered = True
            self._set(ACTIVE)  # only now, so start() never reports a hotkey that isn't registered
        except adapter.ExecutorAdapterError as exc:
            self._set(UNAVAILABLE, str(exc))
            log.warning("Emergency-stop hotkey %s is unavailable: %s", self.hotkey.name, exc)
        except Exception as exc:  # never let the listener's failure escape into the app
            self._set(UNAVAILABLE, f"the listener couldn't start ({type(exc).__name__})")
            log.error("Emergency-stop hotkey listener couldn't start (%s)", type(exc).__name__)
        finally:
            self.ready.set()
        if not registered:
            return
        log.info("Emergency-stop hotkey %s is active", self.hotkey.name)
        try:
            self._loop()
        except adapter.ExecutorAdapterError as exc:  # GetMessageW failed: end the loop, never spin on it
            self._set(FAILED, str(exc))
            log.error("Emergency-stop hotkey listener stopped: %s", exc)
        except Exception as exc:
            self._set(FAILED, f"the listener stopped ({type(exc).__name__})")
            log.error("Emergency-stop hotkey listener stopped (%s)", type(exc).__name__)
        finally:
            if registered:
                adapter.unregister_hotkey(HOTKEY_ID)
            if self.status().state == ACTIVE:
                self._set(STOPPED)

    def _loop(self) -> None:
        while not self.stopping:  # a stop during startup ends the loop before it blocks
            kind, pressed_id = adapter.wait_for_hotkey_message()
            if kind == adapter.QUIT_MESSAGE:
                return
            if kind == adapter.HOTKEY_MESSAGE and pressed_id == HOTKEY_ID:
                self._press()

    def _press(self) -> None:
        """The whole behaviour of a press: trigger the one emergency stop, once. Never resets it, sends
        nothing, and repeating it is harmless (trigger keeps the first stop)."""
        received, wall = time.perf_counter(), time.time()
        emergency_stop.trigger(SOURCE)
        self._record_press(received, time.perf_counter(), wall)


_listener: _Listener | None = None
_unavailable: HotkeyStatus | None = None  # a combination that couldn't even be tried, kept so status() says why
_lock = threading.Lock()


def start() -> HotkeyStatus:
    """Start watching for the configured hotkey. Never raises: a combination that can't be registered
    is reported through the returned status, and the app carries on without it."""
    global _listener, _unavailable
    with _lock:
        if _listener is not None and _listener.thread is not None and _listener.thread.is_alive():
            return _listener.status()  # already listening: never register the same combination twice
        chosen = configured()
        if isinstance(chosen, HotkeyRefusal):
            _listener = None
            _unavailable = HotkeyStatus(UNAVAILABLE, configured_name(), chosen.message)
            log.warning("No emergency-stop hotkey: %s", chosen.message)
            return _unavailable
        _unavailable = None
        _listener = _Listener(chosen)
        return _listener.start()


def stop() -> None:
    """Stop watching and give the hotkey back. Harmless if it was never started; never raises."""
    global _listener, _unavailable
    with _lock:
        listener, _listener, _unavailable = _listener, None, None
    if listener is not None:
        listener.stop()


def status() -> HotkeyStatus:
    """What the listener is doing right now."""
    with _lock:
        listener, unavailable = _listener, _unavailable
    if listener is not None:
        return listener.status()
    return unavailable or HotkeyStatus(STOPPED, configured_name())


@contextmanager
def listening():
    """Watch for the hotkey for as long as the block runs."""
    start()
    try:
        yield status()
    finally:
        stop()


def _reserved_name(parts: list[str]) -> str | None:
    """The combination Windows keeps for itself that `parts` names, if any. It reads the parts loosely
    (Delete and L are not keys this module could ever use) so the refusal can say the real reason."""
    names = [_ALIASES.get(part.lower()) or _RESERVED_ALIASES.get(part.lower()) or _canonical(part) or part.title()
             for part in parts]
    rendered = "+".join([m for m in _ORDER if m in names] + [n for n in names if n not in _MODIFIERS])
    return rendered if rendered in _RESERVED else None


def _canonical(part: str) -> str | None:
    for name in (*_MODIFIERS, *_KEYS):
        if part.lower() == name.lower():
            return name
    return None
