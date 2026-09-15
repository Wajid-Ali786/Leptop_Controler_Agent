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
    """Something the assistant wants to do, described in words (Phase 0: command text)."""
    description: str


@dataclass(frozen=True)
class RiskAssessment:
    level: RiskLevel
    rule: str  # which rule decided the level - safe to log; never contains the full action text


@dataclass(frozen=True)
class SafetyDecision:
    """Returned only when an action is allowed to run."""
    assessment: RiskAssessment
    confirmed: bool  # True if the user explicitly approved it
