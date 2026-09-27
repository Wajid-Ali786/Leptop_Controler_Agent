"""
REAL keyword-THRESHOLD comparison for the spoken-stop prototype (Task 6d1c) - PREPARED, NOT RUN YET.

ONE question: does lowering sherpa's keyword threshold from 0.25 to 0.10 materially change how often a
standalone spoken "stop" is detected on this machine, and what happens to the near-misses?

ONE variable. `keywords_threshold` is the only detector setting that differs between the two
conditions - every other argument comes from the same `_settings()` call, and a test pins that.

    $env:RUN_REAL_KWS_THRESHOLD_TEST=1
    venv\\Scripts\\python.exe -m pytest tests/test_kws_threshold_real.py -s

Marked real_kws_threshold and skipped unless RUN_REAL_KWS_THRESHOLD_TEST=1. That is a NEW switch on
purpose: RUN_REAL_KWS_TEST still means the closed Task 6d1b baseline, measured at one setting, and its
evidence must stay readable as exactly that. Neither switch enables the other.

Why it imports the 6d1b harness: the microphone-ownership rule, the streaming trial loop and the
cleanup-abort rule are safety-critical and must exist in exactly ONE copy. This file adds the two
conditions, the schedule and the reporting; it changes none of that machinery.

This is KEYWORD SPOTTING, NOT TRANSCRIPTION - the detector answers "STOP" or nothing, so `prompt` is
what you were ASKED to say and never what the microphone heard. There is no ASR here.

What it never does: execute an action, submit a command, or touch the emergency stop. It reports
evidence and names no winner: no condition is called better, chosen, approved or optimised.

Privacy: cues are printed before the microphone opens, audio lives only in the chunk being fed, only
its peak/RMS level is kept, nothing is written to disk or played back, and the device NAME is never
printed - only the host API, rate, channels and latency of the path.
"""
import time

import pytest

from app.listener import microphone
from tests import test_kws_prototype_real as baseline

pytestmark = pytest.mark.real_kws_threshold

# --- The two conditions. A tuple, so a run cannot edit them ------------------------------------------
BASELINE = "BASELINE"
LOW_THRESHOLD = "LOW_THRESHOLD"
CONDITIONS = ((BASELINE, 0.25), (LOW_THRESHOLD, 0.10))
ORDER = tuple(name for name, _ in CONDITIONS)   # deterministic: never shuffled, never interleaved

# Everything below is LOCKED, and passed explicitly so neither condition can quietly inherit a
# different library default. These ARE the documented defaults of the installed KeywordSpotter.
NUM_TRAILING_BLANKS = 1
MAX_ACTIVE_PATHS = 4
NUM_THREADS = 1
PROVIDER = "cpu"

# Borrowed unchanged from the closed baseline harness, so there is one definition of each.
SAMPLE_RATE = baseline.SAMPLE_RATE
CHUNK_SECONDS = baseline.CHUNK_SECONDS
LISTEN_SECONDS = baseline.LISTEN_SECONDS
POSITIVES, NEGATIVES = baseline.POSITIVES, baseline.NEGATIVES
POSITIVE, NEGATIVE = baseline.POSITIVE, baseline.NEGATIVE
SILENCE_PROMPT = baseline.NEGATIVES[0]           # the one trial that is MEANT to be silent
announce = baseline.announce

TOTAL_TRIALS = len(ORDER) * (len(POSITIVES) + len(NEGATIVES))

NEEDS_DASH_S = ("This comparison prints cues you react to, so run it with -s:\n"
                "    $env:RUN_REAL_KWS_THRESHOLD_TEST=1\n"
                "    venv\\Scripts\\python.exe -m pytest tests/test_kws_threshold_real.py -s")


def threshold_for(condition) -> float:
    """The threshold of one condition. Read from the frozen tuple - nothing tunes during a run."""
    for name, threshold in CONDITIONS:
        if name == condition:
            return threshold
    raise KeyError(condition)


def _settings(threshold) -> dict:
    """EVERY argument the detector is built with. The only value that varies between the two
    conditions is keywords_threshold; both conditions call this same function."""
    return dict(
        tokens=str(baseline.MODEL / baseline.FILES["tokens"]),
        encoder=str(baseline.MODEL / baseline.FILES["encoder"]),
        decoder=str(baseline.MODEL / baseline.FILES["decoder"]),
        joiner=str(baseline.MODEL / baseline.FILES["joiner"]),
        keywords_file=str(baseline.KEYWORDS),
        keywords_score=baseline.KEYWORDS_SCORE,       # 1.0, exactly as the closed baseline ran
        keywords_threshold=threshold,                 # <-- THE ONE VARIABLE
        num_trailing_blanks=NUM_TRAILING_BLANKS,      # 1: pinned, not tuned
        max_active_paths=MAX_ACTIVE_PATHS,            # 4: pinned, not tuned
        num_threads=NUM_THREADS,
        provider=PROVIDER,
    )


def varying_settings() -> set:
    """Which detector arguments actually differ between the two conditions. Must be exactly one."""
    first, second = (_settings(threshold) for _, threshold in CONDITIONS)
    assert set(first) == set(second), "the two conditions must be built from the same arguments"
    return {name for name in first if first[name] != second[name]}


def _schedule():
    """All 30 trials, in the order the operator will meet them: the whole first condition, then the
    whole second. Each item is (condition, label, prompt, expected). No randomisation, no alternating -
    you would have to keep track of which threshold you were speaking into."""
    for condition in ORDER:
        for label, prompt, expected in baseline._schedule():
            yield condition, label, prompt, expected


def trials_of(condition, trials) -> list:
    """The trials of ONE condition. The only way a summary selects trials, so a result measured at one
    threshold can never be counted under the other."""
    chosen = [trial for trial in trials if trial.condition == condition]
    assert all(trial.condition == condition for trial in chosen), "condition mix-up"
    return chosen


class Outcome:
    """What one condition measured. Descriptive only: it names no winner and approves nothing."""

    def __init__(self, condition, trials):
        assert all(trial.condition == condition for trial in trials), (
            f"a trial from another condition would be counted under {condition}")
        self.condition = condition
        self.threshold = threshold_for(condition)
        self.trials = trials
        self.positives = [trial for trial in trials if trial.expected == POSITIVE]
        self.negatives = [trial for trial in trials if trial.expected == NEGATIVE]
        self.detected = [trial for trial in self.positives if trial.detected]
        self.missed = [trial for trial in self.positives if trial.missed]
        self.false_positives = [trial for trial in self.negatives if trial.false_positive]
        # A trial you were ASKED to speak in that fed nothing audible. Reported, never excluded from
        # the counts above - whether it invalidates a miss is a judgement for a human, not for a test.
        self.silent_spoken = [trial for trial in trials
                              if trial.silent and trial.prompt != SILENCE_PROMPT]
        self.overflows = sum(trial.overflows for trial in trials)

    @property
    def false_positive_prompts(self) -> list:
        return [trial.prompt for trial in self.false_positives]


@pytest.fixture
def ready(request):
    import sys
    if request.config.getoption("capture") != "no" or type(sys.stdin).__name__ == "DontReadFromInput":
        pytest.skip(NEEDS_DASH_S)
    missing = [name for name in baseline.FILES.values() if not (baseline.MODEL / name).is_file()]
    if missing:
        pytest.skip(f"{baseline.NEEDS_MODEL}\n(missing: {', '.join(missing)})")
    if not baseline.KEYWORDS.is_file():
        pytest.skip(baseline.NEEDS_KEYWORDS)
    return baseline.MODEL


@pytest.fixture(autouse=True)
def nothing_left_running():
    yield
    assert microphone.owner() is None, f"the microphone is still owned by {microphone.owner()!r}"


def test_whether_a_lower_keyword_threshold_changes_standalone_stop_detection(ready):
    import sherpa_onnx

    varying = varying_settings()
    assert varying == {"keywords_threshold"}, (
        f"this is a one-variable experiment; these also differ: {sorted(varying)}")

    announce("")
    announce("=" * 78)
    announce("KEYWORD-THRESHOLD COMPARISON - nothing runs, nothing is saved or played back.")
    announce("  This is keyword spotting, not speech transcription. The report shows the phrase you")
    announce("  were asked to say, not what the microphone heard. The detector can only report STOP")
    announce("  or no detection - so if you say something else, it simply will not fire, and the")
    announce("  prompt in the report still says what was asked for.")
    announce("  Each trial also reports the LEVEL of the audio it fed (peak/rms) so a keyword miss can")
    announce("  be told apart from a microphone that delivered nothing. No audio is kept.")
    announce("")
    announce(f"  {TOTAL_TRIALS} TRIALS IN TOTAL, in two blocks you will be told about:")
    for condition, threshold in CONDITIONS:
        announce(f"    {condition}: keywords_threshold = {threshold} "
                 f"({len(POSITIVES)} x 'stop' then {len(NEGATIVES)} near-misses)")
    announce(f"  Identical in both blocks: score={baseline.KEYWORDS_SCORE}, "
             f"trailing_blanks={NUM_TRAILING_BLANKS}, max_active_paths={MAX_ACTIVE_PATHS}, "
             f"{SAMPLE_RATE} Hz mono, {CHUNK_SECONDS*1000:.0f} ms chunks, {LISTEN_SECONDS:g} s maximum,")
    announce(f"  the same model and the same generated keyword. The ONLY difference is the threshold.")
    announce("  Say exactly what each cue asks - the near-misses are the point, so do not correct them.")
    announce("=" * 78)
    for line in _device_path():
        announce(f"  {line}")
    announce("=" * 78)

    trials, aborted = [], None
    for condition in ORDER:
        threshold = threshold_for(condition)
        announce("")
        announce("#" * 78)
        announce(f"#  CONDITION {ORDER.index(condition) + 1} OF {len(ORDER)}: {condition}")
        announce(f"#  keywords_threshold = {threshold}. Everything else is unchanged.")
        announce(f"#  {len(POSITIVES) + len(NEGATIVES)} trials follow.")
        announce("#" * 78)
        started = time.monotonic()
        spotter = sherpa_onnx.KeywordSpotter(**_settings(threshold))
        announce(f"detector ready in {time.monotonic() - started:.2f} s "
                 f"(excluded from every trial's timing)")
        for scheduled, label, prompt, expected in _schedule():
            if scheduled != condition:
                continue
            trial = baseline._listen(spotter, label, prompt, expected, condition=condition)
            trials.append(trial)
            if not trial.cleanup_ok:
                # Data from later trials is not worth risking two overlapping device streams, and that
                # includes the whole other condition.
                aborted = trial
                announce(f"\nCOMPARISON ABORTED during {condition} after {label}: cleanup could not "
                         f"be proven. The remaining trials and any remaining condition are cancelled.")
                break
        if aborted is not None:
            break

    announce("")
    announce("-" * 78)
    for trial in trials:
        announce(f"  {trial!r}")

    outcomes = [Outcome(condition, trials_of(condition, trials)) for condition in ORDER
                if trials_of(condition, trials)]
    for outcome in outcomes:
        _describe(outcome)
    _compare(outcomes)

    assert aborted is None, (
        f"the comparison stopped early because cleanup could not be proven after "
        f"{aborted.condition} / {aborted.label}: {aborted.cleanup_error}" if aborted else "")
    assert len(trials) == TOTAL_TRIALS, "every scheduled trial runs once; none is retried"
    assert [outcome.condition for outcome in outcomes] == list(ORDER), "both conditions were measured"
    assert all(len(outcome.trials) == len(POSITIVES) + len(NEGATIVES) for outcome in outcomes), (
        "each condition ran the whole corpus")
    assert microphone.owner() is None, "the microphone was released"
    assert all(trial.cleanup_ok for trial in trials), "every trial's cleanup was proven"
    assert any(trial.chunks for trial in trials), "no audio was streamed at all"
    fed_formats = {trial.audio_format for trial in trials if trial.samples_fed}
    assert fed_formats == {f"float32, 1-D, {int(CHUNK_SECONDS * SAMPLE_RATE)} samples per chunk "
                           f"at {SAMPLE_RATE} Hz mono"}, (
        f"the detector must be fed 16 kHz mono float32, one channel column: {fed_formats}")
    failed = [trial for trial in trials if trial.error is not None]
    assert failed == [], f"trials failed (their data is above): {failed}"


def _device_path() -> list:
    """The audio path this comparison inherits, printed once so the numbers stay interpretable later.

    It is NOT a variable here - nothing about the device, host API, latency or rate is changed. Only
    the backend's own description is read: no stream is opened and nothing is recorded. The device
    NAME is deliberately never included."""
    lines = ["audio path (recorded, NOT a variable in this comparison):",
             f"  asked for: {SAMPLE_RATE} Hz mono float32, blocksize "
             f"{int(CHUNK_SECONDS * SAMPLE_RATE)}"]
    try:
        import sounddevice
        lines[-1] += f", latency setting {sounddevice.default.latency[0]!r}"
        info = sounddevice.query_devices(sounddevice.default.device[0])
        host = sounddevice.query_hostapis(info["hostapi"])["name"]
        native = float(info["default_samplerate"])
        channels = int(info["max_input_channels"])
        lines.append(f"  default input device: host API {host}, native {native:g} Hz, {channels} "
                     f"channels, reported input latency low "
                     f"{float(info['default_low_input_latency']) * 1000:.0f} ms / high "
                     f"{float(info['default_high_input_latency']) * 1000:.0f} ms")
        if native != SAMPLE_RATE or channels != 1:
            lines.append("  -> Windows resamples and/or downmixes before the detector sees anything. "
                         "Unchanged in this comparison; noted so it is not mistaken for a model result.")
    except Exception as exc:
        # Purely informational: a backend that will not describe itself must never end the comparison.
        lines.append(f"  the audio path could not be described ({type(exc).__name__}: {exc})")
    return lines


def _describe(outcome) -> None:
    """One condition's own numbers. Nothing here is compared with the other condition."""
    announce("")
    announce("=" * 78)
    announce(f"  {outcome.condition}: keywords_threshold = {outcome.threshold} "
             f"({len(outcome.trials)} trials)")
    announce(f"  POSITIVES: detected {len(outcome.detected)}/{len(outcome.positives)}, "
             f"false negatives {len(outcome.missed)}")
    for attribute, title in (("detect_seconds", "cue -> detection"),
                             ("open_seconds", "of which: cue -> device open and running"),
                             ("lag_seconds", "approximate processing lag"),
                             ("slowest_chunk_seconds", "slowest single chunk"),
                             ("close_seconds", "stream close")):
        baseline._spread(outcome.detected, attribute, title)
    announce(f"  NEGATIVES: false positives {len(outcome.false_positives)}/{len(outcome.negatives)}")
    if outcome.false_positives:
        for trial in outcome.false_positives:   # the exact prompts, never only a count
            announce(f"    fired on the prompt {trial.prompt!r} after {trial.detect_seconds:.2f}s "
                     f"-> keyword {trial.keyword!r}")
    else:
        announce("    none fired")
    announce("  AUDIO PRESENCE (levels only - no audio was kept, and no word was identified):")
    for trial in outcome.trials:
        if not trial.samples_fed:
            announce(f"    {trial.label}: nothing fed")
            continue
        announce(f"    {trial.label}: peak {trial.peak:.3f}, rms {trial.rms:.4f}, "
                 f"{trial.audio_seconds_fed:.1f}s fed, overflows {trial.overflows}"
                 + ("   <- NOTHING AUDIBLE" if trial.silent else ""))
    if outcome.silent_spoken:
        announce(f"    FLAG: {len(outcome.silent_spoken)} trial(s) you were asked to SPEAK in received "
                 f"near-silent input. They are STILL counted above - read them yourself:")
        for trial in outcome.silent_spoken:
            announce(f"      {trial.label} (prompt {trial.prompt!r}): peak {trial.peak:.3f}")
    else:
        announce("    every trial you were asked to speak in carried audible audio")
    announce(f"  input overflows reported by the device: {outcome.overflows}"
             + (" (audio was dropped between reads)" if outcome.overflows else ""))
    announce("=" * 78)


def _compare(outcomes) -> None:
    """Side by side, descriptive. No winner, no recommendation, no approval - evidence only."""
    announce("")
    announce("-" * 78)
    announce("  SIDE BY SIDE (evidence only - this test picks no winner and approves nothing)")
    for outcome in outcomes:
        announce("")
        announce(f"  threshold {outcome.threshold}  ({outcome.condition})")
        announce(f"    positive detections : {len(outcome.detected)}/{len(outcome.positives)}")
        announce(f"    false negatives     : {len(outcome.missed)}")
        fired = outcome.false_positive_prompts
        announce(f"    false positives     : {len(fired)}/{len(outcome.negatives)}")
        announce(f"    false-positive prompts: "
                 + (", ".join(repr(prompt) for prompt in fired) if fired else "(none)"))
        announce(f"    near-silent spoken trials: {len(outcome.silent_spoken)}"
                 f", device overflows: {outcome.overflows}")
    announce("")
    announce("  Reading this needs a human: a change in detections is only about the threshold if the")
    announce("  audio levels and overflow counts above are comparable between the two blocks.")
    announce("  For scale: the Ctrl+Alt+Backspace hotkey stops in about 22 ms; the measured Whisper")
    announce("  path took about 11.5-11.9 s; the longest Phase 1 action acts for 10.0 s.")
    announce("  'cue -> detection' is NOT model inference latency: it also contains the device open,")
    announce("  the device's own input buffering, and your reaction time after reading the cue.")
    announce("-" * 78)
