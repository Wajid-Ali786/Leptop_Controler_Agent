"""
REAL speech-model acceptance for Phase 2 Feature 3 (loading only).

Marked real_model and skipped unless RUN_REAL_MODEL_TEST=1. These tests load the model that is ALREADY
in listener.model_dir (data/models). They never download - the network is switched off for their whole
duration - never touch the microphone and never transcribe. If the model isn't there, they SKIP and say
how to get it (python scripts/fetch_voice_model.py); they never fetch it themselves.

    RUN_REAL_MODEL_TEST=1 python -m pytest tests/test_listener_model_real.py -s

The cold-start test runs in a fresh child process, because only a process that has imported nothing
yet can measure what the first spoken command will pay: importing faster-whisper, then loading the
model. Those two numbers are an input to the open Feature 6 decision - whether the voice console should
load the model when it starts rather than on the first command.
"""
import json
import socket
import subprocess
import sys
import textwrap
import time

import pytest

from app.listener import adapter, logic
from app.listener.models import ModelStatus, VoiceFailure
from config.settings import PROJECT_ROOT

pytestmark = pytest.mark.real_model

NO_NETWORK = textwrap.dedent("""
    import socket
    def _refuse(*args, **kwargs):
        raise OSError("network access is forbidden in the real-model test")
    socket.socket.connect = _refuse
    socket.socket.connect_ex = _refuse
    socket.create_connection = _refuse
    socket.getaddrinfo = _refuse
""")

COLD_START = NO_NETWORK + textwrap.dedent("""
    import json, time, sys
    sys.path.insert(0, {root!r})
    from app.listener import adapter, logic
    started = time.perf_counter()
    adapter._whisper()
    imported = time.perf_counter() - started
    status = adapter.ensure_model(logic.listener_settings())
    total = time.perf_counter() - started
    if hasattr(status, "load_seconds"):
        print(json.dumps(dict(ok=True, import_seconds=imported, load_seconds=status.load_seconds,
                              total_seconds=total, device=status.device,
                              compute_type=status.compute_type, fell_back_from=status.fell_back_from)))
    else:
        print(json.dumps(dict(ok=False, message=status.message)))
""")


def announce(capsys, text):
    with capsys.disabled():
        print(f"\n    {text}", flush=True)


@pytest.fixture
def offline(monkeypatch):
    """No network for the whole test: any attempt to connect raises instead of downloading."""
    def refuse(*args, **kwargs):
        raise OSError("network access is forbidden in the real-model test")
    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.fixture
def settings():
    return logic.listener_settings()


def ready_or_skip(result):
    if isinstance(result, VoiceFailure) and "isn't downloaded" in result.message:
        pytest.skip(result.message)
    assert isinstance(result, ModelStatus), result.message if isinstance(result, VoiceFailure) else result
    return result


def test_a_cold_start_is_measured_in_a_fresh_process(capsys):
    """Import time, load time and the total from nothing loaded to a ready model - offline."""
    started = time.monotonic()
    done = subprocess.run([sys.executable, "-c", COLD_START.format(root=str(PROJECT_ROOT))],
                          capture_output=True, text=True, timeout=600, cwd=PROJECT_ROOT)
    assert done.returncode == 0, done.stderr[-2000:]
    report = json.loads(done.stdout.strip().splitlines()[-1])
    if not report["ok"]:
        if "isn't downloaded" in report["message"]:
            pytest.skip(report["message"])
        pytest.fail(report["message"])
    announce(capsys, f"cold import of faster-whisper: {report['import_seconds']:.1f} s")
    announce(capsys, f"model load (find, verify, construct): {report['load_seconds']:.1f} s")
    announce(capsys, f"nothing loaded -> ready model: {report['total_seconds']:.1f} s "
                     f"on {report['device']} ({report['compute_type']})"
                     f"{', fell back from ' + report['fell_back_from'] if report['fell_back_from'] else ''}")
    announce(capsys, f"child process wall time: {time.monotonic() - started:.1f} s")


def test_the_cached_model_loads_offline_on_the_policy_device(offline, settings, capsys):
    status = ready_or_skip(adapter.ensure_model(settings))
    ct2 = adapter._ct2()
    expected = logic.plan_model_load(settings.device, settings.compute_type,
                                     frozenset(ct2.get_supported_compute_types("cpu")),
                                     ct2.get_cuda_device_count(), None if ct2.get_cuda_device_count() == 0
                                     else frozenset(ct2.get_supported_compute_types("cuda")))
    if status.fell_back_from is None:
        assert (status.device, status.compute_type) == tuple(expected), \
            "the device and compute type are what the policy chooses from this machine's REPORTED support"
    assert status.reused is False and status.load_seconds > 0
    announce(capsys, f"loaded offline: {status!r}")


def test_a_second_request_reuses_the_same_instance_and_a_reset_reloads(offline, settings, capsys):
    first = ready_or_skip(adapter.ensure_model(settings))
    instance = adapter._loaded.model
    again = adapter.ensure_model(settings)
    assert again.reused is True and again.load_seconds is None
    assert adapter._loaded.model is instance, "the very same model object, not a second load"
    adapter.forget_model()
    assert adapter._loaded is None
    reloaded = adapter.ensure_model(settings)
    assert reloaded.reused is False and adapter._loaded.model is not instance
    assert (reloaded.device, reloaded.compute_type) == (first.device, first.compute_type), "deterministic"
    announce(capsys, f"first load {first.load_seconds:.1f} s, reuse instant, reload {reloaded.load_seconds:.1f} s")
