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

What it never does: execute an action, call app.console.handle_command, or touch the emergency stop.
This file imports none of them. The result of a trial is a keyword string and some timings.

This is a PROTOTYPE benchmark. It does not prove the production microphone-ownership integration or
the action-seam design - those remain later design tasks. sherpa-onnx is not a permanent dependency
yet, and no module under app/ imports it.

Privacy: the cue is printed before the microphone opens, audio lives only in the chunk being fed and
is discarded, nothing is written to disk or played back, and no audio or detected text reaches the log.
"""
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

POSITIVES = ("stop", "stop", "stop", "stop", "stop")
NEGATIVES = ("(say nothing at all)", "notepad", "stopped", "stopping", "please stop",
             "open notepad and type hello")

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
    print(text, flush=True)


class Trial:
    """One listening attempt. Timings only, plus the keyword the detector itself returned."""

    def __init__(self, label, said):
        self.label = label
        self.said = said                 # what you were asked to say, for the report
        self.keyword = ""                # exactly what get_result() returned (a string)
        self.tokens = None
        self.timestamps = None
        self.listen_seconds = None       # cue -> detection, or the whole window when nothing fired
        self.detect_seconds = None       # same clock; None when nothing fired
        self.audio_seconds_fed = 0.0
        self.chunks = 0
        self.chunk_seconds = []          # how long each decode step took

    @property
    def detected(self) -> bool:
        return bool(self.keyword)

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
        return (f"Trial({self.label}, said={self.said!r}, {found}, detect={detect}, "
                f"approx_lag={lag}, audio_fed={self.audio_seconds_fed:.1f}s, "
                f"chunks={self.chunks}, slowest_chunk={slowest})")

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

    trials = []
    for number, said in enumerate(POSITIVES, start=1):
        trials.append(_listen(spotter, f"positive {number}", said))
    for number, said in enumerate(NEGATIVES, start=1):
        trials.append(_listen(spotter, f"negative {number}", said))

    announce("")
    announce("-" * 78)
    for trial in trials:
        announce(f"  {trial!r}")

    positives = [trial for trial in trials if trial.label.startswith("positive")]
    negatives = [trial for trial in trials if trial.label.startswith("negative")]
    hits = [trial for trial in positives if trial.detected]
    false_positives = [trial for trial in negatives if trial.detected]
    announce("")
    announce(f"  detected on 'stop': {len(hits)}/{len(positives)}   "
             f"false positives: {len(false_positives)}/{len(negatives)}")
    for attribute, title in (("detect_seconds", "cue -> detection"),
                             ("lag_seconds", "approximate processing lag"),
                             ("slowest_chunk_seconds", "slowest single chunk")):
        _spread(hits, attribute, title)
    if false_positives:
        announce(f"  FALSE POSITIVES on: {[trial.said for trial in false_positives]}")
    announce("")
    announce("  For scale: the Ctrl+Alt+Backspace hotkey stops in about 22 ms; the measured Whisper")
    announce("  path took about 11.5-11.9 s; the longest Phase 1 action acts for 10.0 s.")
    announce("-" * 78)

    assert len(trials) == len(POSITIVES) + len(NEGATIVES), "every trial runs once; none is retried"
    assert microphone.owner() is None, "the microphone was released"
    assert any(trial.chunks for trial in trials), "no audio was streamed at all"


def _listen(spotter, label, said) -> Trial:
    """One trial: cue, then stream 100 ms chunks into the detector until it fires or time runs out.

    Synchronous on this thread - sounddevice's blocking read is already bounded to one chunk, so no
    worker, no cancel event and no liveness state are needed for a benchmark."""
    import numpy
    import sounddevice

    trial = Trial(label, said)
    frames = int(CHUNK_SECONDS * SAMPLE_RATE)
    announce("")
    announce(f"[{label}] SAY: {said}")
    if not microphone.acquire(OWNER):
        announce(f"  the microphone is busy ({microphone.owner()!r}); this trial is not measured")
        return trial
    stream = None
    started = time.monotonic()
    try:
        stream = sounddevice.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                         blocksize=frames)
        stream.start()
        spotter_stream = spotter.create_stream()
        while time.monotonic() - started < LISTEN_SECONDS:
            block, _overflowed = stream.read(frames)
            trial.chunks += 1
            trial.audio_seconds_fed += CHUNK_SECONDS
            step = time.monotonic()
            spotter_stream.accept_waveform(SAMPLE_RATE, block[:, 0])
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
                break
    finally:
        if stream is not None:
            closing = time.monotonic()
            stream.stop()
            stream.close()
            trial.close_seconds = time.monotonic() - closing
        microphone.release()
    trial.listen_seconds = time.monotonic() - started
    announce(f"  -> {trial!r}")
    return trial


def _spread(trials, attribute, title) -> None:
    import statistics
    values = [getattr(trial, attribute) for trial in trials
              if getattr(trial, attribute, None) is not None]
    if not values:
        announce(f"  {title}: nothing measured")
        return
    announce(f"  {title}: min {min(values):.3f}s, median {statistics.median(values):.3f}s, "
             f"max {max(values):.3f}s")
