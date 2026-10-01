"""
The offline test suite may never make a real sound or a real TTS network call.

tests/conftest.py installs an autouse barrier for that. These are its own tests: they prove the
barrier intercepts BEFORE the real boundary (no socket, no COM/SAPI object, no winmm command), that it
cannot be swallowed by the speaker's own exception containment, that a test's own fake still wins over
it, and that exactly two markers are exempt.

Why it exists: speaker.enabled became true in config/config.yaml, and four tests in
tests/test_voice_mode.py that accept a spoken command reached voice_console._run and then the REAL
speaker adapter - contacting Microsoft's speech service and playing audio from the laptop speaker
during an ordinary pytest run, with no RUN_REAL_* flag set. The fakes were fine; nothing was watching
the path nobody had thought about.

Nothing here speaks, imports the real speech libraries, or touches the network.
"""
import sys
import tempfile
from pathlib import Path

import pytest

from app.speaker import adapter
from app.speaker.models import SpeakerSettings, SpeechFailure, Spoken
import conftest as root_conftest   # the REPOSITORY-ROOT conftest: the outer safety boundary
from tests import conftest
from tests.conftest import PhysicalAudioEscaped


def settings(**changes) -> SpeakerSettings:
    values = {"enabled": True, "engine": "auto", "voice": "", "rate": 0}
    return SpeakerSettings(**{**values, **changes})


def replies() -> set:
    """Synthesised replies in the system temp folder. Compared before and after - this is a shared
    directory and these tests own only the files their own call would have created."""
    return set(Path(tempfile.gettempdir()).glob("companion-reply-*.mp3"))


# --- The three boundaries -----------------------------------------------------------------------------

@pytest.mark.parametrize("library", ["edge_tts", "pyttsx3"])
def test_importing_a_speech_library_is_refused(library):
    before = set(sys.modules)
    with pytest.raises(PhysicalAudioEscaped, match="offline test attempted real speaker/network"):
        __import__(library)
    assert library not in sys.modules, "refused at import, so the real module never loaded"
    assert set(sys.modules) - before <= set(), "and nothing else was loaded on its way in"


@pytest.mark.parametrize("library", ["edge_tts", "pyttsx3"])
def test_the_refusal_says_which_boundary_was_reached(library):
    with pytest.raises(PhysicalAudioEscaped) as raised:
        __import__(library)
    message = str(raised.value)
    assert library in message, "it names the library"
    assert "real_speaker" in message and "real_voice_console" in message, "and the way out"


def test_playback_through_winmm_is_refused():
    with pytest.raises(PhysicalAudioEscaped, match="winmm/MCI playback"):
        adapter._mci('open "C:/nothing.mp3" type mpegvideo alias x')


def test_the_playback_refusal_names_the_command():
    with pytest.raises(PhysicalAudioEscaped) as raised:
        adapter._mci("play somealias wait")
    assert "'play'" in str(raised.value)


# --- It cannot be swallowed ---------------------------------------------------------------------------

def test_the_guard_is_a_base_exception_not_an_ordinary_one():
    """app/speaker/adapter.py deliberately contains ordinary exceptions, so an Exception here would be
    turned into a quiet SpeechFailure and the test would pass while the guard was being hit."""
    assert issubclass(PhysicalAudioEscaped, BaseException)
    assert not issubclass(PhysicalAudioEscaped, Exception)


@pytest.mark.parametrize("engine", ["online", "auto", "offline"])
def test_the_speakers_own_containment_does_not_swallow_the_guard(engine):
    before = replies()
    with pytest.raises(PhysicalAudioEscaped):
        adapter.speak("this must never be spoken", settings(engine=engine))
    assert replies() - before == set(), "intercepted before any temporary file was created"


def test_an_intercepted_call_returns_nothing_at_all():
    """Not a Spoken, not a SpeechFailure: the run fails instead of quietly continuing."""
    outcome = None
    try:
        outcome = adapter.speak("hello", settings())
    except PhysicalAudioEscaped:
        pass
    assert not isinstance(outcome, (Spoken, SpeechFailure)) and outcome is None


# --- A test's own fake still wins ---------------------------------------------------------------------

def test_a_fake_installed_by_a_test_is_used_instead_of_the_guard(monkeypatch):
    """The barrier is a last resort, not a replacement for unit-test fakes: a fake in sys.modules is
    found before the import system ever consults the finder."""
    import types
    said = []

    class Engine:
        def setProperty(self, name, value):
            pass

        def say(self, text):
            said.append(text)

        def runAndWait(self):
            pass

        def stop(self):
            pass

    fake = types.ModuleType("pyttsx3")
    fake.init = lambda driverName=None, debug=False: Engine()
    monkeypatch.setitem(sys.modules, "pyttsx3", fake)

    result = adapter.speak("spoken by a fake", settings(engine="offline"))
    assert isinstance(result, Spoken) and result.engine == "offline"
    assert said == ["spoken by a fake"], "the fake engine was used, and the guard stayed out of the way"


def test_a_disabled_speaker_never_reaches_the_guard_either():
    result = adapter.speak("anything", settings(enabled=False))
    assert isinstance(result, Spoken) and result.engine == "off"


def test_blank_text_never_reaches_the_guard():
    result = adapter.speak("   ", settings())
    assert isinstance(result, SpeechFailure) and result.kind == "nothing_to_say"


# --- Exactly two markers are exempt -------------------------------------------------------------------

def test_only_the_two_speaking_markers_are_exempt():
    assert conftest.SPEAKING_MARKERS == ("real_speaker", "real_voice_console")
    others = set(conftest.OPT_IN_GATES) - set(conftest.SPEAKING_MARKERS)
    assert others, "there are other real_* markers, and none of them may be exempt"
    for marker in others:
        assert marker not in conftest.SPEAKING_MARKERS, marker
    # The microphone, model, desktop and API tests make no sound, so none of them needs the exemption.
    for quiet in ("real_microphone", "real_recording", "real_model", "real_transcription",
                  "real_kws_latency", "real_vosk_stop", "real_desktop", "real_api"):
        assert quiet in conftest.OPT_IN_GATES and quiet not in conftest.SPEAKING_MARKERS


def test_the_guard_covers_both_speech_libraries_and_playback():
    assert set(conftest.SPEECH_LIBRARIES) == {"edge_tts", "pyttsx3"}
    guard = root_conftest.no_physical_speaker_or_listener
    import inspect
    body = inspect.getsource(guard)
    assert "install_library_guard" in body and "_mci" in body, "both barriers are installed"
    assert "AUDIO_EXEMPT" in body, "and only the speaking markers are exempt"


def test_the_guard_is_autouse_so_a_new_test_is_protected_by_default():
    import inspect
    source = inspect.getsource(root_conftest)
    block = source.split("def no_physical_speaker_or_listener")[0].splitlines()[-2:]
    assert any("autouse=True" in line for line in block), (
        "a test must not have to remember to ask for protection")
