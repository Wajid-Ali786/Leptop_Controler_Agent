"""
Tests for Phase 2 Feature 3 Task 3a: finding, verifying and loading the speech model - offline.

Nothing here downloads, imports faster-whisper or loads a real model. app.listener.adapter._whisper()
and _ct2() are replaced by fakes: a faster-whisper whose download_model() either finds a folder in a
pretend Hugging Face cache or raises FileNotFoundError (as huggingface_hub's LocalEntryNotFoundError
does), whose WhisperModel records exactly what it was given, and a ctranslate2 that reports whatever
capabilities the test chooses - including a CUDA GPU this laptop does not have. The CUDA branches are
therefore proven against fakes only; nothing here claims they ran on real CUDA hardware.

The model folders themselves are real folders under tmp_path, so the completeness check - the guard
against faster-whisper's missing-tokenizer network fetch - runs against a real filesystem.
"""
import dataclasses
import logging
import threading
from types import SimpleNamespace

import pytest

from app.listener import adapter, logic
from app.listener.models import MODEL_UNAVAILABLE, ListenerSettings, ModelStatus, VoiceFailure

SECRET_TEXT = "C:/Users/Someone/private/path/model.bin could not be opened"
GOOD_FILES = {"config.json": b"{}", "model.bin": b"\x00" * 64, "tokenizer.json": b"{}",
              "vocabulary.txt": b"a\nb\n"}
CPU_TYPES = frozenset({"float32", "int16", "int8", "int8_float32"})  # what this laptop reports
CUDA_TYPES = frozenset({"float32", "float16", "int8", "int8_float16", "int8_float32", "bfloat16"})


# --- Fakes ------------------------------------------------------------------------------------------

class FakeLocalEntryNotFound(FileNotFoundError):
    """huggingface_hub's LocalEntryNotFoundError is a FileNotFoundError subclass (checked in the audit)."""


class FakeModel:
    def __init__(self, path, device, compute_type):
        self.path, self.device, self.compute_type = path, device, compute_type


def model_folder(root, size="small", files=None):
    folder = root / f"models--Systran--faster-whisper-{size}" / "snapshots" / "abc123"
    folder.mkdir(parents=True, exist_ok=True)
    for name, content in (GOOD_FILES if files is None else files).items():
        (folder / name).write_bytes(content)
    return folder


class FakeWhisper:
    def __init__(self, root):
        self.root = root
        self.cached = {}          # size -> folder already in the pretend cache
        self.lookups = []         # every download_model() call
        self.downloads = []       # the ones allowed to use the network
        self.constructed = []     # every WhisperModel() call
        self.construct_errors = {}  # device -> exception to raise
        self.download_error = None
        self.download_files = None
        self.gate = None
        self.utils = SimpleNamespace(download_model=self.download_model)

    def download_model(self, size, local_files_only=False, cache_dir=None, **extra):
        self.lookups.append(dict(size=size, local_files_only=local_files_only, cache_dir=cache_dir))
        if local_files_only:
            if size not in self.cached:
                raise FakeLocalEntryNotFound(SECRET_TEXT)
            return str(self.cached[size])
        self.downloads.append(size)
        if self.download_error is not None:
            raise self.download_error
        self.cached[size] = model_folder(self.root, size, self.download_files)
        return str(self.cached[size])

    def WhisperModel(self, path, device="auto", compute_type="default", local_files_only=False, **extra):
        self.constructed.append(dict(path=path, device=device, compute_type=compute_type,
                                     local_files_only=local_files_only))
        if self.gate is not None:
            self.gate()
        error = self.construct_errors.get(device)
        if error is not None:
            raise error
        return FakeModel(path, device, compute_type)


class FakeCT2:
    def __init__(self, cpu=CPU_TYPES, cuda_count=0, cuda=None, count_error=None):
        self.cpu, self.cuda, self.cuda_count, self.count_error = cpu, cuda, cuda_count, count_error

    def get_cuda_device_count(self):
        if self.count_error is not None:
            raise self.count_error
        return self.cuda_count

    def get_supported_compute_types(self, device):
        if device == "cpu":
            return set(self.cpu)
        if self.cuda is None:
            raise RuntimeError("CUDA failed with error CUDA driver version is insufficient")
        return set(self.cuda)


def listener(root, *, size="small", device="auto", compute_type="auto", local_only=True):
    return ListenerSettings(enabled=True, model_size=size, model_dir=str(root),
                            local_files_only=local_only, device=device, compute_type=compute_type,
                            language="auto", sample_rate=16000, input_device="", vad_filter=True,
                            min_silence_ms=800, max_utterance_seconds=15.0,
                            initial_prompt_terms=("notepad",), voice_stop_enabled=True)


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "models"
    root.mkdir()
    whisper = FakeWhisper(root)
    world = SimpleNamespace(root=root, whisper=whisper, ct2=FakeCT2(), whisper_imports=0)

    def import_whisper():
        world.whisper_imports += 1
        return whisper
    monkeypatch.setattr(adapter, "_whisper", import_whisper)
    monkeypatch.setattr(adapter, "_ct2", lambda: world.ct2)
    return world


def cached(world, size="small", files=None):
    world.whisper.cached[size] = model_folder(world.root, size, files)
    return world.whisper.cached[size]


def only_construction(world):
    assert len(world.whisper.constructed) == 1, world.whisper.constructed
    return world.whisper.constructed[0]


def assert_unavailable(result, *words):
    assert isinstance(result, VoiceFailure) and result.kind == MODEL_UNAVAILABLE, result
    for word in words:
        assert word in result.message, (word, result.message)


# --- No implicit download --------------------------------------------------------------------------

def test_a_missing_model_is_unavailable_and_nothing_is_downloaded(world):
    result = adapter.ensure_model(listener(world.root))
    assert_unavailable(result, "isn't downloaded", logic.FETCH_COMMAND)
    assert world.whisper.downloads == [] and world.whisper.constructed == []
    assert all(lookup["local_files_only"] is True for lookup in world.whisper.lookups)


def test_the_lookup_is_local_only_and_in_the_project_model_folder(world):
    cached(world)
    adapter.ensure_model(listener(world.root))
    assert world.whisper.lookups == [dict(size="small", local_files_only=True, cache_dir=str(world.root))]


def test_whisper_model_receives_the_verified_folder_never_a_model_name(world):
    folder = cached(world)
    adapter.ensure_model(listener(world.root))
    given = only_construction(world)
    assert given["path"] == str(folder) and given["path"] != "small"
    assert (folder / "tokenizer.json").is_file(), "a folder that really holds the files"
    assert given["local_files_only"] is True


def test_a_missing_tokenizer_is_refused_before_the_model_is_ever_constructed(world):
    """Without tokenizer.json, faster-whisper fetches one from the internet whatever local_files_only
    says. So the folder is refused first, and WhisperModel is never called."""
    cached(world, files={k: v for k, v in GOOD_FILES.items() if k != "tokenizer.json"})
    result = adapter.ensure_model(listener(world.root))
    assert_unavailable(result, "incomplete", "tokenizer.json", logic.FETCH_COMMAND)
    assert world.whisper.constructed == []


@pytest.mark.parametrize("files, missing", [
    ({**GOOD_FILES, "model.bin": b""}, "model.bin"),                       # present but empty
    ({k: v for k, v in GOOD_FILES.items() if k != "config.json"}, "config.json"),
    ({k: v for k, v in GOOD_FILES.items() if k != "vocabulary.txt"}, "vocabulary.json or vocabulary.txt"),
    ({**GOOD_FILES, "vocabulary.json": b"[]"}, "found both"),
])
def test_an_incomplete_model_folder_is_refused(world, files, missing):
    cached(world, files=files)
    assert_unavailable(adapter.ensure_model(listener(world.root)), missing)
    assert world.whisper.constructed == []


@pytest.mark.parametrize("vocabulary", ["vocabulary.json", "vocabulary.txt"])
def test_either_vocabulary_format_is_accepted_and_preprocessor_config_is_optional(world, vocabulary):
    files = {k: v for k, v in GOOD_FILES.items() if k != "vocabulary.txt"}
    cached(world, files={**files, vocabulary: b"x"})
    assert isinstance(adapter.ensure_model(listener(world.root)), ModelStatus)


def test_a_settings_object_that_allows_downloads_is_refused_outright(world):
    """Validation already refuses local_files_only: false; ensure_model refuses it again on its own."""
    cached(world)
    result = adapter.ensure_model(listener(world.root, local_only=False))
    assert_unavailable(result, "must be true", logic.FETCH_COMMAND)
    assert world.whisper.lookups == [] and world.whisper_imports == 0


def test_nothing_is_imported_until_a_model_is_actually_wanted(world):
    assert world.whisper_imports == 0
    adapter.forget_model()
    assert world.whisper_imports == 0
    cached(world)
    adapter.ensure_model(listener(world.root))
    assert world.whisper_imports == 1


# --- Device and compute type ------------------------------------------------------------------------

def test_auto_with_no_cuda_runs_on_the_cpu_with_int8(world):
    cached(world)
    status = adapter.ensure_model(listener(world.root))
    assert (status.device, status.compute_type, status.fell_back_from) == ("cpu", "int8", None)
    assert only_construction(world)["compute_type"] == "int8"


def test_auto_with_a_reported_cuda_gpu_runs_on_cuda_with_float16(world):
    cached(world)
    world.ct2 = FakeCT2(cuda_count=1, cuda=CUDA_TYPES)
    status = adapter.ensure_model(listener(world.root))
    assert (status.device, status.compute_type) == ("cuda", "float16")


@pytest.mark.parametrize("ct2", [
    FakeCT2(cuda_count=1, cuda=None),                              # a GPU, but inspecting it fails
    FakeCT2(count_error=RuntimeError("no driver")),               # even counting fails
    FakeCT2(cuda_count=1, cuda=frozenset({"bfloat16"})),          # nothing on our CUDA list
])
def test_auto_takes_the_cpu_when_cuda_evidence_is_not_good_enough(world, ct2):
    cached(world)
    world.ct2 = ct2
    assert adapter.ensure_model(listener(world.root)).device == "cpu"
    assert only_construction(world)["device"] == "cpu"


def test_explicit_cuda_without_a_usable_gpu_fails_and_never_tries_the_cpu(world):
    cached(world)
    result = adapter.ensure_model(listener(world.root, device="cuda"))
    assert_unavailable(result, "no usable CUDA GPU", "never switches")
    assert world.whisper.constructed == []


def test_explicit_cpu_stays_on_the_cpu_even_with_a_gpu(world):
    cached(world)
    world.ct2 = FakeCT2(cuda_count=1, cuda=CUDA_TYPES)
    assert adapter.ensure_model(listener(world.root, device="cpu")).device == "cpu"


def test_an_explicit_compute_type_the_device_does_not_support_is_never_swapped(world):
    cached(world)
    result = adapter.ensure_model(listener(world.root, device="cpu", compute_type="float16"))
    assert_unavailable(result, "'float16' isn't supported on CPU", "int8", "never swapped")
    assert world.whisper.constructed == []


def test_no_acceptable_compute_type_at_all_is_unavailable(world):
    cached(world)
    world.ct2 = FakeCT2(cpu=frozenset({"int16"}))
    assert_unavailable(adapter.ensure_model(listener(world.root)), "None of the computation types")


def test_ctranslate2_is_never_told_auto_or_default(world):
    cached(world)
    adapter.ensure_model(listener(world.root))
    given = only_construction(world)
    assert given["device"] in ("cpu", "cuda") and given["compute_type"] not in ("auto", "default")


@pytest.mark.parametrize("device, requested, supported, expected", [
    ("cpu", "auto", CPU_TYPES, "int8"),
    ("cpu", "auto", {"float32", "int16"}, "float32"),
    ("cpu", "auto", {"int16"}, None),
    ("cuda", "auto", CUDA_TYPES, "float16"),
    ("cuda", "auto", {"int8_float16", "int8", "float32"}, "int8_float16"),
    ("cuda", "auto", {"int8", "float32"}, "int8"),
    ("cuda", "auto", {"float32"}, "float32"),
    ("cpu", "int16", CPU_TYPES, "int16"),        # explicit and supported: exactly that
    ("cpu", "float16", CPU_TYPES, None),         # explicit and unsupported: nothing else instead
])
def test_the_compute_type_policy(device, requested, supported, expected):
    assert logic.choose_compute_type(device, requested, supported) == expected


def test_auto_device_with_an_explicit_type_cuda_lacks_uses_the_cpu():
    plan = logic.plan_model_load("auto", "int16", CPU_TYPES, 1, frozenset({"float16"}))
    assert plan == logic.LoadPlan("cpu", "int16")


# --- CUDA reported, CUDA load fails ---------------------------------------------------------------

@pytest.mark.parametrize("error, category", [
    (RuntimeError("Library cublas64_12.dll is not found or cannot be loaded"), logic.LOAD_ERROR),
    (OSError("cudnn64_9.dll missing"), logic.LOAD_ERROR),
    (MemoryError(), logic.OUT_OF_MEMORY),
])
def test_auto_falls_back_to_the_cpu_once_and_says_so(world, error, category, caplog):
    cached(world)
    world.ct2 = FakeCT2(cuda_count=1, cuda=CUDA_TYPES)
    world.whisper.construct_errors["cuda"] = error
    with caplog.at_level(logging.INFO):
        status = adapter.ensure_model(listener(world.root))
    assert (status.device, status.compute_type) == ("cpu", "int8"), "compute re-resolved for the CPU"
    assert (status.fell_back_from, status.fallback_reason) == ("cuda", category)
    assert [c["device"] for c in world.whisper.constructed] == ["cuda", "cpu"], "exactly one retry"
    assert any("trying the CPU once" in r.getMessage() and r.levelno == logging.WARNING
               for r in caplog.records)


def test_no_fallback_when_cuda_rejects_the_settings(world):
    cached(world)
    world.ct2 = FakeCT2(cuda_count=1, cuda=CUDA_TYPES)
    world.whisper.construct_errors["cuda"] = ValueError("Invalid compute type")
    assert_unavailable(adapter.ensure_model(listener(world.root)), "on CUDA", "rejected the settings")
    assert [c["device"] for c in world.whisper.constructed] == ["cuda"]


def test_explicit_cuda_never_falls_back(world):
    cached(world)
    world.ct2 = FakeCT2(cuda_count=1, cuda=CUDA_TYPES)
    world.whisper.construct_errors["cuda"] = RuntimeError("cublas missing")
    assert_unavailable(adapter.ensure_model(listener(world.root, device="cuda")), "on CUDA")
    assert [c["device"] for c in world.whisper.constructed] == ["cuda"]


def test_a_programming_error_during_construction_propagates_without_fallback(world):
    cached(world)
    world.ct2 = FakeCT2(cuda_count=1, cuda=CUDA_TYPES)
    world.whisper.construct_errors["cuda"] = TypeError("a defect")
    with pytest.raises(TypeError, match="a defect"):
        adapter.ensure_model(listener(world.root))
    assert [c["device"] for c in world.whisper.constructed] == ["cuda"]
    assert adapter._loaded is None


def test_the_fallback_failing_too_is_unavailable(world):
    cached(world)
    world.ct2 = FakeCT2(cuda_count=1, cuda=CUDA_TYPES)
    world.whisper.construct_errors.update(cuda=RuntimeError("cublas"), cpu=MemoryError())
    assert_unavailable(adapter.ensure_model(listener(world.root)), "on CUDA", "nor afterwards on CPU")
    assert adapter._loaded is None


def test_the_fallback_never_substitutes_an_explicit_compute_type(world):
    cached(world)
    world.ct2 = FakeCT2(cuda_count=1, cuda=CUDA_TYPES)
    world.whisper.construct_errors["cuda"] = RuntimeError("cublas")
    result = adapter.ensure_model(listener(world.root, compute_type="float16"))
    assert_unavailable(result, "'float16' isn't supported on CPU")
    assert [c["device"] for c in world.whisper.constructed] == ["cuda"]


def test_an_incomplete_model_never_reaches_cuda_or_its_fallback(world):
    cached(world, files={k: v for k, v in GOOD_FILES.items() if k != "tokenizer.json"})
    world.ct2 = FakeCT2(cuda_count=1, cuda=CUDA_TYPES)
    assert_unavailable(adapter.ensure_model(listener(world.root)), "incomplete")
    assert world.whisper.constructed == []


@pytest.mark.parametrize("requested, category, falls_back", [
    ("auto", logic.LOAD_ERROR, True), ("auto", logic.OUT_OF_MEMORY, True),
    ("auto", logic.REJECTED_SETTINGS, False), ("cuda", logic.LOAD_ERROR, False),
])
def test_the_fallback_rule(requested, category, falls_back):
    assert logic.may_fall_back(requested, logic.LoadPlan("cuda", "float16"), category) is falls_back
    assert logic.may_fall_back(requested, logic.LoadPlan("cpu", "int8"), category) is False


# --- One model per process -----------------------------------------------------------------------------

def test_a_second_request_reuses_the_same_model(world):
    cached(world)
    first = adapter.ensure_model(listener(world.root))
    instance = adapter._loaded.model
    second = adapter.ensure_model(listener(world.root))
    assert first.reused is False and isinstance(first.load_seconds, float) and first.load_seconds >= 0
    assert second.reused is True and second.load_seconds is None, "a reuse did no loading"
    assert (second.device, second.compute_type) == (first.device, first.compute_type)
    assert adapter._loaded.model is instance and len(world.whisper.constructed) == 1


def test_concurrent_first_callers_share_one_construction(world):
    """The fake constructor waits (briefly) for all eight callers: without the lock they would all
    arrive and eight models would be built; with it only one caller ever reaches the constructor."""
    cached(world)
    callers = 8
    everyone = threading.Barrier(callers)

    def wait_for_everyone():
        try:
            everyone.wait(timeout=0.3)
        except threading.BrokenBarrierError:
            pass
    world.whisper.gate = wait_for_everyone
    results = []
    threads = [threading.Thread(target=lambda: results.append(adapter.ensure_model(listener(world.root))))
               for _ in range(callers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert len(world.whisper.constructed) == 1
    assert sorted(result.reused for result in results) == [False] + [True] * (callers - 1)


def test_a_failed_load_is_not_cached_and_a_later_call_retries(world):
    cached(world)
    world.whisper.construct_errors["cpu"] = RuntimeError("transient")
    assert_unavailable(adapter.ensure_model(listener(world.root)))
    assert adapter._loaded is None
    del world.whisper.construct_errors["cpu"]
    assert adapter.ensure_model(listener(world.root)).reused is False
    assert len(world.whisper.constructed) == 2


def test_a_missing_model_is_retried_next_time_it_is_wanted(world):
    assert_unavailable(adapter.ensure_model(listener(world.root)), "isn't downloaded")
    cached(world)
    assert isinstance(adapter.ensure_model(listener(world.root)), ModelStatus)


def test_changed_settings_after_a_load_require_a_restart(world):
    cached(world)
    cached(world, size="base")
    adapter.ensure_model(listener(world.root))
    instance = adapter._loaded.model
    for changed in (listener(world.root, size="base"), listener(world.root, device="cpu"),
                    listener(world.root, compute_type="float32")):
        assert_unavailable(adapter.ensure_model(changed), "Restart the assistant")
    assert adapter._loaded.model is instance and len(world.whisper.constructed) == 1, \
        "the loaded model is never discarded or replaced behind its users"


def test_forget_model_clears_the_cache_for_the_next_load(world):
    cached(world)
    adapter.ensure_model(listener(world.root))
    adapter.forget_model()
    assert adapter._loaded is None
    assert adapter.ensure_model(listener(world.root)).reused is False
    assert len(world.whisper.constructed) == 2


def test_a_missing_backend_is_unavailable_and_retried_later(world, monkeypatch):
    monkeypatch.setattr(adapter, "_whisper", lambda: (_ for _ in ()).throw(ImportError("no module")))
    assert_unavailable(adapter.ensure_model(listener(world.root)), "faster-whisper library")
    assert adapter._loaded is None


def test_an_unexpected_error_in_the_lookup_propagates_and_leaves_nothing_cached(world):
    def broken(*args, **kwargs):
        raise KeyError("a defect in the lookup")
    world.whisper.utils.download_model = broken
    with pytest.raises(KeyError):
        adapter.ensure_model(listener(world.root))
    assert adapter._loaded is None


# --- Privacy and the boundary -----------------------------------------------------------------------

def test_no_path_or_exception_text_reaches_a_message_or_the_log(world, caplog):
    cached(world, files={k: v for k, v in GOOD_FILES.items() if k != "tokenizer.json"})
    with caplog.at_level(logging.DEBUG):
        outcomes = [adapter.ensure_model(listener(world.root)),                     # incomplete
                    adapter.ensure_model(listener(world.root, size="base"))]         # not cached
    world.ct2 = FakeCT2(cuda_count=1, cuda=CUDA_TYPES)
    cached(world)
    world.whisper.construct_errors["cuda"] = RuntimeError(SECRET_TEXT)
    with caplog.at_level(logging.DEBUG):
        outcomes.append(adapter.ensure_model(listener(world.root, device="cuda")))
    for shown in [o.message for o in outcomes] + [caplog.text] + [repr(o) for o in outcomes]:
        assert str(world.root) not in shown and "Someone" not in shown and "private" not in shown
    assert [r.getMessage() for r in caplog.records] == [
        "Speech model unavailable (incomplete)", "Speech model unavailable (not_downloaded)",
        "Speech model unavailable (load_failed)"]


def test_the_status_carries_metadata_only(world):
    cached(world)
    status = adapter.ensure_model(listener(world.root))
    fields = {field.name: getattr(status, field.name) for field in dataclasses.fields(status)}
    assert set(fields) == {"model_size", "device", "compute_type", "reused", "load_seconds",
                           "fell_back_from", "fallback_reason"}
    assert all(value is None or isinstance(value, (str, bool, float)) for value in fields.values()), \
        "no model object, no path"
    assert str(world.root) not in repr(status)


def test_the_model_object_is_never_handed_out(world):
    cached(world)
    for result in (adapter.ensure_model(listener(world.root)), adapter.ensure_model(listener(world.root))):
        assert isinstance(result, ModelStatus) and not isinstance(result, FakeModel)
    public = [name for name in vars(adapter) if not name.startswith("_")]
    assert not any(isinstance(getattr(adapter, name), FakeModel) for name in public)


@pytest.mark.parametrize("kwargs", [
    dict(reused=True, load_seconds=1.0), dict(reused=False, load_seconds=None),
    dict(reused=False, load_seconds=1.0, fell_back_from="cuda"),
    dict(reused=False, load_seconds=1.0, fallback_reason="load_error"),
])
def test_a_status_cannot_claim_what_did_not_happen(kwargs):
    with pytest.raises(ValueError):
        ModelStatus(model_size="small", device="cpu", compute_type="int8", **kwargs)


# --- The one download path ---------------------------------------------------------------------------

def test_fetch_model_downloads_into_the_project_folder_and_verifies_it(world):
    folder = adapter.fetch_model("small", str(world.root))
    assert world.whisper.downloads == ["small"]
    assert world.whisper.lookups == [dict(size="small", local_files_only=False, cache_dir=str(world.root))]
    assert folder == str(world.whisper.cached["small"])


def test_fetch_model_refuses_a_download_that_arrived_incomplete(world):
    world.whisper.download_files = {k: v for k, v in GOOD_FILES.items() if k != "tokenizer.json"}
    assert_unavailable(adapter.fetch_model("small", str(world.root)), "incomplete", "tokenizer.json")


@pytest.mark.parametrize("error", [ConnectionError(SECRET_TEXT), FakeLocalEntryNotFound(SECRET_TEXT)])
def test_a_failed_download_names_the_failure_type_only(world, error):
    world.whisper.download_error = error
    result = adapter.fetch_model("small", str(world.root))
    assert_unavailable(result, "couldn't be downloaded", type(error).__name__)
    assert "private" not in result.message


def test_fetch_model_rejects_an_unknown_size(world):
    with pytest.raises(ValueError):
        adapter.fetch_model("enormous", str(world.root))


def test_ctrl_c_during_a_download_is_left_to_the_script(world):
    world.whisper.download_error = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        adapter.fetch_model("small", str(world.root))


# --- The pure pieces --------------------------------------------------------------------------------

def test_a_relative_model_dir_is_inside_the_project_and_an_absolute_one_is_kept(tmp_path):
    from config.settings import PROJECT_ROOT
    assert logic.model_root("data/models") == PROJECT_ROOT / "data" / "models"
    assert logic.model_root(str(tmp_path)) == tmp_path


@pytest.mark.parametrize("sizes, missing", [
    ({"config.json": 1, "model.bin": 1, "tokenizer.json": 1, "vocabulary.txt": 1}, ()),
    ({"config.json": 1, "model.bin": 1, "tokenizer.json": 1, "vocabulary.json": 1}, ()),
    ({"config.json": 1, "model.bin": 0, "tokenizer.json": 1, "vocabulary.txt": 1}, ("model.bin",)),
    ({}, ("config.json", "model.bin", "tokenizer.json", "vocabulary.json or vocabulary.txt")),
])
def test_what_counts_as_a_complete_model(sizes, missing):
    assert logic.missing_model_files(sizes) == missing


def test_every_model_message_is_fixed_wording_that_formats():
    values = dict(size="small", missing="tokenizer.json", device="CPU", requested="float16",
                  supported="int8", reason="x", cuda_reason="y", category="ConnectionError")
    for code in logic.MODEL_MESSAGES:
        failure = logic.model_unavailable(code, **values)
        assert failure.kind == MODEL_UNAVAILABLE and "{" not in failure.message


# --- The real-model tests are opt-in and can never download --------------------------------------------

def test_the_real_model_tests_are_behind_their_own_switch_and_never_download(monkeypatch):
    from tests import conftest
    from tests import test_listener_model_real as real
    from config.settings import PROJECT_ROOT
    assert real.pytestmark.name == "real_model"
    assert conftest.OPT_IN_GATES["real_model"][0] == "RUN_REAL_MODEL_TEST"
    source = (PROJECT_ROOT / "tests" / "test_listener_model_real.py").read_text(encoding="utf-8")
    assert "fetch_model" not in source and "local_files_only=False" not in source
    assert "sounddevice" not in source and "capture(" not in source, "no microphone"


class _FakeHttpxError(Exception):
    """Stands in for an httpx transport error that escapes huggingface_hub's retries."""


_FakeHttpxError.__module__ = "httpx._exceptions"


def test_a_network_error_escaping_huggingface_hub_is_download_trouble(world):
    world.whisper.download_error = _FakeHttpxError(SECRET_TEXT)
    result = adapter.fetch_model("small", str(world.root))
    assert_unavailable(result, "couldn't be downloaded (_FakeHttpxError)")


def test_a_defect_during_a_download_propagates(world):
    world.whisper.download_error = TypeError("a defect in the download code")
    with pytest.raises(TypeError):
        adapter.fetch_model("small", str(world.root))
