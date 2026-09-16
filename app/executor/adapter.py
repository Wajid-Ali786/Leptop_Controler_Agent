"""
The ONLY file in this project allowed to import pyautogui, pywinauto or playwright, or to
start processes (CLAUDE.md rule 2).

Every function here performs a real action on the computer, so nothing may call it except
app/executor/logic.py, which routes every action through app/safety first (CLAUDE.md rule 5).
"""
import shutil
import subprocess

_WINDOWS_ELEVATION_REQUIRED = 740  # ERROR_ELEVATION_REQUIRED


class ExecutorAdapterError(Exception):
    """A real action could not be carried out. Messages are safe to show the user."""


class AppNotFoundError(ExecutorAdapterError):
    """The application isn't installed, or can't be found on this computer."""


class AppLaunchError(ExecutorAdapterError):
    """The application exists but couldn't be started."""


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
