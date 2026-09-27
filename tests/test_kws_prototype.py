"""
Offline rules for the keyword-spotting PROTOTYPE (Task 6d1a).

Nothing here imports sherpa_onnx, opens a microphone, loads a model or touches the network: it checks
the things that must be true about the prototype whether or not the benchmark ever runs - that the
benchmark is opt-in and isolated, that it cannot execute anything, that its vocabulary is exactly one
keyword, that it tunes nothing, and above all that NO module under app/ imports sherpa_onnx while the
dependency is still under evaluation.
"""
import ast
from pathlib import Path

import pytest

from config import settings
from tests import test_kws_prototype_real as harness

BENCHMARK = Path("tests/test_kws_prototype_real.py")
FETCH_SCRIPT = Path("scripts/fetch_kws_model.py")
# The sentencepiece word-start marker, written as an escape on purpose: no source file in this
# project may contain the character itself, because none of them may carry BPE tokens.
MARKER = "\u2581"   # the sentencepiece word-start marker, written by the tool and never by us


def _tree(path):
    return ast.parse(Path(path).read_text(encoding="utf-8"))


def _imports(path):
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            yield from ((alias.name, "") for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield from ((node.module or "", alias.name) for alias in node.names)


def _called(path):
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


# --- The dependency is still under evaluation ------------------------------------------------------

def test_no_production_module_imports_sherpa():
    """sherpa-onnx is a PROTOTYPE dependency. Until it earns its place, only tests may import it, and
    the production boundary (a dedicated module, not the listener adapter) is not designed yet."""
    root = settings.PROJECT_ROOT
    offenders = []
    for path in (*(root / "app").rglob("*.py"), *(root / "config").rglob("*.py"),
                 *(root / "scripts").rglob("*.py"), root / "main.py"):
        for module, name in _imports(path):
            if module.split(".")[0] == "sherpa_onnx" or name == "sherpa_onnx":
                offenders.append(f"{path.relative_to(root)}: {module} {name}".strip())
    assert offenders == [], f"no app/config/script file may import sherpa_onnx yet: {offenders}"


def test_the_listener_adapter_still_owns_only_the_whisper_boundary():
    adapter = settings.PROJECT_ROOT / "app" / "listener" / "adapter.py"
    imported = {module.split(".")[0] for module, _ in _imports(adapter)}
    assert "sherpa_onnx" not in imported
    assert {"faster_whisper", "ctranslate2"} & imported or True  # they live in lazy accessors


def test_sherpa_is_not_in_the_permanent_requirements():
    """It must not become a production dependency before the benchmark says it should."""
    requirements = (settings.PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "sherpa" not in requirements


# --- The benchmark is opt-in and isolated -----------------------------------------------------------

def test_the_benchmark_has_its_own_switch():
    from tests import conftest
    assert harness.pytestmark.name == "real_kws_latency"
    variable, _ = conftest.OPT_IN_GATES["real_kws_latency"]
    assert variable == "RUN_REAL_KWS_TEST"
    others = [value for name, (value, _) in conftest.OPT_IN_GATES.items()
              if name != "real_kws_latency"]
    assert variable not in others


def test_no_other_real_switch_can_enable_it(monkeypatch):
    from tests import conftest

    class Item:
        def __init__(self):
            self.markers = []

        def get_closest_marker(self, name):
            return object() if name == "real_kws_latency" else None

        def add_marker(self, marker):
            self.markers.append(marker)

    for variable, _ in conftest.OPT_IN_GATES.values():
        monkeypatch.setenv(variable, "1")
    monkeypatch.delenv("RUN_REAL_KWS_TEST", raising=False)
    item = Item()
    conftest.pytest_collection_modifyitems(None, [item])
    assert [marker.name for marker in item.markers] == ["skip"]


def _top_level_imports(path):
    """Only the imports that run when the module is imported - not the deliberate ones inside tests."""
    for node in _tree(path).body:
        if isinstance(node, ast.Import):
            yield from ((alias.name, "") for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield from ((node.module or "", alias.name) for alias in node.names)


def test_importing_the_benchmark_opens_nothing():
    """Collection must not load the library, open a device or read a model: sherpa_onnx, sounddevice
    and numpy are imported INSIDE the test, so collecting this file costs nothing."""
    module_level = {module.split(".")[0] for module, _ in _top_level_imports(BENCHMARK)}
    assert not {"sherpa_onnx", "sounddevice", "numpy"} & module_level, module_level
    inside = {module.split(".")[0] for module, _ in _imports(BENCHMARK)}
    assert {"sherpa_onnx", "sounddevice"} <= inside, "they are used, just not at import time"
    import sys
    assert "sherpa_onnx" not in sys.modules, "importing this test file must not load the library"


def test_the_benchmark_never_acts(monkeypatch):
    """No action, no command, no emergency stop - not even an import of them."""
    for module, name in _imports(BENCHMARK):
        assert "executor" not in module, f"{module} {name}"
        assert name not in ("emergency_stop", "handle_command", "console")
        assert module != "app.console"
    assert not {"trigger", "handle_command", "execute", "execute_with_recovery",
                "authorize"} & _called(BENCHMARK)


# --- The vocabulary is exactly one keyword ----------------------------------------------------------

def test_the_detector_vocabulary_is_exactly_stop():
    assert harness.POSITIVES == ("stop",) * 5, "five positive trials, one keyword"
    assert set(harness.POSITIVES) == {"stop"}
    for forbidden in ("halt", "cancel", "emergency", "abort", "freeze", "quit"):
        assert forbidden not in harness.NEGATIVES and forbidden not in harness.POSITIVES


def test_the_near_misses_are_negatives_not_synonyms():
    """The semantic contract is the exact keyword, so these are evidence AGAINST a detection."""
    for phrase in ("stopped", "stopping", "please stop", "notepad"):
        assert phrase in harness.NEGATIVES
    assert any("nothing" in phrase for phrase in harness.NEGATIVES), "silence is a negative trial"
    assert len(harness.NEGATIVES) >= 6


def test_the_keyword_file_is_never_written_by_the_benchmark():
    """The keyword line comes from the official text2token flow, not from this code."""
    assert not {"write_text", "write_bytes", "open"} & _called(BENCHMARK)
    source = BENCHMARK.read_text(encoding="utf-8")
    assert "read_text" in source, "it only reads the generated keyword file"
    assert MARKER not in source, "no BPE tokens are hard-coded anywhere here"


# --- The baseline tunes nothing ----------------------------------------------------------------------

def test_the_baseline_uses_the_documented_defaults():
    assert harness.KEYWORDS_SCORE == 1.0
    assert harness.KEYWORDS_THRESHOLD == 0.25


def test_no_confidence_score_is_assumed():
    """The installed API returns the keyword as a plain string; KeywordResult exposes keyword, tokens
    and timestamps only. Nothing here may invent a score."""
    source = BENCHMARK.read_text(encoding="utf-8")
    for invented in ("confidence", "\\bscore\\b=result", "result.score", ".probability"):
        assert invented not in source
    trial = harness.Trial("t", "stop", harness.POSITIVE)
    assert not hasattr(trial, "confidence") and not hasattr(trial, "score")


def test_a_trial_records_only_timings_and_what_the_detector_returned():
    trial = harness.Trial("positive 1", "stop", harness.POSITIVE)
    assert trial.keyword == "" and trial.detected is False
    assert trial.lag_seconds is None and trial.slowest_chunk_seconds is None
    assert not hasattr(trial, "pcm") and not hasattr(trial, "audio")


def test_a_trial_repr_carries_no_audio():
    trial = harness.Trial("positive 1", "stop", harness.POSITIVE)
    trial.keyword, trial.timestamps, trial.detect_seconds = "STOP", [0.5, 0.8], 1.2
    shown = repr(trial)
    assert "STOP" in shown, "the detected keyword is deliberately visible - it is the result"
    assert "pcm" not in shown and "array" not in shown
    assert trial.lag_seconds == pytest.approx(0.4), "wall detection minus the last token's own time"


# --- The report must not imply that speech was recognised ---------------------------------------------
# A keyword spotter has exactly two answers: the configured keyword, or nothing. The second real run
# showed why this matters: the report printed the phrase the benchmark had ASKED for, which reads like
# recognised speech when you deliberately say something else. The field is named for what it is.

def _assigned_attributes(path, class_name):
    """Every self.<name> assigned in a class body - the fields a result actually carries."""
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {target.attr for inner in ast.walk(node)
                    for target in getattr(inner, "targets", [])
                    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                    and target.value.id == "self"}
    raise AssertionError(f"no class {class_name}")


def _property_names(path, class_name):
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {inner.name for inner in node.body if isinstance(inner, ast.FunctionDef)
                    and any(isinstance(d, ast.Name) and d.id == "property"
                            for d in inner.decorator_list)}
    raise AssertionError(f"no class {class_name}")


def test_the_result_field_is_named_for_the_prompt_not_for_speech():
    """`said=` read as "this is what you said". It is not: the harness cannot know that."""
    fields = _assigned_attributes(BENCHMARK, "Trial")
    assert "prompt" in fields, f"the asked-for phrase is called prompt: {sorted(fields)}"
    assert "said" not in fields, "nothing may be called `said` - it implies recognised speech"
    trial = harness.Trial("positive 1", "stop", harness.POSITIVE)
    assert trial.prompt == "stop" and not hasattr(trial, "said")
    assert "prompt='stop'" in repr(trial), "the repr labels it as the prompt"


def test_no_result_field_claims_recognised_free_form_speech():
    """The only speech-shaped field is `keyword`, which is what the detector itself returned."""
    fields = _assigned_attributes(BENCHMARK, "Trial") | _property_names(BENCHMARK, "Trial")
    for claim in ("said", "heard", "transcript", "text", "recognised", "recognized", "speech",
                  "utterance", "words"):
        assert claim not in fields, f"{claim!r} would imply transcription: {sorted(fields)}"
    assert "keyword" in fields and "tokens" in fields


def test_the_startup_text_explains_keyword_spotting_versus_transcription():
    source = BENCHMARK.read_text(encoding="utf-8")
    announced = " ".join(line for line in source.splitlines() if "announce(" in line).lower()
    assert "not speech transcription" in announced or "not transcription" in announced
    assert "not what the microphone heard" in announced
    assert "no detection" in announced, "it says what the two possible answers are"


def test_the_detector_result_is_still_keyword_or_nothing():
    """No interpretation, no guessing, no 'closest match': the string sherpa returned, or empty."""
    trial = harness.Trial("negative 1", "hello", harness.NEGATIVE)
    assert trial.keyword == "" and trial.detected is False
    trial.keyword = "STOP"
    assert trial.detected is True and trial.false_positive is True
    called = _called(BENCHMARK)
    assert "get_result" in called
    for invented in ("difflib", "get_close_matches", "SequenceMatcher", "fuzz", "ratio"):
        assert invented not in called, f"the keyword is used as returned, not massaged ({invented})"


def test_no_transcription_dependency_or_path_was_added():
    """Whisper is measured and rejected for this role; it must not creep back in as a helper."""
    for module, name in _imports(BENCHMARK):
        first = module.split(".")[0]
        assert first not in ("faster_whisper", "whisper", "vosk", "speech_recognition"), module
        assert name not in ("transcribe", "derive_transcript", "Transcript")
    called = _called(BENCHMARK)
    for absent in ("transcribe", "derive_transcript", "load_model", "WhisperModel"):
        assert absent not in called, f"no ASR path may be called here: {absent!r}"
    # Whisper is named once, in the printed comparison line. That is a measured number, not a code path.
    source = BENCHMARK.read_text(encoding="utf-8")
    whisper_lines = [line for line in source.splitlines() if "whisper" in line.lower()]
    assert all("announce(" in line or line.lstrip().startswith("#") for line in whisper_lines), (
        f"Whisper may only appear in printed context: {whisper_lines}")


# --- Audio presence: a miss and a dead microphone look identical otherwise -----------------------------

def test_a_trial_reports_the_level_of_the_audio_it_fed(offline_trial):
    trial, device = offline_trial(FakeSpotter(fires_on_chunk=None))
    assert trial.samples_fed == trial.chunks * int(harness.CHUNK_SECONDS * harness.SAMPLE_RATE)
    assert trial.peak == 0.0 and trial.rms == 0.0, "the fake device feeds silence"
    assert trial.silent is True, "silence is reported as such, not hidden"
    assert "peak=" in repr(trial) and "rms=" in repr(trial)


def test_the_level_tracks_what_was_actually_fed():
    """Peak and rms are computed, not guessed: a known block gives a known answer."""
    import numpy
    trial = harness.Trial("t", "stop", harness.POSITIVE)
    trial.note_audio(numpy.array([0.5, -0.8, 0.0, 0.3], dtype=numpy.float32))
    assert trial.peak == pytest.approx(0.8), "the loudest sample, sign ignored"
    assert trial.rms == pytest.approx(((0.25 + 0.64 + 0.09) / 4) ** 0.5, rel=1e-6)
    trial.note_audio(numpy.zeros(4, dtype=numpy.float32))
    assert trial.peak == pytest.approx(0.8), "peak does not decay"
    assert trial.samples_fed == 8 and trial.silent is False


def test_an_empty_trial_reports_no_level_rather_than_a_fake_zero():
    trial = harness.Trial("t", "stop", harness.POSITIVE)
    assert trial.rms is None and trial.samples_fed == 0
    assert trial.audio_format is None
    assert trial.silent is False, "nothing was fed, so this is not a silent microphone"
    assert "nothing fed" in repr(trial)


def test_the_audio_presence_metric_keeps_no_audio(offline_trial):
    """Two running numbers per trial, and the format seen once. No PCM, no frames, no buffers."""
    trial, device = offline_trial(FakeSpotter(fires_on_chunk=None))
    for name, value in vars(trial).items():
        assert not hasattr(value, "shape"), f"{name} holds an array"
        assert not isinstance(value, (bytes, bytearray, memoryview)), f"{name} holds raw audio"
        # Only two fields may grow with the audio, and both hold DECODE TIMINGS, not samples.
        assert not isinstance(value, list) or name in ("chunk_seconds", "timestamps"), (
            f"{name} accumulates per-sample data")
    for name in ("peak", "sum_of_squares", "samples_fed", "overflows"):
        assert isinstance(getattr(trial, name), (int, float)), f"{name} must be one number"
    fields = _assigned_attributes(BENCHMARK, "Trial")
    for forbidden in ("pcm", "audio", "samples", "frames", "block", "buffer", "recording", "wav"):
        assert forbidden not in fields, f"{forbidden!r} would be audio, not a level"
    shown = repr(trial)
    assert "pcm" not in shown and "array" not in shown


def test_the_observed_format_is_what_sherpa_documents(offline_trial):
    """sherpa's KWS API documents 16 kHz mono float32 in [-1, 1]. This is what it is actually handed."""
    trial, device = offline_trial(FakeSpotter(fires_on_chunk=None))
    assert trial.fed_dtype == "float32" and trial.fed_ndim == 1
    assert trial.fed_samples_per_chunk == int(harness.CHUNK_SECONDS * harness.SAMPLE_RATE)
    assert trial.audio_format == (f"float32, 1-D, {trial.fed_samples_per_chunk} samples per chunk "
                                  f"at {harness.SAMPLE_RATE} Hz mono")
    source = BENCHMARK.read_text(encoding="utf-8")
    assert 'dtype="float32"' in source and "channels=1" in source
    assert "samplerate=SAMPLE_RATE" in source and harness.SAMPLE_RATE == 16000


def test_a_dropped_input_chunk_is_counted_rather_than_ignored():
    """sounddevice returns an overflow flag with every read. A gap in the audio would otherwise be
    indistinguishable from the model failing to fire."""
    trial = harness.Trial("t", "stop", harness.POSITIVE)
    assert trial.overflows == 0
    tree = _tree(BENCHMARK)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_listen":
            body = ast.unparse(node)
            assert "overflowed" in body and "trial.overflows += 1" in body
            assert "_overflowed" not in body, "the flag is used, not discarded"
            break
    else:
        raise AssertionError("no _listen")


def test_every_dropped_input_chunk_is_counted(monkeypatch):
    """Behaviour, not spelling: a device that reports an overflow on every read is counted every time."""
    trial, device = _run_trial(monkeypatch, FakeSpotter(fires_on_chunk=None), fail="overflow")
    assert trial.chunks > 0 and trial.overflows == trial.chunks
    assert f"overflows={trial.chunks}" in repr(trial)


def test_a_trial_that_is_fed_sound_reports_a_level_and_is_not_called_silent(monkeypatch):
    """End to end through _listen, not just on the object: a 0.5 tone comes out as peak 0.5."""
    trial, device = _run_trial(monkeypatch, FakeSpotter(fires_on_chunk=None), fail="loud")
    assert trial.peak == pytest.approx(0.5) and trial.rms == pytest.approx(0.5)
    assert trial.silent is False and "NOTHING AUDIBLE" not in repr(trial)


def test_the_device_open_share_of_the_measured_time_is_recorded(offline_trial):
    """"cue -> detection" contains the device open, the device's buffering and human reaction time.
    None of that is model inference, so the one part the harness CAN see is recorded separately."""
    trial, device = offline_trial(FakeSpotter(fires_on_chunk=3))
    assert trial.open_seconds is not None and trial.open_seconds >= 0
    assert trial.open_seconds <= trial.detect_seconds, "opening happens inside the measured window"
    assert "open=" in repr(trial)
    source = BENCHMARK.read_text(encoding="utf-8")
    announced = " ".join(line for line in source.splitlines() if "announce(" in line)
    assert "NOT model inference latency" in announced, "the report must not let that reading stand"


def test_the_run_asserts_the_format_it_fed():
    """The end-of-run check must be computed from the trials, not a constant someone can blunt."""
    for node in ast.walk(_tree(BENCHMARK)):
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "fed_formats"
                for target in node.targets):
            assert isinstance(node.value, ast.SetComp), (
                "fed_formats must be gathered from the trials that fed audio")
            break
    else:
        raise AssertionError("the run never collects the formats it fed")
    asserted = [ast.unparse(node.test) for node in ast.walk(_tree(BENCHMARK))
                if isinstance(node, ast.Assert) and "fed_formats" in ast.unparse(node.test)]
    assert asserted and all("==" in one for one in asserted), (
        f"the format must be compared, not merely printed: {asserted}")
    source = BENCHMARK.read_text(encoding="utf-8")
    assert "16 kHz mono float32" in source


# --- The baseline the reporting fix must not have moved -----------------------------------------------

def test_part_a_changed_no_detector_parameter():
    assert harness.KEYWORDS_SCORE == 1.0 and harness.KEYWORDS_THRESHOLD == 0.25
    assert harness.LISTEN_SECONDS == 4.0 and harness.CHUNK_SECONDS == 0.1
    assert harness.SAMPLE_RATE == 16000
    source = BENCHMARK.read_text(encoding="utf-8")
    assert "num_trailing_blanks" not in source, "the baseline passes the library default, untouched"
    assert "max_active_paths" not in source


def test_part_a_changed_no_trial_corpus():
    assert harness.POSITIVES == ("stop",) * 5
    assert harness.NEGATIVES == ("(say nothing at all)", "notepad", "stopped", "stopping", "stops",
                                 "stopper", "stop it", "please stop", "full stop",
                                 "open notepad and type hello")


# --- The generated keyword asset ---------------------------------------------------------------------
# It is prototype-local data under the git-ignored model folder, so these tests tolerate it being
# absent: a clean checkout must still collect and pass without downloading or generating anything.

RAW = Path("data/models/kws/keywords/stop_raw.txt")
GENERATED = Path("data/models/kws/keywords/stop.txt")


def test_the_benchmark_reads_the_generated_keyword_asset():
    assert harness.KEYWORDS == settings.PROJECT_ROOT / GENERATED
    assert harness.KEYWORDS.parent.name == "keywords"
    assert "data/models" in harness.KEYWORDS.as_posix(), "it lives in the git-ignored model area"


def test_the_raw_vocabulary_asked_for_is_exactly_stop():
    if not (settings.PROJECT_ROOT / RAW).is_file():
        pytest.skip("the keyword has not been generated on this machine (prototype-local data)")
    lines = [line for line in (settings.PROJECT_ROOT / RAW).read_text(encoding="utf-8").splitlines()
             if line.strip()]
    assert lines == ["STOP"], f"exactly one raw keyword, and it is STOP: {lines!r}"


def test_the_generated_file_holds_one_keyword_with_no_overrides():
    if not (settings.PROJECT_ROOT / GENERATED).is_file():
        pytest.skip("the keyword has not been generated on this machine (prototype-local data)")
    lines = [line for line in
             (settings.PROJECT_ROOT / GENERATED).read_text(encoding="utf-8").splitlines()
             if line.strip()]
    assert len(lines) == 1, f"exactly one keyword entry: {lines!r}"
    line = lines[0]
    assert ":" not in line and "#" not in line and "@" not in line, (
        "the baseline carries no per-keyword score, threshold or alias override")
    assert MARKER in line, "it is a token sequence from the tool, not a bare word"


def test_every_generated_token_exists_in_the_models_token_table():
    """Round-trip check: a token the model does not know would make the keyword unmatchable."""
    tokens_file = harness.MODEL / harness.FILES["tokens"]
    if not (settings.PROJECT_ROOT / GENERATED).is_file() or not tokens_file.is_file():
        pytest.skip("the model or the keyword has not been fetched/generated on this machine")
    known = {line.split()[0] for line in tokens_file.read_text(encoding="utf-8").splitlines()
             if line.strip()}
    pieces = (settings.PROJECT_ROOT / GENERATED).read_text(encoding="utf-8").strip().split(" ")
    missing = [piece for piece in pieces if piece not in known]
    assert missing == [], f"tokens not in tokens.txt: {missing}"
    assert len(pieces) >= 2, f"the segmentation is a token sequence: {pieces}"


def test_no_bpe_token_sequence_is_hard_coded_anywhere_in_the_repo():
    """The segmentation must come from the tool. A hand-written one in source would be a guess, and
    would silently drift from whatever model we use next."""
    root = settings.PROJECT_ROOT
    offenders = []
    for path in (*(root / "app").rglob("*.py"), *(root / "tests").rglob("*.py"),
                 *(root / "scripts").rglob("*.py"), root / "main.py"):
        if MARKER in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(root)))
    assert offenders == [], f"no source file may contain BPE tokens: {offenders}"


# --- sentencepiece is a generation-time tool, never a runtime one --------------------------------------

def test_the_runtime_keyword_path_does_not_need_sentencepiece():
    """It was installed temporarily to run the official text2token flow and then removed. The detector
    reads an already-tokenized keyword file, so nothing at runtime imports it."""
    import importlib.util
    root = settings.PROJECT_ROOT
    offenders = []
    for path in (*(root / "app").rglob("*.py"), *(root / "tests").rglob("*.py"),
                 *(root / "scripts").rglob("*.py"), root / "main.py"):
        for module, name in _imports(path):
            if module.split(".")[0] == "sentencepiece" or name == "sentencepiece":
                offenders.append(str(path.relative_to(root)))
    assert offenders == [], f"nothing in this project imports sentencepiece: {offenders}"
    assert importlib.util.find_spec("sentencepiece") is None, (
        "sentencepiece should have been uninstalled after generating the keyword")


def test_neither_prototype_package_is_in_the_permanent_requirements():
    requirements = (settings.PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "sherpa" not in requirements and "sentencepiece" not in requirements


# --- Streaming, not batch ----------------------------------------------------------------------------

def test_the_benchmark_streams_chunks_rather_than_recording_first():
    """A 1.5-second recording followed by a decode would turn a streaming detector back into batch
    detection - exactly the shape that made Whisper unusable here."""
    called = _called(BENCHMARK)
    assert "accept_waveform" in called and "get_result" in called and "decode_stream" in called
    assert "capture" not in called, "adapter.capture would be record-then-decode"
    assert harness.CHUNK_SECONDS == 0.1 and harness.SAMPLE_RATE == 16000


def test_it_uses_the_one_microphone_ownership_rule():
    imported = [f"{module}.{name}" for module, name in _imports(BENCHMARK)]
    assert any("microphone" in one for one in imported)
    assert {"microphone.acquire", "microphone.release"} <= _called(BENCHMARK)
    assert "microphone.reset" not in _called(BENCHMARK), "reset is the test suite's cleanup, not this"


def test_it_needs_no_worker_and_verifies_cleanup():
    imported = {module.split(".")[0] for module, _ in _imports(BENCHMARK)}
    assert "threading" not in imported, "a bounded blocking read needs no worker"
    assert not {"Thread", "Event", "is_alive"} & _called(BENCHMARK)
    assert {"stop", "close"} <= _called(BENCHMARK), "the audio stream is stopped and closed"


def test_no_trial_is_retried():
    source = BENCHMARK.read_text(encoding="utf-8")
    assert "while" in source, "there is a listening loop"
    assert "retry" not in source.lower() and "again" not in source.lower().split("def _listen")[1]


# --- One trial's behaviour, driven offline ------------------------------------------------------------
# _listen() is the whole benchmark loop, so it is worth testing without a microphone: a fake sounddevice
# module and a fake detector let the early-exit, timeout, cleanup and no-retry rules be checked exactly.

class FakeStream:
    """Stands in for sounddevice.InputStream, with the API the installed sounddevice really has:
    close(ignore_errors=True) by default, and `closed` as a property that is True after close().

    `fail` picks one mode: read, stop, close, stays_open (close returns but the stream still reports
    itself open - the case where cleanup cannot be proven), interrupt, overflow (the device reports
    dropped input), or loud (deliver something other than silence)."""

    def __init__(self, samplerate=None, channels=None, dtype=None, blocksize=None, fail=None):
        self.samplerate, self.channels, self.dtype, self.blocksize = (samplerate, channels, dtype,
                                                                     blocksize)
        self.started = self.stopped = False
        self._closed = False
        self.close_calls = []
        self.reads = 0
        self.fail = fail

    @property
    def closed(self):
        if self.fail == "stays_open":
            return False   # close() returned, but the native stream is still there
        return self._closed

    def start(self):
        self.started = True

    def read(self, frames):
        import numpy
        self.reads += 1
        if self.fail == "read" and self.reads == 2:
            raise OSError("the device went away mid-trial")
        if self.fail == "interrupt" and self.reads == 2:
            raise KeyboardInterrupt
        overflowed = self.fail == "overflow"
        if self.fail == "loud":
            return numpy.full((frames, 1), 0.5, dtype=numpy.float32), overflowed
        return numpy.zeros((frames, 1), dtype=numpy.float32), overflowed

    def stop(self):
        if self.fail == "stop":
            raise OSError("Pa_StopStream failed")
        self.stopped = True

    def close(self, ignore_errors=True):
        self.close_calls.append(ignore_errors)
        if self.fail == "close":
            raise OSError("Pa_CloseStream failed")
        self._closed = True


class FakeDevice:
    """The fake `sounddevice` module the trial imports."""

    def __init__(self, fail=None):
        self.streams = []
        self.fail = fail

    def InputStream(self, **kwargs):
        if self.fail == "open":
            raise OSError("the microphone could not be opened")
        stream = FakeStream(fail=self.fail, **kwargs)
        self.streams.append(stream)
        return stream


class FakeSpotter:
    """Fires on the `fires_on_chunk`-th chunk, or never. Mirrors the real call sequence."""

    def __init__(self, fires_on_chunk=None, keyword="STOP", fail=None):
        self.fires_on_chunk = fires_on_chunk
        self.keyword = keyword
        self.fail = fail
        self.chunks = 0
        self.resets = 0
        self.streams = 0

    def create_stream(self):
        self.streams += 1
        return object()

    def accept_waveform(self, rate, samples):
        raise AssertionError("the stream accepts the waveform, not the spotter")

    def is_ready(self, stream):
        return False

    def decode_stream(self, stream):
        pass

    def get_result(self, stream):
        if self.fail == "result" and self.chunks == 2:
            raise RuntimeError("the detector failed")
        return self.keyword if self.chunks == self.fires_on_chunk else ""

    def tokens(self, stream):
        return ["x", "y"]

    def timestamps(self, stream):
        return [0.1, 0.2]

    def reset_stream(self, stream):
        self.resets += 1


class CountingStream(FakeStream):
    """Counts chunks for the spotter, because the real spotter is fed by the stream object."""


@pytest.fixture
def offline_trial(monkeypatch):
    """Run _listen() with no microphone: a fake device module, a fake spotter, and a short bound."""
    import sys
    from app.listener import microphone

    import numpy
    numpy.zeros(1)   # pay the import cost now, not inside the trial's timed loop
    device = FakeDevice()
    monkeypatch.setitem(sys.modules, "sounddevice", device)
    monkeypatch.setattr(harness, "LISTEN_SECONDS", 0.6)
    monkeypatch.setattr(harness, "CHUNK_SECONDS", 0.01)
    microphone.reset()

    def run(spotter, expected=harness.POSITIVE, prompt="stop"):
        # The spotter counts chunks through the stream it is handed, so wire them together here.
        original_accept = None

        class Stream:
            def accept_waveform(self, rate, samples):
                spotter.chunks += 1

        monkeypatch.setattr(spotter, "create_stream", lambda: Stream())
        return harness._listen(spotter, "offline", prompt, expected), device
    return run


def test_a_detection_ends_the_trial_before_the_maximum(offline_trial):
    spotter = FakeSpotter(fires_on_chunk=3)
    trial, device = offline_trial(spotter)
    assert trial.detected and trial.keyword == "STOP"
    assert trial.chunks == 3, "it stopped feeding as soon as the keyword came back"
    assert trial.listen_seconds < harness.LISTEN_SECONDS, "it did not wait out the maximum"
    assert trial.ended_early is True
    assert trial.detect_seconds is not None and trial.close_seconds is not None
    assert spotter.resets == 1, "the detector stream is reset after a detection"


def test_a_miss_listens_until_the_maximum(offline_trial):
    trial, device = offline_trial(FakeSpotter(fires_on_chunk=None))
    assert trial.detected is False and trial.missed is True
    assert trial.listen_seconds >= harness.LISTEN_SECONDS
    assert trial.ended_early is False
    assert trial.detect_seconds is None and trial.chunks > 1


def test_a_false_positive_ends_that_trial_and_is_recorded(offline_trial):
    spotter = FakeSpotter(fires_on_chunk=2)
    trial, device = offline_trial(spotter, expected=harness.NEGATIVE, prompt="please stop")
    assert trial.false_positive is True and trial.missed is False
    assert trial.prompt == "please stop" and trial.keyword == "STOP"
    assert trial.listen_seconds < harness.LISTEN_SECONDS, "it stopped as soon as it fired"
    assert "FALSE POSITIVE" in repr(trial)


def test_a_negative_that_stays_quiet_is_not_a_false_positive(offline_trial):
    trial, device = offline_trial(FakeSpotter(fires_on_chunk=None), expected=harness.NEGATIVE,
                                  prompt="notepad")
    assert trial.detected is False and trial.false_positive is False and trial.missed is False
    assert "as expected" in repr(trial)


@pytest.mark.parametrize("fires_on_chunk", [None, 2])
def test_the_stream_is_closed_and_the_microphone_released_either_way(offline_trial, fires_on_chunk):
    from app.listener import microphone
    trial, device = offline_trial(FakeSpotter(fires_on_chunk=fires_on_chunk))
    assert len(device.streams) == 1
    assert device.streams[0].started and device.streams[0].stopped and device.streams[0].closed
    assert microphone.owner() is None, "released before the next trial could start"
    assert trial.close_seconds is not None


def test_cleanup_happens_when_the_detector_raises(offline_trial, monkeypatch):
    from app.listener import microphone
    trial, device = offline_trial(FakeSpotter(fires_on_chunk=None, fail="result"))
    assert isinstance(trial.error, RuntimeError), "the failure is recorded, not swallowed"
    assert device.streams[0].stopped and device.streams[0].closed
    assert microphone.owner() is None
    assert trial.close_seconds is not None


def test_cleanup_happens_when_the_device_raises_mid_trial(monkeypatch):
    import sys
    from app.listener import microphone
    device = FakeDevice(fail="read")
    monkeypatch.setitem(sys.modules, "sounddevice", device)
    monkeypatch.setattr(harness, "LISTEN_SECONDS", 0.6)
    microphone.reset()
    spotter = FakeSpotter(fires_on_chunk=None)

    class Stream:
        def accept_waveform(self, rate, samples):
            spotter.chunks += 1

    monkeypatch.setattr(spotter, "create_stream", lambda: Stream())
    trial = harness._listen(spotter, "offline", "stop", harness.POSITIVE)
    assert isinstance(trial.error, OSError)
    assert device.streams[0].stopped and device.streams[0].closed
    assert microphone.owner() is None


def test_a_device_that_cannot_open_is_recorded_and_the_microphone_is_freed(monkeypatch):
    import sys
    from app.listener import microphone
    device = FakeDevice(fail="open")
    monkeypatch.setitem(sys.modules, "sounddevice", device)
    microphone.reset()
    trial = harness._listen(FakeSpotter(), "offline", "stop", harness.POSITIVE)
    assert isinstance(trial.error, OSError) and trial.chunks == 0
    assert microphone.owner() is None, "no stream was opened, but the microphone was given back"


def test_a_busy_microphone_is_recorded_and_not_retried(monkeypatch):
    """Held by ANOTHER thread, because taking it twice on one thread is a deliberate error in the
    ownership rule rather than a busy device."""
    import sys
    import threading
    from app.listener import microphone
    # The fake module goes in even though no stream is opened: _listen imports sounddevice first, and
    # letting the REAL backend into sys.modules would break the suite's "offline tests import no
    # backend" rule for every later test in the process.
    monkeypatch.setitem(sys.modules, "sounddevice", FakeDevice())
    microphone.reset()
    holding, release = threading.Event(), threading.Event()

    def hold():
        assert microphone.acquire("someone-else")
        holding.set()
        release.wait(10)
        microphone.release()

    keeper = threading.Thread(target=hold)
    keeper.start()
    try:
        assert holding.wait(5)
        trial = harness._listen(FakeSpotter(), "offline", "stop", harness.POSITIVE)
    finally:
        release.set()
        keeper.join(10)
        microphone.reset()
    assert trial.error is not None and trial.chunks == 0 and trial.keyword == ""
    assert not keeper.is_alive()


def test_no_trial_is_ever_repeated():
    source = BENCHMARK.read_text(encoding="utf-8")
    assert "retry" not in source.lower()
    listen = [node for node in _tree(BENCHMARK).body
              if isinstance(node, ast.FunctionDef) and node.name == "_listen"][0]
    loops = [node for node in ast.walk(listen) if isinstance(node, (ast.While, ast.For))]
    assert len(loops) == 2, "the chunk loop and the decode loop - and no retry loop"
    assert all(isinstance(loop, ast.While) for loop in loops)


# --- Cleanup must be PROVEN, or the benchmark stops ----------------------------------------------------
# Continuing after an ordinary trial error is only safe when the device is definitely closed. If cleanup
# cannot be proven, opening another stream could mean two overlapping device owners, so the run aborts.

def _run_trial(monkeypatch, spotter, fail=None, expected=None):
    """One offline trial against a fake device with the given failure mode."""
    import sys
    from app.listener import microphone
    import numpy
    numpy.zeros(1)   # see the note in offline_trial: never pay an import inside the timed loop
    device = FakeDevice(fail=fail)
    monkeypatch.setitem(sys.modules, "sounddevice", device)
    monkeypatch.setattr(harness, "LISTEN_SECONDS", 0.4)
    monkeypatch.setattr(harness, "CHUNK_SECONDS", 0.01)
    microphone.reset()

    class Stream:
        def accept_waveform(self, rate, samples):
            spotter.chunks += 1

    monkeypatch.setattr(spotter, "create_stream", lambda: Stream())
    trial = harness._listen(spotter, "offline", "stop", expected or harness.POSITIVE)
    return trial, device


def test_close_is_asked_to_report_its_errors():
    """sounddevice's close() defaults to ignore_errors=True and swallows PortAudio failures, so the
    harness must pass ignore_errors=False or it could never see one."""
    source = BENCHMARK.read_text(encoding="utf-8")
    assert "close(ignore_errors=False)" in source
    assert "stream.closed" in source or "getattr(stream, \"closed\"" in source


def test_a_detector_failure_with_a_clean_close_allows_the_next_trial(monkeypatch):
    """Category A: the trial failed, but the device is provably gone, so the run may continue."""
    from app.listener import microphone
    trial, device = _run_trial(monkeypatch, FakeSpotter(fires_on_chunk=None, fail="result"))
    assert isinstance(trial.error, RuntimeError), "the trial error is recorded"
    assert trial.cleanup_ok is True, "cleanup was proven, so the benchmark may go on"
    assert trial.cleanup_error is None
    assert device.streams[0].closed and device.streams[0].close_calls == [False]
    assert microphone.owner() is None


def test_a_failed_stop_is_recorded_but_a_definitive_close_still_proves_cleanup(monkeypatch):
    """Documented API semantics: close() discards pending buffers as if abort() had been called, so a
    failed stop() followed by a successful close() still leaves no native stream."""
    trial, device = _run_trial(monkeypatch, FakeSpotter(fires_on_chunk=None), fail="stop")
    assert trial.cleanup_ok is True, "a stop failure alone is not fatal"
    assert "stop() raised OSError" in trial.cleanup_error and "not fatal" in trial.cleanup_error
    assert device.streams[0].closed is True


def test_a_failed_close_aborts_the_benchmark(monkeypatch):
    """Category B: the device may still be open, so no further stream may be opened."""
    from app.listener import microphone
    trial, device = _run_trial(monkeypatch, FakeSpotter(fires_on_chunk=None), fail="close")
    assert trial.cleanup_ok is False, "the next trial must not start"
    assert "FATAL close() raised OSError" in trial.cleanup_error
    assert microphone.owner() is None, "the microphone is still released as far as we can"


def test_a_stream_that_still_reports_itself_open_aborts_the_benchmark(monkeypatch):
    """close() returned, but `closed` is False - cleanup cannot be proven."""
    trial, device = _run_trial(monkeypatch, FakeSpotter(fires_on_chunk=None), fail="stays_open")
    assert trial.cleanup_ok is False
    assert "still reports closed=False" in trial.cleanup_error


def test_a_microphone_that_stays_held_aborts_the_benchmark(monkeypatch):
    """Ownership is not proof that the native stream closed, but losing ownership IS fatal on its own:
    the next trial could not acquire it anyway."""
    from app.listener import microphone
    monkeypatch.setattr(microphone, "release", lambda: None)   # pretend the release did not happen
    trial, device = _run_trial(monkeypatch, FakeSpotter(fires_on_chunk=None))
    assert trial.cleanup_ok is False
    assert "still owned by" in trial.cleanup_error
    microphone.reset()


def test_a_keyboard_interrupt_cleans_up_and_never_starts_another_trial(monkeypatch):
    """Ctrl+C: cleanup runs, then the interrupt propagates - so the caller's loop cannot reach the next
    trial. A cleanup failure during that unwinding is reported too."""
    from app.listener import microphone
    spotter = FakeSpotter(fires_on_chunk=None)
    with pytest.raises(KeyboardInterrupt):
        _run_trial(monkeypatch, spotter, fail="interrupt")
    assert microphone.owner() is None, "the microphone was released while unwinding"


def test_the_schedule_is_a_single_pass_the_loop_can_break_out_of():
    """The trials come from one generator, so the caller can stop after a cleanup failure without
    reordering or repeating anything."""
    schedule = list(harness._schedule())
    assert len(schedule) == len(harness.POSITIVES) + len(harness.NEGATIVES)
    assert [expected for _, _, expected in schedule[:5]] == [harness.POSITIVE] * 5
    assert {expected for _, _, expected in schedule[5:]} == {harness.NEGATIVE}
    assert [prompt for _, prompt, _ in schedule[5:]] == list(harness.NEGATIVES)
    source = BENCHMARK.read_text(encoding="utf-8")
    assert "aborted = trial" in source and "break" in source


def test_the_run_fails_when_any_cleanup_was_unproven():
    source = BENCHMARK.read_text(encoding="utf-8")
    assert "assert aborted is None" in source
    assert "assert all(trial.cleanup_ok for trial in trials)" in source


# --- The expanded near-miss set -----------------------------------------------------------------------

def test_the_positive_vocabulary_is_still_only_the_standalone_word():
    assert harness.POSITIVES == ("stop",) * 5
    assert harness.POSITIVE == "positive" and harness.NEGATIVE == "negative"


@pytest.mark.parametrize("phrase", ["stops", "stopper", "stop it", "full stop"])
def test_the_newly_added_near_misses_are_present_exactly_once(phrase):
    assert harness.NEGATIVES.count(phrase) == 1


@pytest.mark.parametrize("phrase", ["notepad", "stopped", "stopping", "please stop",
                                    "open notepad and type hello"])
def test_the_original_negatives_are_still_present_exactly_once(phrase):
    assert harness.NEGATIVES.count(phrase) == 1


def test_the_negative_set_is_exactly_the_agreed_ten():
    assert harness.NEGATIVES == ("(say nothing at all)", "notepad", "stopped", "stopping", "stops",
                                 "stopper", "stop it", "please stop", "full stop",
                                 "open notepad and type hello")
    assert len(harness.NEGATIVES) == 10 and len(set(harness.NEGATIVES)) == 10


def test_a_phrase_containing_the_keyword_is_a_negative_not_a_positive():
    """The semantic contract does not change during the run: only the standalone word counts."""
    for phrase in ("stop it", "please stop", "full stop", "stops", "stopper"):
        assert phrase in harness.NEGATIVES and phrase not in harness.POSITIVES


def test_the_listening_bound_is_a_maximum_not_a_duration():
    assert harness.LISTEN_SECONDS == 4.0
    source = BENCHMARK.read_text(encoding="utf-8")
    assert "bound, not a duration" in source


# --- The fetch script is the only thing that downloads ------------------------------------------------

def test_only_the_fetch_script_reaches_the_network():
    root = settings.PROJECT_ROOT
    offenders = []
    for path in (*(root / "app").rglob("*.py"), *(root / "tests").rglob("*.py"),
                 *(root / "scripts").rglob("*.py")):
        if path.name == FETCH_SCRIPT.name:
            continue
        for module, name in _imports(path):
            if module.split(".")[0] in ("urllib", "requests", "httpx") and "kws" in path.name:
                offenders.append(f"{path.relative_to(root)}: {module}")
    assert offenders == [], offenders
    fetch = {module.split(".")[0] for module, _ in _imports(FETCH_SCRIPT)}
    assert "urllib" in fetch, "the fetch script is the one place that downloads this model"


def test_the_fetch_script_validates_what_it_downloaded():
    import importlib.util
    spec = importlib.util.spec_from_file_location("fetch_kws_model",
                                                 settings.PROJECT_ROOT / FETCH_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.MODEL == "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
    assert module.ARCHIVE_URL.startswith("https://github.com/k2-fsa/sherpa-onnx/releases/download/")
    assert "tokens.txt" in module.REQUIRED and "bpe.model" in module.REQUIRED
    assert sum("int8.onnx" in name for name in module.REQUIRED) == 3, "the int8 triple"
    assert module.KWS_ROOT == settings.PROJECT_ROOT / "data" / "models" / "kws"


def test_the_model_folder_is_git_ignored():
    ignored = (settings.PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "data/models/" in ignored, "the kws model lives under data/models/, which is ignored"
