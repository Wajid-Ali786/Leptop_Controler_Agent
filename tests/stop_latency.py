"""
Measurement plumbing for the spoken-stop feasibility slice (Task 6c). Not a test module.

It answers one question with numbers instead of argument: how long does the CURRENT approved path -
microphone -> Recording -> adapter.transcribe -> logic.is_stop_phrase - take to produce a stop_match
on this machine? Nothing here builds a StopGuard, imports the Executor or the emergency stop, runs a
command, or changes any product setting: the only settings ever used are per-measurement copies made
with dataclasses.replace.

Two groups use it:

    tests/test_stop_latency_synthetic.py   S1/S2/S3 on one deterministic 1-second Recording. Real
                                          model, no microphone.
    tests/test_stop_latency_real.py        three real spoken "stop" trials, 1.5 s bounded captures.

The synthetic cases exist because of a source finding: with vad_filter on, silence is removed
entirely (collect_chunks returns an empty array), the feature extractor then yields one frame, so
content_frames is 0 and the segment loop exits WITHOUT any encoder pass. S1 records that behaviour;
S2 turns VAD off on its own copy of the settings so the same silence really does reach the encoder,
which is the floor any real utterance must exceed; S3 differs from S2 only by language policy, to
quantify the extra encode that automatic language detection performs. Whether the encoder really ran
is not inferred from the clock - it is counted (see counted_encoder_passes).

Preload is never inside a measured interval, and no measurement retries anything.
"""
import contextlib
import dataclasses
import statistics
import time

from app.listener import adapter, logic
from app.listener.models import LIMIT, SAMPLE_RATE, Recording, Transcript, VoiceFailure

SILENT_SECONDS = 1.0        # the synthetic clip
STOP_WINDOW_SECONDS = 1.5   # the first real capture window (no Enter, no endpoint detection)
STOP_LANGUAGE = "en"        # the stop phrase is one fixed English word
TRIALS = 3
TRANSCRIPT = "transcript"   # Measurement.kind when the recognizer produced one

# (label, language asked for, vad_filter) - the VAD-off variants exist ONLY on these diagnostic
# copies of the settings; nothing in the product changes.
SYNTHETIC_CASES = (("S1", STOP_LANGUAGE, True),
                   ("S2", STOP_LANGUAGE, False),
                   ("S3", "auto", False))

CUE = 'LISTENING FOR "stop" - say it now'


def silence(seconds: float = SILENT_SECONDS) -> Recording:
    """One deterministic canonical Recording: all-zero 16-bit mono PCM at 16 kHz, in memory only.

    No file, no microphone, no playback, and the bytes are never logged."""
    frames = int(round(seconds * SAMPLE_RATE))
    return Recording(b"\x00\x00" * frames, LIMIT)


def synthetic_settings(settings, *, language: str, vad_filter: bool):
    """A per-measurement COPY. The caller's settings object is never modified."""
    return dataclasses.replace(settings, language=language, vad_filter=vad_filter)


def stop_settings(settings):
    """What a future action-time listener would use: everything as configured, but explicit English.

    The model size, compute type, VAD, min_silence_ms and initial_prompt terms all stay exactly as
    configured - "stop" is deliberately NOT added to the prompt."""
    return dataclasses.replace(settings, language=STOP_LANGUAGE)


@dataclasses.dataclass(frozen=True)
class Measurement:
    """One timed run. Safe to print anywhere: it describes the recognizer's output, never quotes it."""
    label: str
    language: str                       # what was ASKED for ("en" or "auto")
    vad_filter: bool
    audio_seconds: float
    transcription_seconds: float
    kind: str                           # "transcript", or the VoiceFailure kind
    characters: int = 0                 # how much text came back, never the text
    detected_language: str = ""
    capture_seconds: float | None = None    # real trials only
    match_seconds: float | None = None      # derive + is_stop_phrase
    total_seconds: float | None = None      # capture start -> stop_match exists
    stop_match: bool | None = None
    encoder_passes: int | None = None   # counted, not guessed; None when not instrumented

    def __repr__(self) -> str:
        parts = [f"{self.label}", f"language={self.language!r}", f"vad={self.vad_filter}",
                 f"audio={self.audio_seconds:.2f}s"]
        if self.capture_seconds is not None:
            parts.append(f"capture={self.capture_seconds:.2f}s")
        parts.append(f"transcribe={self.transcription_seconds:.2f}s")
        if self.match_seconds is not None:
            parts.append(f"match={self.match_seconds * 1000:.1f}ms")
        if self.total_seconds is not None:
            parts.append(f"total={self.total_seconds:.2f}s")
        parts += [f"kind={self.kind}", f"characters={self.characters}",
                  f"detected={self.detected_language!r}", f"stop_match={self.stop_match}",
                  f"encoder_passes={self.encoder_passes}"]
        return f"Measurement({', '.join(parts)})"

    __str__ = __repr__


class _Passes:
    """How many times the loaded model encoded a 30-second window during one measurement."""

    def __init__(self):
        self.passes = 0

    def bump(self):
        self.passes += 1


@contextlib.contextmanager
def counted_encoder_passes():
    """Count WhisperModel.encode() calls for one measurement.

    Test-only instrumentation, and the reason a floor number is interpretable at all: 0 passes means
    the path short-circuited and the clock measured nothing, 1 means one 30-second encoder window, 2
    means automatic language detection encoded as well. The wrapper is an instance attribute on the
    already-loaded model, it delegates to the real method, the model object never leaves this
    function, and the attribute is removed afterwards."""
    loaded = getattr(adapter, "_loaded", None)
    model = getattr(loaded, "model", None)
    real = getattr(model, "encode", None)
    if real is None:
        yield None
        return
    counter = _Passes()

    def encode(*args, **kwargs):
        counter.bump()
        return real(*args, **kwargs)

    model.encode = encode
    try:
        yield counter
    finally:
        try:
            del model.encode
        except AttributeError:  # pragma: no cover - only if something else replaced it meanwhile
            pass


@contextlib.contextmanager
def _no_passes():
    yield None


def require_ready(settings):
    """Make the model ready BEFORE anything is timed. Never called inside a measured interval."""
    return adapter.ensure_model(settings)


def measure_transcription(label, settings, recording, *, clock=time.monotonic, counter=None):
    """Time exactly one adapter.transcribe call on an already-loaded model."""
    started = clock()
    outcome = adapter.transcribe(recording, settings)
    seconds = clock() - started
    return _described(label, settings, recording.seconds, seconds, outcome,
                      encoder_passes=None if counter is None else counter.passes)


def synthetic_measurements(settings, recording, *, clock=time.monotonic, passes=_no_passes):
    """S1, S2 and S3 on the SAME recording. The model must already be ready."""
    measured = []
    for label, language, vad_filter in SYNTHETIC_CASES:
        variant = synthetic_settings(settings, language=language, vad_filter=vad_filter)
        with passes() as counter:
            measured.append(measure_transcription(label, variant, recording, clock=clock,
                                                  counter=counter))
    return measured


def measure_stop_trial(label, settings, *, write, clock=time.monotonic,
                       window=STOP_WINDOW_SECONDS, passes=_no_passes):
    """One real trial: visible cue, then a SYNCHRONOUS bounded capture on this thread, then
    transcription, then the pure match.

    No worker, no thread, no cancel event, no stdin: the capture is bounded by max_seconds and
    Feature 2 already closes the stream and releases the microphone before it returns. The cue is
    written before the clock starts, so the microphone can never open before the user is told."""
    write(CUE)
    started = clock()
    recording = adapter.capture(settings, max_seconds=window)
    captured = clock()
    if isinstance(recording, VoiceFailure):
        return _described(label, settings, 0.0, 0.0, recording,
                          capture_seconds=captured - started, total_seconds=captured - started)
    with passes() as counter:
        transcribed_at, outcome = _transcribe(recording, settings, clock)
    match_started = clock()
    stop_match = None
    if isinstance(outcome, Transcript):
        stop_match = logic.derive_transcript(outcome).stop_match  # the one approved pure matcher
    matched = clock()
    return _described(
        label, settings, recording.seconds, transcribed_at - captured, outcome,
        capture_seconds=captured - started, match_seconds=matched - match_started,
        total_seconds=matched - started, stop_match=stop_match,
        encoder_passes=None if counter is None else counter.passes)


def _transcribe(recording, settings, clock):
    outcome = adapter.transcribe(recording, settings)
    return clock(), outcome


def stop_trials(settings, *, write, trials=TRIALS, clock=time.monotonic,
                window=STOP_WINDOW_SECONDS, passes=_no_passes):
    """`trials` independent trials. A failed or misheard trial is DATA: nothing is retried, and
    nothing is tuned between trials."""
    return [measure_stop_trial(f"trial {number}", settings, write=write, clock=clock, window=window,
                               passes=passes)
            for number in range(1, trials + 1)]


def _described(label, settings, audio_seconds, transcription_seconds, outcome, **extra):
    """Turn one outcome into safe metadata. The transcript's text is measured, never copied."""
    if isinstance(outcome, Transcript):
        kind, characters, detected = TRANSCRIPT, len(outcome.text), outcome.language
    elif isinstance(outcome, VoiceFailure):
        kind, characters, detected = outcome.kind, 0, ""
    else:  # pragma: no cover - a defect, and better seen than described
        raise TypeError(f"unexpected outcome {type(outcome).__name__}")
    return Measurement(label=label, language=settings.language, vad_filter=settings.vad_filter,
                       audio_seconds=audio_seconds, transcription_seconds=transcription_seconds,
                       kind=kind, characters=characters, detected_language=detected, **extra)


def summarise(measurements, attribute: str):
    """(min, median, max) of one timing across trials - never an average alone, and outliers are
    kept. None when nothing was measured."""
    values = [getattr(one, attribute) for one in measurements
              if getattr(one, attribute) is not None]
    if not values:
        return None
    return min(values), statistics.median(values), max(values)


def summary_line(measurements, attribute: str, title: str) -> str:
    found = summarise(measurements, attribute)
    if found is None:
        return f"{title}: nothing measured"
    smallest, middle, largest = found
    return f"{title}: min {smallest:.2f}s, median {middle:.2f}s, max {largest:.2f}s"
