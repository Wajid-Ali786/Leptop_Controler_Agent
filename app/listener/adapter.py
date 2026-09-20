"""
The ONLY file in this project allowed to import faster_whisper or sounddevice.
Captures microphone audio and turns it into a Transcript (docs/step4 Section 5, Phase 2).

Feature 1 defined the boundary, the config and the data shapes. Feature 2 Task 2a adds device
enumeration ONLY: list_input_devices() looks at what microphones exist and nothing else. It opens
no stream, records nothing, and there is deliberately no capture() here yet - a bounded capture
needs a design whose wall-clock upper bound is real, and that design is decided after this
enumeration evidence, not before it.

When capture is built it must: return app.listener.models.Recording or VoiceFailure and nothing
else; keep raw audio in memory only, never on disk; open the microphone only for an explicit,
bounded capture; hold the process-wide microphone ownership for exactly that long; and never call
the Executor, the grammar or app.console. Cancellation arrives as a caller-supplied event - the
application's emergency-stop state is connected to that event by the voice orchestration above,
so this low-level audio adapter never depends on the Executor subsystem.

Pure decisions (which microphone a name means, language policy, normalization, error policy) belong
in app/listener/logic.py, which must stay free of I/O and must not import this file. This module
therefore enumerates and reports; it does not choose.

Two rules hold for every function here:
  - Only a RECOGNIZED backend failure becomes a VoiceFailure. An unexpected exception is a defect
    in this project and propagates, so a test exposes it; it is never relabelled as broken hardware.
  - Device names may be RETURNED (the device list exists to be shown to the user, so they can put a
    name in listener.input_device) but are never written to a log line.
"""
from app.listener.models import CAPTURE_UNAVAILABLE, InputDevice, VoiceFailure


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
