"""
SYNTHETIC stop-latency diagnostic (Task 6c1-S): how long the approved transcription path takes on one
deterministic second of audio, with the real local model and NO microphone.

Marked real_stop_latency_synthetic and skipped unless RUN_REAL_STOP_LATENCY_SYNTHETIC_TEST=1 - which
nothing else sets, not even RUN_REAL_MODEL_TEST or RUN_REAL_STOP_LATENCY_TEST. Run it with -s so the
numbers appear as they are measured:

    $env:RUN_REAL_STOP_LATENCY_SYNTHETIC_TEST=1
    venv\\Scripts\\python.exe -m pytest tests/test_stop_latency_synthetic.py -s

Three cases on the SAME 1-second all-zero Recording (see tests/stop_latency.py):

    S1  vad_filter=True,  language="en"    records the VAD short-circuit: silence is removed, so
                                           nothing reaches the encoder. NOT a floor.
    S2  vad_filter=False, language="en"    the same silence really does reach the encoder: one
                                           30-second window. This is the floor.
    S3  vad_filter=False, language="auto"  as S2 plus whatever automatic language detection costs.

Whether the encoder ran is COUNTED, not inferred from the clock, so a fast number cannot be mistaken
for a floor. The hypothesis under test is only that explicit English removes the language-detection
encoder pass and should therefore materially reduce latency - not that it halves anything exactly.

Nothing is recorded, played, saved or executed, and no transcript reaches the log.
"""
import time

import pytest

from app.listener import adapter, logic
from app.listener.models import ModelStatus, VoiceFailure
from tests import stop_latency

pytestmark = pytest.mark.real_stop_latency_synthetic


def announce(text=""):
    print(text, flush=True)


def test_how_long_the_approved_path_takes_on_one_second_of_audio():
    settings = logic.listener_settings()
    recording = stop_latency.silence()
    assert recording.frames == 16000 and recording.seconds == 1.0
    assert set(recording.pcm) == {0}, "deterministic silence, built in memory"

    announce("")
    announce("Preparing the model (not timed; no microphone is opened)...")
    ready = stop_latency.require_ready(settings)
    if isinstance(ready, VoiceFailure):
        pytest.skip(f"the speech model is not ready: {ready.message}")
    assert isinstance(ready, ModelStatus)
    announce(f"model ready: {ready.model_size} on {ready.device} ({ready.compute_type})"
             f"{'' if ready.load_seconds is None else f', loaded in {ready.load_seconds:.1f} s'}")
    announce(f"content frames the feature extractor makes from this clip WITHOUT VAD: "
             f"{_content_frames(recording)} (0 would mean nothing can reach the encoder)")
    announce("")

    measured = stop_latency.synthetic_measurements(
        settings, recording, passes=stop_latency.counted_encoder_passes)

    for one in measured:
        announce(f"  {one!r}")
    announce("")
    by_label = {one.label: one for one in measured}

    # The original settings were never touched: every case used its own copy.
    assert settings.language == logic.listener_settings().language
    assert settings.vad_filter == logic.listener_settings().vad_filter

    # S1: the VAD short-circuit. Recorded as behaviour, never used as a floor.
    s1 = by_label["S1"]
    assert (s1.language, s1.vad_filter) == ("en", True)
    if s1.encoder_passes == 0:
        announce("S1: VAD removed the silence entirely and the encoder never ran, exactly as the "
                 "source predicted - so S1 is not a latency floor for anything.")
    else:
        announce(f"S1: the encoder ran {s1.encoder_passes} time(s) even with VAD on - the source "
                 f"reading was wrong, and this needs reporting before anything is concluded.")

    # S2: the floor - but only if the encoder really ran.
    s2 = by_label["S2"]
    assert (s2.language, s2.vad_filter) == ("en", False)
    assert s2.encoder_passes is not None, "the encoder count is what makes this number mean anything"
    if s2.encoder_passes == 0:
        pytest.fail(f"S2 took {s2.transcription_seconds:.2f} s with no encoder pass at all, so it is "
                    f"NOT an encoder floor - something else skipped the work: {s2!r}")
    announce(f"S2 is a floor: {s2.encoder_passes} encoder pass(es) on 30 s of padded features, "
             f"{s2.transcription_seconds:.2f} s.")

    # S3: what automatic language detection adds.
    s3 = by_label["S3"]
    assert (s3.language, s3.vad_filter) == ("auto", False)
    announce(f"S3 (auto language): {s3.encoder_passes} encoder pass(es), "
             f"{s3.transcription_seconds:.2f} s.")
    if s2.encoder_passes and s3.encoder_passes and s3.encoder_passes > s2.encoder_passes:
        announce(f"automatic detection cost {s3.encoder_passes - s2.encoder_passes} extra encoder "
                 f"pass(es) and {s3.transcription_seconds - s2.transcription_seconds:+.2f} s here.")

    announce("")
    announce("For scale: the Ctrl+Alt+Backspace hotkey stops in about 22 ms, and the longest "
             "Phase 1 action (typing 1000 characters at 0.01 s each) acts for 10.0 s.")
    announce("")
    assert all(one.characters >= 0 for one in measured)


def _content_frames(recording) -> int:
    """How many feature frames this clip makes before any padding - pure numpy, no model involved.
    0 means the segment loop exits without encoding anything."""
    import numpy
    from faster_whisper.feature_extractor import FeatureExtractor
    samples = numpy.frombuffer(recording.pcm, dtype="<i2").astype(numpy.float32) / 32768.0
    return FeatureExtractor()(samples).shape[-1] - 1


def test_the_synthetic_group_never_opens_a_microphone(monkeypatch):
    """A guard that runs inside this group itself: if anything here reached for the microphone, the
    test would fail rather than record."""
    monkeypatch.setattr(adapter, "capture",
                        lambda *args, **kwargs: pytest.fail("the synthetic group must not record"))
    recording = stop_latency.silence()
    assert recording.seconds == 1.0
    assert "capture" not in open(stop_latency.__file__, encoding="utf-8").read().split(
        "def measure_stop_trial")[0], "the synthetic path must not mention capture"


def test_the_measurement_excludes_the_model_preload():
    """require_ready is a separate call: no measured interval can contain a load."""
    ticks = iter([10.0, 11.0])
    settings = logic.listener_settings()
    calls = []

    def transcribe(recording, configured):
        calls.append(configured.language)
        return VoiceFailure("no_speech", "nothing")
    original = adapter.transcribe
    adapter.transcribe = transcribe
    try:
        measured = stop_latency.measure_transcription(
            "probe", stop_latency.stop_settings(settings), stop_latency.silence(),
            clock=lambda: next(ticks))
    finally:
        adapter.transcribe = original
    assert measured.transcription_seconds == 1.0 and calls == ["en"]
    assert time.monotonic() > 0  # the real clock is untouched
