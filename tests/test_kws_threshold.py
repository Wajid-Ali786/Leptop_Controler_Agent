"""
Offline rules for the keyword-THRESHOLD comparison (Task 6d1c).

Nothing here imports sherpa_onnx, opens a microphone, loads a model or touches the network. It checks
the things that must be true whether or not the comparison ever runs: that it is a ONE-VARIABLE
experiment, that the two conditions differ in exactly one detector setting, that a result measured at
one threshold can never be counted under the other, that the corpus is identical in both blocks, that
it is opt-in behind its OWN switch so the closed Task 6d1b baseline keeps its meaning, and that the
reporting stays truthful about what a keyword spotter can and cannot tell us.

The AST helpers and the fake device come from the 6d1b offline rules, so there is one copy of each.
"""
import ast
import importlib.util
from pathlib import Path

import pytest

from config import settings
from tests import test_kws_prototype_real as baseline
from tests import test_kws_threshold_real as comparison
from tests.test_kws_prototype import (FakeDevice, FakeSpotter, _assigned_attributes, _called, _imports,
                                      _property_names, _top_level_imports, _tree)

COMPARISON = Path("tests/test_kws_threshold_real.py")
BASELINE_HARNESS = Path("tests/test_kws_prototype_real.py")
MARKER = "real_kws_threshold"
OPT_IN = "RUN_REAL_KWS_THRESHOLD_TEST"


def _printed(path) -> str:
    """Everything the harness announces, as one string. Built from the call nodes, not from lines: a
    message that wraps over two lines is still one announce()."""
    said = []
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Call) and "announce" in ast.unparse(node.func):
            said.append(ast.unparse(node))
    return " ".join(said)


def _test_body(path, name="test_whether_a_lower_keyword_threshold_changes_standalone_stop_detection"):
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no function {name}")


def trial(condition, label, prompt, expected, keyword="", peak=0.2, detect=1.0, overflows=0):
    """A finished trial, built without a microphone."""
    made = baseline.Trial(label, prompt, expected, condition)
    made.keyword = keyword
    made.peak, made.samples_fed, made.sum_of_squares = peak, 1600, 1600 * peak * peak
    made.overflows = overflows
    made.cleanup_ok = True
    if keyword:
        made.detect_seconds = detect
    made.listen_seconds = detect if keyword else comparison.LISTEN_SECONDS
    return made


def corpus(condition, detections=(), fired=()):
    """One condition's 15 trials. `detections` are the positive numbers that fired, `fired` the
    negative prompts that fired."""
    made = []
    for number, prompt in enumerate(comparison.POSITIVES, start=1):
        made.append(trial(condition, f"positive {number}", prompt, comparison.POSITIVE,
                          keyword="STOP" if number in detections else ""))
    for number, prompt in enumerate(comparison.NEGATIVES, start=1):
        made.append(trial(condition, f"negative {number}", prompt, comparison.NEGATIVE,
                          keyword="STOP" if prompt in fired else ""))
    return made


# --- Exactly two conditions, and exactly one variable --------------------------------------------------

def test_there_are_exactly_two_threshold_conditions():
    assert comparison.CONDITIONS == (("BASELINE", 0.25), ("LOW_THRESHOLD", 0.10))
    assert comparison.ORDER == ("BASELINE", "LOW_THRESHOLD"), "the baseline setting is measured first"
    assert comparison.threshold_for("BASELINE") == 0.25
    assert comparison.threshold_for("LOW_THRESHOLD") == 0.10
    assert isinstance(comparison.CONDITIONS, tuple), "frozen: a run cannot edit the conditions"


def test_the_baseline_condition_is_the_setting_the_closed_baseline_actually_ran():
    """0.25 is not a guess - it is what Task 6d1b measured, so the comparison has a real anchor."""
    assert comparison.threshold_for("BASELINE") == baseline.KEYWORDS_THRESHOLD


def test_only_the_threshold_differs_between_the_two_conditions():
    assert comparison.varying_settings() == {"keywords_threshold"}
    first = comparison._settings(0.25)
    second = comparison._settings(0.10)
    assert set(first) == set(second)
    for name in first:
        if name != "keywords_threshold":
            assert first[name] == second[name], f"{name} differs between conditions"


def test_the_locked_detector_settings_are_the_agreed_values():
    settings_used = comparison._settings(0.25)
    assert settings_used["keywords_score"] == 1.0 == baseline.KEYWORDS_SCORE
    assert settings_used["num_trailing_blanks"] == 1 == comparison.NUM_TRAILING_BLANKS
    assert settings_used["max_active_paths"] == 4 == comparison.MAX_ACTIVE_PATHS
    assert settings_used["num_threads"] == 1 and settings_used["provider"] == "cpu"


def test_the_pinned_settings_are_the_installed_librarys_own_defaults():
    """They are passed explicitly so neither condition can inherit a different default - but the values
    must still BE the defaults, or the comparison would no longer sit on the baseline evidence.

    Read from the installed source file with ast; sherpa_onnx is never imported here."""
    spec = importlib.util.find_spec("sherpa_onnx")
    if spec is None or not spec.origin:
        pytest.skip("sherpa-onnx is not installed on this machine (prototype dependency)")
    source = Path(spec.origin).with_name("keyword_spotter.py")
    if not source.is_file():
        pytest.skip("the installed sherpa-onnx has no keyword_spotter.py")
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ClassDef) and node.name == "KeywordSpotter":
            init = next(inner for inner in node.body
                        if isinstance(inner, ast.FunctionDef) and inner.name == "__init__")
            break
    else:
        raise AssertionError("no KeywordSpotter class in the installed package")
    arguments = init.args.args[1:] + init.args.kwonlyargs
    defaults = dict(zip([argument.arg for argument in arguments][-len(init.args.defaults):],
                        [ast.literal_eval(default) for default in init.args.defaults]))
    assert defaults["keywords_score"] == comparison._settings(0.25)["keywords_score"]
    assert defaults["num_trailing_blanks"] == comparison.NUM_TRAILING_BLANKS
    assert defaults["max_active_paths"] == comparison.MAX_ACTIVE_PATHS
    assert defaults["keywords_threshold"] == comparison.threshold_for("BASELINE"), (
        "the BASELINE condition is the library default threshold")


def test_nothing_is_tuned_while_a_run_is_in_progress():
    """The thresholds are read from the frozen tuple. No assignment, no arithmetic, no search."""
    tree = _tree(COMPARISON)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript):
                    assert "CONDITIONS" not in ast.unparse(target), "the conditions are not edited"
        if isinstance(node, ast.AugAssign):
            assert "threshold" not in ast.unparse(node.target).lower(), "no threshold arithmetic"
    called = _called(COMPARISON)
    for tuning in ("update", "sort", "append_threshold", "optimize", "search", "tune"):
        assert tuning not in called or tuning == "append", f"{tuning} looks like tuning"
    body = ast.unparse(_test_body(COMPARISON))
    assert "threshold_for(condition)" in body, "the value comes from the frozen table, per condition"
    built = [node for node in ast.walk(_test_body(COMPARISON)) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "_settings"]
    assert len(built) == 1, "the detector is built from one place, with one argument set"


# --- 30 trials, the same corpus in both blocks --------------------------------------------------------

def test_the_schedule_is_thirty_trials_in_two_labelled_blocks():
    schedule = list(comparison._schedule())
    assert len(schedule) == 30 == comparison.TOTAL_TRIALS
    conditions = [condition for condition, _, _, _ in schedule]
    assert conditions == ["BASELINE"] * 15 + ["LOW_THRESHOLD"] * 15, "one block each, in order"


def test_both_blocks_use_exactly_the_same_corpus():
    schedule = list(comparison._schedule())
    first = [(label, prompt, expected) for condition, label, prompt, expected in schedule
             if condition == "BASELINE"]
    second = [(label, prompt, expected) for condition, label, prompt, expected in schedule
              if condition == "LOW_THRESHOLD"]
    assert first == second, "the two conditions must face identical trials"
    assert [prompt for _, prompt, _ in first] == list(comparison.POSITIVES) + list(comparison.NEGATIVES)


def test_the_corpus_is_the_closed_baselines_corpus_unchanged():
    assert comparison.POSITIVES is baseline.POSITIVES == ("stop",) * 5
    assert comparison.NEGATIVES is baseline.NEGATIVES
    assert comparison.NEGATIVES == ("(say nothing at all)", "notepad", "stopped", "stopping", "stops",
                                    "stopper", "stop it", "please stop", "full stop",
                                    "open notepad and type hello")
    assert len(comparison.NEGATIVES) == 10


def test_the_streaming_shape_is_the_closed_baselines():
    assert comparison.SAMPLE_RATE == 16000 and comparison.CHUNK_SECONDS == 0.1
    assert comparison.LISTEN_SECONDS == 4.0
    for name in ("SAMPLE_RATE", "CHUNK_SECONDS", "LISTEN_SECONDS"):
        assert getattr(comparison, name) == getattr(baseline, name)


def test_the_startup_text_says_how_many_trials_there_will_be():
    printed = _printed(COMPARISON)
    assert "TRIALS IN TOTAL" in printed
    assert "CONDITION" in printed and "keywords_threshold" in printed
    assert "ONLY difference is the threshold" in printed


# --- A result may never be read under the wrong threshold ---------------------------------------------

def test_every_trial_carries_its_condition_explicitly():
    made = trial("LOW_THRESHOLD", "positive 1", "stop", comparison.POSITIVE, keyword="STOP")
    assert made.condition == "LOW_THRESHOLD"
    assert "LOW_THRESHOLD | positive 1" in repr(made), "the repr names the condition"
    assert "condition" in _assigned_attributes(BASELINE_HARNESS, "Trial")
    body = ast.unparse(_test_body(COMPARISON))
    assert "condition=condition" in body, "the condition is handed to every trial, not inferred later"


def test_a_baseline_harness_trial_is_unlabelled_and_reads_exactly_as_before():
    """The closed 6d1b harness has one configuration, so its own output must not grow a condition."""
    made = baseline.Trial("positive 1", "stop", baseline.POSITIVE)
    assert made.condition is None
    assert repr(made).startswith("Trial(positive 1, positive, prompt='stop'")
    assert "|" not in repr(made)


def test_selecting_a_conditions_trials_cannot_leak_the_other_condition():
    trials = corpus("BASELINE", detections=(1,)) + corpus("LOW_THRESHOLD", detections=(1, 2, 3, 4))
    for condition in comparison.ORDER:
        chosen = comparison.trials_of(condition, trials)
        assert len(chosen) == 15
        assert {one.condition for one in chosen} == {condition}


def test_an_outcome_refuses_a_trial_from_another_condition():
    trials = corpus("BASELINE") + [trial("LOW_THRESHOLD", "positive 1", "stop", comparison.POSITIVE)]
    with pytest.raises(AssertionError):
        comparison.Outcome("BASELINE", trials)


def test_each_conditions_counts_come_only_from_its_own_trials():
    trials = (corpus("BASELINE", detections=(1,), fired=("stops", "stopper", "stop it"))
              + corpus("LOW_THRESHOLD", detections=(1, 2, 3, 4), fired=("stops",)))
    low, high = (comparison.Outcome(condition, comparison.trials_of(condition, trials))
                 for condition in comparison.ORDER)
    assert (low.threshold, len(low.detected), len(low.missed)) == (0.25, 1, 4)
    assert low.false_positive_prompts == ["stops", "stopper", "stop it"]
    assert (high.threshold, len(high.detected), len(high.missed)) == (0.10, 4, 1)
    assert high.false_positive_prompts == ["stops"]


def test_the_two_summaries_never_print_the_other_conditions_numbers(capsys):
    trials = (corpus("BASELINE", detections=(1,), fired=("stopper",))
              + corpus("LOW_THRESHOLD", detections=(1, 2, 3, 4, 5), fired=("stops", "stop it")))
    outcomes = [comparison.Outcome(condition, comparison.trials_of(condition, trials))
                for condition in comparison.ORDER]
    for outcome in outcomes:
        comparison._describe(outcome)
    blocks = capsys.readouterr().out.split("BASELINE: keywords_threshold")[1].split(
        "LOW_THRESHOLD: keywords_threshold")
    assert "detected 1/5" in blocks[0] and "'stopper'" in blocks[0]
    assert "detected 5/5" not in blocks[0] and "'stops'" not in blocks[0]
    assert "detected 5/5" in blocks[1] and "'stops'" in blocks[1]
    assert "'stopper'" not in blocks[1]


def test_the_comparison_prints_both_thresholds_and_names_no_winner(capsys):
    trials = (corpus("BASELINE", detections=(1,), fired=("stops",))
              + corpus("LOW_THRESHOLD", detections=(1, 2, 3), fired=("stops", "stop it")))
    comparison._compare([comparison.Outcome(condition, comparison.trials_of(condition, trials))
                         for condition in comparison.ORDER])
    shown = capsys.readouterr().out
    assert "threshold 0.25" in shown and "threshold 0.1" in shown
    assert "1/5" in shown and "3/5" in shown
    assert "picks no winner" in shown and "approves nothing" in shown
    for verdict in ("better", "worse", "optimal", "optimized", "recommend", "should use",
                    "production"):
        assert verdict not in shown.lower(), f"the benchmark reports evidence only: {verdict!r}"


# --- The audio diagnostics from Part A stay, and stay privacy-safe -------------------------------------

def test_the_audio_level_diagnostics_are_reported_per_trial(capsys):
    trials = corpus("BASELINE", detections=(1,))
    comparison._describe(comparison.Outcome("BASELINE", trials))
    shown = capsys.readouterr().out
    assert "peak" in shown and "rms" in shown and "overflows" in shown
    assert "s fed" in shown, "the audio duration fed is reported too"
    assert "open" in shown.lower(), "device-open timing is reported"


def test_a_near_silent_spoken_trial_is_flagged_but_still_counted(capsys):
    """Item 11: flag it visibly, never quietly drop it from the headline counts."""
    trials = corpus("BASELINE")
    trials[2].peak = 0.0            # positive 3 received nothing audible
    outcome = comparison.Outcome("BASELINE", trials)
    assert [one.label for one in outcome.silent_spoken] == ["positive 3"]
    assert len(outcome.positives) == 5 and len(outcome.missed) == 5, "still counted, not excluded"
    comparison._describe(outcome)
    shown = capsys.readouterr().out
    assert "NOTHING AUDIBLE" in shown and "STILL counted" in shown
    assert "positive 3" in shown and "peak 0.000" in shown


def test_the_trial_that_is_meant_to_be_silent_is_not_flagged():
    trials = corpus("BASELINE")
    for one in trials:
        if one.prompt == comparison.SILENCE_PROMPT:
            one.peak = 0.0
    outcome = comparison.Outcome("BASELINE", trials)
    assert outcome.silent_spoken == [], "'say nothing at all' is supposed to be silent"


def test_no_audio_is_kept_by_the_comparison():
    made = trial("BASELINE", "positive 1", "stop", comparison.POSITIVE)
    for name, value in vars(made).items():
        assert not hasattr(value, "shape"), f"{name} holds an array"
        assert not isinstance(value, (bytes, bytearray, memoryview)), f"{name} holds raw audio"
    source = Path(COMPARISON).read_text(encoding="utf-8")
    for forbidden in ("wavfile", "write_wav", "soundfile", "np.save", "open(", "to_disk"):
        assert forbidden not in source, f"{forbidden!r} could persist audio"


def test_the_device_path_is_recorded_without_the_device_name():
    """Item 17: host API, rate, channels and latency only. A device name is private."""
    for node in ast.walk(_tree(COMPARISON)):
        if isinstance(node, ast.FunctionDef) and node.name == "_device_path":
            body = ast.unparse(node)
            for wanted in ("hostapi", "default_samplerate", "max_input_channels",
                           "default_high_input_latency", "default_low_input_latency"):
                assert wanted in body, f"{wanted} must be recorded"
            assert "info['name']" not in body and 'info["name"]' not in body, (
                "the DEVICE name is private; only the host API's name is printed")
            assert "{info[" not in body, "no device field is interpolated straight into the output"
            assert "InputStream" not in body and "rec(" not in body, "it opens nothing"
            break
    else:
        raise AssertionError("no _device_path")
    printed = _printed(COMPARISON)
    assert "audio path" in printed or "_device_path" in Path(COMPARISON).read_text(encoding="utf-8")


def test_the_device_path_is_described_but_never_changed():
    source = Path(COMPARISON).read_text(encoding="utf-8")
    for tuning in ("sounddevice.default.device =", "default.latency =", "default.samplerate =",
                   "latency=", "hostapi="):
        assert tuning not in source, f"{tuning!r} would change the audio path - not this experiment"


# --- The reporting fixes from Part A are still true ---------------------------------------------------

def test_the_prompt_field_is_still_truthful():
    assert "prompt" in _assigned_attributes(BASELINE_HARNESS, "Trial")
    assert "said" not in _assigned_attributes(BASELINE_HARNESS, "Trial")
    printed = _printed(COMPARISON)
    assert "not speech transcription" in printed
    assert "not what the microphone heard" in printed
    assert "no detection" in printed


def test_no_result_field_claims_recognised_speech():
    fields = (_assigned_attributes(BASELINE_HARNESS, "Trial")
              | _property_names(BASELINE_HARNESS, "Trial"))
    for claim in ("said", "heard", "transcript", "text", "recognised", "recognized", "speech",
                  "utterance", "words"):
        assert claim not in fields, f"{claim!r} would imply transcription"
    outcome_fields = _assigned_attributes(COMPARISON, "Outcome")
    for claim in ("said", "heard", "transcript", "speech"):
        assert claim not in outcome_fields


def test_no_transcription_was_introduced():
    for module, name in _imports(COMPARISON):
        first = module.split(".")[0]
        assert first not in ("faster_whisper", "whisper", "vosk", "speech_recognition"), module
        assert name not in ("transcribe", "derive_transcript", "Transcript")
    for absent in ("transcribe", "derive_transcript", "WhisperModel", "get_close_matches",
                   "SequenceMatcher"):
        assert absent not in _called(COMPARISON), absent


# --- Opt-in, isolated, and it does not redefine the closed baseline -----------------------------------

def test_the_comparison_has_its_own_new_switch():
    from tests import conftest
    assert comparison.pytestmark.name == MARKER
    variable, _ = conftest.OPT_IN_GATES[MARKER]
    assert variable == OPT_IN
    others = [value for name, (value, _) in conftest.OPT_IN_GATES.items() if name != MARKER]
    assert variable not in others, "no other switch shares this variable"
    assert variable != conftest.KWS_OPT_IN, "the baseline switch must not enable the comparison"


def test_the_closed_baseline_harness_keeps_its_own_marker_and_switch():
    """Task 6d1b's evidence stays interpretable: nothing about its switch or marker moved."""
    from tests import conftest
    assert baseline.pytestmark.name == "real_kws_latency"
    assert conftest.OPT_IN_GATES["real_kws_latency"][0] == "RUN_REAL_KWS_TEST"
    assert conftest.KWS_OPT_IN == "RUN_REAL_KWS_TEST"
    assert baseline.KEYWORDS_THRESHOLD == 0.25 and baseline.KEYWORDS_SCORE == 1.0
    source = Path(BASELINE_HARNESS).read_text(encoding="utf-8")
    assert "num_trailing_blanks" not in source and "max_active_paths" not in source, (
        "the closed baseline still passes the library defaults implicitly, exactly as it ran")


def test_no_other_real_switch_can_enable_the_comparison(monkeypatch):
    from tests import conftest

    class Item:
        def __init__(self):
            self.markers = []

        def get_closest_marker(self, name):
            return object() if name == MARKER else None

        def add_marker(self, marker):
            self.markers.append(marker)

    for variable, _ in conftest.OPT_IN_GATES.values():
        monkeypatch.setenv(variable, "1")
    monkeypatch.delenv(OPT_IN, raising=False)
    item = Item()
    conftest.pytest_collection_modifyitems(None, [item])
    assert [marker.name for marker in item.markers] == ["skip"]


def test_the_comparison_is_skipped_by_default(pytestconfig):
    import os
    assert os.environ.get(OPT_IN) != "1", "this suite must never run with the real switch set"


def test_importing_the_comparison_opens_nothing():
    import sys
    module_level = {module.split(".")[0] for module, _ in _top_level_imports(COMPARISON)}
    assert not {"sherpa_onnx", "sounddevice", "numpy"} & module_level, module_level
    inside = {module.split(".")[0] for module, _ in _imports(COMPARISON)}
    assert {"sherpa_onnx", "sounddevice"} <= inside, "they are used, just not at import time"
    assert "sherpa_onnx" not in sys.modules, "importing this test file must not load the library"


def test_the_comparison_never_acts():
    """No action, no command, no emergency stop - not even an import of them."""
    for module, name in _imports(COMPARISON):
        assert "executor" not in module, f"{module} {name}"
        assert "safety" not in module and "verifier" not in module, f"{module} {name}"
        assert name not in ("emergency_stop", "handle_command", "console")
        assert module != "app.console"
    assert not {"trigger", "handle_command", "execute", "execute_with_recovery",
                "authorize"} & _called(COMPARISON)


def test_the_only_production_module_it_touches_is_the_microphone_ownership_rule():
    production = {module for module, _ in _imports(COMPARISON) if module.startswith("app")}
    assert production == {"app.listener"}, production
    assert ("app.listener", "microphone") in list(_imports(COMPARISON))


def test_no_production_module_imports_the_comparison_or_sherpa():
    root = settings.PROJECT_ROOT
    offenders = []
    for path in (*(root / "app").rglob("*.py"), *(root / "config").rglob("*.py"),
                 *(root / "scripts").rglob("*.py"), root / "main.py"):
        for module, name in _imports(path):
            if module.split(".")[0] == "sherpa_onnx" or "kws_threshold" in module:
                offenders.append(str(path.relative_to(root)))
    assert offenders == [], offenders


def test_the_comparison_downloads_nothing():
    for module, name in _imports(COMPARISON):
        first = module.split(".")[0]
        assert first not in ("urllib", "requests", "http", "httpx", "huggingface_hub", "socket"), module
    for absent in ("urlretrieve", "urlopen", "download", "snapshot_download", "hf_hub_download", "get"):
        if absent == "get":
            continue
        assert absent not in _called(COMPARISON), absent
    source = Path(COMPARISON).read_text(encoding="utf-8")
    assert "http://" not in source and "https://" not in source


# --- Cleanup safety is unchanged and aborts the WHOLE comparison ---------------------------------------

def test_the_comparison_reuses_the_one_copy_of_the_cleanup_rules():
    """It must not grow a second trial loop: the ownership, close-proof and abort rules live once."""
    called = _called(COMPARISON)
    assert "_listen" in called or "baseline._listen" in called
    source = Path(COMPARISON).read_text(encoding="utf-8")
    assert "def _listen" not in source and "def _release" not in source
    assert "InputStream" not in source, "it never opens a device itself"
    assert "Thread" not in source and "daemon" not in source, "no worker, no thread"
    harness_source = Path(BASELINE_HARNESS).read_text(encoding="utf-8")
    assert "close(ignore_errors=False)" in harness_source
    assert 'getattr(stream, "closed"' in harness_source
    assert "microphone.owner() is not None" in harness_source


def test_a_cleanup_failure_cancels_the_rest_of_the_comparison_including_the_other_condition():
    """Not just the remaining trials of this block: the whole run stops, so no further stream opens."""
    body = _test_body(COMPARISON)
    inner_breaks = outer_break = False
    for node in ast.walk(body):
        if isinstance(node, ast.For) and ast.unparse(node.target) == "condition":
            outer = ast.unparse(node)
            outer_break = "if aborted is not None:" in outer and outer.rstrip().endswith("break")
        if isinstance(node, ast.If) and "not trial.cleanup_ok" in ast.unparse(node.test):
            inner_breaks = any(isinstance(inner, ast.Break) for inner in node.body)
    assert inner_breaks, "an unproven cleanup must break out of the trial loop"
    assert outer_break, "and the condition loop must stop too, so the other block never starts"
    asserted = " ".join(ast.unparse(node) for node in ast.walk(body) if isinstance(node, ast.Assert))
    assert "aborted is None" in asserted and "trial.cleanup_ok for trial in trials" in asserted
    assert "ABORTED" in _printed(COMPARISON) and "cancelled" in _printed(COMPARISON)


def test_a_trial_error_with_proven_cleanup_still_lets_the_run_continue(monkeypatch):
    """Category A is unchanged: the shared machinery records the failure and the device is provably
    gone, so the next trial (in either block) may start."""
    import sys
    from app.listener import microphone
    import numpy
    numpy.zeros(1)
    device = FakeDevice()
    monkeypatch.setitem(sys.modules, "sounddevice", device)
    monkeypatch.setattr(baseline, "LISTEN_SECONDS", 0.3)
    monkeypatch.setattr(baseline, "CHUNK_SECONDS", 0.01)
    microphone.reset()
    spotter = FakeSpotter(fires_on_chunk=None, fail="result")

    class Stream:
        def accept_waveform(self, rate, samples):
            spotter.chunks += 1

    monkeypatch.setattr(spotter, "create_stream", lambda: Stream())
    made = baseline._listen(spotter, "positive 1", "stop", baseline.POSITIVE,
                            condition="LOW_THRESHOLD")
    assert made.condition == "LOW_THRESHOLD", "the condition survives an error"
    assert isinstance(made.error, RuntimeError) and made.cleanup_ok is True
    assert microphone.owner() is None


def test_no_trial_is_retried_in_either_condition():
    source = Path(COMPARISON).read_text(encoding="utf-8")
    assert "retry" not in source.lower()
    loops = [node for node in ast.walk(_test_body(COMPARISON))
             if isinstance(node, (ast.For, ast.While))]
    assert not [node for node in loops if isinstance(node, ast.While)], "no retry loop"


# --- The whole two-block flow, driven offline -----------------------------------------------------------
# 30 spoken trials is a lot to ask of a person, so the entire comparison is exercised here first with a
# fake detector and a fake device: no microphone, no sherpa_onnx, no model. This is what would have
# caught a labelling or summary bug before the real sitting rather than after it.

@pytest.fixture
def dry_run(monkeypatch):
    """Run the real comparison function against fakes. Returns (run, seen) where `seen` collects every
    trial in the order it happened."""
    import sys
    import types
    import numpy
    from app.listener import microphone

    numpy.zeros(1)   # never pay numpy's first import inside a timed loop
    monkeypatch.setitem(sys.modules, "sounddevice", FakeDevice(fail="loud"))
    monkeypatch.setattr(baseline, "LISTEN_SECONDS", 0.02)   # chunk size stays real: 1600 samples
    microphone.reset()
    seen = []

    def run(fires, cleanup_fails_at=None):
        """`fires` maps a threshold to the prompts that trigger at it. `cleanup_fails_at` is a
        (condition, label) pair whose cleanup is reported as unprovable."""

        class Spotter:
            def __init__(self, keywords_threshold=None, **rest):
                self.fires = fires[keywords_threshold]
                self.prompt = None

            def create_stream(self):
                return types.SimpleNamespace(accept_waveform=lambda rate, samples: None)

            def is_ready(self, stream):
                return False

            def decode_stream(self, stream):
                pass

            def get_result(self, stream):
                return "STOP" if self.prompt in self.fires else ""

            def tokens(self, stream):
                return ["a", "b", "c"]

            def timestamps(self, stream):
                return [0.1, 0.2, 0.3]

            def reset_stream(self, stream):
                pass

        library = types.ModuleType("sherpa_onnx")
        library.KeywordSpotter = Spotter
        monkeypatch.setitem(sys.modules, "sherpa_onnx", library)
        original = baseline._listen

        def listen(spotter, label, prompt, expected, condition=None):
            spotter.prompt = prompt
            made = original(spotter, label, prompt, expected, condition)
            if cleanup_fails_at == (condition, label):
                made.cleanup_ok, made.cleanup_error = False, "FATAL simulated cleanup failure"
            seen.append(made)
            return made

        monkeypatch.setattr(baseline, "_listen", listen)
        comparison.test_whether_a_lower_keyword_threshold_changes_standalone_stop_detection(
            baseline.MODEL)

    return run, seen


def test_the_whole_comparison_runs_thirty_trials_in_two_labelled_blocks(dry_run, capsys):
    run, seen = dry_run
    run({0.25: {"stop"}, 0.10: {"stop", "stops", "stopper"}})
    assert len(seen) == 30
    assert [made.condition for made in seen] == ["BASELINE"] * 15 + ["LOW_THRESHOLD"] * 15
    assert [made.label for made in seen[:15]] == [made.label for made in seen[15:]]
    shown = capsys.readouterr().out
    assert "30 TRIALS IN TOTAL" in shown
    assert "CONDITION 1 OF 2: BASELINE" in shown and "CONDITION 2 OF 2: LOW_THRESHOLD" in shown
    assert "[BASELINE | positive 1: positive] SAY: stop" in shown
    assert "[LOW_THRESHOLD | positive 1: positive] SAY: stop" in shown
    from app.listener import microphone
    assert microphone.owner() is None


def test_the_two_blocks_results_stay_apart_in_the_real_run(dry_run, capsys):
    """The same prompt fires in one block and not the other - the summaries must not blur that."""
    run, seen = dry_run
    run({0.25: {"stop"}, 0.10: {"stop", "stops", "stopper"}})
    shown = capsys.readouterr().out
    # Anchor on the SUMMARY headers, which name the trial count - the startup banner mentions the
    # conditions too, and splitting on that would mix the two.
    first = "BASELINE: keywords_threshold = 0.25 (15 trials)"
    second = "LOW_THRESHOLD: keywords_threshold = 0.1 (15 trials)"
    assert first in shown and second in shown
    low = shown.split(first)[1].split(second)[0]
    assert "detected 5/5" in low, "every 'stop' prompt fired at 0.25 in this fake"
    assert "false positives 0/10" in low and "none fired" in low
    high = shown.split(second)[1]
    assert "false positives 2/10" in high
    assert "'stops'" in high and "'stopper'" in high
    side = shown.split("SIDE BY SIDE")[1]
    assert "threshold 0.25" in side and "threshold 0.1" in side
    assert side.index("threshold 0.25") < side.index("threshold 0.1"), "baseline is shown first"


def test_a_cleanup_failure_in_the_first_block_means_the_second_never_runs(dry_run, capsys):
    run, seen = dry_run
    with pytest.raises(AssertionError, match="cleanup could not be proven"):
        run({0.25: {"stop"}, 0.10: {"stop"}},
            cleanup_fails_at=("BASELINE", "positive 3"))
    assert [made.label for made in seen] == ["positive 1", "positive 2", "positive 3"]
    assert {made.condition for made in seen} == {"BASELINE"}, "LOW_THRESHOLD never started"
    shown = capsys.readouterr().out
    assert "COMPARISON ABORTED during BASELINE after positive 3" in shown
    assert "cancelled" in shown
    assert "CONDITION 2 OF 2" not in shown, "no second block was even announced"
    from app.listener import microphone
    assert microphone.owner() is None, "the microphone was given back before the run failed"


def test_a_near_silent_spoken_trial_survives_into_the_real_runs_report(dry_run, capsys):
    """The fake device can deliver silence; the flag must reach the printed report, still counted."""
    import sys
    run, seen = dry_run
    monkeypatch_device = FakeDevice()          # silence, not the 0.5 tone
    sys.modules["sounddevice"] = monkeypatch_device
    with pytest.raises(AssertionError):        # nothing fires, so the run's own checks are irrelevant
        run({0.25: set(), 0.10: set()}, cleanup_fails_at=("BASELINE", "positive 1"))
    shown = capsys.readouterr().out
    assert "NOTHING AUDIBLE" in shown and "STILL counted" in shown
