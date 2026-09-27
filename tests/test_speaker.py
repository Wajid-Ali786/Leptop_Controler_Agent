"""
Tests for app/speaker/ (docs/step4 Section 5, Phase 2 - Feature 1: config and boundary only).

Offline and silent: nothing here speaks, imports edge-tts or pyttsx3, or touches the network. The
speaker owns its own settings, like every other module, so app/listener never reads them.
The import-boundary rules for edge_tts/pyttsx3 live in tests/test_listener.py, which checks every
voice library in one place.
"""
from pathlib import Path

import pytest

from app.speaker import logic
from app.speaker.models import SpeakerSettings
from config import settings
from config.settings import SettingsError

CONFIG = ("speaker:\n"
          "  enabled: false\n"
          "  engine: auto\n"
          '  voice: ""\n'
          "  rate: 0\n")


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_PATH", path)

    def rewrite(old, new):
        assert old in path.read_text(encoding="utf-8"), f"{old!r} is not in the test config"
        path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")

    return rewrite


def test_the_speaker_section_is_read_and_validated(config):
    chosen = logic.speaker_settings()
    assert chosen == SpeakerSettings(enabled=False, engine="auto", voice="", rate=0)


def test_the_real_config_file_is_valid():
    assert logic.speaker_settings().engine in logic.ENGINES


@pytest.mark.parametrize("old, new, part", [
    ("engine: auto", "engine: robot", "speaker.engine"),
    ("engine: auto", "engine: true", "speaker.engine"),
    ("rate: 0", "rate: 900", "speaker.rate"),
    ("rate: 0", "rate: 51", "speaker.rate"),
    ("rate: 0", "rate: -51", "speaker.rate"),
    ("rate: 0", "rate: 1.5", "speaker.rate"),
    ("rate: 0", "rate: true", "speaker.rate"),
    ("enabled: false", "enabled: sometimes", "speaker.enabled"),
    ('voice: ""', "voice: 7", "speaker.voice"),
])
def test_invalid_settings_are_refused_by_name(config, old, new, part):
    config(old, new)
    with pytest.raises(SettingsError, match=part.replace(".", r"\.")):
        logic.speaker_settings()


def test_an_empty_voice_means_the_engine_default(config):
    assert logic.speaker_settings().voice == ""


@pytest.mark.parametrize("value", [-50, -20, 0, 20, 50])
def test_rate_is_a_relative_percentage_in_both_directions(config, value):
    """0 is the engine's normal speed; the same number must mean the same thing on either engine,
    so it is a percentage here and each adapter maps it onto its own scale in Feature 9."""
    config("rate: 0", f"rate: {value}")
    assert logic.speaker_settings().rate == value


def test_the_accepted_rate_range_is_conservative():
    assert (logic.SLOWEST_PERCENT, logic.FASTEST_PERCENT) == (-50, 50)


def test_the_rate_error_explains_the_percentage_contract(config):
    config("rate: 0", "rate: 200")
    with pytest.raises(SettingsError, match="percentage"):
        logic.speaker_settings()


def test_the_speaker_owns_its_settings_not_the_listener():
    """Rule 4: a module's settings are read inside that module."""
    from app.listener import logic as listener_logic
    assert not hasattr(listener_logic, "speaker_settings")


# --- Speaking: the offline voice, driven with a fake engine --------------------------------------------
# Phase 2 slice 1. Nothing here makes a sound, imports the real pyttsx3, imports edge_tts or touches
# the network: a fake module is injected into sys.modules, which is what the adapter's lazy
# `import pyttsx3` picks up. The real engine is proved separately by tests/test_speaker_real.py.

import ast
import logging
import sys
import tempfile
import types

from app.speaker import adapter
from app.speaker.models import (BOTH_UNAVAILABLE, NOTHING_TO_SAY, OFF, OFFLINE, OFFLINE_UNAVAILABLE,
                                ONLINE, ONLINE_UNAVAILABLE, SPEAKER_ERROR, SpeechFailure, Spoken)

SECRET = "Opened notepad; its window appeared after 0.2s."   # stands in for a real reply


class FakeEngine:
    """Stands in for a pyttsx3 Engine. `fail` picks one failure mode."""

    def __init__(self, fail=None):
        self.properties = {}
        self.said = []
        self.ran = self.stops = 0
        self.fail = fail

    def setProperty(self, name, value):
        if self.fail == "voice" and name == "voice":
            raise LookupError("this engine has no such voice")
        self.properties[name] = value

    def say(self, text):
        if self.fail == "say":
            raise RuntimeError("the driver refused the utterance")
        self.said.append(text)

    def runAndWait(self):
        self.ran += 1
        if self.fail == "run":
            raise RuntimeError("run loop already started")
        if self.fail == "interrupt":
            raise KeyboardInterrupt
        if self.fail == "unexpected":
            raise ZeroDivisionError("nobody predicted this")

    def stop(self):
        self.stops += 1
        if self.fail == "stop":
            raise OSError("the audio device would not release")


class FakeModule:
    """The fake `pyttsx3` module the adapter imports inside the offline path."""

    def __init__(self, engine=None, fail=None):
        self.engine = engine if engine is not None else FakeEngine()
        self.fail = fail
        self.inits = 0
        self.driver_names = []

    def init(self, driverName=None, debug=False):
        self.inits += 1
        self.driver_names.append(driverName)
        if self.fail == "init":
            raise ImportError("no usable pyttsx3 driver on this machine")
        if self.fail == "init_unexpected":
            raise ZeroDivisionError("nobody predicted this either")
        return self.engine


def offline(**changes) -> SpeakerSettings:
    values = {"enabled": True, "engine": "offline", "voice": "", "rate": 0}
    return SpeakerSettings(**{**values, **changes})


@pytest.fixture
def voice(monkeypatch):
    """Install a fake pyttsx3 and hand back the module, so a test can see what was asked of it."""
    def install(engine=None, fail=None):
        module = FakeModule(engine=engine, fail=fail)
        monkeypatch.setitem(sys.modules, "pyttsx3", module)
        return module
    return install


def _imports(path):
    for node in ast.walk(ast.parse(Path(path).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            yield from ((alias.name, "") for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield from ((node.module or "", alias.name) for alias in node.names)


# --- Switched off, and nothing to say -----------------------------------------------------------------

def test_a_disabled_speaker_says_nothing_and_builds_no_engine(voice):
    module = voice()
    result = adapter.speak(SECRET, offline(enabled=False))
    assert result == Spoken(engine=OFF, seconds=0.0)
    assert module.inits == 0, "a switched-off speaker must not build an engine"
    assert isinstance(result, Spoken), "off is a successful no-op, not a failure"


def test_a_disabled_speaker_does_not_even_import_pyttsx3(monkeypatch):
    monkeypatch.delitem(sys.modules, "pyttsx3", raising=False)
    assert adapter.speak(SECRET, offline(enabled=False)).engine == OFF
    assert "pyttsx3" not in sys.modules, "importing the library is part of the cost of speaking"


@pytest.mark.parametrize("text", ["", "   ", "\n\t ", None])
def test_blank_text_is_refused_before_any_engine_exists(voice, text):
    module = voice()
    result = adapter.speak(text, offline())
    assert result == SpeechFailure(NOTHING_TO_SAY, "There was nothing to say.")
    assert module.inits == 0 and module.engine.said == []


# --- The offline voice --------------------------------------------------------------------------------

def test_the_offline_voice_speaks_the_text_it_was_given(voice):
    module = voice()
    result = adapter.speak(SECRET, offline())
    assert isinstance(result, Spoken) and result.engine == OFFLINE
    assert 0.0 <= result.seconds < 5.0, "a monotonic duration, not a benchmark"
    assert module.engine.said == [SECRET] and module.engine.ran == 1
    assert module.inits == 1, "exactly one engine per call"
    assert module.driver_names == [None], "the platform default driver, never named explicitly"


def test_the_engine_is_never_kept_between_calls(voice):
    module = voice()
    adapter.speak("one", offline())
    adapter.speak("two", offline())
    assert module.inits == 2, "a fresh engine each time: a cached one dies on the first Ctrl+C"
    assert not hasattr(adapter, "_engine"), "no module-level engine cache"


@pytest.mark.parametrize("percent, words_per_minute",
                         [(-50, 100), (-20, 160), (0, 200), (20, 240), (50, 300)])
def test_the_rate_percentage_becomes_words_per_minute(voice, percent, words_per_minute):
    module = voice()
    adapter.speak(SECRET, offline(rate=percent))
    assert module.engine.properties["rate"] == words_per_minute
    assert logic.offline_words_per_minute(percent) == words_per_minute


def test_the_default_rate_is_the_engines_own_normal_speed():
    """0 percent must mean "leave it alone": pyttsx3's sapi5 driver starts at exactly 200 wpm."""
    assert logic.offline_words_per_minute(0) == logic.BASE_WORDS_PER_MINUTE == 200


def test_a_configured_voice_is_applied(voice):
    module = voice()
    adapter.speak(SECRET, offline(voice="Microsoft Zira Desktop"))
    assert module.engine.properties["voice"] == "Microsoft Zira Desktop"


def test_an_empty_voice_leaves_the_engine_default_untouched(voice):
    module = voice()
    adapter.speak(SECRET, offline(voice=""))
    assert "voice" not in module.engine.properties, "an empty voice sets nothing at all"
    assert "rate" in module.engine.properties, "the rate is still set"


# --- Failures: contained, classified, and cleaned up --------------------------------------------------

def test_an_engine_this_machine_cannot_build_is_reported_as_unavailable(voice):
    module = voice(fail="init")
    result = adapter.speak(SECRET, offline())
    assert result == logic.offline_unavailable()
    assert result.kind == OFFLINE_UNAVAILABLE and module.inits == 1


@pytest.mark.parametrize("fail", ["say", "run", "voice"])
def test_an_engine_that_will_not_speak_is_reported_as_unavailable(voice, fail):
    module = voice(engine=FakeEngine(fail=fail))
    result = adapter.speak(SECRET, offline(voice="Some Voice"))
    assert isinstance(result, SpeechFailure) and result.kind == OFFLINE_UNAVAILABLE
    assert module.engine.stops == 1, "the engine is stopped even when it failed"


def test_the_engine_is_stopped_after_a_successful_utterance(voice):
    module = voice()
    assert adapter.speak(SECRET, offline()).engine == OFFLINE
    assert module.engine.stops == 1


def test_a_stop_that_itself_fails_does_not_undo_a_successful_utterance(voice):
    module = voice(engine=FakeEngine(fail="stop"))
    result = adapter.speak(SECRET, offline())
    assert isinstance(result, Spoken) and result.engine == OFFLINE, (
        "the words were spoken; a stubborn device on the way out does not unsay them")
    assert module.engine.stops == 1


@pytest.mark.parametrize("fail", ["unexpected", "init_unexpected"])
def test_an_unexpected_failure_is_contained_as_a_speaker_error(voice, fail):
    """speak() runs AFTER the command has finished, so a defect in here must not escape as an
    exception and be mistaken for the command itself failing."""
    if fail == "unexpected":
        voice(engine=FakeEngine(fail="unexpected"))
    else:
        voice(fail=fail)
    result = adapter.speak(SECRET, offline())
    assert isinstance(result, SpeechFailure) and result.kind == SPEAKER_ERROR
    assert result == logic.speaker_error()


@pytest.mark.parametrize("engine_fail, module_fail",
                         [("say", None), ("run", None), ("unexpected", None), ("stop", None),
                          (None, "init"), (None, "init_unexpected")])
def test_the_public_boundary_never_raises_an_ordinary_exception(voice, engine_fail, module_fail):
    voice(engine=FakeEngine(fail=engine_fail), fail=module_fail)
    outcome = adapter.speak(SECRET, offline())     # must not raise, whatever happened in there
    assert isinstance(outcome, (Spoken, SpeechFailure))


def test_a_keyboard_interrupt_propagates_after_the_engine_is_stopped(voice):
    """Ctrl+C is the user's, not ours: it is never swallowed. Cleanup still happens on the way out."""
    module = voice(engine=FakeEngine(fail="interrupt"))
    with pytest.raises(KeyboardInterrupt):
        adapter.speak(SECRET, offline())
    assert module.engine.stops == 1, "the engine was stopped while the interrupt was propagating"


# --- Nothing that was said is kept, shown or written down ---------------------------------------------

def test_no_result_or_failure_carries_the_spoken_text(voice):
    voice()
    spoken = adapter.speak(SECRET, offline())
    assert SECRET not in repr(spoken) and "notepad" not in repr(spoken)
    for failure in (logic.nothing_to_say(), logic.offline_unavailable(), logic.online_unavailable(),
                    logic.both_unavailable(), logic.speaker_error()):
        assert SECRET not in repr(failure) and "notepad" not in repr(failure)
    assert not hasattr(spoken, "text") and not hasattr(logic.speaker_error(), "text")


def test_the_spoken_text_is_never_logged(voice, caplog):
    voice()
    with caplog.at_level(logging.DEBUG):
        adapter.speak(SECRET, offline())
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert SECRET not in logged and "notepad" not in logged.lower()
    assert "offline" in logged, "the engine and the elapsed time are worth recording"


def test_an_unexpected_failure_logs_its_type_and_nothing_else(voice, caplog):
    """The containment is a deliberate exception to letting our defects propagate, so it must at least
    be findable: the exception TYPE at debug level - never its message, never the text."""
    voice(engine=FakeEngine(fail="unexpected"))
    with caplog.at_level(logging.DEBUG):
        adapter.speak(SECRET, offline())
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "ZeroDivisionError" in logged, "the type name makes the defect findable"
    assert "nobody predicted this" not in logged, "the exception's own message is not ours to show"
    assert SECRET not in logged and "Traceback" not in logged


# --- The boundary rules ------------------------------------------------------------------------------

def test_the_speaker_logic_module_stays_pure():
    """No engine, no network, no OS speech machinery, no temporary files."""
    forbidden = {"pyttsx3", "edge_tts", "aiohttp", "ctypes", "tempfile", "socket", "comtypes",
                 "win32com", "pythoncom", "winsound", "subprocess"}
    used = {module.split(".")[0]
            for module, _ in _imports(settings.PROJECT_ROOT / "app/speaker/logic.py")}
    assert not (used & forbidden), used & forbidden
    assert used <= {"dataclasses", "app", "config"}, used


def test_pyttsx3_is_imported_lazily_inside_the_offline_path():
    """At module level it would load comtypes and build a SAPI object just to collect the tests."""
    source = settings.PROJECT_ROOT / "app/speaker/adapter.py"
    top_level = {alias.name for node in ast.parse(source.read_text(encoding="utf-8")).body
                 if isinstance(node, ast.Import) for alias in node.names}
    assert "pyttsx3" not in top_level and "comtypes" not in top_level
    assert "pyttsx3" in {module for module, _ in _imports(source)}, "used, just not at import time"


def test_edge_tts_is_imported_lazily_inside_the_online_path():
    """At module level it would pull aiohttp in just to collect the tests. Which FILES may import it
    at all is enforced in tests/test_listener.py, where every voice library is checked in one place."""
    source = settings.PROJECT_ROOT / "app/speaker/adapter.py"
    top_level = {alias.name for node in ast.parse(source.read_text(encoding="utf-8")).body
                 if isinstance(node, ast.Import) for alias in node.names}
    assert "edge_tts" not in top_level and "aiohttp" not in top_level
    assert "edge_tts" in {module for module, _ in _imports(source)}, "used, just not at import time"
    import sys
    assert "edge_tts" not in sys.modules, "importing the adapter must not load it"


def test_only_the_voice_console_reaches_the_speaker():
    """The frozen requirement is that the VOICE path hears a reply. Nothing else may speak - and in
    particular the typed console must stay silent, which test_voice_console.py also proves by
    behaviour."""
    root = settings.PROJECT_ROOT
    callers = set()
    for path in (*(root / "app").rglob("*.py"), root / "main.py"):
        if path.parent.name == "speaker":
            continue
        for module, _ in _imports(path):
            if module.startswith("app.speaker"):
                callers.add(str(path.relative_to(root)).replace("\\", "/"))
    assert callers == {"app/voice_console.py"}, callers


# --- The one real smoke is opt-in and isolated --------------------------------------------------------

def test_the_real_speaker_smoke_has_its_own_switch():
    from tests import conftest
    from tests import test_speaker_real as smoke
    assert smoke.pytestmark.name == "real_speaker"
    variable, _ = conftest.OPT_IN_GATES["real_speaker"]
    assert variable == "RUN_REAL_SPEAKER_TEST"
    others = [value for name, (value, _) in conftest.OPT_IN_GATES.items() if name != "real_speaker"]
    assert variable not in others
    import os
    assert os.environ.get(variable) != "1", "the normal suite must never make a sound"


def test_no_other_real_switch_can_make_the_computer_speak(monkeypatch):
    from tests import conftest

    class Item:
        def __init__(self):
            self.markers = []

        def get_closest_marker(self, name):
            return object() if name == "real_speaker" else None

        def add_marker(self, marker):
            self.markers.append(marker)

    for variable, _ in conftest.OPT_IN_GATES.values():
        monkeypatch.setenv(variable, "1")
    monkeypatch.delenv("RUN_REAL_SPEAKER_TEST", raising=False)
    item = Item()
    conftest.pytest_collection_modifyitems(None, [item])
    assert [marker.name for marker in item.markers] == ["skip"]


def test_the_real_smoke_speaks_one_word_and_opens_nothing():
    from tests import test_speaker_real as smoke
    assert smoke.WORD == "ready" and " " not in smoke.WORD
    # Imports are checked with ast, not by searching the text: the file deliberately NAMES edge_tts in
    # an assertion that it was never loaded, and a crude search would trip over that.
    path = settings.PROJECT_ROOT / "tests/test_speaker_real.py"
    imported = {module.split(".")[0] for module, _ in _imports(path)}
    assert not (imported & {"edge_tts", "urllib", "requests", "sounddevice"}), imported
    # aiohttp IS imported, on purpose and for one reason only: to raise the REAL exception type a real
    # outage produces, so the fallback is proved against the genuine class rather than a stand-in.
    source = path.read_text(encoding="utf-8")
    assert "aiohttp.ClientError(" in source, "the real exception class, raised directly"
    for connecting in ("ClientSession", "aiohttp.request", ".get(", ".post("):
        assert connecting not in source, f"aiohttp is raised, never used to connect ({connecting})"
    for absent in ("InputStream", "microphone.acquire", "urlopen"):
        assert absent not in source, f"the smoke must not use {absent}"
    assert 'engine="offline"' in source and 'engine="online"' in source
    assert 'engine="auto"' in source, "all three engine settings are exercised for real"


# --- The online voice, and the auto fallback -----------------------------------------------------------
# Phase 2 slice 2. Still no sound and still no network: a fake edge_tts and a fake MCI stand in, and
# tests/test_speaker_real.py proves the real engines on real hardware.

class FakeEdgeError(Exception):
    """Stands in for edge_tts.exceptions.EdgeTTSException - the family the real adapter looks for."""


class FakeCommunicate:
    def __init__(self, module, text, voice, rate):
        self.module, self.text, self.voice, self.rate = module, text, voice, rate
        self.saved = None

    def save_sync(self, audio_fname, metadata_fname=None):
        if self.module.fail == "service":
            raise FakeEdgeError("no audio received")
        if self.module.fail == "network":
            import aiohttp
            raise aiohttp.ClientError("cannot reach the speech service")
        if self.module.fail == "timeout":
            raise TimeoutError("the speech service did not answer")
        if self.module.fail == "our_bug":
            raise TypeError("text must be str")
        if self.module.fail == "unexpected":
            raise ZeroDivisionError("nobody predicted this")
        Path(audio_fname).write_bytes(b"\x00\x01")      # stand-in for the MP3 the service returns
        self.saved = audio_fname


class FakeEdge:
    """The fake `edge_tts` module the adapter imports inside the online path."""

    def __init__(self, fail=None):
        self.fail = fail
        self.built = []
        self.exceptions = types.SimpleNamespace(EdgeTTSException=FakeEdgeError)

    def Communicate(self, text, voice=None, *, rate=None, **rest):
        made = FakeCommunicate(self, text, voice, rate)
        self.built.append(made)
        return made


class FakeMci:
    """Stands in for adapter._mci: records the command strings and can fail on one of them."""

    def __init__(self, fail=None):
        self.commands = []
        self.fail = fail

    def __call__(self, command):
        self.commands.append(command)
        if self.fail == "open" and command.startswith("open"):
            return 263
        if self.fail == "play" and command.startswith("play"):
            return 264
        if self.fail == "interrupt" and command.startswith("play"):
            raise KeyboardInterrupt
        return 0

    @property
    def verbs(self):
        return [command.split()[0] for command in self.commands]

    @property
    def played_path(self):
        opened = next(command for command in self.commands if command.startswith("open"))
        return opened.split('"')[1]

    @property
    def aliases(self):
        return [command.split("alias ")[1] for command in self.commands if "alias " in command]


@pytest.fixture
def online(monkeypatch):
    """Install a fake edge_tts and a fake MCI; hand both back so a test can see what happened."""
    def install(fail=None, mci_fail=None):
        module = FakeEdge(fail=fail)
        monkeypatch.setitem(sys.modules, "edge_tts", module)
        mci = FakeMci(fail=mci_fail)
        monkeypatch.setattr(adapter, "_mci", mci)
        return module, mci
    return install


def test_the_online_voice_speaks_and_reports_itself(online):
    module, mci = online()
    result = adapter.speak(SECRET, offline(engine="online"))
    assert isinstance(result, Spoken) and result.engine == ONLINE
    assert 0.0 <= result.seconds < 5.0
    assert len(module.built) == 1, "one Communicate per utterance: its stream() is single-use"
    assert module.built[0].text == SECRET


def test_a_fresh_communicate_is_built_for_every_utterance(online):
    module, mci = online()
    adapter.speak("one", offline(engine="online"))
    adapter.speak("two", offline(engine="online"))
    assert [made.text for made in module.built] == ["one", "two"]
    assert module.built[0] is not module.built[1]


@pytest.mark.parametrize("percent, rate", [(-50, "-50%"), (-20, "-20%"), (0, "+0%"), (20, "+20%"),
                                           (50, "+50%")])
def test_the_rate_percentage_becomes_the_string_edge_tts_wants(online, percent, rate):
    module, mci = online()
    adapter.speak(SECRET, offline(engine="online", rate=percent))
    assert module.built[0].rate == rate
    assert logic.online_rate(percent) == rate


def test_the_configured_voice_is_used_and_an_empty_one_means_the_pinned_default(online):
    module, mci = online()
    adapter.speak(SECRET, offline(engine="online", voice="en-GB-SoniaNeural"))
    assert module.built[0].voice == "en-GB-SoniaNeural"
    adapter.speak(SECRET, offline(engine="online", voice=""))
    assert module.built[1].voice == adapter.DEFAULT_ONLINE_VOICE == "en-US-EmmaMultilingualNeural"


# --- The temporary audio file never survives ----------------------------------------------------------

def test_the_reply_is_played_through_mci_and_the_alias_is_closed(online):
    module, mci = online()
    adapter.speak(SECRET, offline(engine="online"))
    assert mci.verbs == ["open", "play", "close"]
    assert "type mpegvideo alias" in mci.commands[0]
    assert mci.commands[1].endswith(" wait"), "playback blocks until the reply has finished"
    assert mci.commands[2].split()[1] == mci.aliases[0], "the alias that was opened is the one closed"


def test_every_call_uses_its_own_alias(online):
    module, mci = online()
    adapter.speak("one", offline(engine="online"))
    adapter.speak("two", offline(engine="online"))
    assert len(set(mci.aliases)) == 2, "two replies must never collide on one alias"


def test_the_temporary_file_is_outside_the_project_and_gone_afterwards(online):
    module, mci = online()
    assert adapter.speak(SECRET, offline(engine="online")).engine == ONLINE
    path = Path(mci.played_path)
    assert path.suffix == ".mp3" and not path.exists(), "no generated speech survives the call"
    for forbidden in (settings.PROJECT_ROOT, settings.PROJECT_ROOT / "data",
                      settings.PROJECT_ROOT / "logs"):
        assert forbidden not in path.parents, f"audio must never be written under {forbidden}"


def _replies() -> set:
    """The synthesised replies currently sitting in the system temp folder.

    Compared BEFORE and AFTER, never required to be empty: this is a shared directory and the test
    only owns the file its own call created."""
    return set(Path(tempfile.gettempdir()).glob("companion-reply-*.mp3"))


@pytest.mark.parametrize("fail, mci_fail", [("service", None), ("unexpected", None), ("our_bug", None),
                                            (None, "open"), (None, "play")])
def test_the_temporary_file_is_deleted_however_the_call_ends(online, fail, mci_fail):
    module, mci = online(fail=fail, mci_fail=mci_fail)
    before = _replies()
    result = adapter.speak(SECRET, offline(engine="online"))
    assert isinstance(result, SpeechFailure)
    left = _replies() - before
    assert left == set(), f"a synthesised reply was left behind: {left}"


def test_the_alias_is_closed_even_when_playback_fails(online):
    module, mci = online(mci_fail="play")
    assert isinstance(adapter.speak(SECRET, offline(engine="online")), SpeechFailure)
    assert mci.verbs == ["open", "play", "close"], "the device is released even after a failed play"


def test_nothing_is_opened_when_the_device_refuses_to_open(online):
    module, mci = online(mci_fail="open")
    assert isinstance(adapter.speak(SECRET, offline(engine="online")), SpeechFailure)
    assert mci.verbs == ["open"], "nothing to close: the open itself failed"


def test_a_keyboard_interrupt_during_playback_still_closes_and_deletes(online):
    module, mci = online(mci_fail="interrupt")
    before = _replies()
    with pytest.raises(KeyboardInterrupt):
        adapter.speak(SECRET, offline(engine="online"))
    assert mci.verbs == ["open", "play", "close"]
    assert _replies() - before == set(), "an interrupt must not leave a synthesised reply behind"


# --- auto, online and offline ---------------------------------------------------------------------------

@pytest.mark.parametrize("enabled, engine, order", [
    (False, "auto", ()),
    (True, "online", ("online",)),
    (True, "offline", ("offline",)),
    (True, "auto", ("online", "offline")),
])
def test_the_engine_order_is_the_whole_policy(enabled, engine, order):
    assert logic.engine_order(offline(enabled=enabled, engine=engine)) == order


def test_auto_uses_the_online_voice_and_never_reaches_the_offline_one(online, voice):
    module, mci = online()
    local = voice()
    result = adapter.speak(SECRET, offline(engine="auto"))
    assert isinstance(result, Spoken) and result.engine == ONLINE
    assert local.inits == 0, "the local voice is not built when the online one worked"


@pytest.mark.parametrize("fail", ["service", "network", "timeout"])
def test_auto_falls_back_to_this_computers_voice_when_the_online_one_cannot_speak(online, voice, fail):
    module, mci = online(fail=fail)
    local = voice()
    result = adapter.speak(SECRET, offline(engine="auto"))
    assert isinstance(result, Spoken) and result.engine == OFFLINE
    assert local.engine.said == [SECRET] and local.inits == 1


def test_auto_falls_back_when_this_machine_cannot_play_the_online_reply(online, voice):
    """A synthesised reply we cannot play is still a reply the user did not hear."""
    module, mci = online(mci_fail="play")
    local = voice()
    assert adapter.speak(SECRET, offline(engine="auto")).engine == OFFLINE


def test_auto_reports_both_when_neither_voice_can_speak(online, voice):
    module, mci = online(fail="network")
    local = voice(fail="init")
    result = adapter.speak(SECRET, offline(engine="auto"))
    assert result == logic.both_unavailable() and result.kind == BOTH_UNAVAILABLE
    assert "Neither" in result.message


def test_online_only_never_falls_back(online, voice):
    module, mci = online(fail="network")
    local = voice()
    result = adapter.speak(SECRET, offline(engine="online"))
    assert result == logic.online_unavailable() and result.kind == ONLINE_UNAVAILABLE
    assert local.inits == 0, "engine: online means the online voice or nothing"


def test_offline_only_never_touches_the_online_library_or_the_network(voice, monkeypatch):
    import socket
    monkeypatch.delitem(sys.modules, "edge_tts", raising=False)

    def refuse(*args, **kwargs):
        raise AssertionError("the offline voice must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    local = voice()
    assert adapter.speak(SECRET, offline(engine="offline")).engine == OFFLINE
    assert "edge_tts" not in sys.modules


def test_our_own_bad_call_is_never_dressed_up_as_an_unavailable_service(online, voice):
    """TypeError/ValueError from building a Communicate is OUR defect. It must not fall back, and it
    must not be reported as the service being unavailable."""
    module, mci = online(fail="our_bug")
    local = voice()
    result = adapter.speak(SECRET, offline(engine="auto"))
    assert result == logic.speaker_error() and result.kind == SPEAKER_ERROR
    assert local.inits == 0, "a bug in our call is not a reason to try the other voice"


def test_an_unexpected_failure_in_the_online_path_is_contained(online, voice):
    module, mci = online(fail="unexpected")
    local = voice()
    result = adapter.speak(SECRET, offline(engine="auto"))
    assert result.kind == SPEAKER_ERROR and local.inits == 0


def test_the_online_failure_types_are_named_not_guessed():
    """They come from the installed edge-tts and aiohttp, not from a bare except."""
    named = {kind.__name__ for kind in adapter._online_failures()}
    assert {"OSError", "TimeoutError", "EdgeTTSException", "ClientError"} <= named
    assert "Exception" not in named and "BaseException" not in named
    assert issubclass(adapter.PlaybackError, OSError), "a playback failure is eligible as an OSError"


def test_no_text_reaches_the_log_on_any_online_path(online, voice, caplog):
    module, mci = online(fail="network")
    voice()
    with caplog.at_level(logging.DEBUG):
        adapter.speak(SECRET, offline(engine="auto"))
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert SECRET not in logged and "notepad" not in logged.lower()
    assert "online" in logged and "offline" in logged, "which voice was tried is worth recording"


# --- The real config file says what is true -----------------------------------------------------------

def test_the_real_config_enables_the_speaker_only_for_the_voice_path():
    """Proved from the call graph, not assumed: speak() is reachable only from voice_console, so
    enabling it cannot make `python main.py --console` talk. tests/test_voice_console.py proves the
    typed console stays silent by behaviour."""
    from config.settings import get_setting
    assert get_setting("speaker.enabled") is True
    assert logic.speaker_settings().enabled is True
    comment = (settings.PROJECT_ROOT / "config/config.yaml").read_text(encoding="utf-8")
    assert "--voice" in comment.split("speaker:")[1], "the comment says which mode speaks"


def test_the_real_config_states_the_network_egress():
    text = (settings.PROJECT_ROOT / "config/config.yaml").read_text(encoding="utf-8")
    speaker_section = text.split("speaker:")[1]
    assert "PRIVACY" in speaker_section
    assert "REPLY TEXT" in speaker_section and "Microsoft" in speaker_section
    assert "offline sends nothing" in speaker_section


def test_the_real_config_no_longer_advertises_a_spoken_stop():
    """It was true that nothing read the setting; it was not true that the feature existed."""
    from config.settings import get_setting
    assert get_setting("listener.voice_stop_enabled") is False
    text = (settings.PROJECT_ROOT / "config/config.yaml").read_text(encoding="utf-8")
    line = [one for one in text.splitlines() if "voice_stop_enabled" in one][0]
    assert "DEFERRED" in line
    assert "Ctrl+Alt+Backspace" in text.split("voice_stop_enabled")[1][:400]


def test_the_stale_phase_2_comments_are_gone():
    text = (settings.PROJECT_ROOT / "config/config.yaml").read_text(encoding="utf-8")
    assert "Phase 2 is not built yet" not in text
