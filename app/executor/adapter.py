"""
The ONLY file in this project allowed to import pyautogui, pywinauto or playwright, to
start processes, or to send messages to other applications' windows (CLAUDE.md rule 2).

Closing is always a polite request - the same message as clicking a window's X button - so the
app can save or ask about unsaved work. Nothing here ends a process.

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

Every function here performs a real action on the computer, so nothing may call it except
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
        self.SendInput = user32.SendInput


_keyboard = None


def _keyboard_api() -> _KeyboardApi:
    global _keyboard
    if _keyboard is None:
        _keyboard = _KeyboardApi()
    return _keyboard
