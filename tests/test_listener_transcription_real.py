"""
REAL recording -> transcription acceptance for Phase 2 Feature 4 (Task 4b).

Marked real_transcription and skipped unless RUN_REAL_TRANSCRIPTION_TEST=1. None of
RUN_REAL_MICROPHONE_TEST, RUN_REAL_RECORDING_TEST or RUN_REAL_MODEL_TEST enables it: this is the only
group that records AND recognizes, so it has its own, explicit switch. Run it with pytest's output
visible, because the recording notice and the transcript are printed for YOU to read:

    RUN_REAL_TRANSCRIPTION_TEST=1 python -m pytest tests/test_listener_transcription_real.py -s

What happens: the model already in listener.model_dir is loaded (never downloaded - the network is
switched off for the whole test), you are asked to say ONE short phrase, a bounded recording is taken,
and it is recognized. The transcript is PRINTED to the screen on purpose, so you can judge the result
yourself; it is never written to a file, never logged and never executed as a command.

What is asserted is structure, not words: a Transcript, some text, the recording's own length, a
language code, and a probability that is a number for auto-detection and None for a configured
language. Word accuracy is deliberately NOT a gate - the 'small' model's Roman Urdu is not stable
enough to assert, and judging the words is later work, not Feature 4's.

Privacy: the audio exists only in the Recording this test holds, and the transcript only in the
Transcript it holds and the line printed for you. Nothing is saved, logged or played back.
"""
import dataclasses
import socket
import time

import pytest

from app.listener import adapter, logic, microphone
from app.listener.models import LIMIT, ModelStatus, Recording, Transcript, VoiceFailure

pytestmark = pytest.mark.real_transcription

PHRASE = "open notepad"
MAX_SECONDS = 6.0


def announce(capsys, text):
    with capsys.disabled():
        print(f"\n    {text}", flush=True)


@pytest.fixture
def offline(monkeypatch):
    """No network for the whole test: any attempt to connect raises instead of reaching the internet."""
    def refuse(*args, **kwargs):
        raise OSError("network access is forbidden in the real-transcription test")
    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.fixture(autouse=True)
def microphone_is_free():
    yield
    assert microphone.owner() is None, f"the microphone is still owned by {microphone.owner()!r}"


def settings(**overrides):
    """The real listener config, always on the system-default microphone."""
    return dataclasses.replace(logic.listener_settings(), input_device="", **overrides)


def model_or_skip(configured):
    result = adapter.ensure_model(configured)
    if isinstance(result, VoiceFailure):
        pytest.skip(result.message)
    assert isinstance(result, ModelStatus)
    return result


def speak(capsys, configured, *, prompt=PHRASE):
    """Ask for one short phrase, record it bounded, and hand back the Recording."""
    announce(capsys, f'RECORDING STARTS NOW - up to {MAX_SECONDS:g} seconds. Please say: "{prompt}"')
    announce(capsys, "The audio is not saved or played back, and nothing is executed.")
    started = time.monotonic()
    try:
        captured = adapter.capture(configured, max_seconds=MAX_SECONDS)
    finally:
        announce(capsys, "RECORDING STOPPED")
    if isinstance(captured, VoiceFailure):
        pytest.skip(f"no usable microphone here: {captured.message}")
    assert isinstance(captured, Recording) and captured.stopped_by == LIMIT
    announce(capsys, f"captured {captured.seconds:.1f} s in {time.monotonic() - started:.1f} s")
    return captured


def test_a_spoken_phrase_is_recognized_with_the_local_model(offline, capsys):
    configured = settings(language="auto")
    status = model_or_skip(configured)
    announce(capsys, f"model ready: {status.model_size} on {status.device} ({status.compute_type})")

    captured = speak(capsys, configured)
    started = time.monotonic()
    result = adapter.transcribe(captured, configured)
    took = time.monotonic() - started

    assert isinstance(result, Transcript), f"{result!r}: {getattr(result, 'message', '')}"
    # Printed on purpose: this is the one place a transcript is meant to be read, by you.
    announce(capsys, f"TRANSCRIPT: {result.text!r}")
    announce(capsys, f"recognized {result.audio_seconds:.1f} s of audio in {took:.1f} s "
                     f"({len(result.text)} characters, language {result.language!r}, "
                     f"probability {result.language_probability})")
    assert result.text.strip(), "nothing was recognized - say the phrase a little louder and try again"
    assert result.audio_seconds == pytest.approx(captured.seconds)
    assert result.language, "auto-detection reports the language it decided on"
    assert isinstance(result.language_probability, float), "a detected language carries a probability"
    assert repr(result) != result.text and result.text not in repr(result), "the repr hides speech"


def test_a_configured_language_is_used_and_reports_no_invented_confidence(offline, capsys):
    """With an explicit listener.language, faster-whisper reports a probability of 1 as a placeholder.
    The Transcript must say None instead - it was told, it did not measure."""
    configured = settings(language="en")
    model_or_skip(configured)
    captured = speak(capsys, configured)
    result = adapter.transcribe(captured, configured)

    assert isinstance(result, Transcript), f"{result!r}: {getattr(result, 'message', '')}"
    announce(capsys, f"TRANSCRIPT (en): {result.text!r}")
    assert result.language == "en"
    assert result.language_probability is None
