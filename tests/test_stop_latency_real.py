"""
REAL spoken-"stop" latency measurement (Task 6c2): three trials, with your microphone and the real
local model.

Marked real_stop_latency and skipped unless RUN_REAL_STOP_LATENCY_TEST=1 - which nothing else sets.
Run it with -s, so each trial's cue and numbers appear as they happen:

    $env:RUN_REAL_STOP_LATENCY_TEST=1
    venv\\Scripts\\python.exe -m pytest tests/test_stop_latency_real.py -s

What happens, three times: a visible cue, then a FIXED 1.5-second bounded capture that ends on its
own, then transcription, then the pure stop matcher. You say the single word "stop" as soon as the cue
appears. There is no Enter to press - an action-time listener could never ask for one - and no
endpoint detection, no adaptive window, no retry.

NOTHING is executed: no command reaches app.console, the emergency stop is never triggered, and this
file does not import it. The only result is a boolean and some timings.

Everything about recognition stays exactly as configured - same model size, compute type, VAD,
min_silence_ms and initial_prompt terms ("stop" is deliberately NOT added to the prompt). The single
difference from normal voice mode is language="en" on a copy of the settings, because the stop phrase
is one fixed English word.

Privacy: the cue is printed before the microphone opens, nothing is saved or played back, no PCM
anywhere, and no transcript reaches companion.log. Each transcript IS printed to your terminal on
purpose - reading it is how you judge a misrecognition, which is data, not a reason to tune anything.
"""
import sys

import pytest

from app.listener import logic, microphone
from app.listener.models import ModelStatus, VoiceFailure
from tests import stop_latency

pytestmark = pytest.mark.real_stop_latency

NEEDS_DASH_S = (
    "This measurement prints a cue you have to react to, so run it with -s:\n"
    "    $env:RUN_REAL_STOP_LATENCY_TEST=1\n"
    "    venv\\Scripts\\python.exe -m pytest tests/test_stop_latency_real.py -s")
NEEDS_ENABLED = ("Set listener.enabled: true in config/config.yaml before running this measurement. "
                 "It never changes your configuration and has no override.")


def announce(text=""):
    print(text, flush=True)


@pytest.fixture(autouse=True)
def nothing_left_running():
    yield
    assert microphone.owner() is None, f"the microphone is still owned by {microphone.owner()!r}"


@pytest.fixture
def settings(request):
    if request.config.getoption("capture") != "no" or type(sys.stdin).__name__ == "DontReadFromInput":
        pytest.skip(NEEDS_DASH_S)
    configured = logic.listener_settings()
    if configured.enabled is not True:
        pytest.skip(NEEDS_ENABLED)
    return configured


def test_how_long_a_spoken_stop_takes_to_detect(settings):
    announce("")
    announce("=" * 78)
    announce("SPOKEN-STOP LATENCY - three trials. Nothing runs; nothing is saved or played back.")
    announce("")
    announce(f'  Each trial prints  {stop_latency.CUE}')
    announce(f"  Say the single word \"stop\" straight away: the microphone closes by itself after "
             f"{stop_latency.STOP_WINDOW_SECONDS:g} s,")
    announce("  and recognition then takes a while on this machine. Nothing to press.")
    announce("")
    announce("  A misheard trial is a result, not a failure - do not repeat it.")
    announce("=" * 78)
    announce("")

    announce("Preparing the model (not timed; no microphone is opened)...")
    ready = stop_latency.require_ready(settings)
    if isinstance(ready, VoiceFailure):
        pytest.skip(f"the speech model is not ready: {ready.message}")
    assert isinstance(ready, ModelStatus)
    announce(f"model ready: {ready.model_size} on {ready.device} ({ready.compute_type})"
             f"{'' if ready.load_seconds is None else f', loaded in {ready.load_seconds:.1f} s'}")

    listening = stop_latency.stop_settings(settings)
    assert listening.language == "en" and settings.language != "en" or settings.language == "en"
    assert listening.vad_filter is settings.vad_filter, "VAD stays exactly as configured"
    assert listening.initial_prompt_terms == settings.initial_prompt_terms

    measured = stop_latency.stop_trials(listening, write=_trial_writer(),
                                        trials=stop_latency.TRIALS)

    announce("")
    announce("-" * 78)
    for one in measured:
        announce(f"  {one!r}")
    announce("")
    for attribute, title in (("capture_seconds", "capture"),
                             ("transcription_seconds", "transcription"),
                             ("total_seconds", "TOTAL detection")):
        announce("  " + stop_latency.summary_line(measured, attribute, title))
    matched = [one.stop_match for one in measured]
    announce(f"  stop_match per trial: {matched}")
    announce("")
    announce("  For scale: the Ctrl+Alt+Backspace hotkey stops in about 22 ms, and the longest")
    announce("  Phase 1 action (typing 1000 characters at 0.01 s each) acts for 10.0 s.")
    announce("-" * 78)

    assert len(measured) == stop_latency.TRIALS, "three independent trials, none retried"
    failures = [one for one in measured if one.kind not in (stop_latency.TRANSCRIPT, "no_speech")]
    if failures:
        announce(f"  trials that could not be measured: {failures}")
    assert any(one.total_seconds is not None for one in measured), (
        "no trial produced a timing at all - the microphone or the model is unavailable, and the "
        "measurement remains open")


def _trial_writer():
    """Prints the cue before each capture, on this thread, immediately."""
    def write(text):
        announce("")
        announce(text)
    return write
