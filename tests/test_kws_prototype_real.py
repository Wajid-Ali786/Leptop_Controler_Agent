"""
REAL keyword-spotting benchmark for the spoken-stop prototype (Task 6d1b) - PREPARED, NOT RUN YET.

Marked real_kws_latency and skipped unless RUN_REAL_KWS_TEST=1, which no other switch sets. It needs
-s, because you react to a printed cue:

    $env:RUN_REAL_KWS_TEST=1
    venv\\Scripts\\python.exe -m pytest tests/test_kws_prototype_real.py -s

What it does: builds a sherpa-onnx KeywordSpotter from the local model (no network), opens the
microphone itself through the project's one ownership rule, and streams 100 ms chunks straight into the
detector - so this measures STREAMING detection, not "record then decode". You say "stop" for the
positive trials and the listed near-misses for the negative ones.

This is KEYWORD SPOTTING, NOT TRANSCRIPTION. The detector has one answer: the configured keyword, or
nothing. It cannot report what you actually said, so no field here may claim to. `prompt` is the phrase
the benchmark ASKED you to say - the harness has no way to check that you said it, and if you say
something else the detector will simply not fire. There is no ASR anywhere in this file.

Because a keyword miss and a microphone that delivered nothing look identical in the detector's output,
each trial also reports the LEVEL of the audio it fed (peak and RMS) - two numbers, computed from each
chunk and thrown away with it. That is an audio-presence check, not word identification.

What it never does: execute an action, call app.console.handle_command, or touch the emergency stop.
This file imports none of them. The result of a trial is a keyword string and some timings.

This is a PROTOTYPE benchmark. It does not prove the production microphone-ownership integration or
the action-seam design - those remain later design tasks. sherpa-onnx is not a permanent dependency
yet, and no module under app/ imports it.

Privacy: the cue is printed before the microphone opens, audio lives only in the chunk being fed and
is discarded, nothing is written to disk or played back, and no audio or detected text reaches the log.
"""
import math
import sys
import time
from pathlib import Path

import pytest

from app.listener import microphone
from config.settings import PROJECT_ROOT

pytestmark = pytest.mark.real_kws_latency

MODEL_NAME = "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
MODEL = PROJECT_ROOT / "data" / "models" / "kws" / MODEL_NAME
KEYWORDS = PROJECT_ROOT / "data" / "models" / "kws" / "keywords" / "stop.txt"
FILES = {
    "tokens": "tokens.txt",
    "encoder": "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    "decoder": "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    "joiner": "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
}

# The library's documented defaults. The baseline run tunes NOTHING.
KEYWORDS_SCORE = 1.0
KEYWORDS_THRESHOLD = 0.25

SAMPLE_RATE = 16000
CHUNK_SECONDS = 0.1          # the official microphone example's read size
LISTEN_SECONDS = 4.0         # how long one trial listens before giving up
OWNER = "kws-benchmark"
QUIET_PEAK = 0.01            # below this peak, treat a trial as "nothing audible arrived"

POSITIVE, NEGATIVE = "positive", "negative"

# Under the current semantic contract, ONLY the standalone word is a positive.
POSITIVES = ("stop", "stop", "stop", "stop", "stop")
# The near-misses are chosen from measured evidence, not guesswork: the generated keyword is the token
# sequence for STOP, and "stops", "stopper", "stop it", "please stop" and "full stop" all contain that
# exact sequence, while "stopped" and "stopping" diverge after the second token. If any of these fires,
# it is a false positive for this benchmark - nothing is tuned to suppress it.
NEGATIVES = ("(say nothing at all)", "notepad", "stopped", "stopping", "stops", "stopper",
             "stop it", "please stop", "full stop", "open notepad and type hello")

NEEDS_DASH_S = ("This benchmark prints a cue you react to, so run it with -s:\n"
                "    $env:RUN_REAL_KWS_TEST=1\n"
                "    venv\\Scripts\\python.exe -m pytest tests/test_kws_prototype_real.py -s")
NEEDS_MODEL = (f"The keyword-spotting model is not downloaded. Run:\n"
               f"    python scripts/fetch_kws_model.py")
NEEDS_KEYWORDS = (f"The STOP keyword file is missing: {KEYWORDS}\n"
                  f"It must be generated with the official flow:\n"
                  f"    sherpa-onnx-cli text2token --tokens <model>/tokens.txt --tokens-type bpe "
                  f"--bpe-model <model>/bpe.model <raw> <out>\n"
                  f"which needs the sentencepiece package. Nothing here invents BPE tokens.")


def announce(text=""):
    """Print safely on a Windows console that cannot encode the BPE marker U+2581.

    The keyword tokens and anything the detector returns may contain it, and a UnicodeEncodeError in
    the middle of a benchmark - after you have already spoken - would throw the run away."""
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        text = text.encode(encoding, errors="backslashreplace").decode(encoding)
    print(text, flush=True)


class Trial:
    """One listening attempt. Timings and audio LEVELS only, plus the keyword the detector returned.

    Nothing on this object represents recognised speech. `keyword` is the configured keyword or the
    empty string - the only two things a keyword spotter can say - and `prompt` is what the benchmark
    asked for, not what the microphone heard."""

    def __init__(self, label, prompt, expected, condition=None):
        self.label = label
        # Which named experimental condition produced this trial. None for the closed 6d1b baseline
        # harness, which has exactly one configuration; the threshold comparison sets it, so a result
        # can never be read under the wrong setting.
        self.condition = condition
        self.prompt = prompt             # the phrase the benchmark ASKED for; NOT recognised speech
        self.expected = expected         # POSITIVE or NEGATIVE - what SHOULD happen
        self.keyword = ""                # exactly what get_result() returned (a string)
        self.tokens = None
        self.timestamps = None
        self.listen_seconds = None       # the whole trial: cue -> stream closed
        self.detect_seconds = None       # cue -> detection; None when nothing fired
        self.close_seconds = None        # how long stopping and closing the stream took
        self.open_seconds = None         # cue -> the device was open and running; part of detect_seconds
        self.audio_seconds_fed = 0.0
        self.chunks = 0
        self.chunk_seconds = []          # how long each decode step took
        self.peak = 0.0                  # loudest single sample fed to the detector
        self.sum_of_squares = 0.0        # for rms; a running total, never the samples themselves
        self.samples_fed = 0
        self.overflows = 0               # chunks where the device reported dropped input
        self.fed_dtype = None            # what the detector was actually handed, observed once
        self.fed_ndim = None
        self.fed_samples_per_chunk = None
        self.error = None                # an exception from this trial, kept so the run still finishes
        self.cleanup_ok = False          # True ONLY when the stream is provably closed and the
                                         # microphone provably free - see _release()
        self.cleanup_error = None        # why cleanup could not be proven; aborts the benchmark

    def note_audio(self, samples) -> None:
        """Record only the LEVEL of this chunk, then forget the chunk.

        Peak and a running sum of squares. No audio is kept, written, played back or logged, and this
        identifies no word - it answers one question the detector cannot: did sound actually arrive?
        The format is captured once, so the real run proves what accept_waveform was handed."""
        if self.fed_dtype is None:
            self.fed_dtype = str(samples.dtype)
            self.fed_ndim = int(samples.ndim)
            self.fed_samples_per_chunk = int(samples.shape[0])
        self.peak = max(self.peak, abs(float(samples.max())), abs(float(samples.min())))
        self.sum_of_squares += float((samples.astype("float64") ** 2).sum())
        self.samples_fed += int(samples.shape[0])

    @property
    def rms(self):
        """Root-mean-square level of everything fed, or None when nothing was fed."""
        if not self.samples_fed:
            return None
        return math.sqrt(self.sum_of_squares / self.samples_fed)

    @property
    def silent(self) -> bool:
        """Effectively nothing arrived. For interpretation only - it changes no detector behaviour."""
        return self.samples_fed > 0 and self.peak < QUIET_PEAK

    @property
    def audio_format(self):
        """What accept_waveform was actually handed, or None when no audio was fed."""
        if self.fed_dtype is None:
            return None
        return (f"{self.fed_dtype}, {self.fed_ndim}-D, {self.fed_samples_per_chunk} samples per chunk "
                f"at {SAMPLE_RATE} Hz mono")

    @property
    def detected(self) -> bool:
        return bool(self.keyword)

    @property
    def false_positive(self) -> bool:
        return self.expected == NEGATIVE and self.detected

    @property
    def missed(self) -> bool:
        return self.expected == POSITIVE and not self.detected

    @property
    def ended_early(self) -> bool:
        """True when the trial stopped listening before its maximum - which is what a detection does."""
        return (self.listen_seconds is not None and self.listen_seconds < LISTEN_SECONDS
                and self.error is None)

    @property
    def lag_seconds(self):
        """APPROXIMATE processing lag: wall-clock detection minus the audio-relative time of the last
        keyword token. It is approximate on purpose - it mixes the model's own timestamps with our
        wall clock, and it is NOT human reaction time."""
        if self.detect_seconds is None or not self.timestamps:
            return None
        return self.detect_seconds - float(self.timestamps[-1])

    @property
    def slowest_chunk_seconds(self):
        return max(self.chunk_seconds) if self.chunk_seconds else None

    def __repr__(self) -> str:
        found = f"keyword={self.keyword!r}" if self.detected else "keyword=(none)"
        detect = "-" if self.detect_seconds is None else f"{self.detect_seconds:.2f}s"
        lag = "-" if self.lag_seconds is None else f"{self.lag_seconds:.2f}s"
        slowest = "-" if self.slowest_chunk_seconds is None else f"{self.slowest_chunk_seconds*1000:.1f}ms"
        closing = "-" if self.close_seconds is None else f"{self.close_seconds*1000:.0f}ms"
        opening = "-" if self.open_seconds is None else f"{self.open_seconds*1000:.0f}ms"
        verdict = ("FALSE POSITIVE" if self.false_positive else
                   "missed" if self.missed else "as expected")
        level = ("level=(nothing fed)" if not self.samples_fed else
                 f"peak={self.peak:.3f}, rms={self.rms:.4f}"
                 + (" NOTHING AUDIBLE ARRIVED" if self.silent else ""))
        where = f"{self.condition} | {self.label}" if self.condition else self.label
        return (f"Trial({where}, {self.expected}, prompt={self.prompt!r}, {found}, {verdict}, "
                f"detect={detect}, approx_lag={lag}, listened={self.listen_seconds or 0:.2f}s, "
                f"audio_fed={self.audio_seconds_fed:.1f}s, chunks={self.chunks}, {level}, "
                f"overflows={self.overflows}, slowest_chunk={slowest}, open={opening}, "
                f"close={closing}"
                + (f", error={type(self.error).__name__}" if self.error else "") + ")")

    __str__ = __repr__


@pytest.fixture
def ready(request):
    if request.config.getoption("capture") != "no" or type(sys.stdin).__name__ == "DontReadFromInput":
        pytest.skip(NEEDS_DASH_S)
    missing = [name for name in FILES.values() if not (MODEL / name).is_file()]
    if missing:
        pytest.skip(f"{NEEDS_MODEL}\n(missing: {', '.join(missing)})")
    if not KEYWORDS.is_file():
        pytest.skip(NEEDS_KEYWORDS)
    return MODEL


@pytest.fixture(autouse=True)
def nothing_left_running():
    yield
    assert microphone.owner() is None, f"the microphone is still owned by {microphone.owner()!r}"


def test_how_quickly_a_spoken_stop_is_detected(ready, capsys):
    import sherpa_onnx

    announce("")
    announce("=" * 78)
    announce("KEYWORD-SPOTTING BENCHMARK - nothing runs, nothing is saved or played back.")
    announce("  This is keyword spotting, not speech transcription. The report shows the phrase you")
    announce("  were asked to say, not what the microphone heard. The detector can only report STOP")
    announce("  or no detection - so if you say something else, it simply will not fire, and the")
    announce("  prompt in the report still says what was asked for.")
    announce("  Each trial also reports the LEVEL of the audio it fed (peak/rms) so a keyword miss can")
    announce("  be told apart from a microphone that delivered nothing. No audio is kept.")
    announce(f'  Detector vocabulary is exactly one keyword. Defaults: score={KEYWORDS_SCORE}, '
             f'threshold={KEYWORDS_THRESHOLD} (nothing is tuned).')
    announce(f"  Each trial listens for up to {LISTEN_SECONDS:g} s after its cue. Say exactly what it")
    announce("  asks - the near-misses are the point, so do not correct them.")
    announce("=" * 78)

    started = time.monotonic()
    spotter = sherpa_onnx.KeywordSpotter(
        tokens=str(MODEL / FILES["tokens"]),
        encoder=str(MODEL / FILES["encoder"]),
        decoder=str(MODEL / FILES["decoder"]),
        joiner=str(MODEL / FILES["joiner"]),
        keywords_file=str(KEYWORDS),
        keywords_score=KEYWORDS_SCORE,
        keywords_threshold=KEYWORDS_THRESHOLD,
        num_threads=1,
        provider="cpu",
    )
    load_seconds = time.monotonic() - started
    announce(f"\ndetector ready in {load_seconds:.2f} s (excluded from every trial's timing)")
    announce(f"keyword file: {KEYWORDS.name} -> {KEYWORDS.read_text(encoding='utf-8').strip()!r}")

    trials, aborted = [], None
    for label, prompt, expected in _schedule():
        trial = _listen(spotter, label, prompt, expected)
        trials.append(trial)
        if not trial.cleanup_ok:
            # Data from later trials is not worth risking two overlapping device streams.
            aborted = trial
            announce(f"\nBENCHMARK ABORTED after {label}: cleanup could not be proven.")
            break

    announce("")
    announce("-" * 78)
    for trial in trials:
        announce(f"  {trial!r}")

    positives = [trial for trial in trials if trial.expected == POSITIVE]
    negatives = [trial for trial in trials if trial.expected == NEGATIVE]
    hits = [trial for trial in positives if trial.detected]
    missed = [trial for trial in positives if trial.missed]
    false_positives = [trial for trial in negatives if trial.false_positive]

    announce("")
    announce(f"  POSITIVES: detected {len(hits)}/{len(positives)}, false negatives {len(missed)}")
    for attribute, title in (("detect_seconds", "cue -> detection"),
                             ("open_seconds", "of which: cue -> device open and running"),
                             ("lag_seconds", "approximate processing lag"),
                             ("slowest_chunk_seconds", "slowest single chunk"),
                             ("close_seconds", "stream close")):
        _spread(hits, attribute, title)
    announce(f"  NEGATIVES: false positives {len(false_positives)}/{len(negatives)}")
    if false_positives:
        for trial in false_positives:   # the exact phrases, never only a count or a percentage
            announce(f"    fired on the prompt {trial.prompt!r} after {trial.detect_seconds:.2f}s "
                     f"-> keyword {trial.keyword!r}")
    else:
        announce("    none fired")
    fed = [trial for trial in trials if trial.samples_fed]
    announce("")
    announce("  AUDIO PRESENCE (levels only - no audio was kept, and no word was identified):")
    for shape in sorted({trial.audio_format for trial in fed}):
        announce(f"    fed to accept_waveform: {shape}")
    for trial in fed:
        announce(f"    {trial.label}: peak {trial.peak:.3f}, rms {trial.rms:.4f}, "
                 f"{trial.audio_seconds_fed:.1f}s fed, overflows {trial.overflows}"
                 + ("   <- NOTHING AUDIBLE" if trial.silent else ""))
    spoken = [trial for trial in fed if trial.prompt != NEGATIVES[0]]
    quiet = [trial for trial in spoken if trial.silent]
    if quiet:
        announce(f"    WARNING: {len(quiet)}/{len(spoken)} trials you were asked to speak in fed "
                 f"nothing audible. The recall numbers above are then NOT about the model.")
    else:
        announce("    every trial you were asked to speak in carried audible audio")
    overflows = sum(trial.overflows for trial in fed)
    announce(f"    input overflows reported by the device: {overflows}"
             + (" (audio was dropped between reads)" if overflows else ""))

    early = [trial for trial in trials if trial.detected and trial.ended_early]
    announce(f"  trials that stopped listening as soon as they detected: {len(early)}/"
             f"{len([trial for trial in trials if trial.detected])}")
    announce("")
    announce("  'cue -> detection' is NOT model inference latency: it also contains the device open,")
    announce("  the device's own input buffering, and your reaction time after reading the cue, none")
    announce("  of which this harness can separate out beyond the device-open figure above.")
    announce("  For scale: the Ctrl+Alt+Backspace hotkey stops in about 22 ms; the measured Whisper")
    announce("  path took about 11.5-11.9 s; the longest Phase 1 action acts for 10.0 s.")
    announce("-" * 78)

    assert aborted is None, (
        f"the benchmark stopped early because cleanup could not be proven after {aborted.label}: "
        f"{aborted.cleanup_error}" if aborted else "")
    assert len(trials) == len(POSITIVES) + len(NEGATIVES), "every trial runs once; none is retried"
    assert microphone.owner() is None, "the microphone was released"
    assert any(trial.chunks for trial in trials), "no audio was streamed at all"
    assert all(trial.close_seconds is not None for trial in trials if trial.chunks), (
        "every trial that opened a stream also closed it")
    assert all(trial.cleanup_ok for trial in trials), "every trial's cleanup was proven"
    fed_formats = {trial.audio_format for trial in trials if trial.samples_fed}
    assert fed_formats == {f"float32, 1-D, {int(CHUNK_SECONDS * SAMPLE_RATE)} samples per chunk "
                           f"at {SAMPLE_RATE} Hz mono"}, (
        f"the detector must be fed 16 kHz mono float32, one channel column: {fed_formats}")
    failed = [trial for trial in trials if trial.error is not None]
    assert failed == [], f"trials failed (their data is above): {failed}"


def _schedule():
    """Every trial, in order: the positives first, then the near-misses. One pass, no retries.

    Each item is (label, prompt, expected) - the prompt being the phrase to ASK for."""
    for number, prompt in enumerate(POSITIVES, start=1):
        yield f"positive {number}", prompt, POSITIVE
    for number, prompt in enumerate(NEGATIVES, start=1):
        yield f"negative {number}", prompt, NEGATIVE


def _listen(spotter, label, prompt, expected, condition=None) -> Trial:
    """One trial: cue, then stream 100 ms chunks into the detector until it fires or the maximum is
    reached. LISTEN_SECONDS is a bound, not a duration - the FIRST detection ends the trial, closes the
    stream and gives the microphone back, so the device is never held open after an answer exists.

    Synchronous on this thread: sounddevice's blocking read is already bounded to one chunk, so no
    worker, no cancel event and no liveness state are needed for a benchmark.

    Nothing is retried. An unexpected failure is kept on the trial so the remaining trials still run -
    you have already spoken for the earlier ones - and the test fails at the end because of it."""
    import sounddevice

    trial = Trial(label, prompt, expected, condition)
    frames = int(CHUNK_SECONDS * SAMPLE_RATE)
    announce("")
    where = f"{condition} | {label}" if condition else label
    announce(f"[{where}: {expected}] SAY: {prompt}")
    if not microphone.acquire(OWNER):
        announce(f"  the microphone is busy ({microphone.owner()!r}); this trial is not measured")
        trial.error = RuntimeError(f"microphone owned by {microphone.owner()!r}")
        trial.cleanup_ok = True  # nothing was acquired and no stream was opened: nothing to clean up
        return trial
    stream = None
    started = time.monotonic()
    try:
        stream = sounddevice.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                        blocksize=frames)
        stream.start()
        # Opening an MME device is not free, and it happens after the cue was printed - so it is inside
        # detect_seconds. Recorded separately so "cue -> detection" can be read apart from it.
        trial.open_seconds = time.monotonic() - started
        spotter_stream = spotter.create_stream()
        while time.monotonic() - started < LISTEN_SECONDS:
            block, overflowed = stream.read(frames)
            trial.chunks += 1
            trial.audio_seconds_fed += CHUNK_SECONDS
            if overflowed:
                # The device dropped input between reads, so the detector saw a gap. Worth counting:
                # it would otherwise be indistinguishable from the model failing to fire.
                trial.overflows += 1
            samples = block[:, 0]
            trial.note_audio(samples)
            step = time.monotonic()
            spotter_stream.accept_waveform(SAMPLE_RATE, samples)
            while spotter.is_ready(spotter_stream):
                spotter.decode_stream(spotter_stream)
            found = spotter.get_result(spotter_stream)
            trial.chunk_seconds.append(time.monotonic() - step)
            if found:
                trial.keyword = found
                trial.tokens = spotter.tokens(spotter_stream)
                trial.timestamps = spotter.timestamps(spotter_stream)
                trial.detect_seconds = time.monotonic() - started
                spotter.reset_stream(spotter_stream)
                break  # the answer exists: stop listening now, do not wait out the maximum
    except Exception as exc:  # recorded, not swallowed: the test fails on it after every trial has run
        trial.error = exc
        announce(f"  the trial failed ({type(exc).__name__}: {exc})")
    finally:
        # Always, on every path: detection, timeout, a sherpa or sounddevice failure, or Ctrl+C. This
        # runs even while a KeyboardInterrupt is propagating, and it reports a cleanup failure itself,
        # because the caller may never get the chance to.
        _release(trial, stream)
    trial.listen_seconds = time.monotonic() - started
    announce(f"  -> {trial!r}")
    return trial


def _release(trial, stream) -> None:
    """Stop, close, and PROVE it. `trial.cleanup_ok` is set only when the native stream is definitely
    gone and the microphone is definitely free.

    What the installed sounddevice actually offers (checked, not assumed):
      * `stream.closed` is a real property - `self._ptr == _ffi.NULL` - True after close().
      * `close()` defaults to ignore_errors=True and swallows PortAudio errors while still clearing
        the pointer, so a close failure is only visible with ignore_errors=False. We ask for it.
      * `active`/`stopped` query the native pointer, so they mean nothing after close; `closed` is the
        post-close state to check.

    A failed stop() alone is recorded but not fatal: close() discards pending buffers "as if abort()
    had been called", so a definitive close still leaves no native stream behind. A failed or
    unprovable close IS fatal, because the device may still be open."""
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


def _spread(trials, attribute, title) -> None:
    import statistics
    values = [getattr(trial, attribute) for trial in trials
              if getattr(trial, attribute, None) is not None]
    if not values:
        announce(f"  {title}: nothing measured")
        return
    announce(f"  {title}: min {min(values):.3f}s, median {statistics.median(values):.3f}s, "
             f"max {max(values):.3f}s")
