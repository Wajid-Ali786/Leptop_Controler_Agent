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

A Recording is what one bounded capture produced: canonical 16 kHz mono PCM, in memory. It can HOLD
audio, but nothing in this file opens a device, loads the model, or touches the disk or the network.
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
LANGUAGE_UNSUPPORTED = "language_unsupported"  # listener.language names a language the loaded
                                         # model does not know. Refused BEFORE any recognition
                                         # starts, so it is never confused with a model that ran
                                         # and failed.

FORMAT_UNSUPPORTED = "format_unsupported"  # the chosen microphone exists and the backend works, but
                                         # THAT device on THAT sound path refuses canonical mono 16 kHz
                                         # int16. Never "fixed" by switching path, device or rate.

FAILURE_KINDS = frozenset({NO_DEVICE, PERMISSION_DENIED, DEVICE_BUSY, DEVICE_LOST,
                           CAPTURE_UNAVAILABLE, FORMAT_UNSUPPORTED, NO_SPEECH, MODEL_UNAVAILABLE,
                           TRANSCRIPTION_FAILED, LANGUAGE_UNSUPPORTED})

# The only audio format a Recording ever holds (Feature 1 locked the rate; Task 2a showed the default
# path accepts it). Raw little-endian signed 16-bit samples, one channel.
SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = "int16"
BYTES_PER_FRAME = 2

# Why a capture stopped - only what the capture primitive itself knows. Silence, "speech complete" or
# "no speech" are judgements about the sound, which belong to later Listener work, not to capture.
LIMIT = "limit"          # the maximum length was reached
CANCELLED = "cancelled"  # the caller's cancel event was set
END_REASONS = frozenset({LIMIT, CANCELLED})


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
        probability = self.language_probability
        if probability is not None and (isinstance(probability, bool)
                                        or not isinstance(probability, (int, float))):
            raise TypeError("Transcript.language_probability must be a number, or None when the "
                            "recognizer did not measure one")

    @property
    def empty(self) -> bool:
        """True when the recognizer returned nothing usable (so it must never become a command)."""
        return not self.text.strip()

    def __repr__(self) -> str:
        # What was SAID is the most private thing this project holds. A repr lands in logs, tracebacks
        # and debuggers - none of which should ever hold speech - so it describes the text by length
        # only. Code that means to show or use what was said reaches for `.text` deliberately.
        characters = len(self.text) if isinstance(self.text, str) else 0
        return (f"Transcript(characters={characters}, language={self.language!r}, "
                f"language_probability={self.language_probability!r}, "
                f"audio_seconds={self.audio_seconds!r})")

    __str__ = __repr__  # so print()/f-strings/%s can't reach the text by an inherited route either


@dataclass(frozen=True)
class VoiceFailure:
    """Why there is no transcript. `message` is safe to show and never contains what was said."""
    kind: str
    message: str

    def __post_init__(self):
        if self.kind not in FAILURE_KINDS:
            raise ValueError(f"Unknown voice failure kind {self.kind!r}; expected one of "
                             f"{', '.join(sorted(FAILURE_KINDS))}")

    def __repr__(self) -> str:
        # The message is for the SCREEN: a device-selection failure legitimately lists real microphone
        # names so the user can pick one. A repr lands in logs, tracebacks and debuggers, so it
        # describes the message by length only - code that means to show it uses `.message`.
        length = len(self.message) if isinstance(self.message, str) else 0
        return f"VoiceFailure(kind={self.kind!r}, message=<{length} characters>)"

    __str__ = __repr__  # so print()/f-strings/%s can't reach the message by an inherited route either


@dataclass(frozen=True)
class Recording:
    """One bounded microphone capture, held in memory only.

    `pcm` is little-endian signed 16-bit mono at 16000 Hz - exactly SAMPLE_RATE/CHANNELS/DTYPE, checked
    on construction. The length is DERIVED from the bytes (frames, seconds), never measured by a clock,
    so it can't disagree with the audio. Nothing here names the microphone. For an explicitly selected
    device, `device_index` is the backend's number for it in THIS run. On the default path it is None:
    the stream is opened as "whatever the default is now", and an index enumerated a moment earlier
    could already be stale - so it isn't claimed.

    The repr and str never contain a sample. There is no save method, and nothing keeps a copy: the
    audio lives exactly as long as this object does."""
    pcm: bytes
    stopped_by: str          # LIMIT or CANCELLED
    overflows: int = 0       # times the backend reported input overflow (audio it had to drop)
    device_index: int | None = None
    used_default: bool = True
    sample_rate: int = SAMPLE_RATE
    channels: int = CHANNELS
    dtype: str = DTYPE

    def __post_init__(self):
        if not isinstance(self.pcm, bytes):
            raise TypeError(f"Recording.pcm must be bytes, got {type(self.pcm).__name__}")
        if len(self.pcm) % BYTES_PER_FRAME:
            raise ValueError("Recording.pcm must hold whole 16-bit samples")
        if (self.sample_rate, self.channels, self.dtype) != (SAMPLE_RATE, CHANNELS, DTYPE):
            raise ValueError(f"A Recording is always {SAMPLE_RATE} Hz, {CHANNELS} channel, {DTYPE}")
        if self.stopped_by not in END_REASONS:
            raise ValueError(f"Unknown capture ending {self.stopped_by!r}; expected one of "
                             f"{', '.join(sorted(END_REASONS))}")
        if isinstance(self.overflows, bool) or not isinstance(self.overflows, int) or self.overflows < 0:
            raise ValueError("Recording.overflows must be a count")

    @property
    def frames(self) -> int:
        """Samples captured (one channel, so frames and samples are the same number)."""
        return len(self.pcm) // BYTES_PER_FRAME

    @property
    def seconds(self) -> float:
        return self.frames / self.sample_rate

    def __repr__(self) -> str:
        return (f"Recording(frames={self.frames}, seconds={self.seconds:.3f}, "
                f"sample_rate={self.sample_rate}, channels={self.channels}, dtype={self.dtype!r}, "
                f"stopped_by={self.stopped_by!r}, overflows={self.overflows}, "
                f"device_index={self.device_index!r}, used_default={self.used_default!r})")

    __str__ = __repr__


@dataclass(frozen=True)
class ModelStatus:
    """What app.listener.adapter.ensure_model() made ready. Safe metadata only: no model object, no
    path (paths carry the user name), no exception text.

    load_seconds is the time to find, verify and construct the model on the call that actually loaded
    it - excluding the one-off import of the speech library. A reused model did no loading, so its
    load_seconds is None rather than a number that would pretend otherwise."""
    model_size: str
    device: str                        # what it really runs on: "cpu" or "cuda"
    compute_type: str                  # what ctranslate2 was really told, never "auto"/"default"
    reused: bool
    load_seconds: float | None
    fell_back_from: str | None = None  # "cuda" when device=auto fell back to CPU
    fallback_reason: str | None = None  # the category of that CUDA failure

    def __post_init__(self):
        if self.reused != (self.load_seconds is None):
            raise ValueError("load_seconds is measured for a real load, and None for a reused model")
        if (self.fell_back_from is None) != (self.fallback_reason is None):
            raise ValueError("a fallback names both what it fell back from and why")


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

    def __repr__(self) -> str:
        # The name is meant to be SHOWN - that is how the user finds out what to put in
        # listener.input_device - but a repr lands in logs, tracebacks and debuggers, where a device
        # name never belongs. So it is left out here, and code that displays a list reaches for
        # `.name` (through logic.readable()) deliberately.
        return (f"InputDevice(index={self.index}, host_api={self.host_api!r}, "
                f"max_input_channels={self.max_input_channels}, "
                f"default_samplerate={self.default_samplerate!r}, is_default={self.is_default!r}, "
                f"is_default_host_api={self.is_default_host_api!r})")

    __str__ = __repr__


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

    def __repr__(self) -> str:
        # Safe operational configuration only. The prompt terms are what the recognizer is told, and
        # are never logged; model_dir and input_device can hold a real path (which carries the user's
        # name) or a real device name, so both are described rather than printed. Every field is
        # still there for code that needs it.
        return (f"ListenerSettings(enabled={self.enabled!r}, model_size={self.model_size!r}, "
                f"model_dir=<configured>, local_files_only={self.local_files_only!r}, "
                f"device={self.device!r}, compute_type={self.compute_type!r}, "
                f"language={self.language!r}, sample_rate={self.sample_rate!r}, "
                f"input_device=<{'default' if self.input_device == '' else 'configured'}>, "
                f"vad_filter={self.vad_filter!r}, min_silence_ms={self.min_silence_ms!r}, "
                f"max_utterance_seconds={self.max_utterance_seconds!r}, "
                f"initial_prompt_term_count={len(self.initial_prompt_terms)}, "
                f"voice_stop_enabled={self.voice_stop_enabled!r})")

    __str__ = __repr__
