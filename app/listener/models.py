"""
Data shapes defined by the Listener (docs/step4 Section 5, Phase 2).

A Transcript holds what the recognizer ACTUALLY emitted, unchanged: if Whisper returns Urdu script
it stays Urdu script, if it returns Latin text it stays Latin text. Roman Urdu is a written
representation, not an acoustic language, so nothing here transliterates, rewrites or reinterprets
what was said - that is the Brain's job in Phase 3. Anything derived (a lowercased copy for
matching, a corrected version the user approved) is a separate value, never written back into the
Transcript as though the recognizer had said it.

A VoiceFailure is the other outcome: the reason voice produced no transcript, in words that are safe
to show. Failures are distinguished by KIND so the console can react differently (a missing
microphone is not a silent room), without a class per error.

Nothing in this file touches audio, the model, the disk or the network.
"""
from dataclasses import dataclass

# Why voice produced no transcript. One flat set of kinds, matching how the Executor names outcomes.
NO_DEVICE = "no_device"                  # enumeration worked and found no microphone we can use
PERMISSION_DENIED = "permission_denied"  # Windows refused access to the microphone
DEVICE_BUSY = "device_busy"              # a microphone is there but already in use - by this process'
                                         # single-owner lock, or by the backend reporting the device
                                         # as taken. Only used when that is actually identifiable.
DEVICE_LOST = "device_lost"              # the microphone went away, or capture failed part-way
CAPTURE_UNAVAILABLE = "capture_unavailable"  # the capture BACKEND itself is unusable (sounddevice or
                                         # PortAudio missing or unimportable), so no enumeration could
                                         # even happen. Deliberately not NO_DEVICE: "no microphone"
                                         # and "no way to look for one" are different facts.
NO_SPEECH = "no_speech"                  # silence or noise only - never treated as a command
MODEL_UNAVAILABLE = "model_unavailable"  # the speech model is missing, or couldn't be loaded
TRANSCRIPTION_FAILED = "transcription_failed"  # the model ran and failed

FAILURE_KINDS = frozenset({NO_DEVICE, PERMISSION_DENIED, DEVICE_BUSY, DEVICE_LOST,
                           CAPTURE_UNAVAILABLE, NO_SPEECH, MODEL_UNAVAILABLE, TRANSCRIPTION_FAILED})


@dataclass(frozen=True)
class Transcript:
    """What the recognizer emitted for one utterance, exactly as it emitted it."""
    text: str                              # verbatim; never normalized, translated or transliterated
    language: str = ""                     # detected language code (e.g. "en", "ur"); "" when unknown
    language_probability: float | None = None  # how sure the recognizer was; None when not reported
    audio_seconds: float | None = None     # how long the captured audio was; None when not measured

    def __post_init__(self):
        if not isinstance(self.text, str):
            raise TypeError(f"Transcript.text must be str, got {type(self.text).__name__}")

    @property
    def empty(self) -> bool:
        """True when the recognizer returned nothing usable (so it must never become a command)."""
        return not self.text.strip()


@dataclass(frozen=True)
class VoiceFailure:
    """Why there is no transcript. `message` is safe to show and never contains what was said."""
    kind: str
    message: str

    def __post_init__(self):
        if self.kind not in FAILURE_KINDS:
            raise ValueError(f"Unknown voice failure kind {self.kind!r}; expected one of "
                             f"{', '.join(sorted(FAILURE_KINDS))}")


@dataclass(frozen=True)
class InputDevice:
    """One microphone the capture backend can see right now.

    `name` is the real name the driver reports, because the device list is meant to be shown to the
    user - that is how they find out what to put in `listener.input_device`. It can be ugly: a name
    may be truncated (MME cuts it at 31 characters), may repeat across host APIs for one physical
    microphone, and may even contain line breaks (a WDM-KS driver resource string does). Anything
    that displays a name should run it through logic.readable() first, and our own log lines never
    carry one at all.

    `index` is valid for THIS run only - the backend renumbers devices between boots and whenever
    hardware appears or disappears.
    """
    index: int
    name: str
    host_api: str               # "MME", "Windows WASAPI", ... - the path, not the hardware
    max_input_channels: int
    default_samplerate: float
    is_default: bool            # the backend's default input device
    is_default_host_api: bool   # this device is reached through the backend's default host API


@dataclass(frozen=True)
class ListenerSettings:
    """The validated `listener` section of config.yaml. Built by listener.logic, never by hand."""
    enabled: bool
    model_size: str
    model_dir: str
    local_files_only: bool
    device: str          # "auto", "cpu" or "cuda" - resolved against real hardware at load time
    compute_type: str
    language: str        # "auto" or a language code
    sample_rate: int      # canonical Listener/ASR rate: 16000, the only accepted value
    input_device: str | int  # "" for the system default
    vad_filter: bool
    min_silence_ms: int
    max_utterance_seconds: float
    initial_prompt_terms: tuple[str, ...]
    voice_stop_enabled: bool

    @property
    def initial_prompt(self) -> str:
        """The recognizer hint built from the configured terms, or "" when there are none."""
        return " ".join(self.initial_prompt_terms)
