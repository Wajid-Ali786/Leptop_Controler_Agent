"""
ONE real smoke for the offline voice (Phase 2 slice 1) - PREPARED, NOT RUN YET.

Every other slice-1 test drives a FAKE pyttsx3, which proves the API shape and nothing about whether
this machine can actually speak. If SAPI or comtypes misbehaves here we would otherwise discover it
during the final combined Phase 2 acceptance - the single highest-stakes session of the phase. So this
test speaks one short word through the real engine, and nothing else.

Marked real_speaker and skipped unless RUN_REAL_SPEAKER_TEST=1, which no other switch sets:

    $env:RUN_REAL_SPEAKER_TEST=1
    venv\\Scripts\\python.exe -m pytest tests/test_speaker_real.py -s -v

YOU WILL HEAR ONE WORD. Turn your volume up enough to hear it and low enough not to be startled.

What it does NOT do: open the microphone, reach the network, write an audio file, read config.yaml, or
touch the online voice. The settings are built here, because speaker.enabled is deliberately still
false in config and this slice does not change configuration.

One thing to expect on a first run: comtypes may generate its SpeechLib wrapper module inside
site-packages the first time a SAPI object is created. That is the library's own cache, not audio, and
it is why the "nothing was written" check below looks at this project and the temporary folder rather
than at site-packages.
"""
import sys
import time

import pytest

from app.listener import microphone
from app.speaker import adapter
from app.speaker.models import OFFLINE, ONLINE, SpeakerSettings, Spoken
from config.settings import PROJECT_ROOT

pytestmark = pytest.mark.real_speaker

WORD = "ready"            # one short word, on purpose
LONGEST_SENSIBLE = 30.0   # a single word taking longer than this means something is wrong, not slow
AUDIO_SUFFIXES = (".wav", ".pcm", ".raw", ".mp3", ".flac", ".ogg", ".m4a")


def announce(text=""):
    print(text, flush=True)


@pytest.fixture
def installed():
    import importlib.util
    if importlib.util.find_spec("pyttsx3") is None:
        pytest.skip("pyttsx3 is not installed")
    return True


@pytest.fixture
def no_network(monkeypatch):
    """The offline voice must not reach out. Enforced, not assumed."""
    import socket

    def refuse(*args, **kwargs):
        raise AssertionError("the offline voice must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    return refuse


def test_this_computer_can_actually_speak_one_word_offline(installed, no_network, tmp_path):
    settings = SpeakerSettings(enabled=True, engine="offline", voice="", rate=0)
    before = _audio_files(PROJECT_ROOT / "data") + _audio_files(tmp_path)

    announce("")
    announce("=" * 78)
    announce(f"ABOUT TO SPEAK ONE WORD ALOUD: \"{WORD}\"")
    announce("  The real Windows voice, no microphone, no network, nothing saved.")
    announce("=" * 78)
    started = time.monotonic()
    result = adapter.speak(WORD, settings)
    elapsed = time.monotonic() - started

    announce(f"result: {result!r}")
    announce(f"wall clock around the call: {elapsed:.2f}s")
    announce("")
    announce(f'>>> YOU SHOULD HAVE HEARD THE WORD "{WORD}" SPOKEN ONCE. <<<')
    announce("    If you heard nothing, this machine's offline voice is the problem, and slice 2")
    announce("    should not be built on top of it until that is understood.")
    announce("=" * 78)

    assert isinstance(result, Spoken), f"the offline voice did not speak: {result!r}"
    assert result.engine == OFFLINE
    assert result.seconds > 0.0, "speaking should take measurable time"
    assert result.seconds < LONGEST_SENSIBLE, f"one word took {result.seconds:.1f}s"
    assert result.seconds <= elapsed + 0.5, "the reported duration is the blocking call, not a guess"

    assert microphone.owner() is None, "the speaker must never touch the microphone"
    assert _audio_files(PROJECT_ROOT / "data") + _audio_files(tmp_path) == before, (
        "the offline voice must not write audio anywhere")
    assert "edge_tts" not in sys.modules, "the offline path must not load the online library"


def _audio_files(folder):
    if not folder.is_dir():
        return []
    return sorted(str(path) for path in folder.rglob("*") if path.suffix.lower() in AUDIO_SUFFIXES)


# --- The online voice, and the fallback (Phase 2 TTS slice 2) -----------------------------------------
# All three tests here share RUN_REAL_SPEAKER_TEST, and each skips itself when the network is in the
# wrong state - so ONE command works with Wi-Fi on and again with Wi-Fi off, and neither run produces
# a confusing failure.

SENTENCE = "Opened notepad."      # one short realistic reply, not a made-up phrase
EDGE_HOST = "speech.platform.bing.com"


def _network_reachable() -> bool:
    """Can we open a TCP connection to the speech service's host? Used only to decide which of these
    real tests can prove anything right now."""
    import socket
    try:
        with socket.create_connection((EDGE_HOST, 443), timeout=4):
            return True
    except OSError:
        return False


def test_the_online_voice_really_speaks_and_leaves_no_file(installed, tmp_path):
    """A: the frozen "TTS output via edge-tts" half, proved by ear."""
    import tempfile
    if not _network_reachable():
        pytest.skip(f"{EDGE_HOST} is not reachable, so the ONLINE voice cannot be proved right now. "
                    f"Turn the network on and run this again.")
    settings = SpeakerSettings(enabled=True, engine="online", voice="", rate=0)
    before = set(_temporary_replies())

    announce("")
    announce("=" * 78)
    announce(f'ABOUT TO SPEAK ALOUD THROUGH THE ONLINE VOICE: "{SENTENCE}"')
    announce("  The reply text - and only the reply text - is sent to Microsoft's speech service.")
    announce("=" * 78)
    result = adapter.speak(SENTENCE, settings)
    announce(f"result: {result!r}")
    announce("")
    announce(f'>>> YOU SHOULD HAVE HEARD "{SENTENCE}" IN A NEURAL VOICE. <<<')
    announce("=" * 78)

    assert isinstance(result, Spoken), f"the online voice did not speak: {result!r}"
    assert result.engine == ONLINE
    assert 0.0 < result.seconds < LONGEST_SENSIBLE
    assert set(_temporary_replies()) - before == set(), (
        "the synthesised MP3 must not survive the call")
    assert microphone.owner() is None


def test_auto_falls_back_to_the_real_local_voice_when_the_online_path_fails(installed, monkeypatch):
    """B, part one: the fallback MECHANISM and the real local engine.

    The online failure is injected - as a real aiohttp.ClientError, the type a real outage produces -
    but NOTHING about the fallback is faked: the engine order, the eligibility decision and the voice
    you hear are all the real ones. Part two below is the faithful whole-outage proof."""
    import aiohttp

    def no_service(text, configured):
        raise aiohttp.ClientError("injected: the speech service is unreachable")

    monkeypatch.setattr(adapter, "_say_online", no_service)
    settings = SpeakerSettings(enabled=True, engine="auto", voice="", rate=0)

    announce("")
    announce("=" * 78)
    announce(f'THE ONLINE VOICE WILL FAIL ON PURPOSE. AUTO SHOULD SPEAK: "{SENTENCE}"')
    announce("  The failure is injected; the fallback and the voice you hear are real.")
    announce("=" * 78)
    result = adapter.speak(SENTENCE, settings)
    announce(f"result: {result!r}")
    announce("")
    announce(f'>>> YOU SHOULD HAVE HEARD "{SENTENCE}" IN THIS COMPUTER\'S OWN VOICE. <<<')
    announce("=" * 78)

    assert isinstance(result, Spoken), f"auto did not fall back: {result!r}"
    assert result.engine == OFFLINE, "auto must reach this computer's own voice"
    assert 0.0 < result.seconds < LONGEST_SENSIBLE


def test_auto_falls_back_with_the_network_genuinely_unavailable(installed):
    """B, part two: nothing is injected and nothing is patched. This is the faithful proof that a real
    outage is classified as an eligible failure and really reaches the local voice.

    It skips while the network is up, with instructions - so the same command can be run twice."""
    if _network_reachable():
        pytest.skip(f"{EDGE_HOST} is still reachable. To prove the REAL fallback: turn Wi-Fi off "
                    f"(or unplug the network), run this file again, then turn it back on.")
    settings = SpeakerSettings(enabled=True, engine="auto", voice="", rate=0)
    before = set(_temporary_replies())

    announce("")
    announce("=" * 78)
    announce(f'THE NETWORK IS DOWN. AUTO SHOULD FALL BACK AND SPEAK: "{SENTENCE}"')
    announce("  Nothing is injected here: this is a real outage and the real classification.")
    announce("=" * 78)
    result = adapter.speak(SENTENCE, settings)
    announce(f"result: {result!r}")
    announce("")
    announce(f'>>> YOU SHOULD HAVE HEARD "{SENTENCE}" IN THIS COMPUTER\'S OWN VOICE. <<<')
    announce("=" * 78)

    assert isinstance(result, Spoken), f"neither voice spoke with the network down: {result!r}"
    assert result.engine == OFFLINE, "a real outage must fall back, not fail"
    assert 0.0 < result.seconds < LONGEST_SENSIBLE
    assert set(_temporary_replies()) - before == set(), "no half-synthesised MP3 was left behind"


def _temporary_replies():
    import tempfile
    from pathlib import Path
    return [str(path) for path in Path(tempfile.gettempdir()).glob("companion-reply-*.mp3")]
