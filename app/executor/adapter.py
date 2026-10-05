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

Window ownership is marked with a Windows window property (SetPropW/GetPropW/RemovePropW), because a
property belongs to the window OBJECT and dies with it, while a handle number can be handed to a new
window later. tag_window/window_token/untag_window are how "the assistant opened this window" is
recorded and re-checked; new_window_token() is the one pure function here and touches nothing.

Every other function here performs a real action on the computer, so nothing may call it except
app/executor/logic.py, which routes every action through app/safety first (CLAUDE.md rule 5).
"""
import ctypes
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

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


def activate_window(handle: int) -> bool:
    """Ask Windows to bring the window `handle` to the front, with SetForegroundWindow and nothing else.

    Returns whether Windows ACCEPTED the request. Acceptance is not arrival: SetForegroundWindow can
    return true and the window still not end up in front, and Windows may refuse outright when another
    process owns the foreground. So the caller verifies the foreground separately, by handle - this
    function never polls, because waiting belongs in logic alongside the emergency-stop checkpoints.

    DELIBERATELY ONE API. No SwitchToThisWindow, no BringWindowToTop, no AttachThreadInput, no
    ShowWindow, no synthetic Alt+Tab. Those are the ways to force a foreground change past the rules
    Windows applies on purpose, and a refusal here is a refusal worth reporting rather than defeating:
    the caller's answer is to ask the user, which is the behaviour this replaced."""
    if sys.platform != "win32":
        raise WindowControlError("bringing a window to the front is only supported on Windows")
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    if not user32.IsWindow(handle):
        raise WindowGoneError("the window is already closed")
    return bool(user32.SetForegroundWindow(handle))


# --- window ownership -------------------------------------------------------------------
# The assistant may close only windows it opened, and a handle NUMBER cannot carry that fact: Windows
# reuses handle numbers, so a window we never opened can later be given a number we recorded and be
# closed as ours. A window PROPERTY can carry it, because the property list belongs to the window
# OBJECT - when a window is destroyed "the system will call RemoveProp on your behalf", so a new window
# that inherits the number never inherits the property.
#
# SetPropW and RemovePropW are restricted by User Interface Privilege Isolation: they work on a window
# belonging to a process of lesser or equal integrity level and fail with ERROR_ACCESS_DENIED (5)
# otherwise. That failure is safe - a window we cannot tag simply never becomes closable. No code is
# injected into the target process; Windows itself keeps the property list.
_OWNERSHIP_PROPERTY = "AIDesktopCompanion.WindowOwnership"  # fixed and internal, never user-configurable
_TOKEN_BITS = ctypes.sizeof(ctypes.c_void_p) * 8 - 2  # fits a HANDLE, and stays positive read as signed


def new_window_token() -> int:
    """An unpredictable ownership token for ONE group of windows, sized to this process's native pointer
    width. Always odd, so it is never zero and can never be confused with GetPropW's NULL ("no such
    property"). Touches nothing: process-local, never persisted, never logged, never shown."""
    return (secrets.randbits(_TOKEN_BITS) << 1) | 1


def tag_window(handle: int, token: int) -> bool:
    """Attach `token` to the window `handle` as proof the assistant opened it, then read it back and
    report success only if it returns identical.

    Returns False - never raises - for every failure, so a window that cannot be PROVED ours never
    becomes closable. The read-back is what makes a wrong prototype, a truncated value or a silently
    rejected property fail closed instead of registering ownership that can't be re-checked later."""
    user32 = _property_api()
    if user32 is None:
        return False
    if not user32.SetPropW(handle, _OWNERSHIP_PROPERTY, token):
        # Read it to clear it: ctypes keeps the last error from this call, and leaving ours behind could
        # be misread by the next adapter call. UIPI gives 5 for a higher-integrity window; every failure
        # is handled identically, and the code is never reported because nothing may carry the token.
        ctypes.get_last_error()
        return False
    return user32.GetPropW(handle, _OWNERSHIP_PROPERTY) == token


def window_token(handle: int) -> int | None:
    """The ownership token attached to the window `handle`, or None if it carries none - which includes
    every failure, so a window that can't be read is never treated as ours."""
    user32 = _property_api()
    if user32 is None:
        return None
    value = user32.GetPropW(handle, _OWNERSHIP_PROPERTY)
    return int(value) if value else None


def untag_window(handle: int, token: int) -> bool:
    """Drop our token from a window that is still alive, when ownership is deliberately given up.

    Removes the property ONLY when it currently holds exactly `token`, because Windows documents that an
    application "can remove only those properties it has added. It must not remove properties added by
    other applications or by the system itself". Not needed for safety - Windows removes the property
    when the window is destroyed - so this covers only the abandoning-a-live-window case."""
    user32 = _property_api()
    if user32 is None:
        return False
    if user32.GetPropW(handle, _OWNERSHIP_PROPERTY) != token:
        return False
    removed = user32.RemovePropW(handle, _OWNERSHIP_PROPERTY)
    return bool(removed) and int(removed) == token


def _property_api():
    """user32 with the three window-property functions declared exactly: HANDLE is pointer-width, and
    SetPropW returns BOOL - not a handle. None off Windows, where no window can be owned."""
    if sys.platform != "win32":
        return None
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetPropW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.HANDLE]
    user32.SetPropW.restype = wintypes.BOOL
    user32.GetPropW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    user32.GetPropW.restype = wintypes.HANDLE
    user32.RemovePropW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    user32.RemovePropW.restype = wintypes.HANDLE
    return user32


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


# --- Assistant-owned browser session (Phase 5 DOM layer, Build Plan Section 6.5) ----------------------
# PLAYWRIGHT LIVES HERE AND NOWHERE ELSE, and the reason is not symmetry with pywinauto - it is the
# opposite of it. UI Automation can only READ, so it sits in the Verifier's adapter. Playwright can read
# AND click, so it belongs in the one file allowed to control this computer. The DECISION about which
# element the user meant is still the Verifier's, as a pure function over what this module hands back
# (app/verifier/observation.py), so no module both decides and acts.
#
# NOT THE USER'S BROWSER. The session is started by us, with Playwright's own temporary profile, and it
# is owned ONLY because this registry has it. There is no launch_persistent_context, no user-data-dir,
# no Default or Profile N, no storage_state and no cookie import - so the user's Chrome profile, their
# logins, their cookies and their history are not reachable from here at all. A title is not ownership.
#
# WHAT LEAVES THIS MODULE: opaque ids and structure. No Playwright object, no accessible name, no page
# text, no HTML, no URL, no form value. The accessible name the user gave is matched INSIDE this module
# and dropped, exactly as the UIA layer does.

# The interactive roles a named target may resolve to, verified against this Playwright's own AriaRole
# list (82 roles; these nine are all present). A target is a NAME - "Login" - and the user should never
# have to say "button", so the name is tried against each of these and the matches are combined.
# Deliberately only interactive roles: a heading called Login is not something to click.
DOM_ROLES = ("button", "link", "checkbox", "radio", "textbox", "combobox", "option", "menuitem", "tab")

_browser_sessions: dict[str, "_BrowserSession"] = {}
_browser_lock = threading.Lock()


class BrowserError(ExecutorAdapterError):
    """A browser session could not be started, found or read."""


# What an element_token stands for, inside this module only.
#
# Slice 1 minted a random token that referred to NOTHING - it was a placeholder, so there was nothing
# to re-resolve from. This is the smallest change that fixes that: the token maps to the SEMANTIC
# DESCRIPTION that found the element, which is the user's own word plus a role, plus a fingerprint of
# the page it was found on.
#
# NOTE WHAT IS NOT STORED. `name` is the normalised word the USER asked for, handed in by the caller -
# it is not an accessible name read back off the page. No selector, no XPath, no element handle and no
# URL is kept: `page_identity` is a one-way digest, so a changed page can be detected without the
# address ever being retained, returned or logged.
@dataclass(frozen=True)
class _DomLocatorDescription:
    page_id: str
    role: str
    name: str                         # the USER's normalised word, never read from the page
    page_identity: str                # sha256 of the page's URL, truncated; the URL itself is not kept


@dataclass
class _BrowserSession:
    """The live Playwright objects for one assistant-owned browser. PRIVATE TO THIS MODULE.

    Nothing here is ever returned, logged or put in a model. Callers hold `session_id` and `page_id`
    strings; this is what those strings mean, and only this module can look them up."""
    runtime: object                   # the Playwright context manager
    browser: object
    context: object
    pages: dict                       # page_id -> Page
    tokens: dict                      # element_token -> _DomLocatorDescription


def browser_open_session(channel: str, launch_timeout_seconds: float,
                         headed: bool = False) -> tuple[str, str]:
    """Start an assistant-owned, NON-PERSISTENT browser and return (session_id, page_id).

    `channel` drives the INSTALLED browser - "chrome" uses the Chrome already on this computer, so no
    Playwright browser binary has to be downloaded. The context is non-persistent, which is what keeps
    the user's own profile out of reach: Playwright makes a throwaway one.

    `headed` asks for a VISIBLE window. It defaults to False because Playwright's own default is
    headless and the gated smokes were written against that; the user-facing OPEN_BROWSER path passes
    True, because a browser the user is expected to navigate themselves has to be one they can see.

    A partial failure leaves NOTHING in the registry and closes whatever had been started, so a caller
    can never be handed, or later find, a half-built session."""
    playwright_module = _playwright()
    runtime = browser = context = None
    try:
        runtime = playwright_module.sync_playwright().start()
        browser = runtime.chromium.launch(channel=channel, headless=not headed,
                                          timeout=max(1.0, launch_timeout_seconds) * 1000)
        context = browser.new_context()           # non-persistent, no storage_state, no user-data-dir
        context.set_default_timeout(max(1.0, launch_timeout_seconds) * 1000)
        page = context.new_page()
    except Exception as exc:
        _abandon(runtime, browser, context)
        raise BrowserError(f"a browser session could not be started ({type(exc).__name__})") from None
    session_id, page_id = secrets.token_hex(16), secrets.token_hex(8)
    with _browser_lock:
        _browser_sessions[session_id] = _BrowserSession(runtime=runtime, browser=browser,
                                                        context=context, pages={page_id: page},
                                                        tokens={})
    return session_id, page_id


def browser_close_session(session_id: str) -> bool:
    """Close the session and make its id unusable. True if there was one to close.

    The registry entry is removed FIRST, under the lock, so a caller racing with this cannot look the
    session up and act on objects that are about to be torn down."""
    with _browser_lock:
        session = _browser_sessions.pop(session_id, None)
    if session is None:
        return False
    _abandon(session.runtime, session.browser, session.context)
    return True


def browser_sessions() -> list:
    """Every live assistant-browser session, as opaque (session_id, page_id) pairs.

    THE SESSION REGISTRY IS THE MEMORY. There is no second store: a session is "the assistant's
    browser" because this registry has it, exactly as a window is "ours" because the ownership token
    is on it. Nothing live comes out - only the ids a caller may hold."""
    with _browser_lock:
        return sorted((session_id, page_id)
                      for session_id, session in _browser_sessions.items()
                      for page_id in session.pages)


def browser_session_exists(session_id: str) -> bool:
    """Whether the registry still has this session. The ONLY ownership test there is."""
    with _browser_lock:
        return session_id in _browser_sessions


def dom_query(session_id: str, page_id: str, normalized_name: str,
              query_timeout_seconds: float) -> list:
    """Every interactive control in that page whose accessible name IS the user's target.

    Returns DomElement - role, tag, state, an opaque token - and NO NAME, so an unmatched label cannot
    be returned, logged or sent anywhere. The accessible name is matched here and discarded.

    MATCHING IS WHOLE-STRING AND CASE-INSENSITIVE, done with an anchored case-insensitive regular
    expression rather than Playwright's `exact=True`. That is not a shortcut: Playwright documents
    `exact=True` as "case-sensitive and whole-string", and its default as "case-insensitive and
    searches for a substring" - so one would refuse "login" for a button called "Login", and the other
    would accept "Log" for it. A regex gives whole-string AND case-insensitive, and `exact` is
    documented as ignored when a pattern is passed, so it is not passed at all.

    Raises BrowserError for an unknown session or page - never a guess at a different one."""
    with _browser_lock:
        session = _browser_sessions.get(session_id)
        page = session.pages.get(page_id) if session is not None else None
    if session is None:
        raise BrowserError("that browser session is not open")
    if page is None:
        raise BrowserError("that page is not part of this browser session")
    if not isinstance(normalized_name, str) or not normalized_name.strip():
        return []

    timeout = max(1.0, query_timeout_seconds) * 1000
    observed_at = time.time()
    matches = _dom_matches(page, normalized_name, timeout)
    identity = _page_identity(page)
    found = []
    for role, locator in matches:
        element = _dom_element(session_id, page_id, role, locator, observed_at, timeout)
        with _browser_lock:
            # The token is only meaningful while this session lives, and it dies with it.
            session.tokens[element.element_token] = _DomLocatorDescription(
                page_id=page_id, role=role, name=normalized_name, page_identity=identity)
        found.append(element)
    return found


def _dom_matches(page, normalized_name: str, timeout: float) -> list:
    """Every (role, locator) in the top-level document whose accessible name IS `normalized_name`.

    THE ONLY MATCHING IMPLEMENTATION, used by the query and again by the action, so the two can never
    disagree about what "the Login button" means. Whole-string and case-insensitive, by anchored
    pattern - see dom_query for why Playwright's own `exact` flag is not what is wanted."""
    wanted = re.compile(rf"^{re.escape(normalized_name)}$", re.IGNORECASE)
    matches = []
    try:
        for role in DOM_ROLES:
            locator = page.get_by_role(role, name=wanted)
            for index in range(locator.count()):
                matches.append((role, locator.nth(index)))
    except Exception as exc:
        raise BrowserError(f"that page's controls could not be read ({type(exc).__name__})") from None
    return matches


def _page_identity(page) -> str:
    """A one-way fingerprint of the page's address, so "is this still the same page?" can be answered
    without the address itself ever being kept, returned or logged."""
    import hashlib

    try:
        return hashlib.sha256(str(page.url).encode("utf-8", "replace")).hexdigest()[:16]
    except Exception:
        return ""


# --- The one DOM action -------------------------------------------------------------------------------
# Outcomes rather than exceptions for the ordinary refusals, so the caller can report each honestly
# without parsing a message - and so no selector, name or URL has to travel in one. BrowserError stays
# for the genuinely exceptional: a session that is gone, or Playwright failing.

DOM_CLICKED = "clicked"
DOM_GONE = "gone"                    # nothing answers to that name here any more
DOM_AMBIGUOUS = "ambiguous"          # more than one does now
DOM_ROLE_CHANGED = "role_changed"    # one does, but it is a different kind of control
DOM_PAGE_CHANGED = "page_changed"    # the page itself is not the one that was confirmed
DOM_FRAMES_UNREAD = "frames_unread"  # not here, and parts of the page are frames we do not read


def dom_click(session_id: str, page_id: str, element_token: str,
              action_timeout_seconds: float) -> str:
    """Re-resolve the token's target and click it with Playwright. Returns one of the DOM_* outcomes.

    RE-RESOLUTION IS THE POINT, not a formality. An ElementHandle taken before the user answered a
    confirmation may be detached by the time they have; a locator is lazy and re-queries on use, which
    is why the token stands for a DESCRIPTION rather than a handle. The description is re-run here,
    and the click happens only if it still names exactly one control, of the same kind, on the same
    page.

    The click is Playwright's ordinary locator action, so its actionability checks apply: it waits for
    the element to be visible, enabled and stable, scrolls it into view, and verifies the element
    actually receives the event rather than something layered over it. `force` is never passed -
    passing it would skip exactly those checks."""
    with _browser_lock:
        session = _browser_sessions.get(session_id)
        if session is None:
            raise BrowserError("that browser session is not open")
        page = session.pages.get(page_id)
        description = session.tokens.get(element_token)
    if page is None:
        raise BrowserError("that page is not part of this browser session")
    if description is None or description.page_id != page_id:
        raise BrowserError("that target is not one I found on this page")

    if _page_identity(page) != description.page_identity:
        return DOM_PAGE_CHANGED

    timeout = max(1.0, action_timeout_seconds) * 1000
    matches = _dom_matches(page, description.name, timeout)
    if not matches:
        return DOM_FRAMES_UNREAD if _page_has_child_frames(page) else DOM_GONE
    if len(matches) > 1:
        return DOM_AMBIGUOUS
    role, locator = matches[0]
    if role != description.role:
        return DOM_ROLE_CHANGED
    try:
        locator.click(timeout=timeout)
    except Exception as exc:
        raise BrowserError(f"the click could not be delivered ({type(exc).__name__})") from None
    return DOM_CLICKED


def dom_page_has_frames(session_id: str, page_id: str) -> bool:
    """Whether the page has child frames this slice does not look inside.

    Used so a miss can be reported as "I did not look everywhere" rather than "it is not there" -
    Page.frames includes the main frame, so more than one means there are children."""
    with _browser_lock:
        session = _browser_sessions.get(session_id)
        page = session.pages.get(page_id) if session is not None else None
    if page is None:
        raise BrowserError("that browser session or page is not open")
    try:
        return _page_has_child_frames(page)
    except Exception as exc:
        raise BrowserError(f"that page could not be read ({type(exc).__name__})") from None


def _page_has_child_frames(page) -> bool:
    """Page.frames includes the main frame, so more than one means there are children."""
    try:
        return len(page.frames) > 1
    except Exception:
        return False


def _dom_element(session_id: str, page_id: str, role: str, locator, observed_at: float, timeout: float):
    """One matched control as structure. Anything unreadable degrades to a safe default rather than
    failing the whole query - a missing bounding box is not a reason to refuse to find a button."""
    from app.verifier.models import DomElement

    def safe(read, default):
        try:
            value = read()
            return default if value is None else value
        except Exception:
            return default

    box = safe(lambda: locator.bounding_box(timeout=timeout), None)
    bounds = None
    if isinstance(box, dict):
        bounds = safe(lambda: (int(box["x"]), int(box["y"]),
                               int(box["x"] + box["width"]), int(box["y"] + box["height"])), None)
    tag = str(safe(lambda: locator.evaluate("node => node.tagName", timeout=timeout), "")).lower()
    # Whether it IS a password box - never what is in it. There is no code path here that asks for a
    # value, and DomElement has no field that could hold one.
    is_password = tag == "input" and str(
        safe(lambda: locator.get_attribute("type", timeout=timeout), "")).lower() == "password"
    return DomElement(
        session_id=session_id,
        page_id=page_id,
        role=role,
        tag=tag,
        element_token=secrets.token_hex(8),
        enabled=bool(safe(lambda: locator.is_enabled(timeout=timeout), True)),
        visible=bool(safe(lambda: locator.is_visible(timeout=timeout), True)),
        bounds=bounds,
        is_password=is_password,
        observed_at=observed_at,
    )


def _abandon(runtime, browser, context) -> None:
    """Tear down whatever exists, in reverse order, never raising. Used by both close and the
    partial-launch path, so there is one teardown rather than two that can disagree."""
    for closer in (lambda: context.close(), lambda: browser.close(), lambda: runtime.stop()):
        try:
            closer()
        except Exception:
            continue


def _playwright():
    """Playwright, imported lazily - importing this module must never load a browser stack, and an
    offline test cannot even reach the import (safety_guards.BROWSER_LIBRARIES)."""
    try:
        import playwright.sync_api as sync_api
    except Exception as exc:
        raise BrowserError(
            f"browser automation is unavailable on this computer ({type(exc).__name__})") from None
    return sync_api


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
