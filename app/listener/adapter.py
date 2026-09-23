"""
The ONLY file in this project allowed to import faster_whisper or sounddevice.
Captures microphone audio and turns it into a Transcript (docs/step4 Section 5, Phase 2).

Feature 2 is capture only: list_input_devices() says what microphones exist, and capture() records
ONE bounded stretch of canonical 16 kHz mono int16 audio into memory and returns it as a Recording.
No model, no transcription, no silence detection - those judge the sound, and come later.

How capture stays bounded
    The stream runs in PortAudio's callback mode. The caller's thread never sits inside a blocking
    PortAudio read(): it waits on our own completion event, with a timeout, and checks the caller's
    cancel event and a wall-clock watchdog while it waits. When anything ends the capture - the
    maximum reached, the caller cancelling, a callback failure, the backend going quiet - the caller's
    thread aborts and closes the stream itself, in `finally`, and only then gives back the microphone.

    The honest contract: capture is bounded under a responsive sounddevice/PortAudio API. The design
    never blocks on a read of its own, but it cannot promise anything if the native abort/close calls
    themselves never return - no in-process design can, and nothing so far suggests they hang.

What the PortAudio callback may do
    Look at the status flags, copy the bytes it needs, update a few counters, look at the cancel event,
    and ask PortAudio to stop. Nothing else: no logging, no enumeration, no model, no files, no
    Executor or Safety. An unexpected exception in it is NOT left to sounddevice (whose C callback
    wrapper would print it and abort, and the caller would never know) - it is kept, the stream is
    aborted and closed, the microphone released, and then it is raised again on the caller's thread.

The rules that hold for every function here
    - Only a RECOGNIZED backend failure becomes a VoiceFailure (app.listener.logic.backend_failure
      decides which). Anything else is a defect and propagates after cleanup - it is never
      relabelled as broken hardware.
    - Audio exists only in memory, only in the returned Recording. No file, no playback, no history.
    - Device names may be RETURNED to be shown to the user, but no log line carries one - or any audio.
    - This low-level primitive does not know about the Executor or its emergency stop. The voice
      orchestration above it turns application stop state into the caller's `cancel` event.

The speech model (Feature 3)
    ensure_model() makes the configured faster-whisper model ready - lazily, once per process - and
    reports a ModelStatus; the model object itself never leaves this file. It NEVER downloads: it asks
    Hugging Face's cache for the model with local_files_only=True, checks that the folder it gets back
    holds every file the model needs (tokenizer.json above all - without it faster-whisper would fetch
    one from the internet), and only then gives WhisperModel that verified FOLDER, never a model name.
    fetch_model() is the one function in the project allowed to download, and only
    scripts/fetch_voice_model.py may call it (a rule test enforces that).

Pure decisions (which device a name means, the length limit, what a failure means, where the model
runs) live in app/listener/logic.py, which must stay free of I/O and must not import this file.
"""
import dataclasses
import logging
import threading
import time
from pathlib import Path

from app.listener import logic, microphone
from app.listener.models import (BYTES_PER_FRAME, CANCELLED, CAPTURE_UNAVAILABLE, CHANNELS, DEVICE_BUSY,
                                 DEVICE_LOST, DTYPE, FORMAT_UNSUPPORTED, LIMIT, SAMPLE_RATE,
                                 InputDevice, ListenerSettings, ModelStatus, Recording, VoiceFailure)

log = logging.getLogger(__name__)

OWNER = "capture"             # what microphone.owner() reports while a capture runs
POLL_SECONDS = 0.05           # how often the waiting caller looks at its cancel event and the watchdog
# How much longer than its own maximum a capture may take before the watchdog gives up on it: time for
# the device to open and deliver its first buffer, and for PortAudio to finish. Normal startup latency
# is a fraction of a second; two seconds only ever trips when sound has genuinely stopped arriving.
WATCHDOG_MARGIN_SECONDS = 2.0
_clock = time.monotonic       # replaced by the tests, so the watchdog is tested without waiting


def _audio():
    """The capture backend, imported on first use.

    One accessor, in the same style as app/executor/adapter.py's lazy pyautogui import: importing
    this module never needs sounddevice or its bundled PortAudio DLL, a machine without them fails
    with a clear VoiceFailure instead of an import error, and the offline tests replace this one
    function so they never import the backend at all."""
    import sounddevice
    return sounddevice


def _unavailable(exc: Exception) -> VoiceFailure:
    """The one truthful answer when the backend itself can't be used: not 'no microphone'."""
    return VoiceFailure(CAPTURE_UNAVAILABLE,
                        f"Microphone support isn't available on this computer: the sound backend "
                        f"couldn't be loaded ({type(exc).__name__}). Voice input can't be used until "
                        f"that is fixed; everything else still works.")


def list_input_devices() -> tuple[InputDevice, ...] | VoiceFailure:
    """Every microphone the backend can see right now, or a VoiceFailure if it can't look.

    An empty tuple is a real answer - the backend worked and there is no microphone. That is
    different from the VoiceFailure, which means the backend itself is unusable.

    One physical microphone normally appears several times, once per host API, with a different
    index each time; app.listener.logic.choose_device() is what turns a configured name into one of
    them. Nothing is opened here."""
    try:
        audio = _audio()
    except (ImportError, OSError) as exc:  # missing package, or a PortAudio DLL that won't load
        return _unavailable(exc)
    try:
        devices = audio.query_devices()
        host_apis = audio.query_hostapis()
        default_index = audio.default.device[0]
        default_host_api = audio.default.hostapi
    except audio.PortAudioError as exc:  # the backend is there but couldn't start its host APIs
        return _unavailable(exc)

    found = []
    for index, device in enumerate(devices):
        channels = int(device["max_input_channels"])
        if channels < 1:
            continue  # an output-only endpoint is not a microphone
        host_api = int(device["hostapi"])
        found.append(InputDevice(
            index=index,
            name=str(device["name"]),
            host_api=str(host_apis[host_api]["name"]),
            max_input_channels=channels,
            default_samplerate=float(device["default_samplerate"]),
            is_default=(index == default_index),
            is_default_host_api=(host_api == default_host_api),
        ))
    return tuple(found)


def capture(settings: ListenerSettings, *, max_seconds: float | None = None,
            cancel: threading.Event | None = None) -> Recording | VoiceFailure:
    """Record at most `max_seconds` (never more than listener.max_utterance_seconds) from the configured
    microphone, into memory. Ends early - with what was captured so far - when `cancel` is set.

    Returns a Recording, or a VoiceFailure for a recognized problem. Anything unrecognized, including a
    bug inside our own PortAudio callback, is raised - after the stream is closed and the microphone
    given back."""
    limit = logic.capture_limit(settings.max_utterance_seconds, max_seconds)
    frames_allowed = logic.max_frames(limit)
    cancel = cancel if cancel is not None else threading.Event()
    used_default = logic.uses_default(settings.input_device)
    if cancel.is_set():  # cancelled before it began: never touch the backend or the microphone
        return _ended(Recording(b"", CANCELLED, device_index=None, used_default=used_default))

    try:
        audio = _audio()
    except (ImportError, OSError) as exc:
        return _failed(_unavailable(exc))
    devices = list_input_devices()
    if isinstance(devices, VoiceFailure):
        return _failed(devices)
    chosen = logic.choose_device(settings.input_device, devices)
    if isinstance(chosen, VoiceFailure):
        return _failed(chosen)

    if not microphone.acquire(OWNER):
        return _failed(VoiceFailure(DEVICE_BUSY, (
            f"The microphone is already in use by the assistant ({microphone.owner() or 'another task'}). "
            f"Try again when that has finished.")))
    try:
        outcome = _record(audio, chosen, used_default, devices, limit, frames_allowed, cancel)
    finally:
        microphone.release()  # _record has closed its stream by now, on every path
    return _ended(outcome) if isinstance(outcome, Recording) else _failed(outcome)


class _Capture:
    """What the PortAudio callback thread and the waiting caller share. Deliberately tiny."""

    def __init__(self, audio, frames_allowed: int, cancel: threading.Event):
        self.audio = audio
        self.frames_allowed = frames_allowed
        self.cancel = cancel
        self.blocks = []            # copies of the input, in order
        self.frames = 0
        self.overflows = 0
        self.reason = None          # LIMIT or CANCELLED, whichever came first
        self.error = None           # an unexpected exception raised inside our callback
        self.calls = 0
        self.closed = False         # set once the stream is closed: a late callback may not add audio
        self.finished = threading.Event()

    def callback(self, indata, frames, time_info, status):
        """Called by PortAudio on its own thread, with however many frames it chose to deliver."""
        self.calls += 1
        if self.closed:
            raise self.audio.CallbackAbort
        try:
            done = self._take(indata, frames, status)
        except Exception as exc:  # a bug here must reach the caller, not vanish into PortAudio
            self.error = exc
            raise self.audio.CallbackAbort from None
        if done:
            raise self.audio.CallbackStop

    def _take(self, indata, frames, status) -> bool:
        """Copy what is still allowed. True when the capture should stop."""
        if status.input_overflow:
            self.overflows += 1
        if self.cancel.is_set():
            self.end(CANCELLED)
            return True
        wanted = min(frames, self.frames_allowed - self.frames)
        if wanted > 0:  # only the frames still allowed: a Recording never exceeds its maximum
            self.blocks.append(bytes(indata[:wanted * BYTES_PER_FRAME]))
            self.frames += wanted
        if self.frames >= self.frames_allowed:
            self.end(LIMIT)
            return True
        return False

    def end(self, reason: str) -> None:
        if self.reason is None:
            self.reason = reason

    def on_finished(self):
        """PortAudio's 'this stream has stopped' notification."""
        self.finished.set()


def _record(audio, chosen: InputDevice, used_default: bool, devices, limit: float,
            frames_allowed: int, cancel: threading.Event) -> Recording | VoiceFailure:
    """Check the format, open, wait, and ALWAYS close. Called only while the microphone is owned."""
    device = None if used_default else chosen.index  # "" means the real default path, not a copy of it
    try:
        audio.check_input_settings(device=device, samplerate=SAMPLE_RATE, channels=CHANNELS, dtype=DTYPE)
    except audio.PortAudioError as exc:
        return _recognized(audio, exc, chosen, used_default, devices)

    run = _Capture(audio, frames_allowed, cancel)
    stream = None
    stalled = False
    try:
        try:
            stream = audio.RawInputStream(device=device, samplerate=SAMPLE_RATE, channels=CHANNELS,
                                          dtype=DTYPE, blocksize=0, callback=run.callback,
                                          finished_callback=run.on_finished)
            stream.start()
        except audio.PortAudioError as exc:
            return _recognized(audio, exc, chosen, used_default, devices)
        deadline = _clock() + limit + WATCHDOG_MARGIN_SECONDS
        while not run.finished.wait(POLL_SECONDS):
            if cancel.is_set():
                run.end(CANCELLED)
                break
            if _clock() >= deadline:
                stalled = True
                break
    finally:
        if stream is not None:
            try:
                stream.abort()  # immediate; harmless on a stream that already finished
            finally:
                stream.close()
        run.closed = True

    if run.error is not None:
        raise run.error  # our own callback's bug, re-raised only now that everything is shut
    if run.reason is None:  # the stream ended, or went quiet, without reaching its limit or a cancel
        return VoiceFailure(DEVICE_LOST, logic.STALLED_MESSAGE if stalled
                            else logic.FAILURE_MESSAGES[DEVICE_LOST])
    pcm = b"".join(run.blocks)
    run.blocks.clear()
    # On the default path the stream was opened with device=None, so PortAudio picked the device at
    # open time. The index enumerated a moment earlier may no longer be it; claim only what we know.
    return Recording(pcm, run.reason, overflows=run.overflows,
                     device_index=None if used_default else chosen.index, used_default=used_default)


def _recognized(audio, exc, chosen: InputDevice, used_default: bool, devices) -> VoiceFailure:
    """The VoiceFailure a PortAudio error truthfully means - or the original error, raised again."""
    code, host_api, host_code = _error_codes(audio, exc)
    failure = logic.backend_failure(code, host_api, host_code)
    if failure is None:
        raise exc
    if failure.kind == FORMAT_UNSUPPORTED:
        return logic.format_refusal(chosen, used_default, devices)
    return failure


def _error_codes(audio, exc):
    """(PortAudio code, host API name, host error code) from a PortAudioError. The error's TEXT is
    never read: it can contain device names, and nothing here needs it."""
    args = exc.args
    code = args[1] if len(args) > 1 and isinstance(args[1], int) else None
    host_api = host_code = None
    if len(args) > 2 and isinstance(args[2], tuple) and len(args[2]) == 3:
        index, host_code = args[2][0], args[2][1]
        if isinstance(index, int) and index >= 0:
            try:
                host_api = audio.query_hostapis(index)["name"]
            except audio.PortAudioError:
                host_api = None
    return code, host_api, host_code


def _ended(recording: Recording) -> Recording:
    log.info("Microphone capture ended (%s): %d frames, %.2f s, %d overflow(s), device index %s, %s",
             recording.stopped_by, recording.frames, recording.seconds, recording.overflows,
             recording.device_index, "system default" if recording.used_default else "selected device")
    return recording


def _failed(failure: VoiceFailure) -> VoiceFailure:
    log.warning("Microphone capture failed: %s", failure.kind)  # the kind only - the message may name devices
    return failure


# --- The speech model (Feature 3) -------------------------------------------------------------------

def _whisper():
    """faster-whisper, imported on first use. It is slow to import (about 9 s cold on the development
    laptop), which is exactly why nothing imports it until a model is actually wanted. Tests replace
    this function."""
    import faster_whisper
    return faster_whisper


def _ct2():
    """ctranslate2, for asking what this machine supports. Tests replace this function."""
    import ctranslate2
    return ctranslate2


class _Loaded:
    """The one model this process has loaded, with the settings it was loaded for."""

    def __init__(self, key, model, status: ModelStatus):
        self.key = key
        self.model = model
        self.status = status


_model_lock = threading.Lock()  # held for a whole load, so concurrent first callers get ONE model
_loaded: _Loaded | None = None


def ensure_model(settings: ListenerSettings) -> ModelStatus | VoiceFailure:
    """Make the configured speech model ready, offline, and say how. The first call loads it; later
    calls reuse it (reused=True). Callers that arrive while it loads wait for that one load.

    A failed load leaves nothing behind, so a later call simply tries again. Once a model is loaded,
    different model settings are refused with "restart required" - a model in use is never swapped
    out underneath its users. Unrecognized exceptions propagate."""
    global _loaded
    if settings.local_files_only is not True:  # validation already refuses this; never trust it alone
        return _model_failed("download_refused")
    key = (settings.model_size, str(logic.model_root(settings.model_dir)), settings.device,
           settings.compute_type)
    with _model_lock:
        if _loaded is not None:
            if _loaded.key != key:
                return _model_failed("restart_required")
            return dataclasses.replace(_loaded.status, reused=True, load_seconds=None)
        try:
            whisper = _whisper()
        except (ImportError, OSError):
            return _model_failed("backend_missing")
        started = _clock()
        loaded = _load(whisper, settings)
        if isinstance(loaded, VoiceFailure):
            return loaded
        model, plan, fallback_reason = loaded
        status = ModelStatus(model_size=settings.model_size, device=plan.device,
                             compute_type=plan.compute_type, reused=False,
                             load_seconds=_clock() - started,
                             fell_back_from=logic.CUDA if fallback_reason else None,
                             fallback_reason=fallback_reason)
        _loaded = _Loaded(key, model, status)
    log.info("Speech model ready: %s on %s (%s), loaded offline in %.1f s%s", status.model_size,
             status.device, status.compute_type, status.load_seconds,
             f" after CUDA failed ({status.fallback_reason})" if status.fell_back_from else "")
    return status


def forget_model() -> None:
    """Drop the loaded model. For the test suite's cleanup (tests/conftest.py) and deliberate resets -
    NOT a way to switch settings at runtime: a settings change needs a process restart."""
    global _loaded
    with _model_lock:
        _loaded = None


def _load(whisper, settings: ListenerSettings):
    """(model, plan, fallback reason or None), or a VoiceFailure. Called with _model_lock held."""
    directory = _local_snapshot(whisper, settings.model_size, settings.model_dir)
    if isinstance(directory, VoiceFailure):
        return directory
    cpu_types, cuda_count, cuda_types = _capabilities()
    plan = logic.plan_model_load(settings.device, settings.compute_type, cpu_types, cuda_count,
                                 cuda_types)
    if isinstance(plan, logic.Refusal):
        return _refused(plan, settings, cpu_types, cuda_types)
    model, category = _construct(whisper, directory, plan)
    if model is not None:
        return model, plan, None
    if not logic.may_fall_back(settings.device, plan, category):
        return _model_failed("load_failed", size=settings.model_size, device=plan.device.upper(),
                             reason=logic.CATEGORY_TEXT[category])
    log.warning("Speech model couldn't load on CUDA (%s); trying the CPU once, because "
                "listener.device is auto", category)
    cpu_plan = logic.plan_model_load(logic.CPU, settings.compute_type, cpu_types, 0, None)
    if isinstance(cpu_plan, logic.Refusal):
        return _refused(cpu_plan, settings, cpu_types, cuda_types)
    model, cpu_category = _construct(whisper, directory, cpu_plan)
    if model is None:
        return _model_failed("fallback_failed", size=settings.model_size,
                             cuda_reason=logic.CATEGORY_TEXT[category],
                             reason=logic.CATEGORY_TEXT[cpu_category])
    return model, cpu_plan, category


def _local_snapshot(whisper, model_size: str, model_dir: str) -> str | VoiceFailure:
    """The model's folder in the project cache - looked up WITHOUT any network access - and verified
    to hold every file loading needs."""
    try:
        path = whisper.utils.download_model(model_size, local_files_only=True,
                                            cache_dir=str(logic.model_root(model_dir)))
    except FileNotFoundError:  # huggingface_hub's LocalEntryNotFoundError: not in the cache
        return _model_failed("not_downloaded", size=model_size)
    return _verified_folder(path, model_size)


def _verified_folder(path, model_size: str) -> str | VoiceFailure:
    folder = Path(path)
    try:
        sizes = {entry.name: entry.stat().st_size for entry in folder.iterdir() if entry.is_file()}
    except OSError:
        return _model_failed("folder_unreadable", size=model_size)
    missing = logic.missing_model_files(sizes)
    if missing:
        return _model_failed("incomplete", size=model_size, missing=", ".join(missing))
    return str(folder)


def _capabilities():
    """(CPU compute types, CUDA device count, CUDA compute types or None) as ctranslate2 reports them
    on this machine. A failed CUDA inspection means CUDA is not usable - it never becomes a guess."""
    ct2 = _ct2()
    try:
        cpu_types = frozenset(ct2.get_supported_compute_types(logic.CPU))
    except RuntimeError:
        cpu_types = frozenset()
    try:
        cuda_count = int(ct2.get_cuda_device_count())
    except RuntimeError:
        cuda_count = 0
    cuda_types = None
    if cuda_count > 0:
        try:
            cuda_types = frozenset(ct2.get_supported_compute_types(logic.CUDA))
        except RuntimeError:
            cuda_types = None
    return cpu_types, cuda_count, cuda_types


def _construct(whisper, directory: str, plan):
    """(model, None) or (None, failure category). Only what the constructor itself raises for a model
    that can't be loaded is recognized - anything else is a defect and propagates."""
    try:
        return whisper.WhisperModel(directory, device=plan.device, compute_type=plan.compute_type,
                                    local_files_only=True), None
    except (RuntimeError, OSError, ValueError, MemoryError) as exc:
        return None, logic.construction_failure(exc, plan.device)


def _refused(refusal, settings: ListenerSettings, cpu_types, cuda_types) -> VoiceFailure:
    supported = cuda_types if refusal.device == logic.CUDA else cpu_types
    return _model_failed(refusal.code, device=refusal.device.upper(), requested=settings.compute_type,
                         supported=", ".join(sorted(supported or ())) or "nothing")


def _model_failed(code: str, **values) -> VoiceFailure:
    log.warning("Speech model unavailable (%s)", code)  # the code only: no paths, no exception text
    return logic.model_unavailable(code, **values)


def fetch_model(model_size: str, model_dir: str) -> str | VoiceFailure:
    """DOWNLOAD the speech model into the project cache - the ONLY code in the project allowed to.

    Only scripts/fetch_voice_model.py may call this: a rule test forbids every app module from reaching
    it, so normal use can never download. Returns the verified model folder, or a VoiceFailure.
    Ctrl+C (KeyboardInterrupt) is left to the script; what already arrived stays in the cache and
    Hugging Face resumes it next time."""
    if model_size not in logic.MODEL_SIZES:
        raise ValueError(f"unknown model size {model_size!r}")
    try:
        whisper = _whisper()
    except (ImportError, OSError):
        return _model_failed("backend_missing")
    try:
        path = whisper.utils.download_model(model_size, local_files_only=False,
                                            cache_dir=str(logic.model_root(model_dir)))
    except Exception as exc:
        if not _is_download_trouble(exc):
            raise  # a defect, not a download problem: never relabelled
        return _model_failed("download_failed", size=model_size, category=type(exc).__name__)
    return _verified_folder(path, model_size)


def _is_download_trouble(exc: Exception) -> bool:
    """Network, HTTP or disk trouble during a download. huggingface_hub's own errors (HfHubHTTPError,
    LocalEntryNotFoundError) are OSErrors; the network errors it lets escape once its retries run out
    come from httpx. httpx is recognized by where the error class lives rather than imported - it is
    the Claude SDK's HTTP layer, which only app/brain/adapter.py may import."""
    return isinstance(exc, OSError) or type(exc).__module__.split(".")[0] == "httpx"
