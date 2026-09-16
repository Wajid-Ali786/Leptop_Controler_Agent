"""
Data shapes defined by the Executor.
"""
from dataclasses import dataclass

OPEN_APP = "open_app"


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
