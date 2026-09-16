"""
The only file in the Verifier allowed to talk to an external system. It READS the Windows
desktop - which visible top-level windows exist, their titles and window classes, and whether
each is enabled - and never changes anything.

Uses the Windows API through the standard library (ctypes), so observing the desktop needs no
extra dependency and has no side effects.
"""
import ctypes
import sys

from app.verifier.models import WindowInfo

_MAX_CLASS_NAME = 256  # Windows limits window class names to 256 characters


class VerifierAdapterError(Exception):
    """The desktop couldn't be observed."""


def list_windows() -> list[WindowInfo]:
    """Every visible top-level window that has a title. A window is not `enabled` while a modal
    dialog it opened (such as "Save changes?") is waiting for the user."""
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
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.IsWindowEnabled.argtypes = [wintypes.HWND]
    user32.IsWindowEnabled.restype = wintypes.BOOL

    windows: list[WindowInfo] = []

    def collect(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                if buffer.value:
                    class_name = ctypes.create_unicode_buffer(_MAX_CLASS_NAME)
                    user32.GetClassNameW(hwnd, class_name, _MAX_CLASS_NAME)
                    windows.append(WindowInfo(handle=int(hwnd), title=buffer.value, class_name=class_name.value,
                                              enabled=bool(user32.IsWindowEnabled(hwnd))))
        return True

    if not user32.EnumWindows(enum_proc(collect), 0):
        raise VerifierAdapterError(f"Windows couldn't list the open windows (error {ctypes.get_last_error()})")
    return windows
