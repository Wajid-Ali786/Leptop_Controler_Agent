"""
Offline regression tests for the spoken-stop measurement harness (Task 6c1).

No microphone, no speech model, no action: adapter.capture and adapter.transcribe are replaced by
fakes and the clock is a list of numbers the test owns, so the timing boundaries, the settings each
case uses, and the "nothing is retried" rule are all checked exactly rather than approximately.

These test the harness, because a measurement nobody can interpret is worse than no measurement: if
S2 silently left VAD on, or the real trials silently used automatic language detection, the numbers
would look fine and mean something else.
"""
import ast
import dataclasses
from pathlib import Path

import pytest

from app.listener import adapter, logic
from app.listener.models import (LIMIT, ListenerSettings, ModelStatus, Recording, Transcript,
                                 VoiceFailure)
from tests import stop_latency

SETTINGS = ListenerSettings(
    enabled=True, model_size="small", model_dir="data/models", local_files_only=True, device="auto",
    compute_type="auto", language="auto", sample_rate=16000, input_device="", vad_filter=True,
    min_silence_ms=800, max_utterance_seconds=15.0,
    initial_prompt_terms=("notepad", "calculator"), voice_stop_enabled=True)
HEARD = Transcript(" Stop.", language="en", language_probability=None, audio_seconds=1.5)


# --- Structural helpers: prose in a docstring is not code, so these read the syntax tree ------------

HARNESS_FILES = ("tests/stop_latency.py", "tests/test_stop_latency_real.py",
                 "tests/test_stop_latency_synthetic.py")


def _tree(path):
    return ast.parse(open(path, encoding="utf-8").read())


def _imports(path):
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            yield from ((alias.name, "") for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield from ((node.module or "", alias.name) for alias in node.names)


def _called(path):
    """Every name actually called: bare names and attribute names."""
    names = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
                if isinstance(node.func.value, ast.Name):
                    names.add(f"{node.func.value.id}.{node.func.attr}")
            elif isinstance(node.func, ast.Name):
                names.add(node.func.id)
    return names


def _names(path):
    return {node.id for node in ast.walk(_tree(path)) if isinstance(node, ast.Name)}


class Clock:
    def __init__(self, *times):
        self.times = list(times)
        self.last = 0.0

    def __call__(self):
        self.last = self.times.pop(0) if self.times else self.last + 1.0
        return self.last


class Ears:
    """Fake capture + transcribe, recording exactly what each was asked for."""

    def __init__(self, recording=None, heard=HEARD):
        self.recording = recording if recording is not None else Recording(b"\x11\x22" * 24000, LIMIT)
        self.heard = heard
        self.captures = []
        self.transcriptions = []

    # Bounded on purpose: a harness that retried a misheard trial would loop forever against a fake
    # that always mishears, and a hanging test proves nothing. This makes that defect FAIL, fast.
    LIMIT_CALLS = 6

    def capture(self, settings, *, max_seconds=None, cancel=None):
        self.captures.append(dict(settings=settings, max_seconds=max_seconds, cancel=cancel))
        if len(self.captures) > self.LIMIT_CALLS:
            raise AssertionError(f"more than {self.LIMIT_CALLS} recordings: something is retrying")
        return self.recording

    def transcribe(self, recording, settings):
        self.transcriptions.append(dict(recording=recording, settings=settings))
        return self.heard


@pytest.fixture
def ears(monkeypatch):
    fake = Ears()
    monkeypatch.setattr(adapter, "capture", fake.capture)
    monkeypatch.setattr(adapter, "transcribe", fake.transcribe)
    monkeypatch.setattr(adapter, "ensure_model",
                        lambda settings: ModelStatus("small", "cpu", "int8", True, None))
    return fake


# --- The synthetic clip ----------------------------------------------------------------------------

def test_the_synthetic_recording_is_one_deterministic_canonical_second():
    recording = stop_latency.silence()
    assert recording.frames == 16000 and recording.seconds == 1.0
    assert recording.sample_rate == 16000 and recording.channels == 1 and recording.dtype == "int16"
    assert recording.pcm == b"\x00\x00" * 16000 and set(recording.pcm) == {0}
    assert stop_latency.silence().pcm == recording.pcm, "the same bytes every time"


def test_all_three_cases_use_that_same_recording(ears):
    recording = stop_latency.silence()
    stop_latency.synthetic_measurements(SETTINGS, recording)
    assert len(ears.transcriptions) == 3
    assert all(one["recording"] is recording for one in ears.transcriptions)


# --- S1 / S2 / S3 ----------------------------------------------------------------------------------

def test_the_three_cases_are_exactly_this(ears):
    stop_latency.synthetic_measurements(SETTINGS, stop_latency.silence())
    asked = [(one["settings"].language, one["settings"].vad_filter) for one in ears.transcriptions]
    assert asked == [("en", True), ("en", False), ("auto", False)]


def test_s1_keeps_vad_on_and_asks_for_english(ears):
    s1 = stop_latency.synthetic_measurements(SETTINGS, stop_latency.silence())[0]
    assert s1.label == "S1" and s1.language == "en" and s1.vad_filter is True


def test_s2_turns_vad_off_and_asks_for_english(ears):
    """Without this, silence is removed before the encoder and the number is a floor for nothing."""
    s2 = stop_latency.synthetic_measurements(SETTINGS, stop_latency.silence())[1]
    assert s2.label == "S2" and s2.language == "en" and s2.vad_filter is False


def test_s3_differs_from_s2_only_by_language(ears):
    _, s2, s3 = stop_latency.synthetic_measurements(SETTINGS, stop_latency.silence())
    assert s3.label == "S3" and s3.language == "auto" and s3.vad_filter is False
    assert (s3.vad_filter, s3.audio_seconds) == (s2.vad_filter, s2.audio_seconds)


def test_the_configured_settings_are_never_modified(ears):
    before = dataclasses.asdict(SETTINGS)
    stop_latency.synthetic_measurements(SETTINGS, stop_latency.silence())
    stop_latency.stop_settings(SETTINGS)
    assert dataclasses.asdict(SETTINGS) == before
    assert SETTINGS.language == "auto" and SETTINGS.vad_filter is True


def test_the_synthetic_group_never_records(ears):
    stop_latency.synthetic_measurements(SETTINGS, stop_latency.silence())
    assert ears.captures == [], "no microphone is involved in the synthetic cases"


# --- Timing boundaries -----------------------------------------------------------------------------

def test_the_transcription_interval_is_exactly_the_transcribe_call(ears):
    clock = Clock(100.0, 103.5)
    measured = stop_latency.measure_transcription("probe", SETTINGS, stop_latency.silence(),
                                                  clock=clock)
    assert measured.transcription_seconds == 3.5


def test_the_preload_is_never_inside_a_measured_interval(ears, monkeypatch):
    """require_ready is its own call, so a load cannot land inside a measurement."""
    clock = Clock(10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0)
    loads = []
    monkeypatch.setattr(adapter, "ensure_model",
                        lambda settings: loads.append(clock()) or ModelStatus("small", "cpu", "int8",
                                                                              True, None))
    stop_latency.require_ready(SETTINGS)
    measured = stop_latency.synthetic_measurements(SETTINGS, stop_latency.silence(), clock=clock)
    assert loads == [10.0], "the load happened before anything was timed"
    assert [one.transcription_seconds for one in measured] == [1.0, 1.0, 1.0]


def test_a_real_trial_times_capture_transcription_match_and_total(ears):
    clock = Clock(0.0, 1.5, 21.5, 21.5, 21.6)   # t0, captured, transcribed, match start, matched
    measured = stop_latency.measure_stop_trial("trial 1", stop_latency.stop_settings(SETTINGS),
                                               write=lambda text: None, clock=clock)
    assert measured.capture_seconds == 1.5
    assert measured.transcription_seconds == 20.0
    assert measured.match_seconds == pytest.approx(0.1)
    assert measured.total_seconds == pytest.approx(21.6)


def test_the_cue_is_written_before_the_microphone_can_open(ears):
    order = []

    def capture(settings, *, max_seconds=None, cancel=None):
        order.append("microphone")
        return ears.recording
    adapter.capture = capture
    stop_latency.measure_stop_trial("trial 1", SETTINGS, write=lambda text: order.append(text),
                                    clock=Clock(*range(10)))
    assert order[0] == stop_latency.CUE and order[1] == "microphone"


# --- The real trials -------------------------------------------------------------------------------

def test_a_real_trial_captures_a_fixed_one_and_a_half_second_window(ears):
    stop_latency.measure_stop_trial("trial 1", stop_latency.stop_settings(SETTINGS),
                                    write=lambda text: None, clock=Clock(*range(10)))
    assert ears.captures[0]["max_seconds"] == 1.5
    assert stop_latency.STOP_WINDOW_SECONDS == 1.5


def test_a_real_trial_asks_for_english_and_keeps_everything_else(ears):
    listening = stop_latency.stop_settings(SETTINGS)
    stop_latency.measure_stop_trial("trial 1", listening, write=lambda text: None,
                                    clock=Clock(*range(10)))
    used = ears.transcriptions[0]["settings"]
    assert used.language == "en"
    assert used.vad_filter is SETTINGS.vad_filter is True, "VAD stays exactly as configured"
    assert used.min_silence_ms == SETTINGS.min_silence_ms
    assert used.initial_prompt_terms == SETTINGS.initial_prompt_terms
    assert "stop" not in used.initial_prompt_terms, '"stop" is never added to the prompt'
    assert (used.model_size, used.compute_type) == (SETTINGS.model_size, SETTINGS.compute_type)


@pytest.mark.parametrize("path", ["tests/stop_latency.py", "tests/test_stop_latency_real.py"])
def test_the_capture_is_synchronous_with_no_worker_no_thread_and_no_stdin(path):
    """Feature 2 already bounds the capture and releases the microphone, so a measurement needs no
    second cleanup architecture: no thread, no worker slot, no liveness state, no stdin."""
    imported = {module.split(".")[0] for module, _ in _imports(path)}
    assert "threading" not in imported and "app" not in (imported & {"app.voice_console"})
    assert not {"Thread", "input", "Event"} & _called(path)
    assert "is_alive" not in _called(path), "there is no worker whose liveness could be checked"
    assert not {"thread", "worker", "_worker", "voice_console"} & _names(path)
    assert "str.join" not in _called(path)  # ", ".join is fine; a thread join would not be


def test_three_independent_trials_and_nothing_is_retried(ears):
    measured = stop_latency.stop_trials(stop_latency.stop_settings(SETTINGS),
                                        write=lambda text: None, clock=Clock(*range(40)))
    assert len(measured) == 3 == len(ears.captures) == len(ears.transcriptions)
    assert [one.label for one in measured] == ["trial 1", "trial 2", "trial 3"]
    assert stop_latency.TRIALS == 3


def test_a_misheard_trial_is_data_and_is_not_repeated(ears):
    ears.heard = Transcript(" stop it", language="en")
    measured = stop_latency.stop_trials(stop_latency.stop_settings(SETTINGS),
                                        write=lambda text: None, clock=Clock(*range(40)))
    assert [one.stop_match for one in measured] == [False, False, False]
    assert len(ears.captures) == 3, "no extra recording was made to get a better answer"


def test_a_failed_trial_does_not_stop_the_others(ears):
    ears.recording = VoiceFailure("device_busy", "The microphone is in use.")
    measured = stop_latency.stop_trials(stop_latency.stop_settings(SETTINGS),
                                        write=lambda text: None, clock=Clock(*range(40)))
    assert len(measured) == 3 and ears.transcriptions == [], "a capture failure is never transcribed"
    assert all(one.kind == "device_busy" and one.stop_match is None for one in measured)


@pytest.mark.parametrize("failure", ["no_speech", "transcription_failed", "language_unsupported"])
def test_a_recognition_failure_is_recorded_without_a_match(ears, failure):
    ears.heard = VoiceFailure(failure, "something safe to show")
    measured = stop_latency.measure_stop_trial("trial 1", SETTINGS, write=lambda text: None,
                                               clock=Clock(*range(10)))
    assert measured.kind == failure and measured.stop_match is None and measured.characters == 0


# --- The match comes only from the approved pure matcher --------------------------------------------

@pytest.mark.parametrize("text, expected", [
    (" stop", True), (" Stop.", True), ("STOP!", True), (" stop,", True),
    (" stop it", False), (" please stop", False), (" stopped", False), (" nothing", False),
])
def test_stop_match_is_exactly_the_pure_rule(ears, text, expected):
    ears.heard = Transcript(text, language="en")
    measured = stop_latency.measure_stop_trial("trial 1", SETTINGS, write=lambda text: None,
                                               clock=Clock(*range(10)))
    assert measured.stop_match is expected is logic.is_stop_phrase(text)


def test_the_harness_has_no_matcher_of_its_own():
    """The boolean comes from the approved pure rule, reached through derive_transcript - the harness
    never compares a string to "stop" itself."""
    called = _called("tests/stop_latency.py")
    assert "is_stop_phrase" not in called, "no second copy of the rule"
    assert "derive_transcript" in called
    compared = [node for node in ast.walk(_tree("tests/stop_latency.py"))
                if isinstance(node, ast.Compare)
                and any(isinstance(side, ast.Constant) and side.value == "stop"
                        for side in node.comparators)]
    assert compared == [], "nothing here decides what counts as the stop phrase"


# --- Nothing is executed, nothing is triggered -------------------------------------------------------

@pytest.mark.parametrize("path", HARNESS_FILES)
def test_no_stop_guard_no_executor_and_no_emergency_stop(path):
    """Measurement only: nothing here can execute a command or trigger the emergency stop."""
    for module, name in _imports(path):
        assert "executor" not in module and "console" not in module, f"{path}: {module} {name}"
        assert name not in ("emergency_stop", "handle_command", "console"), f"{path}: {name}"
    assert not {"trigger", "handle_command", "execute", "execute_with_recovery", "authorize"} & _called(path)
    assert "emergency_stop" not in _names(path) and "StopGuard" not in _names(path)


def test_no_product_setting_is_changed(ears):
    """VAD-off exists only on the diagnostic copies, and only because the case table says so - the
    harness never hard-codes it, and the real trials pass the configured value through."""
    replaces = [node for node in ast.walk(_tree("tests/stop_latency.py"))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "replace"]
    assert len(replaces) == 2, "one copy for the synthetic cases, one for the stop settings"
    hard_coded = [keyword for node in replaces for keyword in node.keywords
                  if keyword.arg == "vad_filter" and isinstance(keyword.value, ast.Constant)]
    assert hard_coded == [], "vad_filter is only ever passed through, never written in"
    assert stop_latency.SYNTHETIC_CASES[1][2] is False and stop_latency.SYNTHETIC_CASES[0][2] is True


# --- Safe representations -----------------------------------------------------------------------------

def test_a_measurement_never_shows_what_was_said(ears):
    ears.heard = Transcript(" Zarqonimbus qwertyuiop", language="ur")
    measured = stop_latency.measure_stop_trial("trial 1", SETTINGS, write=lambda text: None,
                                               clock=Clock(*range(10)))
    for shown in (repr(measured), str(measured), f"{measured}", "%s" % (measured,)):
        assert "Zarqonimbus" not in shown and "qwertyuiop" not in shown
        assert "characters=23" in shown and "detected='ur'" in shown and "stop_match=False" in shown
    assert "notepad" not in repr(measured) and "data/models" not in repr(measured)


def test_a_measurement_carries_no_audio_or_prompt(ears):
    measured = stop_latency.measure_transcription("probe", SETTINGS, stop_latency.silence(),
                                                  clock=Clock(1.0, 2.0))
    assert not hasattr(measured, "pcm") and not hasattr(measured, "recording")
    assert "initial_prompt" not in repr(measured) and "model_dir" not in repr(measured)


def test_nothing_is_logged_by_the_harness(ears, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    ears.heard = Transcript(" Zarqonimbus", language="en")
    stop_latency.stop_trials(SETTINGS, write=lambda text: None, clock=Clock(*range(40)))
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "Zarqonimbus" not in logged
    assert "logging" not in open(stop_latency.__file__, encoding="utf-8").read()


# --- The encoder-pass instrumentation ------------------------------------------------------------------
# It is the only thing that makes a floor number interpretable, and it touches the CACHED model, so it
# must put that model back exactly as it found it - including when transcription raises.

class FakeModel:
    """A stand-in for the loaded WhisperModel. `encode` lives on the CLASS, so removing an instance
    attribute really does restore the original, exactly as it would on the real object."""

    def __init__(self):
        self.encoded = 0

    def encode(self, features):
        self.encoded += 1
        return f"encoded {features}"


def loaded(monkeypatch, model):
    monkeypatch.setattr(adapter, "_loaded", adapter._Loaded(("small",), model,
                                                            ModelStatus("small", "cpu", "int8",
                                                                        True, None)))
    return model


def test_the_counter_counts_encoder_passes_and_delegates(monkeypatch):
    model = loaded(monkeypatch, FakeModel())
    original = model.encode
    with stop_latency.counted_encoder_passes() as counter:
        assert model.encode("a") == "encoded a" and model.encode("b") == "encoded b"
        assert counter.passes == 2
    assert model.encoded == 2, "the real method really ran"
    assert "encode" not in vars(model), "no instrumentation left on the object"
    assert model.encode("c") == "encoded c" and model.encode.__func__ is original.__func__


@pytest.mark.parametrize("outcome", [Transcript(" stop", language="en"),
                                     VoiceFailure("no_speech", "nothing was said")])
def test_the_instrumentation_is_removed_whatever_transcription_returns(monkeypatch, outcome):
    model = loaded(monkeypatch, FakeModel())
    monkeypatch.setattr(adapter, "transcribe", lambda recording, settings: outcome)
    stop_latency.synthetic_measurements(SETTINGS, stop_latency.silence(),
                                        passes=stop_latency.counted_encoder_passes)
    assert "encode" not in vars(model), "nothing is left attached after any of the three cases"


def test_the_instrumentation_is_removed_when_transcription_raises(monkeypatch):
    """The case that matters most: a defect mid-measurement must not leave a wrapper on the cached
    model for every later transcription in the process."""
    model = loaded(monkeypatch, FakeModel())

    def explode(recording, settings):
        raise RuntimeError("something failed inside the recognizer")
    monkeypatch.setattr(adapter, "transcribe", explode)
    with pytest.raises(RuntimeError):
        stop_latency.synthetic_measurements(SETTINGS, stop_latency.silence(),
                                            passes=stop_latency.counted_encoder_passes)
    assert "encode" not in vars(model), "the finally clause put the model back"
    assert model.encode("a") == "encoded a", "and it still works"


def test_the_counter_is_harmless_when_no_model_is_loaded(monkeypatch):
    monkeypatch.setattr(adapter, "_loaded", None)
    with stop_latency.counted_encoder_passes() as counter:
        assert counter is None
    measured = stop_latency.measure_transcription("probe", SETTINGS, stop_latency.silence(),
                                                  clock=Clock(1.0, 2.0), counter=None)
    assert measured.encoder_passes is None, "not zero - unknown"


def test_the_installed_model_class_allows_this_instrumentation():
    """If WhisperModel used __slots__, an instance attribute could not be set and this whole approach
    would be invalid. Checked structurally, without loading a model."""
    import ast
    source = open(Path(adapter.__file__).parent.parent.parent / "venv" / "Lib" / "site-packages"
                  / "faster_whisper" / "transcribe.py", encoding="utf-8").read()
    tree = ast.parse(source)
    whisper = [node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == "WhisperModel"][0]
    assigned = {target.id for node in whisper.body if isinstance(node, ast.Assign)
                for target in node.targets if isinstance(target, ast.Name)}
    assert "__slots__" not in assigned, "instance attributes would be impossible"
    assert any(isinstance(node, ast.FunctionDef) and node.name == "encode" for node in whisper.body)


# --- Summaries ------------------------------------------------------------------------------------------

def test_the_summary_keeps_the_spread_and_the_outliers():
    made = [dataclasses.replace(stop_latency.Measurement("t", "en", True, 1.5, seconds, "transcript"),
                                total_seconds=seconds) for seconds in (10.0, 30.0, 11.0)]
    assert stop_latency.summarise(made, "total_seconds") == (10.0, 11.0, 30.0)
    line = stop_latency.summary_line(made, "total_seconds", "TOTAL detection")
    assert "min 10.00s" in line and "median 11.00s" in line and "max 30.00s" in line
    assert "average" not in line and "mean" not in line


def test_a_summary_of_nothing_says_so():
    assert stop_latency.summarise([], "total_seconds") is None
    assert "nothing measured" in stop_latency.summary_line([], "total_seconds", "capture")


# --- Both new switches are isolated -----------------------------------------------------------------------

@pytest.mark.parametrize("marker, variable, module", [
    ("real_stop_latency_synthetic", "RUN_REAL_STOP_LATENCY_SYNTHETIC_TEST",
     "tests.test_stop_latency_synthetic"),
    ("real_stop_latency", "RUN_REAL_STOP_LATENCY_TEST", "tests.test_stop_latency_real"),
])
def test_each_group_has_its_own_switch(marker, variable, module):
    import importlib
    from tests import conftest
    assert conftest.OPT_IN_GATES[marker][0] == variable
    others = [value for name, (value, _) in conftest.OPT_IN_GATES.items() if name != marker]
    assert variable not in others
    assert importlib.import_module(module).pytestmark.name == marker


@pytest.mark.parametrize("marker", ["real_stop_latency_synthetic", "real_stop_latency"])
def test_no_other_real_switch_can_enable_either_group(monkeypatch, marker):
    from tests import conftest

    class Item:
        def __init__(self):
            self.markers = []

        def get_closest_marker(self, name):
            return object() if name == marker else None

        def add_marker(self, added):
            self.markers.append(added)

    mine = conftest.OPT_IN_GATES[marker][0]
    for variable, _ in conftest.OPT_IN_GATES.values():
        monkeypatch.setenv(variable, "1")
    monkeypatch.delenv(mine, raising=False)
    item = Item()
    conftest.pytest_collection_modifyitems(None, [item])
    assert [added.name for added in item.markers] == ["skip"], f"{mine} is the only switch for it"


def test_the_two_new_groups_do_not_enable_each_other(monkeypatch):
    from tests import conftest
    assert (conftest.OPT_IN_GATES["real_stop_latency"][0]
            != conftest.OPT_IN_GATES["real_stop_latency_synthetic"][0])
    assert conftest.OPT_IN_GATES["real_stop_latency_synthetic"][0] != "RUN_REAL_MODEL_TEST"
