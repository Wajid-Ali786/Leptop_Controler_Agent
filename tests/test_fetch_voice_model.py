"""
Tests for scripts/fetch_voice_model.py - the one intentional model download - WITHOUT downloading.

adapter.fetch_model and adapter.ensure_model are replaced, so these tests prove what the script says,
which exit code it returns and in which order it does things, while nothing touches the network or
loads a model. The real script is run only by a person, deliberately (Feature 3 Task 3b).
"""
import sys
from pathlib import Path

import pytest

from app.listener import adapter, logic
from app.listener.models import ModelStatus, VoiceFailure

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_voice_model as script  # noqa: E402

READY = ModelStatus(model_size="small", device="cpu", compute_type="int8", reused=False, load_seconds=2.5)


@pytest.fixture
def calls(monkeypatch, tmp_path):
    folder = tmp_path / "snapshot"
    folder.mkdir()
    (folder / "model.bin").write_bytes(b"\x00" * 3_000_000)
    record = {"fetch": [], "ensure": [], "fetch_result": str(folder), "ensure_result": READY}

    def fake_fetch(size, model_dir):
        record["fetch"].append((size, model_dir))
        result = record["fetch_result"]
        if isinstance(result, BaseException):
            raise result
        return result

    def fake_ensure(settings):
        record["ensure"].append(settings)
        return record["ensure_result"]
    monkeypatch.setattr(adapter, "fetch_model", fake_fetch)
    monkeypatch.setattr(adapter, "ensure_model", fake_ensure)
    return record


def test_it_announces_the_download_then_downloads_then_proves_an_offline_load(calls, capsys):
    assert script.main([]) == script.OK
    out = capsys.readouterr().out
    assert "About to DOWNLOAD" in out and "huggingface.co" in out and "uses the network" in out
    assert "data" in out and "models" in out, "the destination is named"
    assert calls["fetch"] == [("small", "data/models")], "the configured size and folder"
    assert [s.model_size for s in calls["ensure"]] == ["small"]
    assert calls["ensure"][0].local_files_only is True, "the check is the normal, offline load"
    assert "loads offline on cpu (int8)" in out and "3 MB" in out


def test_a_different_size_can_be_asked_for_and_the_config_is_not_changed(calls, capsys):
    assert script.main(["--model", "base"]) == script.OK
    assert calls["fetch"][0][0] == "base" and calls["ensure"][0].model_size == "base"
    assert "listener.model_size is still 'small'" in capsys.readouterr().out


def test_an_unknown_size_is_refused_by_the_arguments(calls):
    with pytest.raises(SystemExit) as refused:
        script.main(["--model", "enormous"])
    assert refused.value.code == script.BAD_INPUT and calls["fetch"] == []


def test_a_failed_download_exits_1_and_skips_the_load(calls, capsys):
    calls["fetch_result"] = logic.model_unavailable("download_failed", size="small",
                                                    category="ConnectError")
    assert script.main([]) == script.FAILED
    assert "couldn't be downloaded (ConnectError)" in capsys.readouterr().out
    assert calls["ensure"] == []


def test_ctrl_c_exits_130_and_explains_the_partial_download(calls, capsys):
    calls["fetch_result"] = KeyboardInterrupt()
    assert script.main([]) == script.INTERRUPTED
    out = capsys.readouterr().out
    assert "Interrupted" in out and "kept" in out and "resumed" in out
    assert calls["ensure"] == []


def test_a_download_that_will_not_load_offline_exits_1(calls, capsys):
    calls["ensure_result"] = logic.model_unavailable("load_failed", size="small", device="CPU",
                                                     reason="there wasn't enough memory")
    assert script.main([]) == script.FAILED
    assert "did not load" in capsys.readouterr().out


def test_unreadable_settings_exit_2_without_downloading(calls, monkeypatch, capsys):
    from config.settings import SettingsError

    def broken():
        raise SettingsError("Setting 'listener.local_files_only' must be true")
    monkeypatch.setattr(logic, "listener_settings", broken)
    assert script.main([]) == script.BAD_INPUT
    assert calls["fetch"] == [] and "Can't read the listener settings" in capsys.readouterr().out


def test_a_cuda_fallback_is_reported_truthfully(calls, capsys):
    calls["ensure_result"] = ModelStatus(model_size="small", device="cpu", compute_type="int8",
                                         reused=False, load_seconds=4.0, fell_back_from="cuda",
                                         fallback_reason="load_error")
    assert script.main([]) == script.OK
    assert "CUDA failed: load_error; running on CPU" in capsys.readouterr().out
