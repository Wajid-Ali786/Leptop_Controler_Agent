"""
The only file in the Verifier allowed to talk to an external system. It READS the Windows
desktop - which visible top-level windows exist, their titles and window classes, whether each is
enabled or cloaked, which titled windows are hosted inside another window, where each monitor is,
which window is at a screen point, where the mouse pointer is, which window and control have
keyboard focus, and the text of a control - and never changes anything. Reading a control's text
asks the app for a copy (WM_GETTEXT, with a timeout so a hung app can't block); the text is
returned to the caller only, never logged.

Coordinates are real screen pixels on every monitor: before reading anything, this process is made
per-monitor DPI aware (the same call the Executor adapter makes before clicking), so a point read
here and a click sent there mean the same pixel even with display scaling.

Uses the Windows API through the standard library (ctypes), so observing the desktop needs no
extra dependency and has no side effects.
"""
import ctypes
import sys

from app.verifier.models import ActiveTarget, Screen, WindowInfo

_MAX_CLASS_NAME = 256  # Windows limits window class names to 256 characters
_DWMWA_CLOAKED = 14    # "cloaked": the window exists and counts as visible, but isn't drawn on screen
_GA_ROOT = 2           # GetAncestor: the top-level window a child window belongs to
_MONITORINFOF_PRIMARY = 1
_PER_MONITOR_AWARE_V2 = -4  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
_WM_GETTEXT, _WM_GETTEXTLENGTH = 0x000D, 0x000E
_SMTO_BLOCK_ABORTIFHUNG = 0x0001 | 0x0002
_READ_TIMEOUT_MS = 500


class VerifierAdapterError(Exception):
    """The desktop couldn't be observed."""


def list_windows() -> list[WindowInfo]:
    """Every visible top-level window that has a title. A window is not `enabled` while a modal
    dialog it opened (such as "Save changes?") is waiting for the user, and is `cloaked` while
    Windows keeps it off screen (e.g. a Store app still starting, or on another virtual desktop)."""
    api = _api()
    windows: list[WindowInfo] = []

    def collect(hwnd, _lparam):
        if api.user32.IsWindowVisible(hwnd):
            info = _window_info(api, hwnd)
            if info:
                windows.append(info)
        return True

    if not api.user32.EnumWindows(api.enum_proc(collect), 0):
        raise VerifierAdapterError(f"Windows couldn't list the open windows (error {ctypes.get_last_error()})")
    return windows


def list_child_windows(handle: int) -> list[WindowInfo]:
    """Every titled window hosted inside window `handle` (at any depth), e.g. a Store app's content
    window inside its frame. Empty if there are none or the window no longer exists."""
    api = _api()
    windows: list[WindowInfo] = []

    def collect(hwnd, _lparam):
        info = _window_info(api, hwnd)
        if info:
            windows.append(info)
        return True

    api.user32.EnumChildWindows(handle, api.enum_proc(collect), 0)
    return windows


def list_screens() -> list[Screen]:
    """Every monitor's area, in screen pixels."""
    api = _api()
    screens: list[Screen] = []

    def collect(monitor, _hdc, _rect, _lparam):
        info = api.MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if api.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            r = info.rcMonitor
            screens.append(Screen(r.left, r.top, r.right, r.bottom, primary=bool(info.dwFlags & _MONITORINFOF_PRIMARY)))
        return True

    if not api.user32.EnumDisplayMonitors(None, None, api.monitor_enum_proc(collect), 0) or not screens:
        raise VerifierAdapterError(f"Windows couldn't list the screens (error {ctypes.get_last_error()})")
    return screens


def window_at(x: int, y: int) -> WindowInfo | None:
    """The top-level window at screen point (x, y) - its title may be empty - or None if there is none."""
    api = _api()
    hwnd = api.user32.WindowFromPoint(api.POINT(x, y))
    root = api.user32.GetAncestor(hwnd, _GA_ROOT) if hwnd else None
    return _any_window_info(api, root) if root else None


def active_target() -> ActiveTarget:
    """The active (foreground) window and the control inside it that has keyboard focus."""
    api = _api()
    foreground = api.user32.GetForegroundWindow()
    if not foreground:
        return ActiveTarget(window=None)
    window = _any_window_info(api, foreground)
    thread = api.user32.GetWindowThreadProcessId(foreground, None)
    info = api.GuiThreadInfo()
    info.cbSize = ctypes.sizeof(info)
    if not thread or not api.user32.GetGUIThreadInfo(thread, ctypes.byref(info)) or not info.hwndFocus:
        return ActiveTarget(window=window)
    class_name = ctypes.create_unicode_buffer(_MAX_CLASS_NAME)
    api.user32.GetClassNameW(info.hwndFocus, class_name, _MAX_CLASS_NAME)
    return ActiveTarget(window=window, control_handle=int(info.hwndFocus), control_class=class_name.value)


def read_text(handle: int, max_characters: int) -> str | None:
    """The text of control `handle` (e.g. an edit box), or None if it can't be read, the app doesn't
    answer in time, or the text is longer than max_characters."""
    api = _api()
    length = ctypes.c_size_t(0)
    if not api.user32.SendMessageTimeoutW(handle, _WM_GETTEXTLENGTH, 0, 0, _SMTO_BLOCK_ABORTIFHUNG,
                                          _READ_TIMEOUT_MS, ctypes.byref(length)):
        return None
    if length.value > max_characters:
        return None
    buffer = ctypes.create_unicode_buffer(length.value + 1)
    copied = ctypes.c_size_t(0)
    if not api.user32.SendMessageTimeoutW(handle, _WM_GETTEXT, length.value + 1,
                                          ctypes.cast(buffer, ctypes.c_void_p).value or 0,
                                          _SMTO_BLOCK_ABORTIFHUNG, _READ_TIMEOUT_MS, ctypes.byref(copied)):
        return None
    return buffer.value


def cursor_position() -> tuple[int, int]:
    """Where the mouse pointer is, in screen pixels."""
    api = _api()
    point = api.POINT()
    if not api.user32.GetCursorPos(ctypes.byref(point)):
        raise VerifierAdapterError(f"Windows couldn't report the mouse position (error {ctypes.get_last_error()})")
    return point.x, point.y


class _Api:
    """user32/dwmapi with argument types declared, so handles are passed correctly on 64-bit Windows."""

    def __init__(self):
        from ctypes import wintypes

        class MonitorInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        class GuiThreadInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hwndActive", wintypes.HWND),
                        ("hwndFocus", wintypes.HWND), ("hwndCapture", wintypes.HWND),
                        ("hwndMenuOwner", wintypes.HWND), ("hwndMoveSize", wintypes.HWND),
                        ("hwndCaret", wintypes.HWND), ("rcCaret", wintypes.RECT)]

        self.DWORD, self.POINT, self.MonitorInfo = wintypes.DWORD, wintypes.POINT, MonitorInfo
        self.GuiThreadInfo = GuiThreadInfo
        self.user32 = user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.dwmapi = dwmapi = ctypes.WinDLL("dwmapi")
        self.enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.monitor_enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                                    ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
        for name, argtypes, restype in [
            ("SetProcessDpiAwarenessContext", [ctypes.c_void_p], wintypes.BOOL),
            ("EnumDisplayMonitors", [wintypes.HDC, ctypes.c_void_p, self.monitor_enum_proc, wintypes.LPARAM],
             wintypes.BOOL),
            ("GetMonitorInfoW", [wintypes.HMONITOR, ctypes.POINTER(MonitorInfo)], wintypes.BOOL),
            ("WindowFromPoint", [wintypes.POINT], wintypes.HWND),
            ("GetAncestor", [wintypes.HWND, wintypes.UINT], wintypes.HWND),
            ("GetCursorPos", [ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            ("GetForegroundWindow", [], wintypes.HWND),
            ("GetWindowThreadProcessId", [wintypes.HWND, ctypes.c_void_p], wintypes.DWORD),
            ("GetGUIThreadInfo", [wintypes.DWORD, ctypes.POINTER(GuiThreadInfo)], wintypes.BOOL),
            ("SendMessageTimeoutW", [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                     wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)],
             wintypes.LPARAM),
            ("EnumWindows", [self.enum_proc, wintypes.LPARAM], wintypes.BOOL),
            ("EnumChildWindows", [wintypes.HWND, self.enum_proc, wintypes.LPARAM], wintypes.BOOL),
            ("IsWindowVisible", [wintypes.HWND], wintypes.BOOL),
            ("IsWindowEnabled", [wintypes.HWND], wintypes.BOOL),
            ("GetWindowTextLengthW", [wintypes.HWND], ctypes.c_int),
            ("GetWindowTextW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            ("GetClassNameW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        ]:
            function = getattr(user32, name)
            function.argtypes, function.restype = argtypes, restype
        dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.UINT]
        dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long


def _api() -> _Api:
    if sys.platform != "win32":
        raise VerifierAdapterError("checking windows is only supported on Windows")
    api = _Api()
    # Real pixels on every monitor. Fails harmlessly once the process's DPI awareness is already set.
    api.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(_PER_MONITOR_AWARE_V2))
    return api


def _any_window_info(api: _Api, hwnd) -> WindowInfo:
    """The window's details; its title may be empty."""
    length = max(api.user32.GetWindowTextLengthW(hwnd), 0)
    title = ctypes.create_unicode_buffer(length + 1)
    api.user32.GetWindowTextW(hwnd, title, length + 1)
    class_name = ctypes.create_unicode_buffer(_MAX_CLASS_NAME)
    api.user32.GetClassNameW(hwnd, class_name, _MAX_CLASS_NAME)
    return WindowInfo(handle=int(hwnd), title=title.value, class_name=class_name.value,
                      enabled=bool(api.user32.IsWindowEnabled(hwnd)))


def _window_info(api: _Api, hwnd) -> WindowInfo | None:
    """The window's details, or None if it has no title."""
    length = api.user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return None
    buffer = ctypes.create_unicode_buffer(length + 1)
    api.user32.GetWindowTextW(hwnd, buffer, length + 1)
    if not buffer.value:
        return None
    class_name = ctypes.create_unicode_buffer(_MAX_CLASS_NAME)
    api.user32.GetClassNameW(hwnd, class_name, _MAX_CLASS_NAME)
    cloaked = api.DWORD(0)
    if api.dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) != 0:
        cloaked.value = 0  # attribute unavailable: treat the window as shown
    return WindowInfo(handle=int(hwnd), title=buffer.value, class_name=class_name.value,
                      enabled=bool(api.user32.IsWindowEnabled(hwnd)), cloaked=bool(cloaked.value))
