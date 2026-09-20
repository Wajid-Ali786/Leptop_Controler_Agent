"""
Speaker decision-making: reading and validating the `speaker` section of config.yaml, and later
choosing between the online engine and the offline fallback (docs/step4 Section 5, Phase 2).

Pure, like app/listener/logic.py: it imports only its own models and config.settings, never
speaker.adapter and never edge-tts or pyttsx3. Speaking out loud happens in app/speaker/adapter.py.

Only the settings layer exists so far - engine selection and the online -> offline fallback arrive
with the implementation.
"""
from app.speaker.models import SpeakerSettings
from config.settings import SettingsError, get_setting

ENGINES = ("auto", "online", "offline")  # auto = online when reachable, otherwise offline
# speaker.rate is ENGINE-INDEPENDENT: a relative speed adjustment in percent, where 0 is whatever the
# chosen engine calls normal, +20 is about 20% faster and -20 about 20% slower. The same number must
# mean the same thing whichever engine speaks, so each adapter maps it onto its own scale in Feature 9
# (edge-tts has a percentage rate; pyttsx3 has a base words-per-minute that the percentage applies to).
# Nothing here knows about either engine. The range is deliberately conservative: beyond this speech
# stops being comfortably intelligible, and neither engine is tuned yet.
SLOWEST_PERCENT, FASTEST_PERCENT = -50, 50


def speaker_settings() -> SpeakerSettings:
    """The validated `speaker` config section. Raises SettingsError naming the offending key."""
    return SpeakerSettings(
        enabled=_flag("speaker.enabled"),
        engine=_choice("speaker.engine", ENGINES),
        voice=_text("speaker.voice"),
        rate=_rate("speaker.rate"),
    )


def _flag(name: str) -> bool:
    value = get_setting(name)
    if not isinstance(value, bool):
        raise SettingsError(f"Setting '{name}' must be true or false, got {value!r}.")
    return value


def _choice(name: str, allowed):
    value = get_setting(name)
    if isinstance(value, bool) or value not in allowed:
        raise SettingsError(f"Setting '{name}' must be one of {', '.join(allowed)}, got {value!r}.")
    return value


def _text(name: str) -> str:
    """"" is allowed and means "the engine's default"."""
    value = get_setting(name)
    if not isinstance(value, str):
        raise SettingsError(f"Setting '{name}' must be a string, got {value!r}.")
    return value.strip()


def _rate(name: str) -> int:
    """A relative speed adjustment in percent: 0 is the engine's normal speed, +20 faster, -20 slower."""
    value = get_setting(name)
    if isinstance(value, bool) or not isinstance(value, int) or not SLOWEST_PERCENT <= value <= FASTEST_PERCENT:
        raise SettingsError(f"Setting '{name}' must be a whole percentage from {SLOWEST_PERCENT} "
                            f"(slower) to {FASTEST_PERCENT} (faster), where 0 is the engine's normal "
                            f"speed, got {value!r}.")
    return value
