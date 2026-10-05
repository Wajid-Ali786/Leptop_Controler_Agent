"""
Data shapes defined by the Executor.
"""
from dataclasses import dataclass
from enum import Enum

OPEN_APP = "open_app"
CLOSE_APP = "close_app"
CLICK = "click"          # target: screen coordinates "x, y", e.g. ExecutorAction(CLICK, "500, 300")
TYPE_TEXT = "type_text"  # target: the exact text to type. It never appears in repr() or logs.
SHORTCUT = "shortcut"    # target: a keyboard shortcut, e.g. ExecutorAction(SHORTCUT, "ctrl+c")
SCROLL = "scroll"        # target: "up N" or "down N" wheel notches, e.g. ExecutorAction(SCROLL, "down 3")
REFRESH = "refresh"      # no target: refreshes the active window, e.g. ExecutorAction(REFRESH)
WINDOW_CONTROL = "window_control"  # target: minimize | maximize | restore | close (the active window)
# click_target: click a control the USER NAMED, found by Phase 5 screen observation.
#   target  = the configured app whose window to look in, or "" to use the one app opened this session
#   control = the user's own word for the control, e.g. ExecutorAction(CLICK_TARGET, "calculator",
#             control="Seven")
# WHERE it is on screen is never in the action: that is resolved locally, at execution time, from the
# accessibility tree - and re-resolved after the confirmation. See app/verifier/observation.py.
CLICK_TARGET = "click_target"
# The assistant's OWN browser, which is not an app in executor.apps and never the owner's Chrome.
OPEN_BROWSER = "open_browser"    # no target: there is one assistant browser, or none
CLOSE_BROWSER = "close_browser"  # no target

# A RESERVED context selector, usable wherever an app name is (e.g. ExecutorAction(CLICK_TARGET,
# ASSISTANT_BROWSER, control="Login")). It names the live assistant browser session, and it is
# recognised BEFORE configured apps and before Memory's application aliases - so no alias and no
# remembered name can redefine it, and "chrome" can never come to mean it. It is runtime context,
# not an app.
ASSISTANT_BROWSER = "assistant browser"


class Outcome(str, Enum):
    """What happened, beyond ok / not ok, so callers can tell distinct results apart."""
    DONE = "done"                       # the action happened and was verified
    UNVERIFIED = "unverified"           # the action was sent, but its effect wasn't confirmed
    ALREADY_CLOSED = "already_closed"   # nothing to do: the window was already gone
    NEEDS_USER = "needs_user"           # the app is waiting for the user (e.g. a save dialog)
    STILL_OPEN = "still_open"           # asked to close, but the window didn't close in time
    PARTIAL = "partial"                 # only part of the action happened (e.g. some of the text typed)
    FAILED = "failed"                   # the action didn't happen (see the message)


_SUCCESS_OUTCOMES = {Outcome.DONE, Outcome.ALREADY_CLOSED, Outcome.UNVERIFIED}  # ok=True
_VERIFIED_OUTCOMES = {Outcome.DONE, Outcome.ALREADY_CLOSED}  # the end state was actually observed


# --- Target resolution results (Phase 3) --------------------------------------------------------------
# The RULES live in app/executor/logic.resolve(), beside the other target rules. These two shapes live
# here, with the other data shapes, so a pure consumer - the Phase 3 router - can name them without
# importing the Executor's logic module and, through it, its OS adapter.

RESOLVE_NO_TARGET = "no_target"               # the action needs a target and none was given
RESOLVE_UNKNOWN_APP = "unknown_app"           # not a configured app name
RESOLVE_BAD_FORMAT = "bad_format"             # not written in a form this kind accepts
RESOLVE_OUT_OF_RANGE = "out_of_range"         # readable, but beyond a configured limit
RESOLVE_UNWANTED_TARGET = "unwanted_target"   # this kind takes no target, and one was given
RESOLVE_SETTINGS = "settings"                 # the configuration needed to judge the target is invalid
RESOLVE_UNKNOWN_KIND = "unknown_kind"         # no such Executor capability


@dataclass(frozen=True)
class Resolved:
    """The target is usable. `value` is the canonical parsed form the preparer goes on to use:

      open_app / close_app  the configured app name, lower-cased
      click                 (x, y) as whole numbers
      scroll                (direction, notches)
      shortcut              the parsed Shortcut
      window_control        the operation name
      type_text             the text with CRLF normalised to LF
      refresh               None - it has no target
    """
    value: object = None


@dataclass(frozen=True)
class Unresolved:
    """The target cannot be used, and why.

    `message` is what the user sees, and it is deliberately the SAME sentence the preparer produced
    before this seam existed - those sentences were tuned during Phase 1 acceptance."""
    reason: str
    message: str


@dataclass(frozen=True, repr=False)
class ExecutorAction:
    """One thing the Executor should do, e.g. ExecutorAction(OPEN_APP, "notepad").

    `control` is the user's own word for an on-screen control, and it is a SEPARATE FIELD rather than
    part of `target` on purpose. `target` is what `log_label` reports, and `log_label` is used in two
    places that must not see it: the Executor's own result log, and app/planner/logic.plan_summary(),
    whose summaries travel to the model inside a ReplanRequest. Keeping the control name out of
    `target` keeps it out of both by construction, and out of `description` and `repr` as well - so
    the only place it appears is the confirmation the user reads and the plan they are shown, both of
    which are their own words coming back to them.
    """
    kind: str
    target: str = ""
    control: str = ""

    @property
    def description(self) -> str:
        """The words the safety gate classifies, e.g. "open app notepad". Never contains typed text."""
        if self.kind == TYPE_TEXT:
            return f"type text ({self.log_label})"
        return " ".join(part for part in (self.kind.replace("_", " "), self.target.strip()) if part)

    @property
    def log_label(self) -> str:
        """What logs may say about the target: typed text is only ever described by its length."""
        if self.kind == TYPE_TEXT:
            count = len(self.target.replace("\r\n", "\n")) if isinstance(self.target, str) else 0  # as typed
            return f"{count} character{'s' if count != 1 else ''}"
        return self.target.strip()

    def __repr__(self) -> str:
        target = f"<{self.log_label}>" if self.kind == TYPE_TEXT else repr(self.target)
        return f"ExecutorAction(kind={self.kind!r}, target={target})"


@dataclass(frozen=True)
class ActionResult:
    action: ExecutorAction
    ok: bool
    message: str             # plain English, safe to show the user - never contains typed text
    retryable: bool = False  # would trying again plausibly help? (Action -> Result -> Recovery)
    outcome: Outcome | None = None  # defaults to DONE when ok, FAILED otherwise
    progress: tuple[int, int] | None = None  # (sent, total), e.g. characters typed; required for PARTIAL

    @property
    def verified(self) -> bool:
        """True only when the result was observed. ok with verified=False (UNVERIFIED) means the action
        was carried out without error, but nothing confirms it achieved anything - never treat that
        as confirmed success."""
        return self.outcome in _VERIFIED_OUTCOMES

    def __post_init__(self):
        if self.outcome is None:
            object.__setattr__(self, "outcome", Outcome.DONE if self.ok else Outcome.FAILED)
        if self.ok != (self.outcome in _SUCCESS_OUTCOMES):
            raise ValueError(f"ok={self.ok} contradicts outcome {self.outcome.value}")
        if self.retryable and self.outcome is not Outcome.FAILED:
            # never retry into an app waiting for the user, nor repeat half-done work
            raise ValueError(f"outcome {self.outcome.value} can't be retryable")
        if self.outcome is Outcome.PARTIAL and not (
                isinstance(self.progress, tuple) and len(self.progress) == 2
                and 0 < self.progress[0] <= self.progress[1]):
            raise ValueError("a partial result needs progress=(sent, total) with something sent")
