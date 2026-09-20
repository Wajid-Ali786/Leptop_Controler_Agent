"""
Tests for app/listener/ (docs/step4 Section 5, Phase 2 - Feature 1: config, data shapes, boundaries).

Everything here is offline and silent: no microphone is opened, no speech model is loaded, nothing
is downloaded and no audio exists. That is possible because app/listener/logic.py is pure - it reads
validated settings and applies mechanical text rules, while capture and transcription live behind
app/listener/adapter.py, which returns the data shapes in app/listener/models.py.

The architecture tests at the end are the important ones: they pin the boundary BEFORE the code that
could break it exists, in the same AST style as tests/test_executor_logic.py.
"""
import ast

import pytest

from app.listener import logic, models
from app.listener.models import (CAPTURE_UNAVAILABLE, DEVICE_BUSY, DEVICE_LOST, MODEL_UNAVAILABLE,
                                 NO_DEVICE, NO_SPEECH, PERMISSION_DENIED, TRANSCRIPTION_FAILED,
                                 Transcript, VoiceFailure)
from config import settings
from config.settings import SettingsError

CONFIG = (
    "listener:\n"
    "  enabled: false\n"
    "  model_size: small\n"
    "  model_dir: data/models\n"
    "  local_files_only: true\n"
    "  device: auto\n"
    "  compute_type: auto\n"
    "  language: auto\n"
    "  sample_rate: 16000\n"
    '  input_device: ""\n'
    "  vad_filter: true\n"
    "  min_silence_ms: 800\n"
    "  max_utterance_seconds: 15\n"
    "  initial_prompt_terms: [notepad, calculator]\n"
    "  voice_stop_enabled: true\n"
    "speaker:\n"
    "  enabled: false\n"
    "  engine: auto\n"
    '  voice: ""\n'
    "  rate: 0\n"
)


@pytest.fixture
def config(tmp_path, monkeypatch):
    """The listener/speaker config, editable per test by replacing one line."""
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_PATH", path)

    def rewrite(old, new):
        assert old in path.read_text(encoding="utf-8"), f"{old!r} is not in the test config"
        path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")

    return rewrite


# --- Settings: the happy path --------------------------------------------------------------------

def test_the_listener_section_is_read_and_validated(config):
    chosen = logic.listener_settings()
    assert chosen.enabled is False and chosen.model_size == "small"
    assert chosen.device == "auto", "device must default to auto - no hardware is assumed"
    assert chosen.local_files_only is True, "the safe default is never to download by itself"
    assert chosen.sample_rate == 16000 and chosen.language == "auto"
    assert chosen.initial_prompt_terms == ("notepad", "calculator")
    assert chosen.initial_prompt == "notepad calculator"
    assert chosen.input_device == "" and chosen.voice_stop_enabled is True


def test_the_real_config_file_is_valid():
    """The shipped config.yaml must satisfy the same validation the tests use."""
    assert logic.listener_settings().device in logic.DEVICES


def test_sixteen_kilohertz_is_the_only_accepted_rate():
    """Locked until a capture -> resample -> canonical 16 kHz path is designed and approved."""
    assert logic.SAMPLE_RATES == (16000,)


# --- Settings: what must be refused --------------------------------------------------------------

@pytest.mark.parametrize("old, new, part", [
    ("device: auto", "device: gpu", "listener.device"),
    ("device: auto", "device: true", "listener.device"),
    ("compute_type: auto", "compute_type: int4", "listener.compute_type"),
    ("model_size: small", "model_size: enormous", "listener.model_size"),
    ("sample_rate: 16000", "sample_rate: 12345", "listener.sample_rate"),
    ("sample_rate: 16000", "sample_rate: 0", "listener.sample_rate"),
    # 16 kHz is the canonical rate and the ONLY accepted one: no resampling path is approved yet,
    # so a rate a microphone merely happens to offer must not be configurable.
    ("sample_rate: 16000", "sample_rate: 44100", "listener.sample_rate"),
    ("sample_rate: 16000", "sample_rate: 48000", "listener.sample_rate"),
    ("sample_rate: 16000", "sample_rate: 22050", "listener.sample_rate"),
    ("max_utterance_seconds: 15", "max_utterance_seconds: 0", "listener.max_utterance_seconds"),
    ("max_utterance_seconds: 15", "max_utterance_seconds: -3", "listener.max_utterance_seconds"),
    ("max_utterance_seconds: 15", 'max_utterance_seconds: "15"', "listener.max_utterance_seconds"),
    ("min_silence_ms: 800", "min_silence_ms: 0", "listener.min_silence_ms"),
    ("min_silence_ms: 800", "min_silence_ms: 0.5", "listener.min_silence_ms"),
    ("language: auto", "language: Englishy", "listener.language"),
    ("language: auto", "language: 42", "listener.language"),
    ("initial_prompt_terms: [notepad, calculator]", "initial_prompt_terms: notepad",
     "listener.initial_prompt_terms"),
    ("initial_prompt_terms: [notepad, calculator]", "initial_prompt_terms: [notepad, 7]",
     "listener.initial_prompt_terms"),
    ("initial_prompt_terms: [notepad, calculator]", 'initial_prompt_terms: [notepad, "  "]',
     "listener.initial_prompt_terms"),
    ("local_files_only: true", "local_files_only: yes please", "listener.local_files_only"),
    ("vad_filter: true", "vad_filter: 1", "listener.vad_filter"),
    ('input_device: ""', "input_device: -2", "listener.input_device"),
    ('input_device: ""', "input_device: true", "listener.input_device"),
    ("model_dir: data/models", 'model_dir: ""', "listener.model_dir"),
])
def test_invalid_settings_are_refused_by_name(config, old, new, part):
    config(old, new)
    with pytest.raises(SettingsError, match=part.replace(".", r"\.")):
        logic.listener_settings()


def test_a_missing_listener_section_is_reported_not_guessed(config):
    config("listener:\n", "listener_disabled:\n")
    with pytest.raises(SettingsError, match="listener"):
        logic.listener_settings()


# --- The only text changes the Listener may make -------------------------------------------------

@pytest.mark.parametrize("text, tidied", [
    ("  open notepad  ", "open notepad"),
    ("open    notepad", "open notepad"),
    ("open notepad!!!", "open notepad!"),
    ("open notepad\n\ttype hello", "open notepad type hello"),
    ("", ""),
])
def test_tidy_only_touches_whitespace_and_repeated_punctuation(text, tidied):
    assert logic.tidy(text) == tidied


@pytest.mark.parametrize("text", [
    "کیلکولیٹر کھولو",          # Urdu script must survive untouched
    "notepad kholo",            # Roman Urdu stays exactly as written
    "नोटपैड खोलो",               # Hindi script
    "open notepad phir type karo",  # mixed
])
def test_tidy_never_changes_a_single_word(text):
    """Roman Urdu is a written form, not a language to convert: the Listener must not rewrite it."""
    assert logic.tidy(f"  {text}  ") == text
    assert logic.tidy(text).split() == text.split()


def test_for_matching_is_a_separate_lowercased_copy():
    assert logic.for_matching("  STOP!  ") == "stop!"
    assert logic.tidy("  STOP!  ") == "STOP!", "the shown text keeps its own casing"


@pytest.mark.parametrize("text, silent", [
    ("", True), ("   ", True), ("...", True), ("!!!", True), ("\n\t", True),
    ("stop", False), ("کھولو", False), ("7", False),
])
def test_silence_and_noise_are_recognised_as_no_command(text, silent):
    assert logic.is_silence(text) is silent


def test_tidy_refuses_non_text():
    with pytest.raises(TypeError):
        logic.tidy(None)


# --- Data shapes ---------------------------------------------------------------------------------

def test_a_transcript_keeps_what_the_recognizer_said_plus_metadata():
    heard = Transcript(text="open notepad", language="en", language_probability=0.97, audio_seconds=1.4)
    assert heard.text == "open notepad" and heard.language == "en"
    assert heard.language_probability == 0.97 and heard.audio_seconds == 1.4
    assert heard.empty is False


@pytest.mark.parametrize("text, empty", [("", True), ("   ", True), ("hi", False)])
def test_an_empty_transcript_knows_it_is_empty(text, empty):
    assert Transcript(text=text).empty is empty


def test_a_transcript_is_immutable_and_has_no_normalized_field():
    heard = Transcript(text="Open Notepad")
    with pytest.raises(Exception):
        heard.text = "open notepad"
    assert not hasattr(heard, "normalized"), "derived text must never live inside the Transcript"


def test_a_transcript_needs_text():
    with pytest.raises(TypeError):
        Transcript(text=None)


@pytest.mark.parametrize("kind", [NO_DEVICE, PERMISSION_DENIED, DEVICE_BUSY, DEVICE_LOST,
                                  CAPTURE_UNAVAILABLE, NO_SPEECH, MODEL_UNAVAILABLE,
                                  TRANSCRIPTION_FAILED])
def test_every_failure_kind_can_be_built_and_carries_a_message(kind):
    failure = VoiceFailure(kind=kind, message="something to show the user")
    assert failure.kind in models.FAILURE_KINDS and failure.message


def test_an_unknown_failure_kind_is_refused():
    with pytest.raises(ValueError, match="Unknown voice failure kind"):
        VoiceFailure(kind="whoops", message="...")


def test_the_failure_kinds_are_exactly_the_ones_the_project_has_agreed():
    """A new kind is a contract change, so it is listed here deliberately rather than discovered."""
    assert models.FAILURE_KINDS == frozenset({
        "no_device", "permission_denied", "device_busy", "device_lost",
        "capture_unavailable", "no_speech", "model_unavailable", "transcription_failed"})


def test_a_missing_backend_and_a_missing_microphone_are_different_kinds():
    assert CAPTURE_UNAVAILABLE != NO_DEVICE
    assert DEVICE_BUSY not in (NO_DEVICE, DEVICE_LOST)


# --- Architecture rules, pinned before the code that could break them exists ---------------------

def _imports(path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            yield from ((alias.name, "") for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            yield from ((node.module or "", alias.name) for alias in node.names)


def _python_files():
    root = settings.PROJECT_ROOT
    return [*(root / "app").rglob("*.py"), *(root / "config").rglob("*.py"),
            *(root / "scripts").rglob("*.py"), root / "main.py"]


@pytest.mark.parametrize("library, allowed", [
    ("faster_whisper", "app/listener/adapter.py"),
    ("sounddevice", "app/listener/adapter.py"),
    ("edge_tts", "app/speaker/adapter.py"),
    ("pyttsx3", "app/speaker/adapter.py"),
])
def test_only_the_owning_adapter_may_import_the_voice_libraries(library, allowed):
    root = settings.PROJECT_ROOT
    permitted = root / allowed
    offenders = [f"{path.relative_to(root)}: {module}"
                 for path in _python_files() if path != permitted
                 for module, _ in _imports(path)
                 if module.split(".")[0] == library]
    assert offenders == [], f"Only {allowed} may import {library}: {offenders}"


def test_listener_logic_stays_pure():
    """logic.py must not reach the adapter or any audio/model library: that is what makes language,
    normalization and error policy testable without mocking I/O."""
    root = settings.PROJECT_ROOT
    forbidden = {"app.listener.adapter", "faster_whisper", "sounddevice", "numpy", "av",
                 "soundfile", "wave", "audioop"}
    offenders = [f"{module} {name}".strip()
                 for module, name in _imports(root / "app" / "listener" / "logic.py")
                 if module in forbidden or module.split(".")[0] in forbidden
                 or (module == "app.listener" and name == "adapter")]
    assert offenders == [], f"app/listener/logic.py must stay pure: {offenders}"


@pytest.mark.parametrize("package", ["app/listener", "app/speaker"])
def test_voice_modules_never_reach_the_executor_or_act(package):
    """Voice must enter the one safe path through app.console.handle_command - never the Executor
    adapter, never the grammar, never a second execution path."""
    root = settings.PROJECT_ROOT
    forbidden = {"app.executor.adapter", "app.executor.logic", "app.executor.commands",
                 "pyautogui", "pywinauto", "subprocess"}
    offenders = [f"{path.relative_to(root)}: {module} {name}".strip()
                 for path in (root / package).rglob("*.py")
                 for module, name in _imports(path)
                 if module in forbidden or module.split(".")[0] in forbidden
                 or (module == "app.executor" and name in ("adapter", "logic", "commands"))]
    assert offenders == [], f"{package} must not execute actions: {offenders}"


AUDIO_FILE_LIBRARIES = {"wave", "soundfile", "aifc", "sunau", "scipy"}
DISK_WRITERS = ("write_bytes", "write_text", "writeframes", "savetxt")


def test_the_listener_can_never_write_audio_to_disk():
    """'Raw audio stays in memory' as a structural fact, not a promise: nothing under app/listener/
    may import an audio-file library, and no code path there writes bytes anywhere."""
    root = settings.PROJECT_ROOT
    offenders = [f"{path.relative_to(root)}: {module}"
                 for path in (root / "app" / "listener").rglob("*.py")
                 for module, _ in _imports(path)
                 if module.split(".")[0] in AUDIO_FILE_LIBRARIES]
    assert offenders == [], f"app/listener must not import an audio-file library: {offenders}"

    writers = []
    for path in (root / "app" / "listener").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(
                    node.func, "id", "")
                if name in DISK_WRITERS:
                    writers.append(f"{path.relative_to(root)}: {name}()")
                if name == "open" and any(
                        isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                        and "w" in arg.value for arg in node.args[1:]):
                    writers.append(f"{path.relative_to(root)}: open(..., write)")
    assert writers == [], f"app/listener must not write files: {writers}"


def test_a_future_voice_console_must_route_through_handle_command():
    """The rule for the file Feature 6 will add: if app/voice_console.py exists, it may reach the
    Executor only through app.console.handle_command."""
    console = settings.PROJECT_ROOT / "app" / "voice_console.py"
    if not console.is_file():
        pytest.skip("app/voice_console.py does not exist yet (Feature 6)")
    imported = list(_imports(console))
    assert any(module == "app.console" or (module == "app" and name == "console")
               for module, name in imported), "the voice console must use app.console"
    forbidden = [f"{module} {name}".strip() for module, name in imported
                 if module.startswith("app.executor") and name != "emergency_stop"
                 and module != "app.executor.emergency_stop"]
    assert forbidden == [], f"the voice console must not reach the Executor directly: {forbidden}"
