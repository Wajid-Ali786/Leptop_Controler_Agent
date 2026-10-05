"""
Data shapes defined by the Verifier.
"""
import re
from dataclasses import dataclass
from enum import Enum


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
class WindowState:
    """What Windows reports about a top-level window's state and what it offers."""
    minimized: bool
    maximized: bool
    has_minimize_box: bool
    has_maximize_box: bool
    tool_window: bool
    hung: bool


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


# --- Phase 5: observation (docs/step4 Section 8, Build Plan Section 6.5) ------------------------------
# Observation is EVIDENCE about the screen, never intent. Nothing here can carry an ExecutorAction, lower
# a risk level, grant permission or become what the user asked for - app/safety stays the only authority
# on what may run, and a target the user named stays the only thing that can become an action.
#
# THE PRIVACY LINE THAT SHAPES THESE TYPES. A UIA accessible name ("Login", "Save") is user-visible
# content, and it is also the only way to find a target the user named. So the name is read, normalised
# and compared INSIDE app/verifier/adapter.py and never crosses that boundary: `Observed` deliberately
# has no name field, so an unmatched label cannot be returned, logged, persisted or sent anywhere.


class ObservationSource(Enum):
    """Which layer of the frozen hierarchy produced an observation, in its frozen order of preference.

    Only UIA is implemented (Phase 5 Slice 1). The rest are declared so the typed model does not have to
    change shape when they arrive, and so a result can always say which layer answered."""
    UIA = "uia"
    DOM = "dom"
    OCR = "ocr"
    VISION = "vision"
    COORDINATE = "coordinate"


class Sensitivity(Enum):
    """What an observed value is, which decides what may be done with it.

    STRUCTURAL and INTERACTION are safe to log and to reason about. CONTENT is local-only. SECRET is
    never read at all. PIXELS and UNTRUSTED belong to later layers and are declared here so the policy
    is written down once, in one place, before anything can produce them."""
    STRUCTURAL = "structural"      # identifiers, roles, bounds, enabled/focused state
    INTERACTION = "interaction"    # focus, cursor, which modifiers are held
    CONTENT = "content"            # labels, field text, titles - local only, never logged
    SECRET = "secret"              # password controls, clipboard content - never read
    PIXELS = "pixels"              # screenshots - the only provider-egress class, later layers
    UNTRUSTED = "untrusted"        # text out of a page or document: data, never an instruction


@dataclass(frozen=True)
class Target:
    """What the USER asked to find, and where. Both halves come from the user's own words - never from
    the screen - which is why a result may safely echo `name` back."""
    name: str
    window_handle: int


@dataclass(frozen=True)
class Observed:
    """One control, as structural evidence. No accessible name, no value, no text - by construction.

    `runtime_id` is the identity to re-resolve by before acting: UIA elements go stale silently, and a
    bounding rectangle is only meaningful while the window has not moved."""
    source: ObservationSource
    window_handle: int
    control_type: str                       # the UIA control type, e.g. "Button"
    observed_at: float
    runtime_id: str = ""                    # stable while the element lives; "" when UIA gives none
    automation_id: str = ""
    class_name: str = ""
    bounds: tuple[int, int, int, int] | None = None   # left, top, right, bottom in screen pixels
    enabled: bool = True
    focused: bool = False
    offscreen: bool = False
    is_password: bool = False               # SECRET: structural only, the value is never read
    patterns: tuple[str, ...] = ()           # supported UIA patterns, e.g. ("Invoke",)

    def is_fresh(self, now: float, freshness_seconds: float) -> bool:
        """Is this observation still worth acting on? The screen can change between observing and
        acting, so freshness is asked explicitly rather than assumed."""
        return (now - self.observed_at) <= freshness_seconds


@dataclass(frozen=True)
class Found:
    """Exactly one control matched the name the user gave."""
    observed: Observed


@dataclass(frozen=True)
class Ambiguous:
    """More than one matched, so nothing is chosen. The candidates carry STRUCTURAL distinctions only -
    control type, bounds, enabled state - never the labels that made them match."""
    candidates: tuple[Observed, ...]
    message: str


@dataclass(frozen=True)
class NotFound:
    """Nothing matched. The message names what the USER asked for and nothing read off the screen."""
    message: str


@dataclass(frozen=True)
class Unavailable:
    """The window could not be observed at all - no accessibility tree, or the attempt failed."""
    reason: str


def normalize_name(text: str) -> str:
    """The one comparison form for a control name: surrounding and repeated whitespace collapsed, case
    ignored. Deliberately nothing else - no fuzzy matching, no edit distance, no substring matching, no
    model judgement. "Login" matches "login" and "  Login  ", and does not match "Log in" or "Login…"."""
    return " ".join(text.split()).lower() if isinstance(text, str) else ""


@dataclass(frozen=True)
class UiaElement:
    """What app/verifier/adapter.py returns for a matched control. There is deliberately NO name field:
    the accessible name is compared inside the adapter and never crosses that boundary."""
    window_handle: int
    control_type: str
    runtime_id: str = ""
    automation_id: str = ""
    class_name: str = ""
    bounds: tuple[int, int, int, int] | None = None
    enabled: bool = True
    focused: bool = False
    offscreen: bool = False
    is_password: bool = False
    patterns: tuple[str, ...] = ()


# --- Phase 5 DOM Slice 1: the browser DOM layer's evidence --------------------------------------------
# Layer 2 of the frozen hierarchy (Build Plan Section 6.5). The same rules as the UIA layer, for the
# same reasons, with one addition that comes from the page being the OPEN INTERNET.
#
# WHO READS, WHO DECIDES. UI Automation can only read, so app/verifier/adapter.py reads it. Playwright
# can read AND click, so the library lives in app/executor/adapter.py - the one file allowed to control
# this computer - and only the DECISION lives here, as a pure function over evidence it is handed. The
# rule that generalises: whoever owns the library reads, observation decides, the Executor acts.
#
# PAGE CONTENT IS UNTRUSTED (Sensitivity.UNTRUSTED, declared above). A page can say "ignore previous
# instructions" or "click Delete"; none of it can become intent, because intent and evidence are
# different types and only the user's own DomTarget.name can become an action. That type boundary is
# the whole defence - there is no detector, and there does not need to be one.
#
# These types deliberately have NO field for an accessible name, page text, HTML, a URL, an href, a
# form value or a selector. Nothing can leak through a field that does not exist.


@dataclass(frozen=True)
class DomTarget:
    """What the USER asked to find, and in which assistant-owned page.

    `name` is the user's own words, exactly as with a UIA Target. The session and page are opaque ids
    minted by the Executor's adapter; they are not a URL, and they are not a window title."""
    name: str
    session_id: str
    page_id: str


@dataclass(frozen=True)
class DomElement:
    """One matched page control, as structure.

    `element_token` is opaque OUTSIDE the adapter that made it: it stands for a re-resolvable locator
    description, and never a CSS selector, an XPath, an accessible name or a live Playwright object. In
    this read-only slice nothing acts on it; it exists so the later action slice has an identity to
    re-resolve, rather than a handle that silently detaches."""
    session_id: str
    page_id: str
    role: str                                # one of adapter.DOM_ROLES
    tag: str = ""                            # the element's tag name, e.g. "button"
    element_token: str = ""
    enabled: bool = True
    visible: bool = True
    bounds: tuple[int, int, int, int] | None = None
    is_password: bool = False                # SECRET: structural only; the value is never requested
    observed_at: float = 0.0


@dataclass(frozen=True)
class FramesNotSupported:
    """The target was not found in the top-level document, and the page has child frames that this
    slice does not look inside.

    Deliberately NOT NotFound. "I did not find it" and "I did not look everywhere it could be" are
    different claims, and reporting the second as the first would be a false negative that a later
    layer might act on."""
    reason: str


# --- Phase 5 Slice 2: re-identified, action-ready evidence --------------------------------------------
# An Observed is evidence about a MOMENT. By the time the user has read a confirmation and answered it,
# that moment has passed: the window may have moved, the list may have scrolled, the button may have been
# disabled. So nothing here is derived from the observation that was shown to the user - the bounds and
# the click point are read again, after the confirmation, and only then does a target become actionable.
#
# WHY runtime_id IS NOT THE IDENTITY. pywinauto documents its own runtime_id as "may be different from
# run to run", and its element equality deliberately uses UI Automation's CompareElements instead - which
# needs both live COM elements, something this project does not keep across a confirmation and should
# not. So runtime_id is recorded and compared as CORROBORATION and never decides identity: building on it
# would repeat exactly the mistake of assuming a window handle is an identity.


@dataclass(frozen=True)
class ActionTarget:
    """One re-identified control the Executor may act on, and nothing more.

    It carries no accessible name, no value, no document text and no UI tree. `target_name` is the
    USER's own word for the control, carried through from the Target they gave - never a label read off
    the screen - which is why a confirmation prompt may safely show it.

    This is NOT an action: it says where a control is, not what to do to it. The Executor decides that,
    and app/safety still decides whether it may happen."""
    source: ObservationSource               # kept, so provenance survives: a UIA point stays a UIA point
    window_handle: int
    bounds: tuple[int, int, int, int]       # the bounds read AFTER the confirmation, never the old ones
    point: tuple[int, int]                  # derived from those bounds, inside this window
    target_name: str
    reidentified_at: float
    identity: str = ""                      # WHICH KINDS of evidence matched, never their values


@dataclass(frozen=True)
class Stale:
    """The control that is there now is not the one that was observed, so nothing is assumed about it.

    Deliberately distinct from NotFound: something did match the user's word, but its identity differs,
    which is the case where guessing would be most tempting and most wrong."""
    reason: str


@dataclass(frozen=True)
class NotEligible:
    """Re-identified, and still not something to click: disabled, off screen, or in an impossible place."""
    reason: str
