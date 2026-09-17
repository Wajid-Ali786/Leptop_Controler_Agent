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
    cloaked: bool = False  # True while Windows keeps it off screen (a Store app starting, another virtual desktop)


@dataclass(frozen=True)
class Screen:
    """One monitor's area in screen pixels. right and bottom are exclusive: the screen covers
    x from left to right - 1 and y from top to bottom - 1. Other monitors may have negative coordinates."""
    left: int
    top: int
    right: int
    bottom: int
    primary: bool = False

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom


@dataclass(frozen=True)
class ActiveTarget:
    """Where keyboard input goes right now: the active (foreground) window and its focused control."""
    window: WindowInfo | None  # None when no window is active
    control_handle: int | None = None
    control_class: str = ""


@dataclass(frozen=True)
class ControlInfo:
    """A window or control, identified by handle and Windows class only (no text is read)."""
    handle: int
    class_name: str


@dataclass(frozen=True)
class ScrollState:
    """A standard vertical scroll bar: position within minimum..maximum, with `page` units visible."""
    position: int
    minimum: int
    maximum: int
    page: int

    @property
    def at_top(self) -> bool:
        return self.position <= self.minimum

    @property
    def at_bottom(self) -> bool:
        return self.position + max(self.page, 1) - 1 >= self.maximum


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
