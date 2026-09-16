"""
The only file in the Verifier allowed to talk to an external system. It READS the Windows
desktop - which visible top-level windows exist, their titles and window classes, whether each is
enabled or cloaked, and which titled windows are hosted inside another window - and never changes
anything.

Uses the Windows API through the standard library (ctypes), so observing the desktop needs no
extra dependency and has no side effects.
"""
import ctypes
import sys

from app.verifier.models import WindowInfo

_MAX_CLASS_NAME = 256  # Windows limits window class names to 256 characters
_DWMWA_CLOAKED = 14    # "cloaked": the window exists and counts as visible, but isn't drawn on screen


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


class _Api:
    """user32/dwmapi with argument types declared, so handles are passed correctly on 64-bit Windows."""

    def __init__(self):
        from ctypes import wintypes

        self.DWORD = wintypes.DWORD
        self.user32 = user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.dwmapi = dwmapi = ctypes.WinDLL("dwmapi")
        self.enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        for name, argtypes, restype in [
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
    return _Api()


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
