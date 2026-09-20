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

from app.listener.models import NO_DEVICE, InputDevice, ListenerSettings, VoiceFailure
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


# --- Choosing which microphone `listener.input_device` means -------------------------------------
# Pure on purpose: it is handed the device list and decides, so every matching rule below is
# testable with no microphone, no backend and no I/O. The adapter only enumerates and obeys.

MAX_NAME_CHARACTERS = 80  # a driver name this long is already unreadable; longer ones are cut
MAX_LISTED_CANDIDATES = 5  # an ambiguity message stays readable rather than listing everything


def readable(name) -> str:
    """A device name flattened and shortened for DISPLAY.

    Driver names are not tidy strings: MME truncates them at 31 characters, and a WDM-KS name on
    this machine contains a literal line break inside a driver resource path. Anything that shows a
    name to the user sends it through here first."""
    flat = _SPACES.sub(" ", str(name)).strip()
    return flat if len(flat) <= MAX_NAME_CHARACTERS else flat[:MAX_NAME_CHARACTERS - 3] + "..."


def choose_device(selector, devices) -> InputDevice | VoiceFailure:
    """The microphone `selector` names, out of `devices`, or a VoiceFailure saying why there isn't one.

    "" (or whitespace) means the backend's own default microphone. A whole number is used as a device
    index. A name is matched in two passes - exact (ignoring case) first, then substring - and where
    ONE physical microphone appears once per host API, the default host API breaks the tie. Anything
    still ambiguous is refused with the candidates listed: a device is never picked arbitrarily.
    """
    devices = tuple(devices)
    if isinstance(selector, bool):  # True/False is never a device; be explicit rather than surprising
        return VoiceFailure(NO_DEVICE, f"{selector!r} does not name a microphone. Use \"\" for the "
                                       f"default one, or a name or index from the device list.")
    if isinstance(selector, int):
        return _device_by_index(selector, devices)
    return _device_by_name(str(selector).strip(), devices) if str(selector).strip() \
        else _default_device(devices)


def _default_device(devices) -> InputDevice | VoiceFailure:
    if not devices:
        return VoiceFailure(NO_DEVICE, "This computer has no microphone the assistant can use.")
    for device in devices:
        if device.is_default:
            return device
    return VoiceFailure(NO_DEVICE, "There is no default microphone set on this computer. Put a name "
                                   "from the device list in listener.input_device.")


def _device_by_index(index: int, devices) -> InputDevice | VoiceFailure:
    for device in devices:
        if device.index == index:
            return device
    return VoiceFailure(NO_DEVICE, f"No microphone has index {index} right now. Indexes change "
                                   f"between restarts, so a name in listener.input_device is safer.")


def _device_by_name(wanted: str, devices) -> InputDevice | VoiceFailure:
    folded = wanted.casefold()
    exact = [device for device in devices if device.name.strip().casefold() == folded]
    candidates = exact or [device for device in devices if folded in device.name.casefold()]
    if not candidates:
        return VoiceFailure(NO_DEVICE, f"No microphone on this computer is called '{readable(wanted)}'. "
                                       f"Use a name from the device list.")
    if len(candidates) == 1:
        return candidates[0]
    # One microphone usually appears once per host API (MME, DirectSound, WASAPI, WDM-KS). Preferring
    # the default host API resolves exactly that duplication - and nothing else.
    on_default_api = [device for device in candidates if device.is_default_host_api]
    if len(on_default_api) == 1:
        return on_default_api[0]
    return VoiceFailure(NO_DEVICE, f"'{readable(wanted)}' matches {len(candidates)} microphones "
                                   f"({_listed(candidates)}). Set listener.input_device to one of "
                                   f"those names in full, or to its index.")


def _listed(devices) -> str:
    shown = "; ".join(f"[{device.index}] {readable(device.name)} via {device.host_api}"
                      for device in devices[:MAX_LISTED_CANDIDATES])
    extra = len(devices) - MAX_LISTED_CANDIDATES
    return f"{shown}; and {extra} more" if extra > 0 else shown


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
