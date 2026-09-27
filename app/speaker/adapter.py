"""
The ONLY file in this project allowed to import edge_tts or pyttsx3.
Speaks text aloud: edge-tts online, pyttsx3 offline (docs/step4 Section 5, Phase 2).

It speaks text it is given and nothing else: it never reads the microphone, never calls the Executor,
the grammar or app.console, and leaves the choice of engine to app/speaker/logic.py, which stays free
of both libraries, of the network and of the operating system.

  speaker.engine: offline   this computer's own voice. Nothing leaves the machine.
  speaker.engine: online    Microsoft's Edge speech service. THE TEXT IS SENT OVER THE NETWORK.
  speaker.engine: auto      online first; if it cannot speak, this computer's own voice.

WHAT LEAVES THIS MACHINE, in online and auto-online mode: the text handed to speak(), and nothing
else. The one caller passes CommandReply.message, which is built by the Executor and is documented and
tested never to contain typed text or a transcript - so no microphone audio, no recognised speech, no
accepted command and no typed payload can reach the service through here.

Three design points, each from reading the installed packages:

  * Both libraries are imported INSIDE their own path, never at module level, so importing this module
    - or collecting the test suite - loads no comtypes, no aiohttp and no SAPI object.
  * A fresh pyttsx3 engine per call, held only in a local variable. pyttsx3.init() caches engines in a
    WeakValueDictionary, and Engine.runAndWait() sets `_inLoop = False` only AFTER the loop returns -
    so a Ctrl+C during speech leaves a cached engine permanently unusable ("run loop already
    started"). Keeping no reference means the weak cache entry disappears and the next call builds a
    clean engine, through the documented entry point and without touching a private attribute.
  * A fresh edge_tts.Communicate per utterance: its stream() raises RuntimeError on a second use.

Failure containment: speak() returns a value and never raises an ordinary Exception. That is a
deliberate exception to this project's habit of letting our own defects propagate, and it is correct
here because speaking happens AFTER the command has already finished - a defect in this file must not
turn a completed action into a reported failure. To keep such a defect findable, the catch-all logs
the exception's TYPE NAME at debug level; it never logs the message, the traceback, or the text.
KeyboardInterrupt, SystemExit and every other BaseException still propagate, after cleanup.

Privacy: no audio is kept. The online voice returns MP3, which is written to a uniquely named
temporary file outside the project, played, and deleted in a finally - so no generated speech survives
the call. The offline voice writes nothing at all. The text is never logged: only which voice spoke
and how long it took.
"""
import contextlib
import ctypes
import logging
import os
import tempfile
import time
import uuid

from app.speaker import logic
from app.speaker.models import OFF, OFFLINE, ONLINE, SpeakerSettings, SpeechFailure, Spoken

log = logging.getLogger(__name__)

# The voice used when speaker.voice is empty. Pinned here rather than left to the library so our
# behaviour does not change under us: it is the same value edge-tts 7.2.8 uses as its own
# DEFAULT_VOICE, a multilingual English (US) neural voice.
DEFAULT_ONLINE_VOICE = "en-US-EmmaMultilingualNeural"

MCI_OK = 0            # every mciSendStringW call returns 0 on success and an error code otherwise


class PlaybackError(OSError):
    """The reply was synthesised but this machine would not play it.

    An OSError on purpose: a device that will not open or play is an I/O failure, which makes it
    eligible for the auto fallback without needing a special case anywhere."""


def speak(text: str, settings: SpeakerSettings) -> Spoken | SpeechFailure:
    """Say `text` aloud. Returns Spoken when it was said (or when speaking is switched off), and a
    SpeechFailure - never an exception - when it was not.

    Order matters: the switched-off and blank-text answers come before any engine library is
    imported, so a disabled speaker costs nothing and blank text never reaches an engine."""
    order = logic.engine_order(settings)
    if not order:
        return Spoken(engine=OFF, seconds=0.0)
    if logic.is_blank(text):
        return logic.nothing_to_say()

    started = time.monotonic()
    tried = []
    for engine in order:
        unavailable = _online_failures() if engine == ONLINE else _offline_failures()
        try:
            if engine == ONLINE:
                _say_online(text, settings)
            else:
                _say_offline(text, settings)
        except unavailable as exc:
            # This voice cannot speak right now: no network, no service, no driver, no audio device.
            # The type is enough to diagnose it; the exception's own message is not ours to show.
            log.debug("The %s voice is unavailable: %s", engine, type(exc).__name__)
            tried.append(engine)
            continue          # "auto" tries the next voice; the others have none left to try
        except Exception as exc:
            # Containment, on purpose - see the module docstring. A bad call is OUR defect, not a
            # service being unavailable, so it is never dressed up as one and never falls back.
            log.debug("The speaker failed unexpectedly: %s", type(exc).__name__)
            return logic.speaker_error()
        seconds = time.monotonic() - started
        log.debug("Spoke via the %s voice in %.1fs", engine, seconds)   # never what was said
        return Spoken(engine=engine, seconds=seconds)
    return logic.unavailable(tried)


# --- The online voice ---------------------------------------------------------------------------------

def _say_online(text: str, settings: SpeakerSettings) -> None:
    """Synthesise with edge-tts and play the result, then leave nothing behind.

    edge-tts only produces MP3, and its save_sync() is a blocking wrapper around its own asyncio work,
    so there is no event loop to run here. The temporary file lives in the system temp folder - never
    in the project, data/ or logs/ - and is deleted whatever happens, including on Ctrl+C."""
    import edge_tts

    handle, path = tempfile.mkstemp(suffix=".mp3", prefix="companion-reply-")
    os.close(handle)          # the synthesiser and MCI both want to open it themselves
    try:
        speech = edge_tts.Communicate(text, settings.voice or DEFAULT_ONLINE_VOICE,
                                      rate=logic.online_rate(settings.rate))
        speech.save_sync(path)          # a fresh Communicate per utterance: stream() is single-use
        _play(path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)             # no generated speech survives this call


def _play(path: str) -> None:
    """Play one audio file through Windows MCI and block until it finishes.

    Deliberately not a media-player abstraction: three command strings, a unique alias per call so
    two replies can never collide, and a close in a finally so an interrupt cannot leak the device."""
    alias = f"companionReply{uuid.uuid4().hex}"
    if _mci(f'open "{path}" type mpegvideo alias {alias}') != MCI_OK:
        raise PlaybackError("this computer would not open the spoken reply for playback")
    try:
        if _mci(f"play {alias} wait") != MCI_OK:
            raise PlaybackError("this computer would not play the spoken reply")
    finally:
        _mci(f"close {alias}")          # best effort, always: a leaked alias holds the device open


def _mci(command: str) -> int:
    """Send one MCI command string. Returns 0 on success, or PortAudio-style error code otherwise."""
    return int(ctypes.WinDLL("winmm").mciSendStringW(ctypes.c_wchar_p(command), None, 0, None))


def _online_failures() -> tuple:
    """The exception types that mean "the online voice cannot speak right now", named from the
    installed edge-tts 7.2.8: its own EdgeTTSException family (NoAudioReceived, WebSocketError,
    UnknownResponse, UnexpectedResponse, SkewAdjustmentError), the aiohttp errors its websocket
    raises when there is no network or no service, the socket timeouts from its connect/receive
    limits, and OSError - which also covers our own PlaybackError.

    Imported here rather than at module level, and only once we are already about to speak."""
    known = [OSError, TimeoutError]
    with contextlib.suppress(Exception):
        import edge_tts
        known.append(edge_tts.exceptions.EdgeTTSException)
    with contextlib.suppress(Exception):
        import aiohttp
        known.append(aiohttp.ClientError)
    return tuple(known)


# --- This computer's own voice -------------------------------------------------------------------------

def _say_offline(text: str, settings: SpeakerSettings) -> None:
    """Speak through this computer's built-in voice, and block until it has finished.

    `engine` stays local: see the module docstring for why a cached engine is a trap here."""
    import pyttsx3

    engine = pyttsx3.init()
    try:
        engine.setProperty("rate", logic.offline_words_per_minute(settings.rate))
        if settings.voice:                      # "" means the engine's own default; nothing is set
            engine.setProperty("voice", settings.voice)
        engine.say(text)
        engine.runAndWait()                     # blocking: the caller's next prompt waits for this
    finally:
        # Always, including while a KeyboardInterrupt is propagating: an engine left mid-utterance can
        # keep the audio device. A failure to stop is not worth reporting over whatever brought us here.
        try:
            engine.stop()
        except Exception as exc:
            log.debug("The offline voice would not stop cleanly: %s", type(exc).__name__)


def _offline_failures() -> tuple:
    """The exception types that mean "this machine's built-in voice is unusable", named from the
    installed pyttsx3: its driver is imported with importlib (ImportError), the sapi5 driver builds a
    SAPI object through comtypes (COMError, OSError), and the engine raises RuntimeError when its run
    loop is already going. LookupError covers a voice id the engine does not know.

    comtypes is imported here rather than at module level, and only once we are already about to
    speak, so nothing is loaded by importing this module."""
    known = [ImportError, OSError, RuntimeError, LookupError]
    with contextlib.suppress(Exception):
        import comtypes
        known.append(comtypes.COMError)
    return tuple(known)
