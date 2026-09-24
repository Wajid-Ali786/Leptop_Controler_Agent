"""
MANUAL real acceptance for Phase 2 Feature 6 (Task 6b2): the whole spoken path, end to end.

Marked real_voice_console and skipped unless RUN_REAL_VOICE_CONSOLE_TEST=1 - which no other switch
sets. It must be run WITH -s, because you type into it:

    $env:RUN_REAL_VOICE_CONSOLE_TEST=1
    venv\\Scripts\\python.exe -m pytest tests/test_voice_console_real.py -s

Without -s, pytest replaces stdin with an object that raises as soon as anything reads it, and every
prompt would reach you only after the test had finished - so the test refuses to start without it.

What is REAL here: the settings from config/config.yaml, the emergency-stop hotkey context, the
speech model, your microphone, the recognizer, the Task 6a acceptance/correction loop, and the whole
normal command pipeline (parser, safety gate, Executor, Verifier). Nothing is faked. Four functions
are WRAPPED so the test can record safe evidence - when the model became ready, when the microphone
opened, which exact string you accepted, and what the pipeline returned - and every wrapper calls
the real implementation.

You do all the typing: `listen`, the Enter that ends the recording, the acceptance (or a correction),
and any safety confirmation. The test never answers for you, because the manual gate is the thing
being accepted.

Privacy: no audio file, no playback, no PCM anywhere, and neither the transcript nor the accepted
command is written to the log. Both appear on your screen on purpose - reading and accepting them is
the point - and the test keeps the accepted string only in memory, to compare it.
"""
import contextlib
import sys
import time

import pytest

import main
from app import console, voice_console
from app.executor import hotkey
from app.executor.models import OPEN_APP, Outcome
from app.listener import adapter, logic, microphone
from app.listener.models import ModelStatus, VoiceFailure
from config.settings import PROJECT_ROOT, SettingsError

pytestmark = pytest.mark.real_voice_console

PHRASE = "open notepad"
NEEDS_DASH_S = (
    "This acceptance is interactive: run it with -s, or pytest hides every prompt and input() "
    "raises.\n    $env:RUN_REAL_VOICE_CONSOLE_TEST=1\n"
    "    venv\\Scripts\\python.exe -m pytest tests/test_voice_console_real.py -s")
NEEDS_ENABLED = ("Set listener.enabled: true in config/config.yaml before running this manual "
                 "acceptance. The test never changes your configuration, and there is no override - "
                 "that gate is part of what is being accepted.")
AUDIO_SUFFIXES = (".wav", ".pcm", ".raw", ".mp3", ".flac", ".ogg", ".m4a")


def announce(text=""):
    print(text, flush=True)


class Evidence:
    """Everything the test may assert on afterwards. Safe metadata, plus the accepted string, which
    is kept in memory only and never logged."""

    def __init__(self):
        self.model_at = None
        self.model_status = None
        self.capture_at = None
        self.captures = 0
        self.transcriptions = 0
        self.displayed = []        # every candidate the console showed, by identity
        self.handed = []           # every string handle_command actually received
        self.replies = []
        self.hotkey_active = None
        self.hotkey_entered = False
        self.hotkey_left = False


@pytest.fixture
def evidence(request, monkeypatch):
    """Wrap - never replace - the four places worth recording."""
    if request.config.getoption("capture") != "no" or type(sys.stdin).__name__ == "DontReadFromInput":
        pytest.skip(NEEDS_DASH_S)
    try:
        settings = logic.listener_settings()
    except SettingsError as exc:
        pytest.skip(f"{exc}\n{NEEDS_ENABLED}")
    if settings.enabled is not True:
        pytest.skip(NEEDS_ENABLED)

    seen = Evidence()
    real_model, real_capture = adapter.ensure_model, adapter.capture
    real_transcribe, real_handle = adapter.transcribe, console.handle_command
    real_line, real_listening = voice_console.command_line, hotkey.listening

    def ensure_model(configured):
        status = real_model(configured)
        seen.model_at, seen.model_status = time.monotonic(), status
        return status

    def capture(configured, **kwargs):
        seen.captures += 1
        if seen.capture_at is None:
            seen.capture_at = time.monotonic()
        return real_capture(configured, **kwargs)

    def transcribe(recording, configured):
        seen.transcriptions += 1
        return real_transcribe(recording, configured)

    def command_line(candidate):
        seen.displayed.append(candidate)   # the object itself, not a copy
        return real_line(candidate)

    def handle_command(text, **kwargs):
        seen.handed.append(text)
        reply = real_handle(text, **kwargs)
        seen.replies.append(reply)
        return reply

    @contextlib.contextmanager
    def listening():
        seen.hotkey_entered = True
        with real_listening() as state:
            seen.hotkey_active = hotkey.status().active
            try:
                yield state
            finally:
                seen.hotkey_left = True

    monkeypatch.setattr(adapter, "ensure_model", ensure_model)
    monkeypatch.setattr(adapter, "capture", capture)
    monkeypatch.setattr(adapter, "transcribe", transcribe)
    monkeypatch.setattr(voice_console, "command_line", command_line)
    monkeypatch.setattr(console, "handle_command", handle_command)
    monkeypatch.setattr(main.hotkey, "listening", listening)
    return seen


@pytest.fixture(autouse=True)
def nothing_left_running():
    yield
    assert microphone.owner() is None, f"the microphone is still owned by {microphone.owner()!r}"
    worker = voice_console._worker
    assert worker is None or not worker.thread.is_alive(), "a capture worker is still running"


def test_a_spoken_command_opens_notepad_through_the_normal_safe_pipeline(evidence, tmp_path):
    """The whole path, with you at the keyboard. Read the script below before you start."""
    started = time.time()
    announce("")
    announce("=" * 78)
    announce("MANUAL VOICE ACCEPTANCE - you drive this; the test types nothing for you.")
    announce("")
    announce("  For the clearest evidence, close any Notepad windows first (this test will NOT")
    announce("  close anything for you). Then, at the prompts:")
    announce("")
    announce("    1. type:  listen")
    announce(f'    2. say:   "{PHRASE}"   then press Enter to finish the recording')
    announce("    3. read the transcript, then type: accept")
    announce("       (if it was misheard, type correct or redictate first, and accept the")
    announce("        corrected command - that is a valid run)")
    announce("    4. type:  exit")
    announce("")
    announce("  Nothing is saved or played back. You do not need to press Ctrl+Alt+Backspace.")
    announce("=" * 78)
    announce("")

    exit_code = main.main(["--voice"])

    announce("")
    announce("-" * 78)
    if isinstance(evidence.model_status, VoiceFailure):
        pytest.skip(f"the speech model is not ready, so nothing could be accepted: "
                    f"{evidence.model_status.message}")
    assert isinstance(evidence.model_status, ModelStatus), "voice mode never prepared a model"

    # 1-4: voice mode really started, with the hotkey up and the model ready before the microphone.
    assert evidence.hotkey_entered and evidence.hotkey_left, "the hotkey context did not wrap the run"
    assert evidence.hotkey_active is True, "the emergency-stop hotkey was not active during the run"
    assert logic.listener_settings().enabled is True
    assert evidence.captures >= 1, ("no recording was made, so there is nothing to accept - "
                                    "real acceptance remains open")
    assert evidence.model_at is not None and evidence.capture_at is not None
    assert evidence.model_at < evidence.capture_at, "the microphone opened before the model was ready"

    # 5-12: something was recognized, shown, and explicitly accepted by a person.
    assert evidence.transcriptions >= 1, "nothing was recognized"
    assert evidence.displayed, "no candidate was ever shown for acceptance"
    assert evidence.handed, ("nothing was accepted, so nothing ran - a cancelled or abandoned "
                             "attempt is not acceptance evidence")

    accepted, reply = evidence.handed[-1], evidence.replies[-1]
    announce(f"ACCEPTED CANDIDATE: [{accepted}]")
    announce(f"PIPELINE SAID:      {reply.status.value} - {reply.message}")
    announce(f"recordings: {evidence.captures}, recognitions: {evidence.transcriptions}, "
             f"candidates shown: {len(evidence.displayed)}, commands run: {len(evidence.handed)}")

    # 13: the exact string shown is the exact string that ran - identity, not equality.
    assert any(candidate is accepted for candidate in evidence.displayed), (
        "the string handed to handle_command was not one that was displayed for acceptance")
    assert accepted is evidence.displayed[-1], "the last candidate shown is the one that ran"

    # 14-15: the normal parser/safety/Executor/Verifier path did the work, and Notepad really opened.
    reading = console.preview(accepted)
    assert reading.is_command and reading.kind == OPEN_APP, (
        f"the accepted command was not an open-app command ({reading!r}); say '{PHRASE}' and accept "
        f"a candidate that reads as one")
    assert reply.status is console.Status.RAN, f"the command did not run: {reply.message}"
    assert reply.result is not None and reply.result.ok, f"opening Notepad failed: {reply.message}"
    assert reply.result.outcome is Outcome.DONE and reply.result.verified, (
        f"the Verifier did not confirm Notepad's window appeared: {reply.message}")
    assert exit_code == 0

    # 16-18: nothing was kept, and nothing private was written down.
    assert _audio_files(tmp_path) == [] and _audio_files(PROJECT_ROOT / "data") == []
    logged = _log_text(tmp_path)
    assert accepted.strip() not in logged and PHRASE not in logged.lower(), (
        "the accepted command reached the log file")
    announce("")
    announce("Notepad is still open - close it yourself; this test does not close anything.")
    announce(f"(run took {time.time() - started:.0f} s)")
    announce("-" * 78)


def _audio_files(folder):
    if not folder.is_dir():
        return []
    return [str(path) for path in folder.rglob("*") if path.suffix.lower() in AUDIO_SUFFIXES]


def _log_text(tmp_path) -> str:
    """Whatever this run wrote to companion.log (the autouse fixture puts it under tmp_path)."""
    found = list(tmp_path.rglob("companion.log*"))
    return "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in found)
