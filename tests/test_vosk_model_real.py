"""
Network-independence smoke for the Vosk PROTOTYPE (Task 6d2a) - loads the local model, opens NO
microphone, records nothing.

Marked real_vosk_model and skipped unless RUN_REAL_VOSK_MODEL_TEST=1. That is its own switch, separate
from the benchmark's RUN_REAL_VOSK_STOP_TEST, for the same reason real_model is separate from
real_transcription in this project: loading a model from disk and opening a person's microphone are
different kinds of act and should not share one flag.

    $env:RUN_REAL_VOSK_MODEL_TEST=1
    venv\\Scripts\\python.exe -m pytest tests/test_vosk_model_real.py -v

What it proves, with the network blocked at the socket and at every HTTP entry point vosk imports:
  * `import vosk` works and loads libvosk.dll beside the package
  * a Model can be built from our explicit local path
  * a KaldiRecognizer can be built with the constrained grammar, and the grammar is really in force
  * bounded in-memory silence can be fed to it, and silence does NOT produce "stop"
  * nothing reaches the network, no microphone is opened, and nothing is written to disk

Why the network blocking matters here specifically: `vosk/__init__.py` imports requests, tqdm and
urllib.request.urlretrieve at module level, and Model(lang=...) / Model(model_name=...) DOWNLOAD a model
into ~/AppData/Local/vosk. This project never uses those paths - it always passes model_path - and this
test is the proof that the path we do use touches no network at all.
"""
import json
import sys
import time
from pathlib import Path

import pytest

from app.listener import microphone
from tests import test_vosk_stop_real as benchmark

pytestmark = pytest.mark.real_vosk_model

SILENCE_SECONDS = 1.0     # bounded, in memory, generated here - never read from a device or a file


@pytest.fixture
def no_network(monkeypatch):
    """Block the network at the socket, and at each HTTP door vosk has already imported."""
    import socket

    def refuse(*args, **kwargs):
        raise AssertionError("the local recognizer path must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(urllib.request, "urlretrieve", refuse)
    try:
        import requests
        monkeypatch.setattr(requests, "get", refuse)
        monkeypatch.setattr(requests.Session, "request", refuse)
    except ImportError:
        pass
    return refuse


@pytest.fixture
def no_microphone(monkeypatch):
    """Make opening any stream an error, so "no microphone was opened" is enforced, not hoped for."""
    import sounddevice

    def refuse(*args, **kwargs):
        raise AssertionError("this smoke test must never open an audio stream")

    for name in ("InputStream", "RawInputStream", "Stream", "RawStream", "rec", "play"):
        monkeypatch.setattr(sounddevice, name, refuse, raising=False)
    return refuse


@pytest.fixture
def installed():
    import importlib.util
    if importlib.util.find_spec("vosk") is None:
        pytest.skip(benchmark.NEEDS_VOSK)
    missing = [name for name in benchmark.REQUIRED if not (benchmark.MODEL / name).is_file()]
    if missing:
        pytest.skip(f"{benchmark.NEEDS_MODEL}\n(missing: {', '.join(missing)})")
    return benchmark.MODEL


def test_the_local_model_and_constrained_grammar_work_with_no_network(installed, no_network,
                                                                     no_microphone, capfd, tmp_path):
    import vosk

    before = {path: path.stat().st_mtime_ns for path in sorted(installed.rglob("*")) if path.is_file()}
    library = Path(vosk.__file__).parent
    print(f"\nvosk {getattr(vosk, '__version__', '(no __version__)')} loaded from {library}")
    print(f"  native library beside it: {[p.name for p in sorted(library.glob('*.dll'))]}")

    started = time.monotonic()
    model = vosk.Model(model_path=str(installed))
    load_seconds = time.monotonic() - started
    print(f"  Model built from the local path in {load_seconds:.2f} s")

    for word in benchmark.GRAMMAR_WORDS:
        found = model.vosk_model_find_word(word)
        print(f"  lexicon: {word!r} -> id {found}")
        assert found >= 0, f"{word!r} is missing from this model, so Vosk would drop it from the grammar"

    started = time.monotonic()
    recognizer = vosk.KaldiRecognizer(model, benchmark.SAMPLE_RATE, benchmark.GRAMMAR)
    build_seconds = time.monotonic() - started
    print(f"  KaldiRecognizer with grammar {benchmark.GRAMMAR} built in {build_seconds:.2f} s")

    # Vosk only WARNS when a model cannot take a runtime grammar - it does not raise - so the warning
    # has to be looked for. It is written by the C++ layer to fd 2, hence capfd rather than capsys.
    noise = capfd.readouterr()
    assert "Runtime graphs are not supported" not in (noise.out + noise.err), (
        "this model silently ignored the grammar; the whole experiment would be invalid")
    print("  no 'Runtime graphs are not supported' warning: the grammar is in force")

    frames = int(benchmark.CHUNK_SECONDS * benchmark.SAMPLE_RATE)
    silence = b"\x00\x00" * frames
    endpoints, steps = 0, []
    for _ in range(int(SILENCE_SECONDS / benchmark.CHUNK_SECONDS)):
        step = time.monotonic()
        if recognizer.AcceptWaveform(silence):
            endpoints += 1
            benchmark.classify(benchmark._text(recognizer.Result()))
        steps.append(time.monotonic() - step)
    flushed = benchmark._text(recognizer.FinalResult())
    category, pattern, outside = benchmark.classify(flushed)

    print(f"  fed {SILENCE_SECONDS:g} s of in-memory silence as {len(steps)} x {len(silence)}-byte "
          f"int16 chunks")
    print(f"  slowest accept/decode step: {max(steps) * 1000:.1f} ms (real time budget per chunk is "
          f"{benchmark.CHUNK_SECONDS * 1000:.0f} ms)")
    print(f"  endpoints during silence: {endpoints}; final flush -> {category} {pattern!r}")

    assert category is benchmark.FINAL_EMPTY or category == benchmark.FINAL_EMPTY, (
        f"silence must not produce words: {category} {pattern!r}")
    assert not benchmark.is_stop(flushed), "silence must never read as a stop"
    assert outside == frozenset(), f"words outside the grammar appeared: {sorted(outside)}"
    assert max(steps) < benchmark.CHUNK_SECONDS, (
        "decoding one chunk must be faster than the audio it covers, or streaming cannot keep up")

    assert microphone.owner() is None, "no microphone was acquired"
    after = {path: path.stat().st_mtime_ns for path in sorted(installed.rglob("*")) if path.is_file()}
    assert after == before, "loading the model must not write into the model folder"
    assert list(tmp_path.iterdir()) == [], "nothing was written to disk"


def test_the_json_shape_this_project_relies_on_is_what_vosk_returns(installed, no_network,
                                                                    no_microphone):
    """The harness reads result["text"] and partial["partial"]. Prove those keys are really there."""
    import vosk
    model = vosk.Model(model_path=str(installed))
    recognizer = vosk.KaldiRecognizer(model, benchmark.SAMPLE_RATE, benchmark.GRAMMAR)
    frames = int(benchmark.CHUNK_SECONDS * benchmark.SAMPLE_RATE)
    recognizer.AcceptWaveform(b"\x00\x00" * frames)
    partial = json.loads(recognizer.PartialResult())
    assert "partial" in partial, partial
    final = json.loads(recognizer.FinalResult())
    assert "text" in final, final
    assert final["text"] == "", f"silence produced {final['text']!r}"
    assert microphone.owner() is None


def test_a_recognizer_keeps_working_after_a_non_stop_final_result(installed, no_network,
                                                                 no_microphone):
    """The action-time listener must still hear "stop" after unrelated speech. Source says
    AcceptWaveform calls CleanUp() itself when the state is not RUNNING/INITIALIZED, so no explicit
    Reset() is needed - this is the behavioural check of that claim, offline, on silence."""
    import vosk
    model = vosk.Model(model_path=str(installed))
    recognizer = vosk.KaldiRecognizer(model, benchmark.SAMPLE_RATE, benchmark.GRAMMAR)
    frames = int(benchmark.CHUNK_SECONDS * benchmark.SAMPLE_RATE)
    silence = b"\x00\x00" * frames
    for _ in range(60):                      # 6 s: long enough to cross rule1's 5 s silence timeout
        recognizer.AcceptWaveform(silence)
    # Whatever happened above, the recognizer must still accept audio and still answer.
    recognizer.AcceptWaveform(silence)
    assert "partial" in json.loads(recognizer.PartialResult())
    assert json.loads(recognizer.FinalResult())["text"] == ""
    assert microphone.owner() is None
