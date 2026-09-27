"""
REAL spoken-STOP benchmark for the Vosk prototype (Task 6d2a) - PREPARED, NOT RUN YET.

Marked real_vosk_stop and skipped unless RUN_REAL_VOSK_STOP_TEST=1, which no other switch sets - not
the sherpa flags, whose runs are finished and whose evidence must keep its own meaning. It needs -s,
because you react to a printed cue:

    $env:RUN_REAL_VOSK_STOP_TEST=1
    venv\\Scripts\\python.exe -m pytest tests/test_vosk_stop_real.py -s

THE QUESTION: does a constrained-grammar recognizer give us a cleaner STANDALONE-WORD boundary than
keyword spotting did? Vosk returns TEXT, so "standalone stop" is expressible exactly - `text == "stop"`
- instead of being approximated by counting blank frames after a token sequence.

What it does: loads the local Vosk model (no network), builds a recognizer whose whole vocabulary is
["stop", "[unk]"], opens the microphone through the project's one ownership rule, and streams 100 ms
chunks of int16 PCM into it - STREAMING detection, not "record then decode".

THE TRIGGER RULE, which is the point of the experiment:
  * A detection requires a FINAL result - AcceptWaveform() returning True, meaning Vosk's own
    endpointer fired - whose text is EXACTLY "stop". Nothing else counts.
  * A PARTIAL result is never a trigger and never ends a trial. Whether one ever said "stop" is
    recorded as a single flag, for diagnosis.
  * No fuzzy matching, no "contains", no synonyms, no semantic interpretation.
  * At the listening bound we call FinalResult() ONCE for diagnosis. If "stop" appears only there, it
    is recorded as FLUSH_ONLY_STOP and is NOT counted as a detection - a stop that needs us to force a
    flush at a timeout is no use to someone trying to interrupt a running action.

What it never does: execute an action, submit a command, or touch the emergency stop. This file imports
none of them. It is recognition only.

Privacy: cues are printed before the microphone opens, audio lives only in the chunk being fed and is
discarded, only its peak/RMS level is kept, nothing is written to disk or played back, and no transcript
is persisted. The recognizer's whole vocabulary is two tokens, so a final result can only ever be a
sequence of "stop" and "[unk]" - printing that is not a transcript, and the harness FAILS LOUDLY if any
other word ever appears, because that would mean the grammar was not in force.

This is a PROTOTYPE benchmark. vosk is not a permanent dependency, no module under app/ imports it, and
this proves nothing about the production integration - that remains a later design task.
"""
import json
import math
import sys
import time

import pytest

from app.listener import microphone
from config.settings import PROJECT_ROOT

pytestmark = pytest.mark.real_vosk_stop

MODEL_NAME = "vosk-model-small-en-us-0.15"
MODEL = PROJECT_ROOT / "data" / "models" / "vosk" / MODEL_NAME
# What vosk_model_new actually reads. The graph is HCLr.fst + Gr.fst, which is what makes a runtime
# grammar possible at all: with a single static HCLG.fst, Vosk only WARNS and silently keeps the full
# vocabulary (see _check_grammar_is_really_in_force).
REQUIRED = ("am/final.mdl", "conf/mfcc.conf", "conf/model.conf",
            "graph/HCLr.fst", "graph/Gr.fst", "graph/phones/word_boundary.int")

# The whole vocabulary. Exactly two tokens: the word we want, and a sink for everything else.
STOP_WORD = "stop"
UNKNOWN = "[unk]"
GRAMMAR_WORDS = (STOP_WORD, UNKNOWN)
GRAMMAR = json.dumps(list(GRAMMAR_WORDS))     # '["stop", "[unk]"]'

SAMPLE_RATE = 16000
# 100 ms, the same as the sherpa runs, so the two detectors' timings are directly comparable. It is
# also the right order of magnitude for this API: Vosk's own SrtResult reads 4000-byte chunks (125 ms
# at this rate), and 100 ms is far finer than the 0.5 s minimum trailing silence the model's endpoint
# rules need, so the chunk size cannot hide an endpoint. Nothing here is tuned.
CHUNK_SECONDS = 0.1
BYTES_PER_SAMPLE = 2                          # int16: what the Python AcceptWaveform accepts
LISTEN_SECONDS = 4.0                          # a bound, not a duration
OWNER = "vosk-benchmark"
QUIET_PEAK = 0.01                             # below this peak, "nothing audible arrived"
FULL_SCALE = 32768.0                          # int16 -> the same [-1, 1] scale the sherpa runs reported

POSITIVE, NEGATIVE = "positive", "negative"
# Unchanged from the sherpa benchmarks, so the two are directly comparable, and unchanged in meaning:
# ONLY the standalone word is a positive.
POSITIVES = ("stop", "stop", "stop", "stop", "stop")
NEGATIVES = ("(say nothing at all)", "notepad", "stopped", "stopping", "stops", "stopper",
             "stop it", "please stop", "full stop", "open notepad and type hello")

# What one final result was. Categories, not transcripts.
FINAL_STOP = "FINAL_STOP"          # exactly the word we want
FINAL_UNK = "FINAL_UNK"            # only the out-of-grammar sink
FINAL_EMPTY = "FINAL_EMPTY"        # the endpointer fired on nothing
FINAL_OTHER = "FINAL_OTHER"        # a mix, e.g. the sink followed by the word
FINAL_NONE = "FINAL_NONE"          # no final result happened at all

NEEDS_DASH_S = ("This benchmark prints a cue you react to, so run it with -s:\n"
                "    $env:RUN_REAL_VOSK_STOP_TEST=1\n"
                "    venv\\Scripts\\python.exe -m pytest tests/test_vosk_stop_real.py -s")
NEEDS_MODEL = ("The Vosk model is not downloaded. Run:\n"
               "    python scripts/fetch_vosk_model.py")
NEEDS_VOSK = ("The vosk package is not installed. It is a PROTOTYPE dependency and is deliberately "
              "not in requirements.txt.")


def announce(text=""):
    """Print safely on a Windows console that cannot encode every character we might be handed."""
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        text = text.encode(encoding, errors="backslashreplace").decode(encoding)
    print(text, flush=True)


def normalised(text) -> str:
    """Collapse whitespace. That is the whole normalisation, on purpose.

    The prototype deliberately does NOT reuse app/listener/logic.py's is_stop_phrase: the experiment
    must not inherit production semantics that could change under it, and there is nothing else to
    normalise - a constrained-grammar result has no punctuation and no casing. An offline test checks
    the two agree on this corpus anyway, without this file depending on production code."""
    return " ".join(text.split())


def is_stop(text) -> bool:
    """EXACTLY the word, and nothing else. No fuzzy matching, no containment, no synonyms."""
    return normalised(text) == STOP_WORD


def classify(text):
    """Turn one result into (category, pattern, words outside the grammar).

    `pattern` is the grammar-word sequence, e.g. "[unk] stop". That is not a transcript: the
    recognizer's entire vocabulary is two tokens. Any word outside them means the grammar was not in
    force, which invalidates the run - so those are collected and reported loudly."""
    words = normalised(text).split()
    outside = frozenset(word for word in words if word not in GRAMMAR_WORDS)
    pattern = " ".join(words)
    if not words:
        return FINAL_EMPTY, "", outside
    if outside:
        return FINAL_OTHER, pattern, outside
    if is_stop(text):
        return FINAL_STOP, pattern, outside
    if all(word == UNKNOWN for word in words):
        return FINAL_UNK, pattern, outside
    return FINAL_OTHER, pattern, outside


class Trial:
    """One listening attempt. Timings, audio LEVELS and result CATEGORIES - no transcript is kept.

    `prompt` is the phrase the benchmark ASKED for. The harness cannot check that you said it."""

    def __init__(self, label, prompt, expected):
        self.label = label
        self.prompt = prompt              # what the benchmark ASKED for; NOT recognised speech
        self.expected = expected          # POSITIVE or NEGATIVE - what SHOULD happen
        self.timely = False               # a FINAL result of exactly "stop", within the bound
        self.detect_seconds = None        # cue -> that final result
        self.final_category = FINAL_NONE  # the last final result the endpointer produced
        self.final_pattern = ""
        self.endpoints = 0                # how many times AcceptWaveform reported an endpoint
        self.flush_category = None        # FinalResult() at the bound - diagnosis only
        self.flush_pattern = ""
        self.flush_only_stop = False      # "stop" appeared ONLY because we forced a flush
        self.partials = 0                 # how many non-empty partials were seen
        self.partial_stop_seen = False    # a partial said "stop" - RECORDED, NEVER a trigger
        self.outside_grammar = frozenset()
        self.listen_seconds = None
        self.open_seconds = None          # cue -> the device was open and running
        self.close_seconds = None
        self.recognizer_seconds = None    # building the grammar recognizer; BEFORE the cue, untimed
        self.audio_seconds_fed = 0.0
        self.chunks = 0
        self.chunk_seconds = []            # how long each accept/decode step took
        self.peak = 0.0
        self.sum_of_squares = 0.0
        self.samples_fed = 0
        self.overflows = 0
        self.fed_bytes_per_chunk = None
        self.error = None
        self.cleanup_ok = False            # True ONLY when provably closed and released
        self.cleanup_error = None

    def note_audio(self, samples) -> None:
        """Record only the LEVEL of this chunk, then forget it. Scaled to [-1, 1] so the numbers are
        comparable with the sherpa runs, which fed float32."""
        if self.fed_bytes_per_chunk is None:
            self.fed_bytes_per_chunk = int(samples.shape[0]) * BYTES_PER_SAMPLE
        self.peak = max(self.peak, abs(float(samples.max())) / FULL_SCALE,
                        abs(float(samples.min())) / FULL_SCALE)
        scaled = samples.astype("float64") / FULL_SCALE
        self.sum_of_squares += float((scaled ** 2).sum())
        self.samples_fed += int(samples.shape[0])

    @property
    def rms(self):
        if not self.samples_fed:
            return None
        return math.sqrt(self.sum_of_squares / self.samples_fed)

    @property
    def silent(self) -> bool:
        return self.samples_fed > 0 and self.peak < QUIET_PEAK

    @property
    def audio_format(self):
        if self.fed_bytes_per_chunk is None:
            return None
        return (f"int16 bytes, {self.fed_bytes_per_chunk} bytes = "
                f"{self.fed_bytes_per_chunk // BYTES_PER_SAMPLE} samples per chunk at "
                f"{SAMPLE_RATE} Hz mono")

    @property
    def false_positive(self) -> bool:
        """A TIMELY trigger on a negative. A flush-only stop is reported separately, not here."""
        return self.expected == NEGATIVE and self.timely

    @property
    def missed(self) -> bool:
        return self.expected == POSITIVE and not self.timely

    @property
    def ended_early(self) -> bool:
        return (self.listen_seconds is not None and self.listen_seconds < LISTEN_SECONDS
                and self.error is None)

    @property
    def slowest_chunk_seconds(self):
        return max(self.chunk_seconds) if self.chunk_seconds else None

    def __repr__(self) -> str:
        detect = "-" if self.detect_seconds is None else f"{self.detect_seconds:.2f}s"
        slowest = ("-" if self.slowest_chunk_seconds is None
                   else f"{self.slowest_chunk_seconds * 1000:.1f}ms")
        opening = "-" if self.open_seconds is None else f"{self.open_seconds * 1000:.0f}ms"
        closing = "-" if self.close_seconds is None else f"{self.close_seconds * 1000:.0f}ms"
        level = ("level=(nothing fed)" if not self.samples_fed else
                 f"peak={self.peak:.3f}, rms={self.rms:.4f}"
                 + (" NOTHING AUDIBLE ARRIVED" if self.silent else ""))
        verdict = ("FALSE POSITIVE" if self.false_positive else
                   "missed" if self.missed else "as expected")
        flush = "" if not self.flush_category else f", flush={self.flush_category}"
        return (f"Trial({self.label}, {self.expected}, prompt={self.prompt!r}, "
                f"timely={self.timely}, {self.final_category}"
                + (f" {self.final_pattern!r}" if self.final_pattern else "")
                + f"{flush}"
                + (", FLUSH_ONLY_STOP" if self.flush_only_stop else "")
                + f", {verdict}, detect={detect}, endpoints={self.endpoints}, "
                f"partials={self.partials}"
                + (", a partial said stop" if self.partial_stop_seen else "")
                + f", listened={self.listen_seconds or 0:.2f}s, "
                f"audio_fed={self.audio_seconds_fed:.1f}s, chunks={self.chunks}, {level}, "
                f"overflows={self.overflows}, slowest_chunk={slowest}, open={opening}, "
                f"close={closing}"
                + (f", OUT OF GRAMMAR {sorted(self.outside_grammar)}" if self.outside_grammar else "")
                + (f", error={type(self.error).__name__}" if self.error else "") + ")")

    __str__ = __repr__


@pytest.fixture
def ready(request):
    if request.config.getoption("capture") != "no" or type(sys.stdin).__name__ == "DontReadFromInput":
        pytest.skip(NEEDS_DASH_S)
    import importlib.util
    if importlib.util.find_spec("vosk") is None:
        pytest.skip(NEEDS_VOSK)
    missing = [name for name in REQUIRED if not (MODEL / name).is_file()]
    if missing:
        pytest.skip(f"{NEEDS_MODEL}\n(missing: {', '.join(missing)})")
    return MODEL


@pytest.fixture(autouse=True)
def nothing_left_running():
    yield
    assert microphone.owner() is None, f"the microphone is still owned by {microphone.owner()!r}"


def test_whether_a_constrained_grammar_hears_a_standalone_stop(ready):
    import vosk

    announce("")
    announce("=" * 78)
    announce("VOSK CONSTRAINED-GRAMMAR STOP BENCHMARK - nothing runs, nothing is saved or played back.")
    announce(f"  The recognizer's ENTIRE vocabulary is {GRAMMAR}. It can return only those two")
    announce("  tokens, so no transcript of anything you say exists or is kept.")
    announce("  The report shows the phrase you were ASKED to say (prompt=), not what was heard.")
    announce("  A detection needs a FINAL result of exactly 'stop'. A partial saying 'stop' is")
    announce("  recorded but NEVER counts, and a 'stop' that appears only when we force a flush at")
    announce("  the timeout is recorded separately as FLUSH_ONLY_STOP and does not count either.")
    announce(f"  {len(POSITIVES)} x 'stop', then {len(NEGATIVES)} near-misses. Each trial listens for")
    announce(f"  up to {LISTEN_SECONDS:g} s. Say exactly what it asks - the near-misses are the point.")
    announce("=" * 78)
    for line in _device_path():
        announce(f"  {line}")
    announce("=" * 78)

    started = time.monotonic()
    model = vosk.Model(model_path=str(MODEL))
    announce(f"\nmodel loaded in {time.monotonic() - started:.2f} s (excluded from trial timing)")
    _check_grammar_is_really_in_force(model)

    def make_recognizer():
        return vosk.KaldiRecognizer(model, SAMPLE_RATE, GRAMMAR)

    trials, aborted = [], None
    for label, prompt, expected in _schedule():
        trial = _listen(make_recognizer, label, prompt, expected)
        trials.append(trial)
        if not trial.cleanup_ok:
            aborted = trial
            announce(f"\nBENCHMARK ABORTED after {label}: cleanup could not be proven.")
            break

    announce("")
    announce("-" * 78)
    for trial in trials:
        announce(f"  {trial!r}")
    _summarise(trials)

    assert aborted is None, (
        f"the benchmark stopped early because cleanup could not be proven after {aborted.label}: "
        f"{aborted.cleanup_error}" if aborted else "")
    assert len(trials) == len(POSITIVES) + len(NEGATIVES), "every trial runs once; none is retried"
    assert microphone.owner() is None, "the microphone was released"
    assert all(trial.cleanup_ok for trial in trials), "every trial's cleanup was proven"
    assert any(trial.chunks for trial in trials), "no audio was streamed at all"
    leaked = sorted({word for trial in trials for word in trial.outside_grammar})
    assert leaked == [], (
        f"a result contained words outside the grammar {GRAMMAR}: {leaked}. The constrained grammar "
        f"was NOT in force, so none of the numbers above mean anything.")
    formats = {trial.audio_format for trial in trials if trial.samples_fed}
    expected_format = (f"int16 bytes, {int(CHUNK_SECONDS * SAMPLE_RATE) * BYTES_PER_SAMPLE} bytes = "
                       f"{int(CHUNK_SECONDS * SAMPLE_RATE)} samples per chunk at {SAMPLE_RATE} Hz mono")
    assert formats == {expected_format}, f"the recognizer must be fed 16 kHz mono int16: {formats}"
    failed = [trial for trial in trials if trial.error is not None]
    assert failed == [], f"trials failed (their data is above): {failed}"


def _check_grammar_is_really_in_force(model) -> None:
    """Vosk only WARNS when a model cannot take a runtime grammar, then keeps the full vocabulary - so
    a silently-ignored grammar would look like a detector result. Two checks before any trial:
    the two tokens must exist in this model's lexicon, or UpdateGrammarFst skips them with a warning
    ("Ignoring word missing in vocabulary"), and the graph files a runtime grammar needs must be here.
    The out-of-grammar assertion at the end of the run is the third, behavioural check."""
    announce(f"grammar: {GRAMMAR}")
    for word in GRAMMAR_WORDS:
        found = model.vosk_model_find_word(word)
        announce(f"  {word!r} -> lexicon id {found}")
        assert found is not None and found >= 0, (
            f"{word!r} is not in this model's lexicon, so Vosk would silently drop it from the "
            f"grammar and the experiment would not be the one we designed")
    for name in ("graph/HCLr.fst", "graph/Gr.fst"):
        assert (MODEL / name).is_file(), (
            f"{name} is missing: this model cannot build a runtime grammar, and Vosk would only warn")
    announce("  both tokens exist and the model has the HCLr/Gr graph a runtime grammar needs")


def _schedule():
    """Every trial, in order: the positives first, then the near-misses. One pass, no retries."""
    for number, prompt in enumerate(POSITIVES, start=1):
        yield f"positive {number}", prompt, POSITIVE
    for number, prompt in enumerate(NEGATIVES, start=1):
        yield f"negative {number}", prompt, NEGATIVE


def _listen(make_recognizer, label, prompt, expected) -> Trial:
    """One trial: build the recognizer, cue, then stream 100 ms chunks until a FINAL exactly-"stop"
    result arrives or the bound is reached.

    The recognizer is built BEFORE the cue is printed, because composing the grammar graph is not
    instant and speech that arrives while it is composing would be lost.

    Synchronous on this thread: sounddevice's blocking read is already bounded to one chunk, so no
    worker, no cancel event and no liveness state are needed for a benchmark.

    Nothing is retried. An unexpected failure is kept on the trial so the remaining trials still run,
    and the test fails at the end because of it."""
    import sounddevice
    import numpy

    numpy.zeros(1)          # never pay numpy's first import inside the timed loop
    trial = Trial(label, prompt, expected)
    frames = int(CHUNK_SECONDS * SAMPLE_RATE)
    if not microphone.acquire(OWNER):
        announce("")
        announce(f"[{label}: {expected}] the microphone is busy ({microphone.owner()!r}); not measured")
        trial.error = RuntimeError(f"microphone owned by {microphone.owner()!r}")
        trial.cleanup_ok = True   # nothing was acquired and no stream was opened
        return trial
    stream = None
    recognizer = None
    started = None
    try:
        building = time.monotonic()
        recognizer = make_recognizer()
        trial.recognizer_seconds = time.monotonic() - building
        announce("")
        announce(f"[{label}: {expected}] SAY: {prompt}")
        started = time.monotonic()
        # RawInputStream, and int16: the Python AcceptWaveform passes its argument to the C char*
        # entry point with len(data) bytes, so Vosk wants raw int16 PCM. Same device, same rate, same
        # channel count and same latency setting as the sherpa runs - only the sample format differs,
        # because this API accepts nothing else.
        stream = sounddevice.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                                           blocksize=frames)
        stream.start()
        trial.open_seconds = time.monotonic() - started
        while time.monotonic() - started < LISTEN_SECONDS:
            buffer, overflowed = stream.read(frames)
            chunk = bytes(buffer)
            trial.chunks += 1
            trial.audio_seconds_fed += CHUNK_SECONDS
            if overflowed:
                trial.overflows += 1
            trial.note_audio(numpy.frombuffer(chunk, dtype="<i2"))
            step = time.monotonic()
            ended = recognizer.AcceptWaveform(chunk)
            trial.chunk_seconds.append(time.monotonic() - step)
            if ended:
                # Vosk's own endpointer fired. THIS is the only thing that can be a detection.
                trial.endpoints += 1
                category, pattern, outside = classify(_text(recognizer.Result()))
                trial.final_category, trial.final_pattern = category, pattern
                trial.outside_grammar |= outside
                if category == FINAL_STOP:
                    trial.timely = True
                    trial.detect_seconds = time.monotonic() - started
                    break
                # Not our word. The recognizer resumes by itself: AcceptWaveform calls CleanUp() when
                # the state is not RUNNING/INITIALIZED, so the same recognizer can still hear "stop"
                # later in this trial. No Reset() is needed, and none is done.
                continue
            partial = _text(recognizer.PartialResult(), key="partial")
            if partial:
                trial.partials += 1
                if is_stop(partial):
                    trial.partial_stop_seen = True   # recorded; it is NOT a detection and ends nothing
        if not trial.timely and recognizer is not None:
            # Diagnosis only, once, at the bound. FinalResult() deletes the decoder and the feature
            # pipeline, so it can never be called mid-trial.
            category, pattern, outside = classify(_text(recognizer.FinalResult()))
            trial.flush_category, trial.flush_pattern = category, pattern
            trial.outside_grammar |= outside
            trial.flush_only_stop = category == FINAL_STOP
    except Exception as exc:   # recorded, not swallowed: the test fails on it after every trial has run
        trial.error = exc
        announce(f"  the trial failed ({type(exc).__name__}: {exc})")
    finally:
        # Always, on every path: detection, timeout, a vosk or sounddevice failure, or Ctrl+C.
        _release(trial, stream)
    if started is not None:
        trial.listen_seconds = time.monotonic() - started
    announce(f"  -> {trial!r}")
    return trial


def _text(result, key="text") -> str:
    """Pull one field out of a Vosk JSON result, tolerating anything unexpected."""
    try:
        return json.loads(result).get(key, "") or ""
    except (ValueError, TypeError, AttributeError):
        return ""


def _release(trial, stream) -> None:
    """Stop, close, and PROVE it. Deliberately the same rule the sherpa benchmarks used - written out
    here rather than imported, so this prototype does not depend on a paused one; an offline test
    checks the two rules stay identical in substance.

    What the installed sounddevice offers (checked, not assumed): `stream.closed` is a real property
    (`self._ptr == _ffi.NULL`), and close() defaults to ignore_errors=True, which swallows PortAudio
    errors while clearing the pointer - so a close failure is only visible with ignore_errors=False.

    A failed stop() alone is recorded but not fatal: close() discards pending buffers "as if abort()
    had been called", so a definitive close still leaves no native stream. A failed or unprovable
    close IS fatal, because the device may still be open."""
    problems = []
    closing = time.monotonic()
    if stream is not None:
        try:
            stream.stop()
        except Exception as exc:
            problems.append(f"stop() raised {type(exc).__name__}: {exc} (not fatal by itself)")
        try:
            stream.close(ignore_errors=False)
        except Exception as exc:
            problems.append(f"FATAL close() raised {type(exc).__name__}: {exc}")
        closed = getattr(stream, "closed", None)
        if closed is not True:
            problems.append(f"FATAL the stream still reports closed={closed!r}")
        trial.close_seconds = time.monotonic() - closing
    try:
        microphone.release()
    except RuntimeError as exc:
        problems.append(f"FATAL release() raised {type(exc).__name__}: {exc}")
    if microphone.owner() is not None:
        problems.append(f"FATAL the microphone is still owned by {microphone.owner()!r}")

    fatal = [problem for problem in problems if problem.startswith("FATAL")]
    trial.cleanup_ok = not fatal
    trial.cleanup_error = "; ".join(problems) or None
    if problems:
        announce(f"  cleanup: {trial.cleanup_error}")
    if fatal:
        announce("  CLEANUP COULD NOT BE PROVEN - no further microphone stream will be opened.")


def _device_path() -> list:
    """The audio path, recorded once so the numbers stay interpretable and comparable with the sherpa
    runs. It is NOT a variable here - nothing about the device, host API, latency or rate is changed.
    Only the backend's own description is read: no stream is opened, nothing is recorded, and the
    device NAME is deliberately never included."""
    lines = ["audio path (recorded, deliberately UNCHANGED from the sherpa runs):",
             f"  asked for: {SAMPLE_RATE} Hz mono int16, blocksize {int(CHUNK_SECONDS * SAMPLE_RATE)}"]
    try:
        import sounddevice
        lines[-1] += f", latency setting {sounddevice.default.latency[0]!r}"
        info = sounddevice.query_devices(sounddevice.default.device[0])
        host = sounddevice.query_hostapis(info["hostapi"])["name"]
        lines.append(f"  default input device: host API {host}, native "
                     f"{float(info['default_samplerate']):g} Hz, "
                     f"{int(info['max_input_channels'])} channels, reported input latency low "
                     f"{float(info['default_low_input_latency']) * 1000:.0f} ms / high "
                     f"{float(info['default_high_input_latency']) * 1000:.0f} ms")
        lines.append("  int16 rather than the sherpa runs' float32 because the Python AcceptWaveform "
                     "accepts nothing else; everything else about the path is the same.")
    except Exception as exc:
        # Purely informational: a backend that will not describe itself must never end the benchmark.
        lines.append(f"  the audio path could not be described ({type(exc).__name__}: {exc})")
    return lines


def _summarise(trials) -> None:
    """Evidence only. It names no winner, approves nothing, and keeps the endpoint timings visible
    rather than folding them into one number."""
    positives = [trial for trial in trials if trial.expected == POSITIVE]
    negatives = [trial for trial in trials if trial.expected == NEGATIVE]
    detected = [trial for trial in positives if trial.timely]
    missed = [trial for trial in positives if trial.missed]
    false_positives = [trial for trial in negatives if trial.false_positive]
    flush_positives = [trial for trial in positives if trial.flush_only_stop]
    flush_negatives = [trial for trial in negatives if trial.flush_only_stop]

    announce("")
    announce(f"  POSITIVES: TIMELY detections {len(detected)}/{len(positives)}, "
             f"false negatives {len(missed)}")
    announce(f"    of the misses, {len(flush_positives)} produced 'stop' ONLY on the forced flush "
             f"(FLUSH_ONLY_STOP - not counted as a detection)")
    # Endpoint latency stays its own number, measured only on the trials that actually detected.
    _spread(detected, "detect_seconds", "cue -> final exactly-'stop' result (detections only)")
    for attribute, title in (("open_seconds", "cue -> device open and running (every trial)"),
                             ("recognizer_seconds", "building the grammar recognizer (before the cue)"),
                             ("slowest_chunk_seconds", "slowest single accept/decode step"),
                             ("close_seconds", "stream close")):
        _spread(trials, attribute, title)
    announce(f"  NEGATIVES: TIMELY false positives {len(false_positives)}/{len(negatives)}")
    if false_positives:
        for trial in false_positives:
            announce(f"    fired on the prompt {trial.prompt!r} after {trial.detect_seconds:.2f}s")
    else:
        announce("    none fired")
    if flush_negatives:
        announce(f"    flush-only 'stop' on {len(flush_negatives)} negative(s), reported separately:")
        for trial in flush_negatives:
            announce(f"      {trial.prompt!r}")
    else:
        announce("    no flush-only 'stop' on any negative")

    announce("")
    announce("  WHAT THE ENDPOINTER RETURNED (categories, not transcripts):")
    for trial in trials:
        announce(f"    {trial.label}: {trial.final_category}"
                 + (f" {trial.final_pattern!r}" if trial.final_pattern else "")
                 + f", endpoints {trial.endpoints}, partials {trial.partials}"
                 + (", a partial said 'stop'" if trial.partial_stop_seen else "")
                 + (f", flush {trial.flush_category}" if trial.flush_category else ""))
    partial_only = [trial for trial in trials if trial.partial_stop_seen and not trial.timely]
    announce(f"  trials where a partial said 'stop' but no final result did: {len(partial_only)}")
    if partial_only:
        announce("    each one is a case where triggering on a partial would have been wrong or early")

    announce("")
    announce("  AUDIO PRESENCE (levels only - no audio was kept, and no word was identified):")
    fed = [trial for trial in trials if trial.samples_fed]
    for shape in sorted({trial.audio_format for trial in fed}):
        announce(f"    fed to AcceptWaveform: {shape}")
    for trial in fed:
        announce(f"    {trial.label}: peak {trial.peak:.3f}, rms {trial.rms:.4f}, "
                 f"{trial.audio_seconds_fed:.1f}s fed, overflows {trial.overflows}"
                 + ("   <- NOTHING AUDIBLE" if trial.silent else ""))
    spoken = [trial for trial in fed if trial.prompt != NEGATIVES[0]]
    quiet = [trial for trial in spoken if trial.silent]
    if quiet:
        announce(f"    FLAG: {len(quiet)}/{len(spoken)} trial(s) you were asked to SPEAK in received "
                 f"near-silent input. They are STILL counted above - read them yourself:")
        for trial in quiet:
            announce(f"      {trial.label} (prompt {trial.prompt!r}): peak {trial.peak:.3f}")
    elif spoken:
        announce("    every trial you were asked to speak in carried audible audio")
    else:
        announce("    NO trial fed any audio at all - nothing here says anything about the recognizer")
    announce(f"    input overflows reported by the device: {sum(trial.overflows for trial in fed)}")

    announce("")
    announce("  This model's endpoint rules (conf/model.conf): a final result needs 0.5 s of trailing")
    announce("  silence when the decode is confident, 0.75 s when it is adequate, 1.0 s regardless.")
    announce("  So 'cue -> final result' contains your reaction time, the device open, the device's")
    announce("  own buffering AND that trailing-silence wait. It is NOT model inference latency.")
    announce("  For scale: the Ctrl+Alt+Backspace hotkey stops in about 22 ms; the measured Whisper")
    announce("  path took about 11.5-11.9 s; the longest Phase 1 action acts for 10.0 s.")
    announce("-" * 78)


def _spread(trials, attribute, title) -> None:
    import statistics
    values = [getattr(trial, attribute) for trial in trials
              if getattr(trial, attribute, None) is not None]
    if not values:
        announce(f"  {title}: nothing measured")
        return
    announce(f"  {title}: min {min(values):.3f}s, median {statistics.median(values):.3f}s, "
             f"max {max(values):.3f}s")
