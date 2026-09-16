"""
The only file in the Verifier allowed to talk to an external system. It READS the Windows
desktop - which visible top-level windows exist, and their titles - and never changes anything.

Uses the Windows API through the standard library (ctypes), so observing the desktop needs no
extra dependency and has no side effects.
"""
import ctypes
import sys

from app.verifier.models import WindowInfo


class VerifierAdapterError(Exception):
    """The desktop couldn't be observed."""


def list_windows() -> list[WindowInfo]:
    """Every visible top-level window that has a title."""
    if sys.platform != "win32":
        raise VerifierAdapterError("checking windows is only supported on Windows")
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int

    windows: list[WindowInfo] = []

    def collect(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                if buffer.value:
                    windows.append(WindowInfo(handle=int(hwnd), title=buffer.value))
        return True

    if not user32.EnumWindows(enum_proc(collect), 0):
        raise VerifierAdapterError(f"Windows couldn't list the open windows (error {ctypes.get_last_error()})")
    return windows
