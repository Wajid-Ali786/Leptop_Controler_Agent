"""
The ONLY file in this project allowed to import pyautogui, pywinauto or playwright, to
start processes, or to send messages to other applications' windows (CLAUDE.md rule 2).

Closing is always a polite request - the same message as clicking a window's X button - so the
app can save or ask about unsaved work. Nothing here ends a process.

Minimize, maximize and restore are polite requests too: WM_SYSCOMMAND with SC_MINIMIZE, SC_MAXIMIZE or
SC_RESTORE, exactly what the window's title-bar buttons send, so the app handles them its own way.

Clicking uses pyautogui, imported only when a click is actually sent. Its fail-safe stays ON: if the
mouse pointer is in a corner of the main screen, pyautogui refuses to act - a physical emergency
stop - and that is reported as MouseFailSafeError. pyautogui's built-in pause after each action is
skipped (the Executor's own checkpoints decide timing). Coordinates are real screen pixels on every
monitor: the process is made per-monitor DPI aware before pyautogui loads (the same call the
Verifier adapter makes before reading coordinates).

Typing uses the Windows SendInput API directly (ctypes), one character per call: each character is
sent as a Unicode character, not as keyboard keys, so the result doesn't depend on the keyboard
layout, Caps Lock or an input method, and any language or emoji types exactly. A line break is a real
Enter key press. Every call carries a key's down AND up events together, so an interruption between
characters never leaves a key held down. Typed text never appears in an error message.

Keyboard shortcuts use SendInput with virtual keys (plus scan codes), the WHOLE shortcut in one call:
modifiers down, key down, key up, modifiers up in reverse. Windows never mixes other input into the
events of one call. release_keys() is the defensive release: a key-up for every key involved, with an
unassigned key tapped first when Alt or Win is involved, so releasing them alone doesn't open the menu
bar or the Start menu.

Scrolling sends one mouse-wheel notch per SendInput call, at the pointer's current position (no
coordinates are sent and the pointer is never moved).

The global emergency-stop hotkey is registered with RegisterHotKey, so Windows delivers ONLY that
one combination to us: no keyboard hook, no key polling, no other keystroke ever seen. Those
functions send nothing and change nothing; app/executor/hotkey.py is the only caller, and a press
does exactly one thing - the existing emergency stop.

Every other function here performs a real action on the computer, so nothing may call it except
app/executor/logic.py, which routes every action through app/safety first (CLAUDE.md rule 5).
"""
import ctypes
import shutil
import subprocess
import sys

_WINDOWS_ELEVATION_REQUIRED = 740  # ERROR_ELEVATION_REQUIRED
_WINDOWS_ACCESS_DENIED = 5         # ERROR_ACCESS_DENIED - e.g. the window belongs to an elevated app
_WINDOWS_INVALID_WINDOW = 1400     # ERROR_INVALID_WINDOW_HANDLE - the window is already gone
_WM_CLOSE = 0x0010
_PER_MONITOR_AWARE_V2 = -4  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2


class ExecutorAdapterError(Exception):
    """A real action could not be carried out. Messages are safe to show the user."""


class AppNotFoundError(ExecutorAdapterError):
    """The application isn't installed, or can't be found on this computer."""


class AppLaunchError(ExecutorAdapterError):
    """The application exists but couldn't be started."""


class WindowGoneError(ExecutorAdapterError):
    """The window closed before the close request could be sent."""


class WindowCloseError(ExecutorAdapterError):
    """The close request couldn't be sent. The message completes "I couldn't ask it to close: ..."."""


class MouseFailSafeError(ExecutorAdapterError):
    """pyautogui's fail-safe fired: the pointer is in a corner of the main screen (the manual abort)."""


class ClickError(ExecutorAdapterError):
    """The click couldn't be sent. The message completes "I couldn't click: ..."."""


class WindowControlError(ExecutorAdapterError):
    """A minimize/maximize/restore request couldn't be sent. The message completes "I couldn't ...: ..."."""


class ShortcutError(ExecutorAdapterError):
    """A shortcut couldn't be attempted at all (e.g. not on Windows). Nothing was sent."""


class TypingError(ExecutorAdapterError):
    """Windows didn't accept the keyboard input for one character. `partly_sent` is True when some of
    that character's events went through, so the character may have been typed."""

    def __init__(self, message: str, partly_sent: bool = False):
        super().__init__(message)
        self.partly_sent = partly_sent


def launch_app(executable: str) -> int:
    """Start `executable` (e.g. "notepad.exe") without a shell; return its process id."""
    path = shutil.which(executable)
    if path is None:
        raise AppNotFoundError(f"'{executable}' isn't installed or can't be found on this computer.")
    try:
        process = subprocess.Popen(
            [path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except OSError as exc:
        if getattr(exc, "winerror", None) == _WINDOWS_ELEVATION_REQUIRED:
            raise AppLaunchError(
                f"'{executable}' needs administrator rights, and the assistant doesn't run elevated."
            ) from None
        raise AppLaunchError(f"'{executable}' could not be started ({type(exc).__name__}).") from None
    return process.pid


def request_close(handle: int) -> None:
    """Ask the window `handle` to close, exactly like clicking its X button (WM_CLOSE). Returns once
    the request is queued; the app decides what happens next, e.g. asking to save changes."""
    if sys.platform != "win32":
        raise WindowCloseError("closing windows is only supported on Windows")
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    if user32.PostMessageW(handle, _WM_CLOSE, 0, 0):
        return
    error = ctypes.get_last_error()
    if error == _WINDOWS_INVALID_WINDOW:
        raise WindowGoneError("the window is already closed")
    if error == _WINDOWS_ACCESS_DENIED:
        raise WindowCloseError("it runs with administrator rights, and the assistant doesn't run elevated")
    raise WindowCloseError(f"Windows refused the request (error {error})")


def click(x: int, y: int) -> None:
    """Move the pointer to screen point (x, y) and send one left click there. Returns once Windows has
    accepted the input; what the click does is up to whatever is under the pointer."""
    if sys.platform != "win32":
        raise ClickError("clicking is only supported on Windows")
    _use_physical_pixels()
    try:
        import pyautogui
    except Exception as exc:  # e.g. the package is missing or broken
        raise ClickError(f"the mouse library couldn't be loaded ({type(exc).__name__})") from None
    pyautogui.FAILSAFE = True
    try:
        pyautogui.click(x, y, button="left", _pause=False)
    except pyautogui.FailSafeException:
        raise MouseFailSafeError("the mouse pointer is in a corner of the main screen (the manual abort)") from None
    except Exception as exc:
        raise ClickError(f"Windows didn't accept the click ({type(exc).__name__})") from None


def _use_physical_pixels() -> None:
    """Make coordinates real pixels on every monitor. Harmless if the process is already DPI aware."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetProcessDpiAwarenessContext.restype = ctypes.c_int
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(_PER_MONITOR_AWARE_V2))


_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_VK_RETURN = 0x0D


def send_character(character: str) -> None:
    """Type one character into whatever has keyboard focus: "\\n" presses Enter; anything else is sent
    as that exact Unicode character. Raises TypingError if Windows doesn't accept it."""
    if sys.platform != "win32":
        raise TypingError("typing is only supported on Windows")
    if len(character) != 1:
        raise TypingError("exactly one character must be sent at a time")
    api = _keyboard_api()
    if character == "\n":
        events = [(_VK_RETURN, 0, 0), (_VK_RETURN, 0, _KEYEVENTF_KEYUP)]
    else:
        encoded = character.encode("utf-16-le")
        units = [int.from_bytes(encoded[i:i + 2], "little") for i in range(0, len(encoded), 2)]
        events = [event for unit in units  # both halves of an emoji go in the same call
                  for event in ((0, unit, _KEYEVENTF_UNICODE), (0, unit, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP))]
    inputs = (api.Input * len(events))()
    for item, (virtual_key, scan, flags) in zip(inputs, events):
        item.type = _INPUT_KEYBOARD
        item.ki.wVk, item.ki.wScan, item.ki.dwFlags = virtual_key, scan, flags
    sent = api.SendInput(len(events), inputs, ctypes.sizeof(api.Input))
    if sent != len(events):
        raise TypingError("Windows didn't accept the keyboard input", partly_sent=sent > 0)


class _KeyboardApi:
    def __init__(self):
        from ctypes import wintypes

        class KeyboardInput(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class MouseInput(ctypes.Structure):  # only here so the union has Windows' real size
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class _Union(ctypes.Union):
            _fields_ = [("ki", KeyboardInput), ("mi", MouseInput)]

        class Input(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", _Union)]

        self.Input = Input
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
        user32.SendInput.restype = wintypes.UINT
        user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
        user32.MapVirtualKeyW.restype = wintypes.UINT
        self.SendInput = user32.SendInput
        self.MapVirtualKeyW = user32.MapVirtualKeyW


_keyboard = None


def _keyboard_api() -> _KeyboardApi:
    global _keyboard
    if _keyboard is None:
        _keyboard = _KeyboardApi()
    return _keyboard


_KEYEVENTF_EXTENDEDKEY = 0x0001
_UNASSIGNED = "(unassigned)"
_NAMED_KEYS = {"Ctrl": 0x11, "Alt": 0x12, "Shift": 0x10, "Win": 0x5B, "Tab": 0x09, "Esc": 0x1B, "Enter": 0x0D,
               "Space": 0x20, "Delete": 0x2E, _UNASSIGNED: 0xE8}
_EXTENDED_KEYS = {"Win", "Delete"}


def send_shortcut(modifiers: tuple[str, ...], key: str) -> tuple[int, int]:
    """Press a shortcut in ONE SendInput call: modifiers down (in order), key down, key up, modifiers up
    (in reverse). Returns (events Windows accepted, events sent)."""
    events = ([(m, False) for m in modifiers] + [(key, False), (key, True)]
              + [(m, True) for m in reversed(modifiers)])
    return _send_keys(events), len(events)


def release_keys(modifiers: tuple[str, ...], key: str) -> bool:
    """Defensive release of every key in a shortcut. Returns True if Windows accepted all of it."""
    events = [(key, True)]
    if "Alt" in modifiers or "Win" in modifiers:  # so a lone Alt/Win release opens no menu
        events += [(_UNASSIGNED, False), (_UNASSIGNED, True)]
    events += [(m, True) for m in reversed(modifiers)]
    return _send_keys(events) == len(events)


def _virtual_key(name: str) -> int:
    if name in _NAMED_KEYS:
        return _NAMED_KEYS[name]
    if len(name) == 1 and name.isascii() and name.isalnum():
        return ord(name.upper())
    if name.startswith("F") and name[1:].isdigit() and 1 <= int(name[1:]) <= 24:
        return 0x70 + int(name[1:]) - 1
    raise ShortcutError(f"the key {name} has no key code")


def _send_keys(events: list[tuple[str, bool]]) -> int:
    """Send (key name, is_key_up) events in one SendInput call; returns how many Windows accepted."""
    if sys.platform != "win32":
        raise ShortcutError("keyboard shortcuts are only supported on Windows")
    api = _keyboard_api()
    inputs = (api.Input * len(events))()
    for item, (name, key_up) in zip(inputs, events):
        virtual_key = _virtual_key(name)
        item.type = _INPUT_KEYBOARD
        item.ki.wVk = virtual_key
        item.ki.wScan = api.MapVirtualKeyW(virtual_key, 0) & 0xFFFF
        item.ki.dwFlags = (_KEYEVENTF_KEYUP if key_up else 0) | (_KEYEVENTF_EXTENDEDKEY if name in _EXTENDED_KEYS else 0)
    return int(api.SendInput(len(events), inputs, ctypes.sizeof(api.Input)))


_INPUT_MOUSE = 0
_MOUSEEVENTF_WHEEL = 0x0800
_WHEEL_DELTA = 120  # one notch of a standard mouse wheel


def send_wheel_notch(up: bool) -> bool:
    """One vertical wheel notch at the pointer's position: up (away from the user) or down. Returns
    True if Windows accepted it."""
    if sys.platform != "win32":
        raise ShortcutError("scrolling is only supported on Windows")
    api = _keyboard_api()
    inputs = (api.Input * 1)()
    inputs[0].type = _INPUT_MOUSE
    inputs[0].mi.dwFlags = _MOUSEEVENTF_WHEEL
    inputs[0].mi.mouseData = _WHEEL_DELTA if up else (-_WHEEL_DELTA) & 0xFFFFFFFF
    return api.SendInput(1, inputs, ctypes.sizeof(api.Input)) == 1


_WM_SYSCOMMAND = 0x0112
_SYSTEM_COMMANDS = {"minimize": 0xF020, "maximize": 0xF030, "restore": 0xF120}  # SC_MINIMIZE / SC_MAXIMIZE / SC_RESTORE


def request_window_state(handle: int, operation: str) -> None:
    """Ask window `handle` to minimize, maximize or restore, like clicking its title-bar button. Returns once
    the request is queued; the Verifier reads the resulting state."""
    if sys.platform != "win32":
        raise WindowControlError("window controls are only supported on Windows")
    command = _SYSTEM_COMMANDS[operation]
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    if user32.PostMessageW(handle, _WM_SYSCOMMAND, command, 0):
        return
    error = ctypes.get_last_error()
    if error == _WINDOWS_INVALID_WINDOW:
        raise WindowGoneError("the window is already closed")
    if error == _WINDOWS_ACCESS_DENIED:
        raise WindowControlError("it runs with administrator rights, and the assistant doesn't run elevated")
    raise WindowControlError(f"Windows refused the request (error {error})")


# --- Global emergency-stop hotkey -------------------------------------------------------------
# RegisterHotKey is deliberate: Windows delivers ONLY the one registered combination to us, so no
# keyboard hook and no key polling is needed and no other keystroke is ever seen. These functions
# send nothing and change nothing on the desktop; app/executor/hotkey.py owns the thread that uses
# them, and the only thing a press does is call the existing emergency stop (CLAUDE.md rule 4).

_WM_QUIT = 0x0012
_WM_HOTKEY = 0x0312
_WM_USER = 0x0400
_PM_NOREMOVE = 0x0000
_WINDOWS_HOTKEY_TAKEN = 1409  # ERROR_HOTKEY_ALREADY_REGISTERED

HOTKEY_MESSAGE, QUIT_MESSAGE, OTHER_MESSAGE = "hotkey", "quit", "other"


class HotkeyError(ExecutorAdapterError):
    """A global hotkey couldn't be registered or watched."""


class _HotkeyApi:
    def __init__(self):
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.UnregisterHotKey.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = ctypes.c_int  # -1 is a real failure, so this must NOT be BOOL
        user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
                                        wintypes.UINT]
        user32.PeekMessageW.restype = wintypes.BOOL
        user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostThreadMessageW.restype = wintypes.BOOL
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self.user32, self.kernel32, self.MSG = user32, kernel32, wintypes.MSG


_hotkey_api = None


def _hotkey() -> _HotkeyApi:
    global _hotkey_api
    if sys.platform != "win32":
        raise HotkeyError("a global hotkey is only supported on Windows")
    if _hotkey_api is None:
        _hotkey_api = _HotkeyApi()
    return _hotkey_api


def current_thread_id() -> int:
    """The Windows id of the calling thread - the thread a hotkey is delivered to."""
    return int(_hotkey().kernel32.GetCurrentThreadId())


def create_message_queue() -> None:
    """Make Windows create this thread's message queue, so a WM_QUIT posted to it can't be lost."""
    api = _hotkey()
    message = api.MSG()
    api.user32.PeekMessageW(ctypes.byref(message), None, _WM_USER, _WM_USER, _PM_NOREMOVE)


def register_hotkey(hotkey_id: int, modifiers: int, virtual_key: int) -> None:
    """Register one system-wide hotkey for the CALLING thread; its presses arrive as messages there."""
    api = _hotkey()
    if api.user32.RegisterHotKey(None, hotkey_id, modifiers, virtual_key):
        return
    error = ctypes.get_last_error()
    if error == _WINDOWS_HOTKEY_TAKEN:
        raise HotkeyError("another program has already registered that key combination")
    raise HotkeyError(f"Windows refused the key combination (error {error})")


def unregister_hotkey(hotkey_id: int) -> bool:
    """Give the hotkey back. Must run on the thread that registered it. Never raises."""
    try:
        return bool(_hotkey().user32.UnregisterHotKey(None, hotkey_id))
    except Exception:  # shutting down must never fail because of this
        return False


def wait_for_hotkey_message() -> tuple[str, int | None]:
    """Block until this thread gets a message: ("hotkey", id), ("quit", None) for WM_QUIT, or
    ("other", None). Raises HotkeyError if Windows reports a failure, which must end the loop."""
    api = _hotkey()
    message = api.MSG()
    result = int(api.user32.GetMessageW(ctypes.byref(message), None, 0, 0))
    if result == -1:  # a real error: the caller must stop, never loop on this
        raise HotkeyError(f"Windows stopped delivering messages (error {ctypes.get_last_error()})")
    if result == 0:
        return QUIT_MESSAGE, None
    if message.message == _WM_HOTKEY:
        return HOTKEY_MESSAGE, int(message.wParam)
    return OTHER_MESSAGE, None


def post_quit_to_thread(thread_id: int) -> bool:
    """Ask the listener thread to end its message loop. False if it is already gone. Never raises."""
    try:
        return bool(_hotkey().user32.PostThreadMessageW(thread_id, _WM_QUIT, 0, 0))
    except Exception:
        return False
