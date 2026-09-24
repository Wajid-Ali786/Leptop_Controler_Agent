"""
Tests for Phase 2 Feature 6 Task 6b1: starting voice mode, preloading the model, and the one
bounded recording - offline.

No microphone is opened, no speech model is loaded and no action runs: app.listener.adapter's
capture/transcribe/ensure_model are replaced, and the "microphone" is a thread that waits on the
same cancel event the real capture waits on. What is being tested is the LIFECYCLE - who is told
what, in which order, on which thread, and what happens to the audio when the user changes their
mind.
"""
import contextlib
import dataclasses
import threading

import pytest

import main
from app import console, voice_console
from app.listener import logic
from app.listener.models import (CAPTURE_UNAVAILABLE, DEVICE_BUSY, LANGUAGE_UNSUPPORTED, LIMIT,
                                 MODEL_UNAVAILABLE, NO_SPEECH, TRANSCRIPTION_FAILED,
                                 ListenerSettings, ModelStatus, Recording, Transcript, VoiceFailure)

SPOKEN = " open notepad"
SETTINGS = ListenerSettings(
    enabled=True, model_size="small", model_dir="data/models", local_files_only=True, device="auto",
    compute_type="auto", language="auto", sample_rate=16000, input_device="", vad_filter=True,
    min_silence_ms=800, max_utterance_seconds=4.0, initial_prompt_terms=("notepad",),
    voice_stop_enabled=True)
READY = ModelStatus(model_size="small", device="cpu", compute_type="int8", reused=False,
                    load_seconds=4.3)


class Screen:
    def __init__(self):
        self.lines = []

    def __call__(self, text=""):
        self.lines.append(str(text))

    @property
    def text(self):
        return "\n".join(self.lines)

    def index(self, fragment):
        for number, line in enumerate(self.lines):
            if fragment in line:
                return number
        raise AssertionError(f"{fragment!r} was never printed:\n{self.text}")


class Keys:
    """The user's keyboard. An entry may be a string, an exception to raise, or a callable to run
    first (so a test can let the recording finish before Enter arrives)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        while self.answers and callable(self.answers[0]) and not isinstance(self.answers[0], type):
            self.answers.pop(0)()
        if not self.answers:
            raise EOFError("the script ran out")
        answer = self.answers.pop(0)
        if isinstance(answer, type) and issubclass(answer, BaseException):
            raise answer()
        return answer


class Mic:
    """Stands in for adapter.capture(): a thread that behaves like a real bounded capture.

        wait  - records until the caller cancels (the normal case)
        cap   - returns at once, as a capture that reached its own maximum does
        hang  - ignores the cancel event entirely, so cleanup has to notice
        raise - a defect inside the capture primitive
    """

    def __init__(self, outcome=None, mode="wait"):
        self.outcome = outcome if outcome is not None else Recording(b"\x01\x02" * 1600, LIMIT)
        self.mode = mode
        self.calls = []
        self.entered = threading.Event()
        self.done = threading.Event()
        self.release = threading.Event()
        self.thread_names = []

    def __call__(self, settings, *, max_seconds=None, cancel=None):
        self.calls.append(dict(settings=settings, max_seconds=max_seconds, cancel=cancel))
        self.thread_names.append(threading.current_thread().name)
        self.entered.set()
        try:
            if self.mode == "wait":
                assert cancel is not None, "the real capture is always given a cancel event"
                cancel.wait(10)
            elif self.mode == "hang":
                self.release.wait(30)   # deliberately deaf to the cancel event
            elif self.mode == "raise":
                raise TypeError("a defect inside the capture primitive")
            return self.outcome
        finally:
            self.done.set()


@pytest.fixture
def voice(monkeypatch):
    """Voice mode with every backend replaced: settings, model, microphone and recognizer."""
    world = type("World", (), {})()
    world.settings = SETTINGS
    world.status = READY
    world.mic = Mic()
    world.transcribed = []
    world.heard = Transcript(SPOKEN, language="en", language_probability=0.6, audio_seconds=1.0)
    world.ran = []

    monkeypatch.setattr(logic, "listener_settings", lambda: world.settings)
    monkeypatch.setattr(voice_console.adapter, "ensure_model", lambda settings: world.status)
    monkeypatch.setattr(voice_console.adapter, "capture", lambda *a, **k: world.mic(*a, **k))

    def transcribe(recording, settings):
        world.transcribed.append(recording)
        return world.heard
    monkeypatch.setattr(voice_console.adapter, "transcribe", transcribe)

    def handle_command(text, confirm=None, offer_retry=None, focus=None):
        world.ran.append(text)
        return console.CommandReply(console.Status.RAN, "pretend it ran")
    monkeypatch.setattr(console, "handle_command", handle_command)
    monkeypatch.setattr(console, "hotkey_line", lambda: "Emergency stop: pretend it is active.")
    return world


def start(voice, *answers):
    screen, keys = Screen(), Keys(*answers)
    code = voice_console.run_voice_mode(read=keys, write=screen)
    return screen, keys, code


def speak(voice, *answers):
    """listen, let the recording end, then answer the acceptance prompt."""
    return start(voice, "listen", "", *answers, "exit")


# --- The CLI ------------------------------------------------------------------------------------

@pytest.fixture
def cli(monkeypatch):
    calls = []

    @contextlib.contextmanager
    def listening():
        calls.append("hotkey on")
        try:
            yield None
        finally:
            calls.append("hotkey off")
    monkeypatch.setattr(main.hotkey, "listening", listening)
    monkeypatch.setattr(main, "run_voice_mode", lambda: calls.append("voice") or 7)
    monkeypatch.setattr(main, "run_console", lambda: calls.append("typed") or 5)
    monkeypatch.setattr(main, "run_health_check", lambda check_claude=False: calls.append("health") or [])
    monkeypatch.setattr(main, "format_report", lambda results: "report")
    return calls


def test_the_voice_flag_runs_voice_mode_inside_the_hotkey(cli):
    assert main.main(["--voice"]) == 7
    assert cli == ["hotkey on", "voice", "hotkey off"], "the stop is live before voice mode starts"


def test_the_typed_console_is_unchanged(cli):
    assert main.main(["--console"]) == 5
    assert cli == ["hotkey on", "typed", "hotkey off"]


def test_the_default_health_check_is_unchanged(cli):
    assert main.main([]) == 0 and cli == ["health"], "no hotkey, no voice, no model"


@pytest.mark.parametrize("argv", [["--voice", "--console"], ["--voice", "--check-claude"]])
def test_the_modes_stay_mutually_exclusive(argv):
    with pytest.raises(SystemExit) as leaving:
        main.main(argv)
    assert leaving.value.code == 2


def test_main_stays_wiring_only():
    """No voice logic in the entry point: it calls run_voice_mode and nothing else."""
    import ast
    tree = ast.parse(open(main.__file__, encoding="utf-8").read())
    called = {node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
              for node in ast.walk(tree) if isinstance(node, ast.Call)}
    assert "run_voice_mode" in called
    for forbidden in ("capture", "transcribe", "ensure_model", "listener_settings"):
        assert forbidden not in called, f"main.py must not do voice work itself ({forbidden})"


# --- listener.enabled -------------------------------------------------------------------------------

def test_voice_mode_refuses_when_voice_is_switched_off(voice, monkeypatch):
    voice.settings = dataclasses.replace(SETTINGS, enabled=False)
    exploded = []
    for name in ("ensure_model", "capture", "transcribe", "_whisper", "_audio"):
        monkeypatch.setattr(voice_console.adapter, name,
                            lambda *a, **k: exploded.append(name) or pytest.fail(f"{name} was used"))
    screen, _, code = start(voice)
    assert code == 1 and voice_console.DISABLED in screen.lines
    assert "listener.enabled" in voice_console.DISABLED
    assert exploded == [], "no model, no microphone, no backend import"


def test_bad_settings_are_reported_before_anything_loads(voice, monkeypatch):
    from config.settings import SettingsError
    monkeypatch.setattr(logic, "listener_settings",
                        lambda: (_ for _ in ()).throw(SettingsError("Setting 'listener' is missing")))
    monkeypatch.setattr(voice_console.adapter, "ensure_model",
                        lambda settings: pytest.fail("the model must not be loaded"))
    screen, _, code = start(voice)
    assert code == 1 and "listener" in screen.text


# --- Preload ------------------------------------------------------------------------------------------

def test_the_model_is_prepared_before_the_prompt_and_before_any_microphone(voice):
    screen, _, _ = start(voice, "exit")
    assert screen.index(voice_console.PREPARING) < screen.index("Speech model ready")
    assert voice.mic.calls == [], "preparing the model opens no microphone"
    assert "4.3 s" in screen.text and "small on cpu (int8)" in screen.text


def test_the_preparation_message_promises_no_particular_duration():
    """~14 s is what this laptop measured, not a promise about anyone's machine."""
    for text in (voice_console.PREPARING, voice_console.READY, voice_console.REUSED):
        for number in ("14", "15", "about 15"):
            assert number not in text


def test_a_model_that_cannot_be_prepared_stops_voice_mode(voice):
    voice.status = VoiceFailure(MODEL_UNAVAILABLE, "The 'small' speech model isn't downloaded yet.")
    screen, keys, code = start(voice, "listen")
    assert code == 1 and voice.status.message in screen.lines
    assert voice.mic.calls == [] and keys.prompts == [], "no prompt, no recording"


def test_the_emergency_stop_line_is_shown_in_voice_mode(voice):
    screen, _, _ = start(voice, "exit")
    assert "Emergency stop" in screen.text


# --- The recording notice ------------------------------------------------------------------------------

def test_the_user_is_told_before_the_microphone_can_open(voice):
    order = []
    real = voice.mic.__call__

    def watched(*args, **kwargs):
        order.append("microphone")
        return real(*args, **kwargs)
    voice.mic.__call__ = watched
    screen, _, _ = speak(voice, "x")
    told = screen.index(voice_console.STARTED)
    assert told < screen.index(voice_console.STOPPED)
    assert voice_console.SPEAK_NOW in screen.lines
    assert "4 seconds" in screen.text, "the configured cap is shown"


def test_nothing_is_recorded_before_the_notice_is_printed(voice, monkeypatch):
    """The notice is printed by the main thread before the worker exists, so there is no window in
    which the microphone could be open without the user having been told."""
    screen, keys, seen = Screen(), Keys("listen", "", "x", "exit"), []

    def capture(*args, **kwargs):
        seen.append(list(screen.lines))   # what was on screen when the microphone was opened
        return voice.mic(*args, **kwargs)
    monkeypatch.setattr(voice_console.adapter, "capture", capture)
    voice_console.run_voice_mode(read=keys, write=screen)
    assert seen and voice_console.STARTED in seen[0], "the microphone opened after the notice"


def test_the_stopped_notice_follows_the_microphone_closing(voice):
    screen, _, _ = speak(voice, "x")
    assert voice.mic.done.is_set()
    assert screen.index(voice_console.STARTED) < screen.index(voice_console.STOPPED)


# --- Enter means "done speaking" -------------------------------------------------------------------------

def test_enter_cancels_the_capture_and_the_audio_is_recognized(voice):
    screen, _, _ = speak(voice, "a")
    assert voice.mic.calls[0]["cancel"].is_set(), "Enter asks the capture to end"
    assert voice.transcribed == [voice.mic.outcome], "the recording reached transcription"
    assert voice.ran == [SPOKEN], "and the accepted transcript reached the command pipeline"


def test_the_capture_runs_on_its_own_named_non_daemon_thread(voice):
    speak(voice, "x")
    assert voice.mic.thread_names == [voice_console.WORKER_NAME]
    assert voice.mic.thread_names[0] != threading.current_thread().name


def test_the_worker_is_never_a_daemon(voice):
    speak(voice, "x")
    assert voice_console._worker is None, "a finished worker is forgotten"


def test_the_terminal_stays_on_the_main_thread(voice):
    """No stdin reader thread: the prompt is read by the thread that started voice mode."""
    reading = []

    class Watching(Keys):
        def __call__(self, prompt=""):
            reading.append(threading.current_thread().name)
            return super().__call__(prompt)
    screen, keys = Screen(), Watching("listen", "", "x", "exit")
    voice_console.run_voice_mode(read=keys, write=screen)
    assert set(reading) == {threading.current_thread().name}


# --- A recording with no frames --------------------------------------------------------------------------

def test_an_empty_recording_is_still_handed_to_the_recognizer(voice):
    """Feature 4 owns the one no_speech rule. A second copy of it here could disagree with it."""
    voice.mic = Mic(outcome=Recording(b"", LIMIT))
    voice.heard = VoiceFailure(NO_SPEECH, "Nothing was said.")
    screen, _, _ = speak(voice, "exit")
    assert len(voice.transcribed) == 1 and voice.transcribed[0].frames == 0
    assert "Nothing was said." in screen.lines


def test_the_voice_console_has_no_no_speech_rule_of_its_own():
    import ast
    tree = ast.parse(open(voice_console.__file__, encoding="utf-8").read())
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "NO_SPEECH" not in names and "frames" not in attributes
    assert "is_silence" not in attributes and "is_silence" not in names


# --- Ctrl+C abandons the attempt ----------------------------------------------------------------------------

def test_ctrl_c_while_recording_throws_the_audio_away(voice):
    screen, _, code = start(voice, "listen", KeyboardInterrupt, "exit")
    assert voice.mic.calls[0]["cancel"].is_set(), "the capture is still asked to end"
    assert voice.mic.done.is_set(), "and the microphone is given back"
    assert voice.transcribed == [], "abandoned audio is never recognized"
    assert voice.ran == [] and voice_console.ABANDONED in screen.lines
    assert code == 0, "voice mode carries on"


def test_after_ctrl_c_the_prompt_comes_back(voice):
    screen, keys, _ = start(voice, "listen", KeyboardInterrupt, "listen", "", "x", "exit")
    assert len([line for line in screen.lines if line == voice_console.STARTED]) == 2
    assert len(voice.transcribed) == 1, "only the SECOND attempt was recognized"
    assert voice.ran == [], "and it was cancelled at the acceptance prompt"


# --- End of input leaves voice mode ---------------------------------------------------------------------------

def test_end_of_input_while_recording_abandons_and_leaves(voice):
    screen, _, code = start(voice, "listen", EOFError)
    assert voice.mic.calls[0]["cancel"].is_set() and voice.mic.done.is_set()
    assert voice.transcribed == [] and voice.ran == []
    assert voice_console.LEAVING in screen.lines and code == 0


def test_the_two_endings_are_not_the_same_thing():
    assert voice_console.ABANDONED != voice_console.LEAVING
    assert issubclass(voice_console.LeaveVoiceMode, Exception)
    assert not issubclass(voice_console.AttemptAbandoned, voice_console.LeaveVoiceMode)


# --- The maximum arriving before Enter --------------------------------------------------------------------------

def capped(voice):
    """A capture that ends on its own, before the user presses Enter."""
    voice.mic = Mic(mode="cap")
    return lambda: voice.mic.done.wait(5)


def test_a_recording_that_reaches_its_maximum_closes_the_microphone_at_once(voice):
    wait = capped(voice)
    screen, _, _ = start(voice, "listen", wait, "", "a", "exit")
    assert voice.mic.done.is_set()
    assert "the maximum of 4 seconds was reached" in screen.text
    assert voice_console.STOPPED in screen.text


def test_the_enter_after_a_capped_recording_continues_that_same_recording(voice):
    wait = capped(voice)
    screen, _, _ = start(voice, "listen", wait, "", "a", "exit")
    assert len(voice.mic.calls) == 1, "that Enter must never start a second recording"
    assert voice.transcribed == [voice.mic.outcome] and voice.ran == [SPOKEN]


def test_ctrl_c_after_a_capped_recording_still_abandons_it(voice):
    """The capture has already finished, so there is nothing left to cancel - but the user's intent
    is still to abandon the attempt, and a finished recording must not become un-abandonable."""
    wait = capped(voice)
    screen, _, code = start(voice, "listen", wait, KeyboardInterrupt, "exit")
    assert voice.transcribed == [] and voice.ran == []
    assert voice_console.ABANDONED in screen.lines and code == 0


def test_end_of_input_after_a_capped_recording_abandons_it_and_leaves(voice):
    wait = capped(voice)
    screen, _, code = start(voice, "listen", wait, EOFError)
    assert voice.transcribed == [] and voice.ran == []
    assert voice_console.LEAVING in screen.lines and code == 0


# --- A capture thread that will not finish -------------------------------------------------------------------------

@pytest.fixture
def quick_cleanup(monkeypatch):
    """Shorten only the waiting, never the checking."""
    monkeypatch.setattr(voice_console, "CLEANUP_SLACK_SECONDS", 0.05)
    monkeypatch.setattr(voice_console.adapter, "WATCHDOG_MARGIN_SECONDS", 0.05)


def test_a_capture_that_never_finishes_is_reported_not_assumed_clean(voice, quick_cleanup):
    voice.settings = dataclasses.replace(SETTINGS, max_utterance_seconds=0.05)
    voice.mic = Mic(mode="hang")
    try:
        screen, _, _ = start(voice, "listen", "", "exit")
        assert voice.transcribed == [], "nothing is recognized from a capture that did not finish"
        assert voice_console.STUCK in screen.text
        assert voice_console._worker is not None and voice_console._worker.thread.is_alive()
    finally:
        voice.mic.release.set()
        voice_console._worker.thread.join(10)


def test_no_second_recording_is_started_while_a_worker_is_still_alive(voice, quick_cleanup):
    voice.settings = dataclasses.replace(SETTINGS, max_utterance_seconds=0.05)
    voice.mic = Mic(mode="hang")
    try:
        screen, _, _ = start(voice, "listen", "", "listen", "exit")
        assert len(voice.mic.calls) == 1, "the microphone is never opened again"
        assert screen.text.count(voice_console.STUCK) == 2, "and it says so both times"
    finally:
        voice.mic.release.set()
        voice_console._worker.thread.join(10)


def test_a_stuck_worker_is_reported_as_the_microphone_being_busy():
    failure = voice_console._stuck()
    assert isinstance(failure, VoiceFailure) and failure.kind == DEVICE_BUSY
    assert "restart" in failure.message


def test_a_join_that_times_out_is_not_cleanup():
    """The rule this whole design turns on: liveness is checked, never inferred from a join."""
    import ast
    tree = ast.parse(open(voice_console.__file__, encoding="utf-8").read())
    stop = [node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "stop"][0]
    source = ast.dump(stop)
    assert "is_alive" in source, "stop() must decide on is_alive(), not on join() returning"
    daemons = [node for node in ast.walk(tree) if isinstance(node, ast.keyword)
               and node.arg == "daemon"]
    assert daemons == [], "the capture worker is never a daemon thread"


# --- What happens when something goes wrong -----------------------------------------------------------------------

def test_a_capture_failure_is_shown_and_never_recognized(voice):
    voice.mic = Mic(outcome=VoiceFailure(CAPTURE_UNAVAILABLE, "Microphone support isn't available."))
    screen, _, code = speak(voice, "exit")
    assert voice.transcribed == [], "a capture failure is never handed to the recognizer"
    assert "Microphone support isn't available." in screen.lines and code == 0


@pytest.mark.parametrize("kind, message", [
    (NO_SPEECH, "Nothing was said, or the recording held only background noise."),
    (TRANSCRIPTION_FAILED, "The speech model failed while turning that recording into text."),
])
def test_a_recognition_failure_comes_back_to_the_prompt(voice, kind, message):
    voice.heard = VoiceFailure(kind, message)
    screen, _, code = start(voice, "listen", "", "listen", "", "exit")
    assert screen.text.count(message) == 2, "it can simply be tried again"
    assert voice.ran == [] and code == 0


def test_an_unsupported_language_ends_the_session_instead_of_recording_again(voice):
    voice.heard = VoiceFailure(LANGUAGE_UNSUPPORTED,
                               "listener.language is 'pt-br', which the speech model does not know.")
    screen, _, code = start(voice, "listen", "", "listen", "", "exit")
    assert voice.heard.message in screen.lines
    assert voice_console.LANGUAGE_ENDS in screen.lines
    assert len(voice.mic.calls) == 1, "it never records again under settings known to fail"
    assert code == 0


def test_a_defect_inside_the_capture_primitive_propagates_after_cleanup(voice):
    voice.mic = Mic(mode="raise")
    with pytest.raises(TypeError):
        start(voice, "listen", "", "exit")
    assert voice.mic.done.is_set(), "the microphone was given back first"
    assert voice.transcribed == [] and voice.ran == []


# --- Privacy ---------------------------------------------------------------------------------------------------------

def test_nothing_spoken_or_recorded_reaches_the_log(voice, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    voice.heard = Transcript(" open notepad", language="en")
    speak(voice, "a")
    logged = "\n".join(record.getMessage() for record in caplog.records)
    for forbidden in ("notepad", "\\x01", "pcm"):
        assert forbidden not in logged.lower(), f"{forbidden!r} reached the log"


def test_the_recorder_never_shows_the_audio_it_holds(voice):
    recorder = voice_console._Recorder(SETTINGS, Screen())
    recorder.outcome = Recording(b"\x11\x22" * 100, LIMIT)
    for shown in (repr(recorder), str(recorder)):
        assert "\\x11" not in shown and "pcm" not in shown.lower()
        assert "capped=False" in shown and "Recording" in shown


def test_the_visible_lifecycle_is_always_printed(voice):
    screen, _, _ = speak(voice, "x")
    assert voice_console.STARTED in screen.lines and voice_console.STOPPED in screen.lines


# --- Boundaries -------------------------------------------------------------------------------------------------------

def test_the_real_voice_acceptance_has_its_own_switch():
    from tests import conftest
    variable, _ = conftest.OPT_IN_GATES["real_voice_console"]
    assert variable == "RUN_REAL_VOICE_CONSOLE_TEST"
    others = [value for name, (value, _) in conftest.OPT_IN_GATES.items()
              if name != "real_voice_console"]
    assert variable not in others


def test_no_other_real_switch_can_enable_it(monkeypatch):
    from tests import conftest

    class Item:
        def __init__(self):
            self.markers = []

        def get_closest_marker(self, name):
            return object() if name == "real_voice_console" else None

        def add_marker(self, marker):
            self.markers.append(marker)
    for variable, _ in conftest.OPT_IN_GATES.values():
        monkeypatch.setenv(variable, "1")
    monkeypatch.delenv("RUN_REAL_VOICE_CONSOLE_TEST", raising=False)
    item = Item()
    conftest.pytest_collection_modifyitems(None, [item])
    assert [marker.name for marker in item.markers] == ["skip"]


def test_no_stop_guard_exists_yet():
    source = open(voice_console.__file__, encoding="utf-8").read()
    assert "emergency_stop" not in source and "is_stop_phrase" not in source
    assert "trigger(" not in source
