"""
Data shapes defined by the Executor.
"""
from dataclasses import dataclass
from enum import Enum

OPEN_APP = "open_app"
CLOSE_APP = "close_app"
CLICK = "click"          # target: screen coordinates "x, y", e.g. ExecutorAction(CLICK, "500, 300")
TYPE_TEXT = "type_text"  # target: the exact text to type. It never appears in repr() or logs.


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


@dataclass(frozen=True, repr=False)
class ExecutorAction:
    """One thing the Executor should do, e.g. ExecutorAction(OPEN_APP, "notepad")."""
    kind: str
    target: str = ""

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
