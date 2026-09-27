"""
Speaker decision-making: reading and validating the `speaker` section of config.yaml, and later
choosing between the online engine and the offline fallback (docs/step4 Section 5, Phase 2).

Pure, like app/listener/logic.py: it imports only its own models and config.settings, never
speaker.adapter and never edge-tts or pyttsx3. Speaking out loud happens in app/speaker/adapter.py.

Only the settings layer exists so far - engine selection and the online -> offline fallback arrive
with the implementation.
"""
from app.speaker.models import (BOTH_UNAVAILABLE, NOTHING_TO_SAY, OFFLINE, OFFLINE_UNAVAILABLE,
                                ONLINE, ONLINE_UNAVAILABLE, SPEAKER_ERROR, SpeakerSettings,
                                SpeechFailure)
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


# --- Speaking decisions: pure, and deliberately small -------------------------------------------------
# Nothing below imports pyttsx3, edge-tts, aiohttp, ctypes, tempfile, socket or any COM module. It
# turns settings into plain numbers the adapter hands to whichever engine it is using, so no
# engine-specific value ever reaches the rest of the application.

# pyttsx3's own normal speed. Its `rate` property is words per minute, and the sapi5 driver starts at
# exactly this value - so speaker.rate = 0 means "leave the engine at its normal speed" on both
# engines, which is what the setting promises.
BASE_WORDS_PER_MINUTE = 200


def offline_words_per_minute(percent: int) -> int:
    """speaker.rate as words per minute for the local voice: -50 -> 100, 0 -> 200, +50 -> 300.

    Exact for whole percentages, because 200 * percent / 100 is 2 * percent."""
    return round(BASE_WORDS_PER_MINUTE * (1 + percent / 100))


def is_blank(text) -> bool:
    """Nothing to say. Checked before any engine is touched, so blank text never reaches one."""
    return not isinstance(text, str) or not text.strip()


def nothing_to_say() -> SpeechFailure:
    return SpeechFailure(NOTHING_TO_SAY, "There was nothing to say.")


def offline_unavailable() -> SpeechFailure:
    return SpeechFailure(OFFLINE_UNAVAILABLE,
                         "This computer's built-in voice could not speak. Nothing else was affected.")


def speaker_error() -> SpeechFailure:
    return SpeechFailure(SPEAKER_ERROR,
                         "The assistant could not speak that. Nothing else was affected.")


def engine_order(settings: SpeakerSettings) -> tuple:
    """Which voices to try, in order. The whole engine policy lives here, and it is pure.

    ()                      speaking is switched off
    ("online",)             engine: online  - the online voice or nothing; never a silent fallback
    ("offline",)            engine: offline - the local voice only; the network is never touched
    ("online", "offline")   engine: auto    - the online voice, and the local one if it cannot speak
    """
    if not settings.enabled:
        return ()
    if settings.engine == ONLINE:
        return (ONLINE,)
    if settings.engine == OFFLINE:
        return (OFFLINE,)
    return (ONLINE, OFFLINE)          # "auto": the only remaining validated value


def online_rate(percent: int) -> str:
    """speaker.rate as the percentage string edge-tts wants: -50 -> "-50%", 0 -> "+0%", +50 -> "+50%".

    The sign is always written out, because edge-tts validates the format rather than the number."""
    return f"{percent:+d}%"


def online_unavailable() -> SpeechFailure:
    return SpeechFailure(ONLINE_UNAVAILABLE,
                         "The online voice could not speak. Nothing else was affected.")


def both_unavailable() -> SpeechFailure:
    return SpeechFailure(BOTH_UNAVAILABLE,
                         "Neither the online voice nor this computer's built-in one could speak. "
                         "Nothing else was affected.")


def unavailable(tried) -> SpeechFailure:
    """The right "it could not speak" answer for the voices that were actually tried."""
    attempted = tuple(tried)
    if attempted == (ONLINE, OFFLINE):
        return both_unavailable()
    if attempted == (ONLINE,):
        return online_unavailable()
    return offline_unavailable()
