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
import math
import re
from pathlib import Path
from typing import NamedTuple

from app.listener.models import (DEVICE_BUSY, DEVICE_LOST, FORMAT_UNSUPPORTED, MODEL_UNAVAILABLE,
                                 NO_DEVICE, PERMISSION_DENIED, SAMPLE_RATE, InputDevice,
                                 ListenerSettings, VoiceFailure)
from config.settings import PROJECT_ROOT, SettingsError, get_setting

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
        local_files_only=_local_files_only("listener.local_files_only"),
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


def uses_default(selector) -> bool:
    """True when `listener.input_device` asks for the system default ("" or whitespace)."""
    return isinstance(selector, str) and not selector.strip()


# --- How long a capture may run ------------------------------------------------------------------

def capture_limit(configured: float, requested=None) -> float:
    """The maximum a capture may last: the caller may LOWER the configured cap, never raise it.

    A bad request (zero, negative, not a number, infinite) is a programming error and raises
    ValueError - it is not something to quietly replace with the cap."""
    if requested is None:
        return float(configured)
    if (isinstance(requested, bool) or not isinstance(requested, (int, float))
            or not math.isfinite(requested) or requested <= 0):
        raise ValueError(f"max_seconds must be a positive number of seconds, got {requested!r}")
    return min(float(requested), float(configured))


def max_frames(seconds: float) -> int:
    """How many samples `seconds` allows - rounded DOWN, so a capture never exceeds its maximum."""
    frames = math.floor(seconds * SAMPLE_RATE)
    if frames < 1:
        raise ValueError(f"{seconds!r} seconds is shorter than one sample at {SAMPLE_RATE} Hz")
    return frames


# --- What a backend failure means ----------------------------------------------------------------
# Only failures with evidence behind them are named. The adapter hands over the PortAudio error code
# and, for a host error, the host API's name and its own code - never the error TEXT, which can
# contain device names. A failure not listed here is not relabelled: backend_failure() returns None
# and the adapter lets the original exception propagate after cleanup, so it is seen, not disguised.

PA_UNANTICIPATED_HOST_ERROR = -9999
PA_INVALID_CHANNEL_COUNT = -9998
PA_INVALID_SAMPLE_RATE = -9997
PA_INVALID_DEVICE = -9996
PA_SAMPLE_FORMAT_NOT_SUPPORTED = -9994
_FORMAT_CODES = frozenset({PA_INVALID_CHANNEL_COUNT, PA_INVALID_SAMPLE_RATE,
                           PA_SAMPLE_FORMAT_NOT_SUPPORTED})

MME, WASAPI = "MME", "Windows WASAPI"
# (host API, host error code) -> kind. Documented Windows codes; the HRESULTs are compared unsigned.
_HOST_ERRORS = {
    (MME, 2): DEVICE_LOST,                      # MMSYSERR_BADDEVICEID - no longer there
    (MME, 4): DEVICE_BUSY,                      # MMSYSERR_ALLOCATED - another program has it
    (MME, 6): DEVICE_LOST,                      # MMSYSERR_NODRIVER - its driver went away
    (WASAPI, 0x80070005): PERMISSION_DENIED,    # E_ACCESSDENIED - Windows privacy said no
    (WASAPI, 0x8889000A): DEVICE_BUSY,          # AUDCLNT_E_DEVICE_IN_USE - held exclusively
    (WASAPI, 0x88890004): DEVICE_LOST,          # AUDCLNT_E_DEVICE_INVALIDATED - unplugged/disabled
}

FAILURE_MESSAGES = {
    DEVICE_BUSY: "The microphone is being used by another program, so it couldn't be opened. Close "
                 "whatever is using it and try again.",
    DEVICE_LOST: "The microphone went away while it was being opened or used (it may have been "
                 "unplugged or disabled). Check it is connected and try again.",
    PERMISSION_DENIED: "Windows refused access to the microphone. Allow desktop apps to use it in "
                       "Settings > Privacy > Microphone, then try again.",
}
STALLED_MESSAGE = ("The microphone stopped delivering sound before the recording finished, so it was "
                   "abandoned. Check it is connected and try again.")


def backend_failure(code, host_api=None, host_code=None) -> VoiceFailure | None:
    """The truthful VoiceFailure for a PortAudio error, or None when there is no evidence for one.

    FORMAT_UNSUPPORTED comes back with a placeholder message: the adapter replaces it with
    format_refusal(), which knows which device and path were involved."""
    if code in _FORMAT_CODES:
        return VoiceFailure(FORMAT_UNSUPPORTED, "canonical 16 kHz mono int16 was refused")
    if code == PA_INVALID_DEVICE:  # it was in the device list a moment ago, then it wasn't
        return VoiceFailure(DEVICE_LOST, FAILURE_MESSAGES[DEVICE_LOST])
    if code == PA_UNANTICIPATED_HOST_ERROR and isinstance(host_code, int):
        kind = _HOST_ERRORS.get((host_api, host_code & 0xFFFFFFFF))
        if kind:
            return VoiceFailure(kind, FAILURE_MESSAGES[kind])
    return None


def format_refusal(device: InputDevice, used_default: bool, devices) -> VoiceFailure:
    """FORMAT_UNSUPPORTED with a message the user can act on. It says what refused and what else might
    work - and that nothing was switched, because an explicit choice has to stay explicit."""
    if used_default:
        return VoiceFailure(FORMAT_UNSUPPORTED, (
            f"Your default microphone ([{device.index}] {readable(device.name)} via {device.host_api}) "
            f"refused to record 16 kHz mono, the format voice input needs. Nothing else was tried. "
            f"Choose another microphone as the Windows default, or name one in listener.input_device."))
    others = sorted({other.host_api for other in devices if other.host_api != device.host_api})
    elsewhere = (f" The same hardware is often also reachable through another sound path on this "
                 f"computer ({', '.join(others)}), which may accept it - choose that entry by name or "
                 f"index in listener.input_device." if others else "")
    return VoiceFailure(FORMAT_UNSUPPORTED, (
        f"The microphone you selected, [{device.index}] {readable(device.name)} on {device.host_api}, "
        f"refused to record 16 kHz mono (the format voice input needs) on that sound path."
        f"{elsewhere} The assistant did not switch to anything automatically."))


# --- The speech model: where it lives, what "complete" means, and where it runs -------------------
# Pure policy for app/listener/adapter.ensure_model(). The adapter gathers the evidence - the files in
# the snapshot folder, what ctranslate2 reports the machine supports - and these functions decide.
# Nothing here assumes the development laptop: every choice is made from reported capabilities.

FETCH_COMMAND = "python scripts/fetch_voice_model.py"
CPU, CUDA = "cpu", "cuda"
# Tried in order; the first one ctranslate2 REPORTS as supported on the device wins. A compatibility
# and memory order, not a benchmark result: int8 keeps weights in 8 bits on CPU (which cannot run
# float16 at all); CUDA prefers half precision, then the quantized forms, then full precision.
COMPUTE_PREFERENCE = {CPU: ("int8", "float32"),
                      CUDA: ("float16", "int8_float16", "int8", "float32")}

# What the installed faster-whisper 1.2.1 + ctranslate2 4.8.2 read from a model folder. tokenizer.json
# matters most: without it faster-whisper fetches a tokenizer from the internet, whatever
# local_files_only says - so a folder missing it is refused BEFORE the model is ever constructed.
REQUIRED_MODEL_FILES = ("config.json", "model.bin", "tokenizer.json")
# At least one of these: ctranslate2's converter writes vocabulary.json, and its runtime also reads
# the older vocabulary.txt, which the published Systran conversions use. A snapshot carrying both is
# fine - the loader picks the one it wants, and refusing it would reject a valid future model.
VOCABULARY_FILES = ("vocabulary.json", "vocabulary.txt")
OPTIONAL_MODEL_FILES = ("preprocessor_config.json",)  # faster-whisper checks it exists before reading

# Why a model construction failed - a category, never the exception's text (which carries paths).
LOAD_ERROR, OUT_OF_MEMORY, REJECTED_SETTINGS = "load_error", "out_of_memory", "rejected_settings"
CUDA_RUNTIME = "cuda_runtime"  # CUDA itself, or a CUDA runtime library, failed - not the model files
# ONLY a cuda_runtime failure justifies trying the CPU afterwards. A model that cannot be read is not
# a CUDA problem: a truncated model.bin passes the "exists and is not empty" check and then fails
# construction with a plain "Unable to open file" - falling back would fail again for the same reason
# and blame CUDA for it.
FALLBACK_CATEGORIES = frozenset({CUDA_RUNTIME})
CATEGORY_TEXT = {LOAD_ERROR: "the model or its runtime libraries couldn't be loaded",
                 OUT_OF_MEMORY: "there wasn't enough memory",
                 REJECTED_SETTINGS: "the device rejected the settings",
                 CUDA_RUNTIME: "CUDA or a CUDA runtime library failed"}

# The only messages accepted as evidence that CUDA ITSELF failed. Every one of them is a message
# template in the INSTALLED ctranslate2 4.8.2 binary, and the first was also seen on this machine
# (asking for a CUDA device here raises "CUDA failed with error CUDA driver version is insufficient
# for CUDA runtime version"). Nothing is matched from memory or expectation.
#   "cuda failed with error"            - ctranslate2.dll, and observed on this machine
#   "cublas failed with status"         - ctranslate2.dll
#   "is not found or cannot be loaded"  - ctranslate2.dll, beside its cuBLAS/CUDA_PATH loader strings
# Matching text is fragile: if ctranslate2 rewords these, nothing matches and the failure is treated
# as a plain load error - which means NO fallback. That is the safe direction, on purpose.
CUDA_FAILURE_PATTERNS = ("cuda failed with error", "cublas failed with status",
                         "is not found or cannot be loaded")


class LoadPlan(NamedTuple):
    device: str
    compute_type: str


class Refusal(NamedTuple):
    """Why no plan is possible: a MODEL_MESSAGES code, and the device it concerns."""
    code: str
    device: str


def model_root(model_dir: str) -> Path:
    """listener.model_dir as a folder: relative paths are relative to the project, not the shell."""
    path = Path(model_dir)
    return path if path.is_absolute() else PROJECT_ROOT / path


def missing_model_files(sizes) -> tuple[str, ...]:
    """Given {file name: size in bytes} for a model folder, what is missing or empty (sorted).
    Every required file must be there and non-empty, plus exactly one vocabulary file."""
    missing = [name for name in REQUIRED_MODEL_FILES if sizes.get(name, 0) <= 0]
    if not any(sizes.get(name, 0) > 0 for name in VOCABULARY_FILES):
        missing.append(" or ".join(VOCABULARY_FILES))
    return tuple(missing)


def choose_compute_type(device: str, requested: str, supported) -> str | None:
    """The compute type to use on `device`, or None. `auto` walks COMPUTE_PREFERENCE; an explicit type
    must itself be reported supported - it is never swapped for another. Never returns "auto" or
    "default": ctranslate2 is always told exactly what to use."""
    if requested == AUTO:
        return next((kind for kind in COMPUTE_PREFERENCE[device] if kind in supported), None)
    return requested if requested in supported else None


def plan_model_load(device: str, compute_type: str, cpu_types, cuda_count: int,
                    cuda_types) -> LoadPlan | Refusal:
    """Where the model should run, decided only from what ctranslate2 reported on THIS machine.

    cuda_types is None when CUDA inspection failed. `cuda` never becomes CPU and `cpu` never becomes
    CUDA. `auto` takes CUDA only when a device exists AND a compute type can be chosen for it;
    otherwise CPU."""
    cuda_usable = cuda_count >= 1 and cuda_types is not None
    if device == CUDA and not cuda_usable:
        return Refusal("cuda_unavailable", CUDA)
    if device in (CUDA, AUTO) and cuda_usable:
        chosen = choose_compute_type(CUDA, compute_type, cuda_types)
        if chosen:
            return LoadPlan(CUDA, chosen)
        if device == CUDA:
            return Refusal(_compute_code(compute_type), CUDA)
    chosen = choose_compute_type(CPU, compute_type, cpu_types)
    return LoadPlan(CPU, chosen) if chosen else Refusal(_compute_code(compute_type), CPU)


def construction_failure(exception: BaseException, device: str) -> str:
    """Why a WhisperModel construction failed, as a CATEGORY. The exception is examined here and goes
    no further: only the category is ever shown or logged.

    A CUDA construction is called a CUDA failure only when its message matches one of the templates
    ctranslate2 really produces (CUDA_FAILURE_PATTERNS). Anything else - an unreadable model, a
    filesystem error, an unfamiliar message - stays a plain load error, so it can never be blamed on
    CUDA or "fixed" by falling back to the CPU. A MemoryError from the host is not a CUDA failure
    either: loading on the CPU needs MORE host memory, not less."""
    if isinstance(exception, ValueError):
        return REJECTED_SETTINGS
    if isinstance(exception, MemoryError):
        return OUT_OF_MEMORY
    text = str(exception).lower()
    if device == CUDA and any(pattern in text for pattern in CUDA_FAILURE_PATTERNS):
        return CUDA_RUNTIME
    return LOAD_ERROR


def may_fall_back(requested_device: str, plan: LoadPlan, category: str) -> bool:
    """CUDA -> CPU only when the user said `auto`, CUDA was chosen from the evidence, and CUDA ITSELF
    is what failed. Never for `cuda`, never for a model that cannot be read, never for bad settings."""
    return requested_device == AUTO and plan.device == CUDA and category in FALLBACK_CATEGORIES


def _compute_code(requested: str) -> str:
    return "no_compute_type" if requested == AUTO else "compute_unsupported"


MODEL_MESSAGES = {
    "not_downloaded": "The '{size}' speech model isn't downloaded yet. Voice input never downloads "
                      "anything by itself - to get it, run: {fetch}",
    "incomplete": "The downloaded '{size}' speech model is incomplete (missing or empty: {missing}). "
                  "Run {fetch} again to finish it.",
    "folder_unreadable": "The '{size}' speech model's folder couldn't be read. Run {fetch} again.",
    "backend_missing": "Speech recognition isn't available on this computer: the faster-whisper "
                       "library couldn't be loaded.",
    "cuda_unavailable": "listener.device is 'cuda', but no usable CUDA GPU was found. Set it to 'auto' "
                        "or 'cpu' - the assistant never switches to the CPU on its own.",
    "no_compute_type": "None of the computation types voice input can use is supported on {device} "
                       "(it supports: {supported}).",
    "compute_unsupported": "listener.compute_type '{requested}' isn't supported on {device} (it "
                           "supports: {supported}). It is never swapped for another type automatically.",
    "load_failed": "The '{size}' speech model couldn't be loaded on {device}: {reason}.",
    "fallback_failed": "The '{size}' speech model couldn't be loaded on CUDA ({cuda_reason}), nor "
                       "afterwards on CPU ({reason}).",
    "restart_required": "The speech model settings changed after the model was loaded. Restart the "
                        "assistant to use the new settings.",
    "download_refused": "listener.local_files_only must be true: voice input never downloads anything "
                        "during normal use. To download the speech model, run: {fetch}",
    "download_failed": "The '{size}' speech model couldn't be downloaded ({category}). Check the "
                       "internet connection and run {fetch} again - what already arrived is kept.",
}


def model_unavailable(code: str, **values) -> VoiceFailure:
    """MODEL_UNAVAILABLE with fixed wording for `code`. Values are settings and category words we
    chose ourselves - never exception text or paths."""
    return VoiceFailure(MODEL_UNAVAILABLE, MODEL_MESSAGES[code].format(fetch=FETCH_COMMAND, **values))


# --- Validation helpers (the project's style: raise SettingsError naming the key) ----------------

def _value(name: str):
    return get_setting(name)


def _local_files_only(name: str) -> bool:
    """Must be true: normal use never downloads. The fetch script is the one way to get a model."""
    if _value(name) is not True:
        raise SettingsError(f"Setting '{name}' must be true: voice input never downloads anything during "
                            f"normal use. To download the speech model, run: {FETCH_COMMAND}")
    return True


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
