"""
Data shapes defined by the Executor.
"""
from dataclasses import dataclass
from enum import Enum

OPEN_APP = "open_app"
CLOSE_APP = "close_app"


class Outcome(str, Enum):
    """What happened, beyond ok / not ok, so callers can tell distinct results apart."""
    DONE = "done"                       # the action happened and was verified
    ALREADY_CLOSED = "already_closed"   # nothing to do: the window was already gone
    NEEDS_USER = "needs_user"           # the app is waiting for the user (e.g. a save dialog)
    STILL_OPEN = "still_open"           # asked to close, but the window didn't close in time
    FAILED = "failed"                   # the action didn't happen (see the message)


_SUCCESS_OUTCOMES = {Outcome.DONE, Outcome.ALREADY_CLOSED}


@dataclass(frozen=True)
class ExecutorAction:
    """One thing the Executor should do, e.g. ExecutorAction(OPEN_APP, "notepad")."""
    kind: str
    target: str = ""

    @property
    def description(self) -> str:
        """The words the safety gate classifies, e.g. "open app notepad"."""
        return " ".join(part for part in (self.kind.replace("_", " "), self.target.strip()) if part)


@dataclass(frozen=True)
class ActionResult:
    action: ExecutorAction
    ok: bool
    message: str             # plain English, safe to show the user
    retryable: bool = False  # would trying again plausibly help? (Action -> Result -> Recovery)
    outcome: Outcome | None = None  # defaults to DONE when ok, FAILED otherwise

    def __post_init__(self):
        if self.outcome is None:
            object.__setattr__(self, "outcome", Outcome.DONE if self.ok else Outcome.FAILED)
        if self.ok != (self.outcome in _SUCCESS_OUTCOMES):
            raise ValueError(f"ok={self.ok} contradicts outcome {self.outcome.value}")
        if self.retryable and self.outcome is not Outcome.FAILED:
            # a window waiting for the user must never be retried into
            raise ValueError(f"outcome {self.outcome.value} can't be retryable")
