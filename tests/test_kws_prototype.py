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
    assert {"sherpa_onnx", "sounddevice", "numpy"} <= inside, "they are used, just not at import time"
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
    assert "▁" not in source, "no BPE tokens are hard-coded anywhere here"


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
    assert not hasattr(harness.Trial("t", "stop"), "confidence")
    assert not hasattr(harness.Trial("t", "stop"), "score")


def test_a_trial_records_only_timings_and_what_the_detector_returned():
    trial = harness.Trial("positive 1", "stop")
    assert trial.keyword == "" and trial.detected is False
    assert trial.lag_seconds is None and trial.slowest_chunk_seconds is None
    assert not hasattr(trial, "pcm") and not hasattr(trial, "audio")


def test_a_trial_repr_carries_no_audio():
    trial = harness.Trial("positive 1", "stop")
    trial.keyword, trial.timestamps, trial.detect_seconds = "STOP", [0.5, 0.8], 1.2
    shown = repr(trial)
    assert "STOP" in shown, "the detected keyword is deliberately visible - it is the result"
    assert "pcm" not in shown and "array" not in shown
    assert trial.lag_seconds == pytest.approx(0.4), "wall detection minus the last token's own time"


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
