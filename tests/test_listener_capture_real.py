"""
REAL microphone recording acceptance for Phase 2 Feature 2 (bounded capture).

Every test here is marked real_recording and is skipped unless RUN_REAL_RECORDING_TEST=1.
RUN_REAL_MICROPHONE_TEST (the read-only probes) does NOT enable these - recording needs its own,
explicit opt-in. Run them with pytest's output visible, so the start/stop notices appear:

    RUN_REAL_RECORDING_TEST=1 python -m pytest tests/test_listener_capture_real.py -s

What these tests prove is only what Feature 2 owns: that a real device opens on the canonical format,
that a bounded capture comes back with exactly its maximum, that cancellation on the real backend ends
promptly, and that the stream and the microphone are given back so the next capture works. They do
NOT judge the sound - no loudness threshold, no speech content, no transcription. Whether the audio is
all zero is printed as a diagnostic and never asserted: a muted, denied or merely silent microphone all
look the same at this layer.

Privacy: audio exists only in the Recording objects these tests hold. Nothing is written to a file,
logged or played back, and each test drops its references once its assertions are done. That releases
them normally - it does not securely wipe the memory they occupied, and nothing here claims it does.
"""
import dataclasses
import threading
import time

import pytest

from app.listener import adapter, logic, microphone
from app.listener.models import CANCELLED, FORMAT_UNSUPPORTED, LIMIT, Recording, VoiceFailure

pytestmark = pytest.mark.real_recording

HELPER_NAME = "real-capture-helper"
CAPTURE_SLACK_SECONDS = 1.0  # beyond the product's own watchdog margin, for process/driver overhead


def default_settings():
    """The real listener config, but always on the system-default path (whatever the file says)."""
    return dataclasses.replace(logic.listener_settings(), input_device="")


def announce(capsys, text):
    with capsys.disabled():
        print(f"\n    {text}", flush=True)


def bound(max_seconds):
    """The most a capture may take on the wall clock: its maximum, the watchdog margin, and slack."""
    return max_seconds + adapter.WATCHDOG_MARGIN_SECONDS + CAPTURE_SLACK_SECONDS


def describe(result):
    """For assertion messages only: a failure's user-facing text (this is test output, not a log)."""
    return f"{result!r}: {result.message}" if isinstance(result, VoiceFailure) else repr(result)


def record(capsys, max_seconds, *, note=""):
    announce(capsys, f"RECORDING STARTS NOW — up to {max_seconds:g} seconds{note}")
    announce(capsys, "No speech is required. Audio will not be saved or played back.")
    started = time.monotonic()
    try:
        return adapter.capture(default_settings(), max_seconds=max_seconds), time.monotonic() - started
    finally:
        announce(capsys, "RECORDING STOPPED")


@pytest.fixture(autouse=True)
def nothing_left_running():
    """After every real test: the microphone is free and no helper thread survives."""
    yield
    assert microphone.owner() is None, f"the microphone is still owned by {microphone.owner()!r}"
    survivors = [thread.name for thread in threading.enumerate() if thread.name == HELPER_NAME]
    assert survivors == [], f"capture helper threads still alive: {survivors}"


# --- A. The default path, two seconds ---------------------------------------------------------------

def test_a_default_two_second_capture(capsys):
    recording, elapsed = record(capsys, 2.0)
    assert isinstance(recording, Recording), describe(recording)
    assert recording.stopped_by == LIMIT
    assert (recording.sample_rate, recording.channels, recording.dtype) == (16000, 1, "int16")
    assert recording.frames == 32000, "the last callback block is trimmed to exactly the maximum"
    assert len(recording.pcm) == recording.frames * 2
    assert recording.seconds == recording.frames / 16000 == 2.0
    assert recording.used_default is True and recording.device_index is None
    assert microphone.owner() is None
    assert elapsed < bound(2.0), f"took {elapsed:.2f}s"
    announce(capsys, f"result: {recording!r}")
    announce(capsys, f"elapsed {elapsed:.2f}s, overflows {recording.overflows}, "
                     f"all-zero audio: {not any(recording.pcm)} (diagnostic only)")
    del recording


# --- B. Immediately again: the first stream really was given back -----------------------------------

def test_b_an_immediate_second_capture(capsys):
    recording, elapsed = record(capsys, 0.5)
    assert isinstance(recording, Recording), describe(recording)
    assert recording.stopped_by == LIMIT and recording.frames == 8000
    assert microphone.owner() is None
    assert elapsed < bound(0.5), f"took {elapsed:.2f}s"
    announce(capsys, f"result: {recording!r}, elapsed {elapsed:.2f}s, "
                     f"all-zero audio: {not any(recording.pcm)} (diagnostic only)")
    del recording


# --- C. Cancellation on the real backend: after ownership, never on a timer ---------------------------

def test_c_real_cancellation_after_the_capture_owns_the_microphone(capsys):
    """Cancel is set only once microphone.owner() proves the capture has started - no timer guesses
    the order. Ownership is taken before the format check and before audio flows, so this may land
    before the first callback (a correct cancelled Recording with 0 frames) or mid-stream. Either is
    a real cancellation; the test reports which one happened and asserts neither."""
    cancel = threading.Event()
    outcome = {}

    def run():
        try:
            outcome["result"] = adapter.capture(default_settings(), max_seconds=5.0, cancel=cancel)
        except BaseException as exc:  # handed back to the test thread below
            outcome["error"] = exc

    helper = threading.Thread(target=run, name=HELPER_NAME, daemon=True)
    announce(capsys, "RECORDING STARTS NOW — up to 5 seconds (it will be cancelled right away)")
    announce(capsys, "No speech is required. Audio will not be saved or played back.")
    started = time.monotonic()
    helper.start()
    try:
        handshake_deadline = started + 5.0
        while microphone.owner() is None and helper.is_alive() and time.monotonic() < handshake_deadline:
            time.sleep(0.001)
        owner_seen = microphone.owner()
        cancel.set()
        cancelled_at = time.monotonic() - started
        helper.join(bound(5.0))
    finally:
        cancel.set()  # never leave a capture running if an assertion above failed
        announce(capsys, "RECORDING STOPPED")
    elapsed = time.monotonic() - started

    assert "error" not in outcome, f"capture raised {outcome.get('error')!r}"
    assert owner_seen == adapter.OWNER, \
        f"the capture never took the microphone before the cancel (owner seen: {owner_seen!r})"
    assert not helper.is_alive(), "the capture helper did not finish after cancel"
    result = outcome.get("result")
    assert isinstance(result, Recording), describe(result)
    assert result.stopped_by == CANCELLED
    assert result.frames < 5 * 16000, "it must end before its 5-second maximum"
    assert elapsed < 5.0, f"cancellation took {elapsed:.2f}s - it should end well before the maximum"
    assert microphone.owner() is None
    case = ("cancelled BEFORE audio arrived (0 frames)" if result.frames == 0
            else f"cancelled MID-STREAM after {result.frames} frames ({result.seconds:.3f}s of audio)")
    announce(capsys, f"result: {result!r}")
    announce(capsys, f"cancel set {cancelled_at * 1000:.1f} ms after start; helper ended "
                     f"{elapsed:.2f}s after start; case: {case}")
    del result, outcome

    after, after_elapsed = record(capsys, 0.25, note=" (checking the cancelled stream was released)")
    assert isinstance(after, Recording) and after.stopped_by == LIMIT and after.frames == 4000, describe(after)
    announce(capsys, f"follow-up capture: {after!r}, elapsed {after_elapsed:.2f}s")
    del after


# --- D. A real format refusal - this one records nothing ---------------------------------------------

class _NoRecordingBackend:
    """The real sounddevice, except that opening a stream is forbidden and every format check is
    recorded - so the test can prove what was, and was not, attempted."""

    def __init__(self, real):
        self._real = real
        self.checked = []
        self.opened = []

    def __getattr__(self, name):
        return getattr(self._real, name)

    def check_input_settings(self, **kwargs):
        self.checked.append(kwargs)
        return self._real.check_input_settings(**kwargs)

    def RawInputStream(self, **kwargs):
        self.opened.append(kwargs)
        raise AssertionError("a stream was opened - this test must never record")


def test_d_the_realtek_wasapi_path_is_refused_without_recording(monkeypatch):
    import sounddevice  # the real backend; the spy below forwards to it

    devices = adapter.list_input_devices()
    assert isinstance(devices, tuple), describe(devices)
    candidates = [d for d in devices if d.host_api == "Windows WASAPI" and "realtek" in d.name.lower()]
    if not candidates:
        pytest.skip("the Realtek microphone is not on the WASAPI path in today's device list")
    target = candidates[0]
    try:
        sounddevice.check_input_settings(device=target.index, samplerate=16000, channels=1, dtype="int16")
    except sounddevice.PortAudioError:
        pass
    else:
        pytest.skip(f"device [{target.index}] on WASAPI now ACCEPTS 16 kHz mono - nothing to refuse")

    spy = _NoRecordingBackend(sounddevice)
    monkeypatch.setattr(adapter, "_audio", lambda: spy)
    result = adapter.capture(dataclasses.replace(default_settings(), input_device=target.index),
                             max_seconds=0.5)
    assert isinstance(result, VoiceFailure) and result.kind == FORMAT_UNSUPPORTED, describe(result)
    assert spy.opened == [], "no recording stream may be opened"
    assert [check["device"] for check in spy.checked] == [target.index], \
        "exactly the selected device was checked - no other path, copy or default was tried"
    assert f"[{target.index}]" in result.message and "Windows WASAPI" in result.message
    assert "did not switch" in result.message
    assert microphone.owner() is None
