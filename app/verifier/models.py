"""
Data shapes defined by the Verifier.
"""
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class WindowInfo:
    """A visible top-level window on the desktop."""
    handle: int
    title: str
    class_name: str = ""  # the Windows window class, e.g. "ApplicationFrameWindow"
    enabled: bool = True  # False while the window is blocked by a dialog it opened (e.g. "Save changes?")


@dataclass(frozen=True)
class WindowExpectation:
    """What 'this app opened' looks like: a NEW window whose title matches `pattern`."""
    app_name: str
    pattern: re.Pattern
    timeout_seconds: float
    poll_interval_seconds: float


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    message: str                          # plain English, safe to show the user
    retryable: bool = False               # would trying the action again plausibly help?
    elapsed_seconds: float | None = None
    window_handle: int | None = None
    window_handles: frozenset[int] = frozenset()  # every new matching window (Calculator shows two)
    needs_user: bool = False              # a close is blocked by a dialog waiting for the user
