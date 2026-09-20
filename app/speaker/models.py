"""
Data shapes defined by the Speaker (docs/step4 Section 5, Phase 2).

Only the validated settings exist so far; what a spoken reply looks like is defined when the
adapter is built.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class SpeakerSettings:
    """The validated `speaker` section of config.yaml. Built by speaker.logic, never by hand."""
    enabled: bool
    engine: str   # "auto", "online" (edge-tts) or "offline" (pyttsx3)
    voice: str    # "" = the engine's default
    rate: int     # relative speed in percent: 0 = the engine's normal speed, +20 faster,
                  # -20 slower. Each adapter maps it onto its own scale (Feature 9).
