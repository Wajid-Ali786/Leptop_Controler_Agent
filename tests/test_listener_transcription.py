"""
Tests for Phase 2 Feature 4 Task 4a: turning a Recording into a Transcript - offline.

Nothing here opens a microphone, imports faster-whisper or loads a real model. adapter._whisper() and
adapter._ct2() are replaced by fakes, so the REAL ensure_model() runs against a real (tiny) model
folder under tmp_path and hands back a fake recognizer whose transcribe() behaves the way the
installed faster-whisper 1.2.1 was audited to behave:

    - it returns (generator, info): the recognizer only runs while that generator is consumed,
    - it reports language_probability = 1 whenever it was TOLD which language to use,
    - its segment text carries its own leading space and is never stripped.

numpy is real (it is a dependency of faster-whisper and needs no model), so the PCM conversion is
proven against the actual array library rather than a stand-in.
"""
import logging
import struct
import threading
import time
from types import SimpleNamespace

import pytest

from app.listener import adapter, logic
from app.listener.models import (LANGUAGE_UNSUPPORTED, LIMIT, MODEL_UNAVAILABLE, NO_SPEECH,
                                 TRANSCRIPTION_FAILED, ListenerSettings, Recording, Transcript,
                                 VoiceFailure)

SPOKEN = " open notepad phir type karo"      # what the fake recognizer "heard": must never be logged
URDU = " نوٹ پیڈ کھولو"
HINDI = " नोटपैड खोलो"
GOOD_FILES = {"config.json": b"{}", "model.bin": b"\x00" * 64, "tokenizer.json": b"{}",
              "vocabulary.txt": b"a\nb\n"}
CPU_TYPES = frozenset({"float32", "int16", "int8", "int8_float32"})
# What the fake model reports it knows. The real property returns faster-whisper's own 100 codes; the
# point of the fake is that the list comes from the MODEL, not from any code of ours.
LANGUAGES = ("en", "ur", "hi", "pa", "ar", "yue")
SECRET_MESSAGE = "C:/Users/Someone/private/model.bin failed at frame 12"


# --- Fakes ------------------------------------------------------------------------------------------

class FakeRecognizer:
    """Stands in for a loaded WhisperModel, faithful to what the audit found."""

    def __init__(self, path, device, compute_type):
        self.path, self.device, self.compute_type = path, device, compute_type
        self.calls = []                     # every transcribe() call, with its exact arguments
        self.segments = [SPOKEN]            # what the generator yields, in order
        self.detected, self.probability = "en", 0.98    # only used when asked to auto-detect
        self.duration = 99.0                # deliberately NOT the recording's length
        self.supported_languages = list(LANGUAGES)
        self.eager_error = None             # raised by transcribe() itself
        self.iterator_error = None          # raised while the generator is being consumed
        self.during = None                  # called between yields, to look at the world mid-inference

    def transcribe(self, audio, **options):
        self.calls.append(dict(audio=audio, **options))
        if self.eager_error is not None:
            raise self.eager_error
        language = options.get("language")
        info = SimpleNamespace(
            language=self.detected if language is None else language,
            # the installed library hard-codes 1 when it was told the language - a placeholder
            language_probability=self.probability if language is None else 1,
            duration=self.duration, duration_after_vad=self.duration)
        return self._generate(), info

    def _generate(self):
        for text in self.segments:
            if self.during is not None:
                self.during()
            yield SimpleNamespace(text=text)
        if self.iterator_error is not None:
            raise self.iterator_error


class FakeLocalEntryNotFound(FileNotFoundError):
    """huggingface_hub's LocalEntryNotFoundError is a FileNotFoundError subclass."""


class FakeWhisper:
    def __init__(self, root):
        self.root = root
        self.cached = {}
        self.lookups = []
        self.downloads = []
        self.models = []
        self.utils = SimpleNamespace(download_model=self.download_model)

    def download_model(self, size, local_files_only=False, cache_dir=None, **extra):
        self.lookups.append(dict(size=size, local_files_only=local_files_only, cache_dir=cache_dir))
        if local_files_only:
            if size not in self.cached:
                raise FakeLocalEntryNotFound("not in the cache")
            return str(self.cached[size])
        self.downloads.append(size)
        raise AssertionError("transcription must never reach a download")

    def WhisperModel(self, path, device="auto", compute_type="default", local_files_only=False, **extra):
        model = FakeRecognizer(path, device, compute_type)
        self.models.append(model)
        return model


class FakeCT2:
    def get_cuda_device_count(self):
        return 0

    def get_supported_compute_types(self, device):
        return set(CPU_TYPES)


def model_folder(root, size="small"):
    folder = root / f"models--Systran--faster-whisper-{size}" / "snapshots" / "abc123"
    folder.mkdir(parents=True, exist_ok=True)
    for name, content in GOOD_FILES.items():
        (folder / name).write_bytes(content)
    return folder


def listener(root, *, language="auto", vad=True, min_silence_ms=800,
             terms=("notepad", "calculator")):
    return ListenerSettings(enabled=True, model_size="small", model_dir=str(root),
                            local_files_only=True, device="cpu", compute_type="int8",
                            language=language, sample_rate=16000, input_device="", vad_filter=vad,
                            min_silence_ms=min_silence_ms, max_utterance_seconds=15.0,
                            initial_prompt_terms=terms, voice_stop_enabled=True)


def recording(frames=1600, sample=1000, stopped_by=LIMIT):
    return Recording(struct.pack("<h", sample) * frames, stopped_by)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A cached model folder, a fake faster-whisper, and the one model ensure_model() loads from it."""
    root = tmp_path / "models"
    root.mkdir()
    whisper = FakeWhisper(root)
    whisper.cached["small"] = model_folder(root)
    monkeypatch.setattr(adapter, "_whisper", lambda: whisper)
    monkeypatch.setattr(adapter, "_ct2", lambda: FakeCT2())
    world = SimpleNamespace(root=root, whisper=whisper, settings=listener(root))

    def model():
        if not whisper.models:                     # load it the way transcribe() would
            adapter.ensure_model(world.settings)
        return whisper.models[0]
    world.model = model
    return world


def transcribe(world, **overrides):
    settings = listener(world.root, **overrides) if overrides else world.settings
    return adapter.transcribe(recording(), settings)


def only_call(world):
    calls = world.model().calls
    assert len(calls) == 1, calls
    return calls[0]


# --- The PCM the model is given --------------------------------------------------------------------

@pytest.mark.parametrize("sample, expected", [(-32768, -1.0), (0, 0.0), (32767, 32767 / 32768.0)])
def test_int16_samples_become_the_float32_the_recognizer_expects(world, sample, expected):
    """Exactly what faster-whisper's own decoder does (astype(float32) / 32768.0), so handing it an
    array instead of a file changes nothing about the audio."""
    adapter.transcribe(recording(frames=4, sample=sample), world.settings)
    audio = only_call(world)["audio"]
    assert audio.dtype.name == "float32" and audio.ndim == 1 and len(audio) == 4
    assert audio[0] == pytest.approx(expected, abs=1e-7)


def test_the_recording_is_only_ever_read(world):
    """frombuffer does not copy and astype does, so the captured audio is never modified in place."""
    captured = recording(frames=8, sample=1234)
    before = bytes(captured.pcm)
    audio = adapter._samples(captured)
    audio *= 0.0
    assert captured.pcm == before and captured.frames == 8


def test_an_empty_recording_is_no_speech_and_never_loads_a_model(world):
    result = adapter.transcribe(recording(frames=0), world.settings)
    assert isinstance(result, VoiceFailure) and result.kind == NO_SPEECH
    assert world.whisper.models == [] and world.whisper.lookups == []


# --- The model it uses -----------------------------------------------------------------------------

def test_a_model_that_cannot_be_loaded_is_reported_unchanged(world):
    """A model that never loaded did not fail to transcribe: its own failure is passed straight on."""
    world.whisper.cached.clear()
    expected = adapter.ensure_model(world.settings)
    adapter.forget_model()
    result = adapter.transcribe(recording(), world.settings)
    assert isinstance(result, VoiceFailure) and result.kind == MODEL_UNAVAILABLE
    assert result == expected and result.message == expected.message


def test_the_loaded_model_is_reused_and_nothing_is_ever_downloaded(world):
    for _ in range(3):
        assert isinstance(transcribe(world), Transcript)
    assert len(world.whisper.models) == 1, "one model, loaded once"
    assert world.whisper.downloads == []
    assert all(lookup["local_files_only"] is True for lookup in world.whisper.lookups)


def test_the_model_object_never_escapes(world):
    result = transcribe(world)
    assert isinstance(result, Transcript)
    assert world.model() not in vars(result).values()
    assert not any(isinstance(value, FakeRecognizer) for value in vars(result).values())


# --- What the recognizer emitted is what the Transcript holds ---------------------------------------

def test_one_segment_is_kept_exactly_as_it_was_emitted(world):
    world.model().segments = [SPOKEN]
    result = transcribe(world)
    assert result.text == SPOKEN, "including the leading space faster-whisper emits"


def test_several_segments_are_joined_in_order_with_nothing_added(world):
    world.model().segments = [" open notepad", " phir type karo", " thank you."]
    assert transcribe(world).text == " open notepad phir type karo thank you."


@pytest.mark.parametrize("segments", [
    [URDU], [HINDI], [" open notepad phir type karo"], [" Kya haal hai? Notepad kholo!"],
    [" one, two... three?!"], ["  leading and trailing  "],
])
def test_no_script_or_punctuation_is_touched(world, segments):
    """Urdu stays Urdu, Hindi stays Hindi, Roman Urdu stays Roman Urdu, punctuation and spacing stay
    exactly as emitted. Everything mechanical the Listener may do is a separate, derived value."""
    world.model().segments = list(segments)
    assert transcribe(world).text == "".join(segments)


def test_the_transcript_is_not_tidied_lowercased_or_stripped(world):
    world.model().segments = ["  Open   Notepad..  "]
    result = transcribe(world)
    assert result.text == "  Open   Notepad..  "
    assert result.text != logic.tidy(result.text) and result.text != result.text.strip()
    assert logic.for_matching(result.text) == "open notepad."


# --- Language and probability ----------------------------------------------------------------------

def test_auto_asks_the_recognizer_to_detect_and_keeps_what_it_measured(world):
    world.model().detected, world.model().probability = "ur", 0.87
    result = transcribe(world, language="auto")
    assert only_call(world)["language"] is None
    assert result.language == "ur" and result.language_probability == pytest.approx(0.87)


def test_an_explicit_language_is_passed_through(world):
    result = transcribe(world, language="ur")
    assert only_call(world)["language"] == "ur" and result.language == "ur"


def test_a_told_language_reports_no_probability_rather_than_the_libraries_placeholder(world):
    """faster-whisper reports 1 whenever it was TOLD the language. That is a placeholder, not
    confidence, so it is never passed off as a measurement."""
    result = transcribe(world, language="en")
    assert only_call(world)["language"] == "en"
    assert result.language_probability is None


def test_a_language_the_model_does_not_know_is_refused_before_anything_runs(world):
    result = transcribe(world, language="pt-br")
    assert isinstance(result, VoiceFailure) and result.kind == LANGUAGE_UNSUPPORTED
    assert "pt-br" in result.message and "auto" in result.message
    assert world.model().calls == [], "nothing was recognized"


def test_auto_is_never_refused_even_if_the_model_lists_nothing(world):
    world.model().supported_languages = []
    assert isinstance(transcribe(world, language="auto"), Transcript)


def test_which_languages_exist_is_the_models_own_evidence(world):
    """No list of languages lives in our code: the model is asked, so it can never drift from the
    installed library."""
    world.model().supported_languages = ["en", "xy"]
    assert isinstance(transcribe(world, language="xy"), Transcript)
    assert transcribe(world, language="ur").kind == LANGUAGE_UNSUPPORTED


# --- The options the recognizer is given ------------------------------------------------------------

def test_the_configured_terms_are_passed_as_the_recognizers_hint(world):
    transcribe(world, terms=("notepad", "calculator"))
    assert only_call(world)["initial_prompt"] == "notepad calculator"


def test_no_terms_means_no_hint(world):
    transcribe(world, terms=())
    assert only_call(world)["initial_prompt"] is None


def test_the_voice_activity_filter_is_off_when_the_setting_is_off(world):
    transcribe(world, vad=False)
    call = only_call(world)
    assert call["vad_filter"] is False and call["vad_parameters"] is None


def test_the_voice_activity_filter_gets_the_configured_silence(world):
    transcribe(world, vad=True, min_silence_ms=1500)
    call = only_call(world)
    assert call["vad_filter"] is True
    assert call["vad_parameters"] == {"min_silence_duration_ms": 1500}


def test_feature_four_introduces_no_other_recognizer_tuning(world):
    transcribe(world)
    assert set(only_call(world)) == {"audio", "language", "initial_prompt", "vad_filter",
                                     "vad_parameters"}


# --- Silence ----------------------------------------------------------------------------------------

def test_a_recognizer_that_emitted_nothing_is_no_speech(world):
    world.model().segments = []
    result = transcribe(world)
    assert isinstance(result, VoiceFailure) and result.kind == NO_SPEECH


@pytest.mark.parametrize("segments", [["   ", " "], [""], [" ..."], [" . . ."]])
def test_output_without_a_single_word_is_no_speech(world, segments):
    """Judged by the existing pure is_silence rule, on a DERIVED copy - silence and noise must never
    become a command."""
    world.model().segments = list(segments)
    assert transcribe(world).kind == NO_SPEECH


def test_no_speech_is_decided_on_what_was_recognized_not_on_the_audio(world):
    """Loud audio with no words is no_speech; quiet audio with words is a Transcript. Nothing here
    looks at sample amplitude."""
    world.model().segments = [" "]
    assert adapter.transcribe(recording(sample=32000), world.settings).kind == NO_SPEECH
    world.model().segments = [" hello"]
    assert isinstance(adapter.transcribe(recording(sample=1), world.settings), Transcript)


# --- Metadata ---------------------------------------------------------------------------------------

def test_the_transcript_reports_the_recordings_own_length(world):
    """Not the recognizer's duration, and not its duration after voice-activity filtering: those are
    different concepts, and only the recording knows how long it was."""
    captured = recording(frames=32000)     # exactly 2.0 s at 16 kHz
    world.model().duration = 99.0
    result = adapter.transcribe(captured, world.settings)
    assert result.audio_seconds == pytest.approx(2.0)
    assert result.audio_seconds == captured.seconds != world.model().duration


def test_the_transcript_carries_no_timing_of_its_own(world):
    result = transcribe(world)
    assert not hasattr(result, "inference_seconds") and not hasattr(result, "seconds")
    assert set(vars(result)) == {"text", "language", "language_probability", "audio_seconds"}


def test_a_cancelled_recording_with_audio_is_still_recognized_when_it_is_handed_over(world):
    """Whether to throw a cancelled utterance away is the voice console's decision, later."""
    result = adapter.transcribe(recording(stopped_by="cancelled"), world.settings)
    assert isinstance(result, Transcript) and result.text == SPOKEN


# --- What may fail, and what may not be disguised -----------------------------------------------------

class FakeOnnxError(Exception):
    """onnxruntime's exception classes derive straight from Exception, not from RuntimeError (checked
    against the installed onnxruntime during the audit)."""


FakeOnnxError.__module__ = "onnxruntime.capi.onnxruntime_pybind11_state"


@pytest.mark.parametrize("error", [RuntimeError(SECRET_MESSAGE), ValueError(SECRET_MESSAGE),
                                   MemoryError(), OSError(SECRET_MESSAGE), FakeOnnxError(SECRET_MESSAGE)])
def test_a_recognized_failure_of_the_call_itself_is_a_transcription_failure(world, error):
    world.model().eager_error = error
    result = transcribe(world)
    assert isinstance(result, VoiceFailure) and result.kind == TRANSCRIPTION_FAILED
    assert SECRET_MESSAGE not in result.message and "model.bin" not in result.message


@pytest.mark.parametrize("error", [RuntimeError(SECRET_MESSAGE), FakeOnnxError(SECRET_MESSAGE)])
def test_a_recognized_failure_while_the_segments_are_produced_is_caught_too(world, error):
    """The recognizer really runs while the generator is consumed, so the error boundary has to cover
    the consumption - not just the call that handed the generator over."""
    world.model().iterator_error = error
    result = transcribe(world)
    assert isinstance(result, VoiceFailure) and result.kind == TRANSCRIPTION_FAILED


@pytest.mark.parametrize("place", ["eager_error", "iterator_error"])
def test_an_unexpected_exception_propagates_and_lets_go_of_the_lock(world, place):
    """A defect of ours is never relabelled as a failed model - and it must not leave the model locked
    for the rest of the process either."""
    setattr(world.model(), place, TypeError("a defect in our own code"))
    with pytest.raises(TypeError):
        transcribe(world)
    assert adapter._transcribe_lock.acquire(blocking=False), "the lock was left held"
    adapter._transcribe_lock.release()


def test_a_transcription_failure_is_not_a_model_failure(world):
    world.model().eager_error = RuntimeError(SECRET_MESSAGE)
    assert transcribe(world).kind != MODEL_UNAVAILABLE


# --- One transcription at a time ---------------------------------------------------------------------

def test_the_lock_is_still_held_while_the_segments_are_produced(world):
    """Releasing it as soon as transcribe() returns would let a second caller into the model exactly
    when the first one is really using it."""
    held = []
    model = world.model()
    model.segments = [" one", " two"]

    def look():
        free = adapter._transcribe_lock.acquire(blocking=False)
        if free:
            adapter._transcribe_lock.release()
        held.append(not free)
    model.during = look
    assert isinstance(transcribe(world), Transcript)
    assert held == [True, True], "the lock must be held for every yielded segment"


def test_two_callers_never_use_the_model_at_the_same_time(world):
    model = world.model()
    model.segments = [" one", " two"]
    active, peak, guard = [0], [0], threading.Lock()

    def look():
        with guard:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.02)
        with guard:
            active[0] -= 1
    model.during = look

    results = []
    threads = [threading.Thread(target=lambda: results.append(transcribe(world)), daemon=True)
               for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads), "a transcription never finished"
    assert peak[0] == 1, "two callers were inside the recognizer at once"
    assert all(isinstance(result, Transcript) for result in results) and len(results) == 2


def test_the_model_load_lock_is_not_held_while_transcribing(world):
    """Lock order: ensure_model() takes and releases the load lock, and only then is the transcription
    lock taken - so the two can never wait on each other."""
    model = world.model()

    def look():
        assert adapter._model_lock.acquire(blocking=False), "the load lock was held during inference"
        adapter._model_lock.release()
    model.during = look
    assert isinstance(transcribe(world), Transcript)


# --- Privacy -----------------------------------------------------------------------------------------

def test_what_was_said_is_not_in_the_transcripts_repr_or_str(world):
    result = transcribe(world)
    for shown in (repr(result), str(result), f"{result}", "%s" % (result,)):
        assert SPOKEN.strip() not in shown and "notepad" not in shown
        assert f"characters={len(SPOKEN)}" in shown and "language=" in shown
    assert result.text == SPOKEN, "the text itself is still there for code that means to use it"


def test_no_log_line_carries_what_was_said_the_hint_or_the_audio(world, caplog):
    caplog.set_level(logging.DEBUG)
    world.model().segments = [SPOKEN, URDU]
    result = transcribe(world, terms=("notepad", "calculator"))
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert isinstance(result, Transcript)
    for forbidden in ("notepad", "karo", URDU.strip(), "calculator", "1000", "\u0000"):
        assert forbidden not in logged, f"{forbidden!r} reached the log"
    assert "characters" in logged or "segment" in logged, "safe metadata is still logged"


def test_a_failure_logs_the_kind_only(world, caplog):
    caplog.set_level(logging.DEBUG)
    world.model().eager_error = RuntimeError(SECRET_MESSAGE)
    transcribe(world)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "RuntimeError" in logged
    assert SECRET_MESSAGE not in logged and "model.bin" not in logged


def test_silence_logs_nothing_about_the_audio_but_its_length(world, caplog):
    caplog.set_level(logging.DEBUG)
    world.model().segments = ["   "]
    transcribe(world)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "no speech" in logged.lower() and "1000" not in logged


# --- The real transcription test is behind its own switch --------------------------------------------

def test_the_real_transcription_test_has_its_own_switch(monkeypatch):
    from tests import conftest
    from tests import test_listener_transcription_real as real
    assert real.pytestmark.name == "real_transcription"
    variable, _ = conftest.OPT_IN_GATES["real_transcription"]
    assert variable == "RUN_REAL_TRANSCRIPTION_TEST"
    assert variable not in ("RUN_REAL_MICROPHONE_TEST", "RUN_REAL_RECORDING_TEST",
                            "RUN_REAL_MODEL_TEST")


def test_the_other_real_switches_cannot_turn_real_transcription_on(monkeypatch):
    class Item:
        def __init__(self):
            self.markers = []

        def get_closest_marker(self, name):
            return object() if name == "real_transcription" else None

        def add_marker(self, marker):
            self.markers.append(marker)

    from tests import conftest
    for variable in ("RUN_REAL_MICROPHONE_TEST", "RUN_REAL_RECORDING_TEST", "RUN_REAL_MODEL_TEST"):
        monkeypatch.setenv(variable, "1")
    monkeypatch.delenv("RUN_REAL_TRANSCRIPTION_TEST", raising=False)
    item = Item()
    conftest.pytest_collection_modifyitems(None, [item])
    assert [marker.name for marker in item.markers] == ["skip"]


def test_the_real_transcription_test_never_downloads_and_executes_nothing():
    from config.settings import PROJECT_ROOT
    source = (PROJECT_ROOT / "tests" / "test_listener_transcription_real.py").read_text(encoding="utf-8")
    assert "fetch_model" not in source and "local_files_only=False" not in source
    assert "handle_command" not in source and "pyautogui" not in source
