"""
The ONLY file in this project allowed to import pyautogui, pywinauto or playwright, to
start processes, or to send messages to other applications' windows (CLAUDE.md rule 2).

Closing is always a polite request - the same message as clicking a window's X button - so the
app can save or ask about unsaved work. Nothing here ends a process.

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
