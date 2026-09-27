"""
Data shapes defined by the Speaker (docs/step4 Section 5, Phase 2).

Nothing here holds what was said. `Spoken` carries evidence about the act of speaking - which engine
spoke and how long it took - and `SpeechFailure` carries a reason written to be shown. Neither carries
the text, an exception, or anything a COM layer handed us, so neither can leak a reply into a repr, a
log line or a test failure.
"""
from dataclasses import dataclass

# Which voice actually spoke. "off" means speaking is switched off in config and nothing was said -
# that is a successful no-op, not a failure, so callers need no special case for it.
OFF = "off"
ONLINE = "online"            # Microsoft's Edge speech service, over the network (edge-tts)
OFFLINE = "offline"          # this computer's own built-in voice, no network (pyttsx3)

# Why nothing was said. A small, closed set on purpose.
NOTHING_TO_SAY = "nothing_to_say"            # the text was empty or only whitespace
ONLINE_UNAVAILABLE = "online_unavailable"    # the online voice could not speak, and was the only one
OFFLINE_UNAVAILABLE = "offline_unavailable"  # this computer's built-in voice could not speak
BOTH_UNAVAILABLE = "both_unavailable"        # engine: auto tried both and neither spoke
SPEAKER_ERROR = "speaker_error"              # anything else went wrong inside the speaker


@dataclass(frozen=True)
class SpeakerSettings:
    """The validated `speaker` section of config.yaml. Built by speaker.logic, never by hand."""
    enabled: bool
    engine: str   # "auto", "online" (edge-tts) or "offline" (pyttsx3)
    voice: str    # "" = the engine's default
    rate: int     # relative speed in percent: 0 = the engine's normal speed, +20 faster,
                  # -20 slower. Each adapter maps it onto its own scale (Feature 9).


@dataclass(frozen=True)
class Spoken:
    """Speaking finished. `engine` is OFF, ONLINE or OFFLINE - never the text."""
    engine: str
    seconds: float   # the whole blocking speak() call, monotonic - so on a fallback it includes the
                     # failed online attempt, because that is what the listener actually waited for.
                     # 0.0 when nothing was said.


@dataclass(frozen=True)
class SpeechFailure:
    """Why nothing was said. `message` is safe to show: a written sentence, never the text that was
    not spoken, never an exception, never a traceback."""
    kind: str
    message: str
