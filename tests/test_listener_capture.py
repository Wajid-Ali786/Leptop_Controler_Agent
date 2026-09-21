"""
Tests for Phase 2 Feature 2 Task 2b: bounded microphone capture (app/listener/adapter.capture).

No microphone is opened here. app.listener.adapter._audio() is replaced by a fake backend whose
RawInputStream behaves like sounddevice's callback stream: start() hands the callback however many
frames the test script chooses (deliberately irregular), and a callback's CallbackStop / CallbackAbort
/ unexpected exception is treated exactly as sounddevice's C wrapper treats it - including the part
that matters most: an unexpected exception is swallowed and the stream aborted, so the caller would
never hear of it unless capture() keeps it and raises it again. The fake has no read() at all, so a
blocking read would fail loudly rather than quietly work.

Every audio sample is a deterministic ramp (sample n has value n), so the tests can prove not just
how many frames came back but that they are exactly the right ones, in order.
"""
import ast
import itertools
import logging
import math
import threading

import pytest

from app.listener import adapter, logic, microphone
from app.listener.models import (CANCELLED, CAPTURE_UNAVAILABLE, DEVICE_BUSY, DEVICE_LOST,
                                 FORMAT_UNSUPPORTED, LIMIT, NO_DEVICE, PERMISSION_DENIED, SAMPLE_RATE,
                                 InputDevice, ListenerSettings, Recording, VoiceFailure)
from config import settings as config_settings

MME, DIRECTSOUND, WASAPI, WDMKS = "MME", "Windows DirectSound", "Windows WASAPI", "Windows WDM-KS"
HOST_APIS = (MME, DIRECTSOUND, WASAPI, WDMKS)
REALTEK = "Microphone (Realtek High Definition Audio)"
E_ACCESSDENIED_SIGNED = 0x80070005 - 2 ** 32  # how PortAudio's `long` host error code arrives


# --- A fake sounddevice ---------------------------------------------------------------------------

class FakePortAudioError(Exception):
    """Same argument shape as sounddevice.PortAudioError: (text, code[, (host API index, host code,
    host text)])."""


class FakeCallbackStop(Exception):
    pass


class FakeCallbackAbort(Exception):
    pass


class Flags:
    def __init__(self, overflow=False):
        self.input_overflow = overflow


def ramp(start, frames):
    """int16 little-endian samples start, start+1, ... - a sample's value is its position."""
    return b"".join(((start + n) % 65536).to_bytes(2, "little") for n in range(frames))


class FakeStream:
    def __init__(self, backend, *, device, samplerate, channels, dtype, blocksize, callback,
                 finished_callback):
        self.backend = backend
        self.settings = dict(device=device, samplerate=samplerate, channels=channels, dtype=dtype,
                             blocksize=blocksize)
        self.callback = callback
        self.finished_callback = finished_callback
        self.done = False
        self.closed = False
        self.position = 0
        if backend.open_error is not None:  # like sounddevice: a failed open leaves no stream behind
            backend.events.append("open-failed")
            raise backend.open_error
        backend.events.append("open")
        backend.streams.append(self)

    def start(self):
        self.backend.events.append("start")
        if self.backend.start_error is not None:
            raise self.backend.start_error
        self.backend.script(self)

    def deliver(self, frames, *, overflow=False, data=None):
        """One callback, handled the way sounddevice's _wrap_callback + CFFI handle it."""
        if self.done:
            return "done"
        if data is None:
            data = ramp(self.position, frames)
        self.position += frames
        try:
            self.callback(data, frames, None, Flags(overflow))
        except FakeCallbackStop:
            self.finish()
            return "complete"
        except FakeCallbackAbort:
            self.finish()
            return "abort"
        except Exception:  # CFFI: print the traceback, return paAbort - the caller never sees it
            self.finish()
            return "swallowed"
        return "continue"

    def finish(self):
        self.done = True
        self.finished_callback()

    def abort(self):
        self.backend.events.append("abort")

    def close(self):
        self.backend.events.append("close")
        self.closed = True


class FakeDefaults:
    def __init__(self, input_index, host_api):
        self.device = [input_index, 99]
        self.hostapi = host_api


def steady(sizes):
    """A script: deliver blocks of these sizes, repeating, until the capture stops the stream."""
    def script(stream):
        for size in itertools.cycle(sizes):
            if stream.deliver(size) != "continue":
                return
    return script


class FakeAudio:
    PortAudioError = FakePortAudioError
    CallbackStop = FakeCallbackStop
    CallbackAbort = FakeCallbackAbort

    def __init__(self, devices=None, *, default_input=0, default_host_api=0):
        self.devices = devices if devices is not None else [dev(REALTEK[:31], host_api=0)]
        self.default = FakeDefaults(default_input, default_host_api)
        self.events = []
        self.streams = []
        self.checked = []
        self.check_error = None
        self.open_error = None
        self.start_error = None
        self.script = steady([441, 1024, 7, 3000, 160])  # irregular on purpose

    def query_devices(self):
        return self.devices

    def query_hostapis(self, index=None):
        apis = [{"name": name} for name in HOST_APIS]
        return apis if index is None else apis[index]

    def check_input_settings(self, **kwargs):
        self.checked.append(kwargs)
        if self.check_error is not None:
            raise self.check_error

    def RawInputStream(self, **kwargs):
        return FakeStream(self, **kwargs)

    @property
    def stream(self):
        assert len(self.streams) == 1, f"expected exactly one stream, got {len(self.streams)}"
        return self.streams[0]


def dev(name, *, host_api=0, inputs=1):
    return {"name": name, "hostapi": host_api, "max_input_channels": inputs,
            "max_output_channels": 0, "default_samplerate": 44100.0}


def this_machine():
    """The shape Task 2a found on this laptop: one Realtek mic through four paths (and a headset)."""
    return FakeAudio([dev(REALTEK[:31], host_api=0), dev("Headset (TWS Hands-Free AG Audi", host_api=0),
                      dev(REALTEK, host_api=1), dev(REALTEK, host_api=2),
                      dev("Microphone (Realtek HD Audio Mic input)", host_api=3)],
                     default_input=0, default_host_api=0)


def listener(*, input_device="", cap=0.1):
    return ListenerSettings(enabled=True, model_size="small", model_dir="data/models",
                            local_files_only=True, device="auto", compute_type="auto", language="auto",
                            sample_rate=16000, input_device=input_device, vad_filter=True,
                            min_silence_ms=800, max_utterance_seconds=cap,
                            initial_prompt_terms=("notepad",), voice_stop_enabled=True)


@pytest.fixture
def audio(monkeypatch):
    backend = FakeAudio()
    monkeypatch.setattr(adapter, "_audio", lambda: backend)
    monkeypatch.setattr(adapter, "POLL_SECONDS", 0.001)
    real_release = microphone.release

    def recorded_release():
        backend.events.append("release")
        real_release()
    monkeypatch.setattr(microphone, "release", recorded_release)
    return backend


def install(monkeypatch, audio, backend):
    """Swap the fixture's backend for another one, keeping the fixture's release recording."""
    monkeypatch.setattr(adapter, "_audio", lambda: backend)
    backend.events = audio.events
    return backend


def within(seconds, function, *args, **kwargs):
    """Run a capture that depends on the caller's own wait loop, and FAIL - never hang the suite - if
    that loop doesn't end it. A daemon thread, so a genuinely stuck one can't keep pytest alive."""
    outcome = {}

    def run():
        try:
            outcome["value"] = function(*args, **kwargs)
        except BaseException as exc:  # handed back to the test thread below
            outcome["error"] = exc
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), f"capture did not end within {seconds} s - the wait loop is unbounded"
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def assert_cleaned_up(backend):
    """The lifecycle every exit path must end with: stream shut, THEN the microphone given back."""
    events = backend.events
    if backend.streams:
        assert events[-3:] == ["abort", "close", "release"], events
        assert all(stream.closed for stream in backend.streams)
    assert events[-1] == "release", events
    assert microphone.owner() is None
    assert microphone.acquire("check") and microphone.owner() == "check"
    microphone.reset()


# --- A normal capture -----------------------------------------------------------------------------

def test_irregular_callback_sizes_are_assembled_into_exactly_the_right_samples(audio):
    recording = adapter.capture(listener(cap=0.1))
    assert isinstance(recording, Recording), recording
    assert recording.frames == 1600 and recording.pcm == ramp(0, 1600), \
        "every frame, in order, from blocks of 441, 1024, 7 and a trimmed 3000"
    assert recording.stopped_by == LIMIT
    assert_cleaned_up(audio)


def test_the_last_block_is_trimmed_so_the_maximum_is_never_exceeded(audio):
    audio.script = steady([1000])  # 1000 + 1000 would overshoot 1600
    recording = adapter.capture(listener(cap=0.1))
    assert recording.frames == 1600 and len(recording.pcm) == 3200


def test_duration_is_derived_from_the_samples(audio):
    recording = adapter.capture(listener(cap=1.0), max_seconds=0.25)
    assert recording.frames == 4000 and recording.seconds == 4000 / SAMPLE_RATE == 0.25


def test_the_stream_is_opened_in_callback_mode_with_portaudio_choosing_the_block_size(audio):
    adapter.capture(listener())
    assert audio.stream.settings == dict(device=None, samplerate=16000, channels=1, dtype="int16",
                                         blocksize=0)
    assert not hasattr(audio.stream, "read"), "the caller must never sit inside a blocking read()"


def test_the_empty_selector_uses_the_real_default_path_not_a_copy_of_it(audio):
    recording = adapter.capture(listener(input_device=""))
    assert audio.checked == [dict(device=None, samplerate=16000, channels=1, dtype="int16")]
    assert recording.used_default is True
    assert recording.device_index is None, \
        "device=None lets PortAudio pick at open time; the index enumerated earlier may be stale"


def test_the_default_path_claims_no_index_even_when_the_default_moves_under_it(monkeypatch, audio):
    """Windows switches its default between our enumeration and PortAudio's open (e.g. a Bluetooth
    headset connects). Whatever opened, the Recording must not name the index we saw earlier."""
    backend = install(monkeypatch, audio, this_machine())
    real_check = backend.check_input_settings

    def default_moves(**kwargs):
        backend.default = FakeDefaults(1, 0)  # the headset becomes the default mid-capture setup
        return real_check(**kwargs)
    backend.check_input_settings = default_moves
    recording = adapter.capture(listener(input_device=""))
    assert recording.used_default is True and recording.device_index is None
    assert backend.stream.settings["device"] is None


def test_an_explicit_selection_checks_and_opens_exactly_that_device(monkeypatch, audio):
    backend = install(monkeypatch, audio, this_machine())
    recording = adapter.capture(listener(input_device=2))
    assert backend.checked[0]["device"] == 2 and backend.stream.settings["device"] == 2
    assert recording.used_default is False and recording.device_index == 2


def test_a_second_capture_can_start_as_soon_as_the_first_has_finished(audio):
    first = adapter.capture(listener())
    second = adapter.capture(listener())
    assert isinstance(first, Recording) and isinstance(second, Recording)
    assert len(audio.streams) == 2 and all(stream.closed for stream in audio.streams)


# --- The maximum ----------------------------------------------------------------------------------

def test_the_caller_can_lower_the_configured_cap(audio):
    assert adapter.capture(listener(cap=1.0), max_seconds=0.05).frames == 800


def test_the_caller_can_never_raise_the_configured_cap(audio):
    recording = adapter.capture(listener(cap=0.1), max_seconds=60)
    assert recording.frames == 1600, "60 s was asked for; the configured 0.1 s still wins"


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), True, "5"])
def test_a_meaningless_maximum_is_a_programming_error_not_a_silent_default(audio, bad):
    with pytest.raises(ValueError):
        adapter.capture(listener(), max_seconds=bad)
    assert audio.events == [], "nothing may be opened for a request that makes no sense"


# --- Cancellation ---------------------------------------------------------------------------------

def test_cancelled_before_it_began_never_touches_the_backend_or_the_microphone(monkeypatch):
    def forbidden():
        raise AssertionError("the backend must not be touched")
    monkeypatch.setattr(adapter, "_audio", forbidden)
    cancel = threading.Event()
    cancel.set()
    recording = adapter.capture(listener(), cancel=cancel)
    assert recording.stopped_by == CANCELLED and recording.frames == 0
    assert microphone.owner() is None


def test_cancelled_before_the_first_callback_arrives(audio):
    cancel = threading.Event()

    def cancel_with_no_sound_yet(stream):
        cancel.set()  # the device has opened but not delivered anything
    audio.script = cancel_with_no_sound_yet
    recording = within(5, adapter.capture, listener(), cancel=cancel)
    assert recording.stopped_by == CANCELLED and recording.frames == 0
    assert_cleaned_up(audio)


def test_cancelled_mid_capture_keeps_exactly_what_arrived_before_it(audio):
    cancel = threading.Event()

    def two_blocks_then_cancel(stream):
        stream.deliver(500)
        stream.deliver(300)
        cancel.set()
        assert stream.deliver(400) == "complete", "the callback must stop on seeing the cancel"
    audio.script = two_blocks_then_cancel
    recording = adapter.capture(listener(), cancel=cancel)
    assert recording.stopped_by == CANCELLED
    assert recording.frames == 800 and recording.pcm == ramp(0, 800)
    assert_cleaned_up(audio)


def test_the_waiting_caller_notices_a_cancel_the_callback_never_sees(audio):
    """The stream is open but silent (no callbacks), so only the caller's own wait loop can react."""
    cancel = threading.Event()

    def open_but_silent(stream):
        threading.Timer(0.01, cancel.set).start()
    audio.script = open_but_silent
    recording = within(5, adapter.capture, listener(cap=5.0), cancel=cancel)
    assert recording.stopped_by == CANCELLED and recording.frames == 0
    assert_cleaned_up(audio)


# --- Microphone ownership -------------------------------------------------------------------------

def test_a_second_capture_while_one_runs_gets_device_busy_and_the_first_is_undisturbed(audio):
    second = {}

    def meanwhile_another_thread_tries(stream):
        stream.deliver(500)
        worker = threading.Thread(target=lambda: second.update(result=adapter.capture(listener())))
        worker.start()
        worker.join(5)
        assert not worker.is_alive()
        steady([500])(stream)
    audio.script = meanwhile_another_thread_tries
    first = adapter.capture(listener())
    assert isinstance(second["result"], VoiceFailure) and second["result"].kind == DEVICE_BUSY
    assert "capture" in second["result"].message, "the busy message says what holds it"
    assert isinstance(first, Recording) and first.frames == 1600 and first.pcm == ramp(0, 1600)
    assert len(audio.streams) == 1, "the refused capture must never have opened a stream"


def test_a_capture_while_something_else_owns_the_microphone_is_refused(audio):
    owned, done = threading.Event(), threading.Event()

    def stop_guard():
        assert microphone.acquire("stop-guard")
        owned.set()
        done.wait(5)
        microphone.release()
    holder = threading.Thread(target=stop_guard)
    holder.start()
    owned.wait(5)
    try:
        refused = adapter.capture(listener())
        assert isinstance(refused, VoiceFailure) and refused.kind == DEVICE_BUSY
        assert "stop-guard" in refused.message and microphone.owner() == "stop-guard"
        assert audio.streams == []
    finally:
        done.set()
        holder.join(5)
    assert microphone.owner() is None


def test_nesting_on_the_same_thread_refuses_instead_of_deadlocking():
    outcome = {}

    def nest():
        assert microphone.acquire("capture")
        try:
            microphone.acquire("capture")
        except RuntimeError as exc:
            outcome["error"] = str(exc)
        finally:
            microphone.release()
    worker = threading.Thread(target=nest, daemon=True)
    worker.start()
    worker.join(2)
    assert not worker.is_alive(), "nested acquisition deadlocked"
    assert "would deadlock" in outcome["error"]
    assert microphone.owner() is None


def test_only_the_owning_thread_may_release():
    assert microphone.acquire("capture")
    failure = {}

    def intruder():
        try:
            microphone.release()
        except RuntimeError as exc:
            failure["error"] = exc
    thread = threading.Thread(target=intruder)
    thread.start()
    thread.join(2)
    assert "error" in failure and microphone.owner() == "capture"
    microphone.release()


def test_a_bounded_wait_gives_up_rather_than_queueing():
    owned, done = threading.Event(), threading.Event()

    def holder():
        microphone.acquire("capture")
        owned.set()
        done.wait(5)
        microphone.release()
    thread = threading.Thread(target=holder)
    thread.start()
    owned.wait(5)
    try:
        assert microphone.acquire("other", wait=0.02) is False
    finally:
        done.set()
        thread.join(5)


@pytest.mark.parametrize("purpose, wait", [("", 0), ("  ", 0), ("capture", -1), ("capture", True)])
def test_ownership_rejects_a_nonsensical_request(purpose, wait):
    with pytest.raises(ValueError):
        microphone.acquire(purpose, wait=wait)
    assert microphone.owner() is None


def test_ownership_is_released_after_success(audio):
    adapter.capture(listener())
    assert_cleaned_up(audio)


def test_ownership_is_released_after_a_recognized_failure(audio):
    audio.open_error = FakePortAudioError("Invalid device", -9996)
    assert adapter.capture(listener()).kind == DEVICE_LOST
    assert audio.events[-1] == "release" and microphone.owner() is None


def test_ownership_is_released_after_an_unexpected_exception(audio):
    audio.start_error = KeyError("a bug somewhere")
    with pytest.raises(KeyError):
        adapter.capture(listener())
    assert_cleaned_up(audio)


def test_the_stream_is_closed_before_the_microphone_is_released(audio):
    adapter.capture(listener())
    assert audio.events.index("close") < audio.events.index("release")
    assert audio.events.index("abort") < audio.events.index("close")


def test_a_selection_failure_never_takes_the_microphone(monkeypatch, audio):
    install(monkeypatch, audio, FakeAudio([dev("Speakers", inputs=0)]))
    assert adapter.capture(listener()).kind == NO_DEVICE
    assert "release" not in audio.events and microphone.owner() is None


# --- The callback -----------------------------------------------------------------------------------

def test_overflow_reports_from_the_backend_are_counted(audio):
    def overflowing(stream):
        for overflow in (False, True, False, True, True):
            stream.deliver(100, overflow=overflow)
        steady([1000])(stream)
    audio.script = overflowing
    assert adapter.capture(listener()).overflows == 3


def test_a_bug_inside_our_callback_is_raised_on_the_caller_thread_after_cleanup(audio):
    class Poisoned:
        def __getitem__(self, _):
            raise ZeroDivisionError("a defect in the capture code")

    outcomes = []

    def poisoned_second_block(stream):
        outcomes.append(stream.deliver(200))
        outcomes.append(stream.deliver(200, data=Poisoned()))
    audio.script = poisoned_second_block
    with pytest.raises(ZeroDivisionError, match="a defect in the capture code") as raised:
        adapter.capture(listener())
    assert outcomes == ["continue", "abort"], "our callback must turn the bug into a clean abort"
    assert "_take" in "".join(str(entry.name) for entry in raised.traceback), \
        "the original exception, with its original traceback from the callback"
    assert_cleaned_up(audio)


def test_a_bug_is_never_relabelled_as_a_lost_device(audio):
    def broken(stream):
        stream.deliver(100)
        stream.deliver(100, data=object())  # not a buffer at all: slicing it is a TypeError
    audio.script = broken
    with pytest.raises(TypeError):
        adapter.capture(listener())
    assert_cleaned_up(audio)


def test_a_callback_arriving_after_close_adds_nothing(audio):
    recording = adapter.capture(listener())
    stream = audio.stream
    with pytest.raises(FakeCallbackAbort):
        stream.callback(ramp(0, 100), 100, None, Flags())
    assert recording.frames == 1600


def test_the_callback_path_does_no_forbidden_work():
    """Inside the PortAudio callback: no logging, enumeration, model, files, Executor or ownership."""
    source = (config_settings.PROJECT_ROOT / "app" / "listener" / "adapter.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    capture_class = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                         and node.name == "_Capture")
    forbidden = {"log", "logging", "print", "open", "microphone", "list_input_devices",
                 "query_devices", "query_hostapis", "check_input_settings", "sleep", "time", "logic"}
    found = sorted({name for method in capture_class.body if isinstance(method, ast.FunctionDef)
                    and method.name in ("callback", "_take", "end")
                    for node in ast.walk(method)
                    for name in [getattr(node, "id", None) or getattr(node, "attr", None)]
                    if name in forbidden})
    assert found == [], f"the PortAudio callback path must stay small: {found}"


# --- Failure mapping --------------------------------------------------------------------------------

def test_no_microphone_is_no_device(monkeypatch, audio):
    install(monkeypatch, audio, FakeAudio([dev("Speakers", inputs=0)]))
    failure = adapter.capture(listener())
    assert failure.kind == NO_DEVICE and audio.streams == []


def test_a_missing_backend_is_capture_unavailable(monkeypatch):
    monkeypatch.setattr(adapter, "_audio", lambda: (_ for _ in ()).throw(ImportError("gone")))
    assert adapter.capture(listener()).kind == CAPTURE_UNAVAILABLE
    assert microphone.owner() is None


@pytest.mark.parametrize("error, kind", [
    (FakePortAudioError("Unanticipated host error", -9999, (0, 4, "")), DEVICE_BUSY),       # MME allocated
    (FakePortAudioError("Unanticipated host error", -9999, (2, 0x8889000A - 2 ** 32, "")), DEVICE_BUSY),
    (FakePortAudioError("Unanticipated host error", -9999, (2, E_ACCESSDENIED_SIGNED, "")),
     PERMISSION_DENIED),
    (FakePortAudioError("Unanticipated host error", -9999, (2, 0x88890004 - 2 ** 32, "")), DEVICE_LOST),
    (FakePortAudioError("Unanticipated host error", -9999, (0, 6, "")), DEVICE_LOST),       # MME no driver
    (FakePortAudioError("Invalid device", -9996), DEVICE_LOST),
    (FakePortAudioError("Invalid sample rate", -9997), FORMAT_UNSUPPORTED),
])
@pytest.mark.parametrize("stage", ["open", "start"])
def test_recognized_backend_failures_map_truthfully(audio, error, kind, stage):
    setattr(audio, f"{stage}_error", error)
    failure = adapter.capture(listener())
    assert isinstance(failure, VoiceFailure) and failure.kind == kind
    assert_cleaned_up(audio)


@pytest.mark.parametrize("error", [
    FakePortAudioError("Internal PortAudio error", -9986),
    FakePortAudioError("Unanticipated host error", -9999, (0, 12345, "")),  # a host code nobody mapped
    FakePortAudioError("Unanticipated host error", -9999, (1, 4, "")),      # MME's code, wrong host API
    FakePortAudioError("no code at all"),
])
def test_an_unrecognized_backend_failure_propagates_instead_of_being_guessed(audio, error):
    audio.start_error = error
    with pytest.raises(FakePortAudioError) as raised:
        adapter.capture(listener())
    assert raised.value is error
    assert_cleaned_up(audio)


def test_a_stream_that_ends_by_itself_before_its_limit_is_device_lost(audio):
    def dies_part_way(stream):
        stream.deliver(300)
        stream.finish()  # PortAudio stopped the stream; we never asked it to
    audio.script = dies_part_way
    failure = adapter.capture(listener())
    assert failure.kind == DEVICE_LOST
    assert_cleaned_up(audio)


def test_the_watchdog_abandons_a_stream_that_goes_quiet(monkeypatch, audio):
    ticks = itertools.count(0.0, 10.0)  # every look at the clock is 10 s later: no real waiting
    monkeypatch.setattr(adapter, "_clock", lambda: next(ticks))
    monkeypatch.setattr(adapter, "WATCHDOG_MARGIN_SECONDS", 0.0)

    def delivers_a_little_then_nothing(stream):
        stream.deliver(300)
    audio.script = delivers_a_little_then_nothing
    failure = within(5, adapter.capture, listener(cap=5.0))
    assert failure.kind == DEVICE_LOST and failure.message == logic.STALLED_MESSAGE
    assert_cleaned_up(audio)


def test_the_watchdog_bound_is_the_maximum_plus_the_documented_margin(monkeypatch, audio):
    seen = []
    clock = iter([100.0, 100.0 + 5.0 + adapter.WATCHDOG_MARGIN_SECONDS - 0.001,
                  100.0 + 5.0 + adapter.WATCHDOG_MARGIN_SECONDS])

    def recorded():
        seen.append(next(clock))
        return seen[-1]
    monkeypatch.setattr(adapter, "_clock", recorded)
    audio.script = lambda stream: None
    assert within(5, adapter.capture, listener(cap=5.0)).kind == DEVICE_LOST
    assert len(seen) == 3, "it must keep waiting until exactly max + margin has passed, then stop"


def test_the_raw_backend_text_never_reaches_the_message_or_the_log(audio, caplog):
    host_text = "Microphone (Wajid's private headset) could not be opened"
    audio.open_error = FakePortAudioError("Unanticipated host error", -9999, (0, 4, host_text))
    with caplog.at_level(logging.DEBUG):
        failure = adapter.capture(listener())
    assert host_text not in failure.message and "Wajid" not in caplog.text
    assert [record.getMessage() for record in caplog.records] == ["Microphone capture failed: device_busy"]


# --- format_unsupported: actionable, and never auto-switched ------------------------------------------

def test_an_explicit_wasapi_selection_that_refuses_16k_says_exactly_what_and_what_else(monkeypatch, audio):
    """The real case from Task 2a: the Realtek mic on WASAPI (index 3 here) refuses 16 kHz."""
    backend = install(monkeypatch, audio, this_machine())
    backend.check_error = FakePortAudioError("Invalid sample rate", -9997)
    failure = adapter.capture(listener(input_device=3))
    assert failure.kind == FORMAT_UNSUPPORTED
    message = failure.message
    assert "[3]" in message and REALTEK in message and WASAPI in message, "which device, on which path"
    assert "16 kHz mono" in message, "what was refused"
    for other in (MME, DIRECTSOUND, WDMKS):
        assert other in message, "the other paths to the hardware that may accept it"
    assert "did not switch" in message, "and that nothing was switched behind the user's back"
    assert backend.streams == [] and [c["device"] for c in backend.checked] == [3], \
        "no other device, path or rate may be tried"


def test_a_refusing_default_path_is_reported_without_trying_anything_else(monkeypatch, audio):
    backend = install(monkeypatch, audio, this_machine())
    backend.check_error = FakePortAudioError("Invalid sample rate", -9997)
    failure = adapter.capture(listener(input_device=""))
    assert failure.kind == FORMAT_UNSUPPORTED and "default microphone" in failure.message
    assert "Nothing else was tried" in failure.message and backend.streams == []
    assert len(backend.checked) == 1


def test_a_format_refused_only_when_the_stream_opens_is_still_format_unsupported(monkeypatch, audio):
    backend = install(monkeypatch, audio, this_machine())
    backend.open_error = FakePortAudioError("Sample format not supported", -9994)
    failure = adapter.capture(listener(input_device=3))
    assert failure.kind == FORMAT_UNSUPPORTED and WASAPI in failure.message
    assert backend.events.count("open-failed") == 1 and backend.streams == [], \
        "one attempt, on the chosen device only"


def test_a_format_refusal_never_takes_the_microphone_for_longer_than_the_check(monkeypatch, audio):
    backend = install(monkeypatch, audio, this_machine())
    backend.check_error = FakePortAudioError("Invalid sample rate", -9997)
    adapter.capture(listener(input_device=3))
    assert audio.events == ["release"] and microphone.owner() is None


# --- Privacy ------------------------------------------------------------------------------------------

SECRET_SAMPLES = b"AUDIO-THAT-MUST-NOT-LEAK!" * 4  # 100 bytes: 50 whole samples


def test_a_recording_never_shows_its_samples():
    recording = Recording(SECRET_SAMPLES, LIMIT, device_index=3, used_default=False)
    for shown in (repr(recording), str(recording), f"{recording}", "%s" % (recording,)):
        assert "AUDIO" not in shown and "\\x" not in shown and "pcm" not in shown
    assert "frames=50" in repr(recording) and "device_index=3" in repr(recording)


def test_a_recording_names_no_device():
    assert "Realtek" not in repr(Recording(b"", CANCELLED, device_index=1))
    assert not any("name" in field for field in Recording.__dataclass_fields__)


def test_a_failure_repr_hides_the_user_facing_message():
    names = "[1] Microphone (Wajid's Headset) via MME; [8] Microphone (Wajid's Headset) via DS"
    failure = VoiceFailure(NO_DEVICE, f"'headset' matches 2 microphones ({names}).")
    for shown in (repr(failure), str(failure), f"{failure}", "%s" % (failure,)):
        assert "Wajid" not in shown and "Headset" not in shown
        assert "no_device" in shown and f"<{len(failure.message)} characters>" in shown
    assert "Wajid" in failure.message, "the message itself still carries what the user needs"


def test_a_capture_log_carries_metadata_only(monkeypatch, audio, caplog):
    backend = install(monkeypatch, audio, this_machine())
    backend.script = lambda stream: stream.deliver(2000, data=SECRET_SAMPLES * 40)
    with caplog.at_level(logging.DEBUG):
        adapter.capture(listener(input_device="headset"))
    assert "AUDIO" not in caplog.text and "Headset" not in caplog.text and "Realtek" not in caplog.text
    assert [record.getMessage() for record in caplog.records] == [
        "Microphone capture ended (limit): 1600 frames, 0.10 s, 0 overflow(s), device index 1, "
        "selected device"]


def test_no_audio_outlives_the_recording_in_module_state(audio):
    adapter.capture(listener())
    for module in (adapter, microphone, logic):
        leftovers = [name for name, value in vars(module).items()
                     if isinstance(value, (bytes, bytearray, memoryview)) and value]
        assert leftovers == [], f"{module.__name__} kept audio in {leftovers}"


# --- The Recording shape ------------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs, error", [
    (dict(pcm=bytearray(4), stopped_by=LIMIT), TypeError),
    (dict(pcm=b"abc", stopped_by=LIMIT), ValueError),                      # half a sample
    (dict(pcm=b"", stopped_by="silence"), ValueError),                     # not Feature 2's to decide
    (dict(pcm=b"", stopped_by="no_speech"), ValueError),
    (dict(pcm=b"", stopped_by=LIMIT, sample_rate=48000), ValueError),
    (dict(pcm=b"", stopped_by=LIMIT, channels=2), ValueError),
    (dict(pcm=b"", stopped_by=LIMIT, dtype="float32"), ValueError),
    (dict(pcm=b"", stopped_by=LIMIT, overflows=-1), ValueError),
    (dict(pcm=b"", stopped_by=LIMIT, overflows=True), ValueError),
])
def test_a_recording_can_only_hold_canonical_audio_and_known_endings(kwargs, error):
    with pytest.raises(error):
        Recording(**kwargs)


def test_a_recording_is_immutable():
    recording = Recording(b"\x00\x00", LIMIT)
    with pytest.raises(Exception):
        recording.pcm = b""


# --- The pure rules behind it -------------------------------------------------------------------------

@pytest.mark.parametrize("requested, expected", [(None, 15.0), (3, 3.0), (15, 15.0), (99, 15.0)])
def test_capture_limit_lowers_but_never_raises(requested, expected):
    assert logic.capture_limit(15, requested) == expected


def test_max_frames_rounds_down_and_refuses_less_than_one_sample():
    assert logic.max_frames(0.1) == 1600 and logic.max_frames(1 / 16000 * 2.9) == 2
    with pytest.raises(ValueError):
        logic.max_frames(1e-9)


def test_backend_failure_has_no_opinion_without_evidence():
    assert logic.backend_failure(-9986) is None
    assert logic.backend_failure(None) is None
    assert logic.backend_failure(-9999, "MME", None) is None
    assert logic.backend_failure(-9999, "Windows DirectSound", 4) is None, \
        "a host code only means something on the host API it came from"


def test_every_mapped_failure_message_is_fixed_wording():
    for kind, message in logic.FAILURE_MESSAGES.items():
        assert VoiceFailure(kind, message).kind == kind and "{" not in message
    assert math.isfinite(adapter.WATCHDOG_MARGIN_SECONDS) and adapter.WATCHDOG_MARGIN_SECONDS > 0


# --- The recording opt-in is its own switch ----------------------------------------------------------

class _Item:
    """Just enough of a pytest item for conftest's gate."""

    def __init__(self, marker):
        self.marker = marker
        self.added = []

    def get_closest_marker(self, name):
        return object() if name == self.marker else None

    def add_marker(self, marker):
        self.added.append(marker)


@pytest.mark.parametrize("enabled, recording_skipped", [
    ({}, True),
    ({"RUN_REAL_MICROPHONE_TEST": "1"}, True),     # the read-only probes: must NEVER record
    ({"RUN_REAL_DESKTOP_TEST": "1", "RUN_REAL_MICROPHONE_TEST": "1"}, True),
    ({"RUN_REAL_RECORDING_TEST": "1"}, False),     # the one switch that records
])
def test_only_the_recording_switch_lets_a_recording_test_run(monkeypatch, enabled, recording_skipped):
    from tests import conftest
    for variable, _ in conftest.OPT_IN_GATES.values():
        monkeypatch.delenv(variable, raising=False)
    for variable, value in enabled.items():
        monkeypatch.setenv(variable, value)
    recording, probe = _Item("real_recording"), _Item("real_microphone")
    conftest.pytest_collection_modifyitems(None, [recording, probe])
    assert bool(recording.added) is recording_skipped
    assert conftest.OPT_IN_GATES["real_recording"][0] == "RUN_REAL_RECORDING_TEST"
    assert conftest.OPT_IN_GATES["real_recording"][0] != conftest.OPT_IN_GATES["real_microphone"][0]


def test_every_real_recording_test_is_behind_the_recording_switch():
    from tests import test_listener_capture_real as real
    assert real.pytestmark.name == "real_recording"
    tests = [name for name in vars(real) if name.startswith("test_")]
    assert len(tests) == 4, tests
    source = (config_settings.PROJECT_ROOT / "tests" / "test_listener_capture_real.py").read_text(
        encoding="utf-8")
    assert "real_microphone" not in source.split('"""', 2)[2], \
        "a recording test must never carry the read-only probe marker"
