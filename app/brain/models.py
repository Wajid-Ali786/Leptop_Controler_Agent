"""
Data shapes defined by the Brain.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ClaudeReply:
    """One Claude response, reduced to what the project needs."""
    text: str
    model: str
    stop_reason: str | None
    input_tokens: int
    output_tokens: int
