"""
Data shapes defined by the safety module.
"""
from dataclasses import dataclass
from enum import IntEnum


class RiskLevel(IntEnum):
    """Risk scale from the Build Plan (Section 2) / vision doc Section 10.
    The Phase 0 gate only produces LOW and MEDIUM; HIGH/CRITICAL arrive with later phases."""
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


@dataclass(frozen=True)
class Action:
    """Something the assistant wants to do, described in words (Phase 0: command text).

    minimum_level lets the caller declare a floor the words can't lower - e.g. a coordinate click is
    always Medium risk, because "click at (500, 300)" says nothing about what is under the pointer.
    minimum_reason is the rule logged and shown when that floor decides the level."""
    description: str
    minimum_level: RiskLevel = RiskLevel.LOW
    minimum_reason: str = ""

    def __repr__(self) -> str:
        # The description is what the user is asked to approve (it can preview typed text), so it is
        # shown on screen only - diagnostics describe it by length.
        length = len(self.description) if isinstance(self.description, str) else 0
        return (f"Action(description=<{length} characters>, minimum_level={self.minimum_level!r}, "
                f"minimum_reason={self.minimum_reason!r})")


@dataclass(frozen=True)
class RiskAssessment:
    level: RiskLevel
    rule: str  # which rule decided the level - safe to log; never contains the full action text


@dataclass(frozen=True)
class SafetyDecision:
    """Returned only when an action is allowed to run."""
    assessment: RiskAssessment
    confirmed: bool  # True if the user explicitly approved it
