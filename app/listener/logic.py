"""
Listener decision-making: reading and validating the `listener` section of config.yaml, and the
mechanical text rules the Listener is allowed to apply (docs/step4 Section 5, Phase 2).

This file is PURE. It imports only listener.models and config.settings - never listener.adapter,
never faster-whisper, sounddevice, audio or the model. Capture and transcription happen in
app/listener/adapter.py, which returns a Transcript or a VoiceFailure; those values then flow
through here and on to the voice console, which is the only place that calls
app.console.handle_command(). That direction (adapter -> models -> pure logic -> orchestration)
is what keeps language, normalization and error policy testable with no I/O to mock.

What this module may do to text is deliberately narrow: whitespace, duplicate punctuation and a
lowercased copy for matching. It never translates, transliterates, expands, guesses or rewrites
meaning - Roman Urdu interpretation, synonyms and loosely-worded intent are Phase 3's (the Brain's)
job, and the raw Transcript is always preserved alongside anything derived from it.
"""
import re

from app.listener.models import ListenerSettings
from config.settings import SettingsError, get_setting

LISTENER = "listener"

MODEL_SIZES = ("tiny", "tiny.en", "base", "base.en", "small", "small.en",
               "medium", "medium.en", "large-v2", "large-v3")
DEVICES = ("auto", "cpu", "cuda")
# ctranslate2's names. "auto" lets the adapter pick the best the real hardware supports.
COMPUTE_TYPES = ("auto", "int8", "int8_float32", "int8_float16", "int16", "float16", "float32")
# 16 kHz is the canonical rate the Listener and the model consume, and the ONLY accepted value.
# No resampling path exists or has been approved, so another rate must not be configurable: if a real
# microphone turns out to be unable to give 16 kHz, that needs a designed capture -> resample ->
# canonical 16 kHz mono path first, not a second rate setting.
SAMPLE_RATES = (16000,)
AUTO = "auto"

_LANGUAGE = re.compile(r"^[a-z]{2,3}(-[a-z]{2,4})?$")  # "en", "ur", "hi", "pt-br" - not a word list
_SPACES = re.compile(r"\s+")
_REPEATED_PUNCTUATION = re.compile(r"([,.!?;:])\1+")


# --- Settings ---------------------------------------------------------------------------------

def listener_settings() -> ListenerSettings:
    """The validated `listener` config section. Raises SettingsError naming the offending key."""
    return ListenerSettings(
        enabled=_flag("listener.enabled"),
        model_size=_choice("listener.model_size", MODEL_SIZES),
        model_dir=_text("listener.model_dir"),
        local_files_only=_flag("listener.local_files_only"),
        device=_choice("listener.device", DEVICES),
        compute_type=_choice("listener.compute_type", COMPUTE_TYPES),
        language=_language("listener.language"),
        sample_rate=_choice("listener.sample_rate", SAMPLE_RATES),
        input_device=_input_device("listener.input_device"),
        vad_filter=_flag("listener.vad_filter"),
        min_silence_ms=_positive_int("listener.min_silence_ms"),
        max_utterance_seconds=_positive_number("listener.max_utterance_seconds"),
        initial_prompt_terms=_terms("listener.initial_prompt_terms"),
        voice_stop_enabled=_flag("listener.voice_stop_enabled"),
    )


# --- The only text changes the Listener may make ------------------------------------------------

def tidy(text: str) -> str:
    """Mechanical clean-up of one transcript: outer whitespace, runs of spaces, repeated punctuation.

    Nothing here changes a single word. It is safe to apply to any language and to Roman Urdu,
    because it never touches letters."""
    if not isinstance(text, str):
        raise TypeError(f"tidy() takes str, got {type(text).__name__}")
    return _REPEATED_PUNCTUATION.sub(r"\1", _SPACES.sub(" ", text)).strip()


def for_matching(text: str) -> str:
    """A lowercased, tidied copy used only to compare against fixed words (e.g. a stop phrase).

    This is never shown as what was heard and never sent anywhere as the transcript."""
    return tidy(text).lower()


def is_silence(text: str) -> bool:
    """True when a transcript carries no words - silence or noise must never become a command."""
    return not any(character.isalnum() for character in tidy(text))


# --- Validation helpers (the project's style: raise SettingsError naming the key) ----------------

def _value(name: str):
    return get_setting(name)


def _flag(name: str) -> bool:
    value = _value(name)
    if not isinstance(value, bool):
        raise SettingsError(f"Setting '{name}' must be true or false, got {value!r}.")
    return value


def _choice(name: str, allowed):
    value = _value(name)
    if isinstance(value, bool) or value not in allowed:
        raise SettingsError(f"Setting '{name}' must be one of "
                            f"{', '.join(str(option) for option in allowed)}, got {value!r}.")
    return value


def _text(name: str) -> str:
    value = _value(name)
    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f"Setting '{name}' must be a non-empty string, got {value!r}.")
    return value.strip()


def _language(name: str) -> str:
    value = _value(name)
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned == AUTO or _LANGUAGE.match(cleaned):
            return cleaned
    raise SettingsError(f"Setting '{name}' must be '{AUTO}' or a language code such as en, ur or hi, "
                        f"got {value!r}.")


def _input_device(name: str):
    """"" means the system default; otherwise a device name or its index."""
    value = _value(name)
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise SettingsError(f"Setting '{name}' must be a microphone name, its index, or \"\" for the "
                            f"default, got {value!r}.")
    if isinstance(value, int) and value < 0:
        raise SettingsError(f"Setting '{name}' must not be a negative index, got {value!r}.")
    return value.strip() if isinstance(value, str) else value


def _positive_int(name: str) -> int:
    value = _value(name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SettingsError(f"Setting '{name}' must be a positive whole number, got {value!r}.")
    return value


def _positive_number(name: str) -> float:
    value = _value(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise SettingsError(f"Setting '{name}' must be a positive number, got {value!r}.")
    return float(value)


def _terms(name: str) -> tuple[str, ...]:
    value = _value(name)
    if not isinstance(value, (list, tuple)) or not all(
            isinstance(term, str) and term.strip() for term in value):
        raise SettingsError(f"Setting '{name}' must be a list of non-empty words, got {value!r}.")
    return tuple(term.strip() for term in value)
