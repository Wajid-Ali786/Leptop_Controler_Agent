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
normal command pipeline (parser, safety gate, Executor, Verifier). Nothing is faked. A few functions
are WRAPPED so the test can record safe evidence, and every wrapper calls the real implementation.

Evidence semantics matter here, and one of them was wrong once: adapter.transcribe() calls
ensure_model() itself to reuse the loaded model, so a wrapper that stamped "the model became ready"
on EVERY ensure_model call recorded the last reuse instead of the startup preload, and a long session
then looked as though the microphone had opened first. Every field below now says plainly whether it
means the first occurrence, a count, or all occurrences - see Evidence.

You do all the typing: `listen`, the Enter that ends the recording, the acceptance (or a correction),
and any safety confirmation. The test never answers for you, because the manual gate is the thing
being accepted.

Privacy, and what it does and does not claim:
  * Microphone PCM is never written to disk, and no audio file is created anywhere.
  * Neither the transcript nor the accepted command is written to the log. Both appear on your SCREEN
    on purpose - reading and accepting them is the point - and the accepted string is kept only in
    memory, to compare it.
  * TTS PLAYBACK IS EXPECTED. With speaker.enabled true, each reply is spoken aloud; that is part of
    what this session accepts. The old "nothing is played back" claim was true only before the
    speaker existed.

Two PRODUCTION limitations this acceptance measured, recorded here so they cannot be lost before the
Step 4 closeout is written (both are limitations of the app, not of this harness):

  1. TYPE_TEXT from voice mode always targets the console window you are driving. app/console.py sets
     `hands_over = focus is not None and needs_handover(action.kind)`, and run_voice_console/
     run_voice_mode never pass a focus object - main.py calls run_voice_mode() with none. Only
     run_console (typed mode, --console) builds a FocusHandover and prompts you to switch windows. So
     in voice mode the active window at preparation time IS the target, the safety gate names it
     truthfully ("type N characters into window 'Windows PowerShell'"), and there is no moment in a
     voice session when another window could be focused. docs/step4 line 185 already places the fix in
     the Phase 2/9 command window. DO NOT say `type ...` during this acceptance.

  2. Spoken Roman Urdu is not reliably transcribed as Roman text. faster-whisper small returned
     Devanagari for a Roman Urdu utterance, with the language reported as Hindi: "type mera naam Wajid
     hai" came back as "type मेरा नेम वाजिजली" (hi, 0.62) - and the content was wrong too. docs/step4
     assumes Roman Urdu is "English-script text the Brain interprets contextually"; with this model
     that premise does not hold, so Phase 3's Brain will need to handle Urdu and Hindi script as well
     as Roman Urdu.
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
from app.speaker import adapter as speaker_adapter
from app.listener.models import ModelStatus, VoiceFailure
from config.settings import PROJECT_ROOT, SettingsError

pytestmark = pytest.mark.real_voice_console

PHRASE = "open notepad"
APP = "notepad"
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


class Handoff:
    """One accepted candidate and what the real pipeline made of it, kept together so the pair can
    never drift apart the way two parallel lists can."""

    def __init__(self, text, reply=None, error=None):
        self.text = text
        self.reply = reply
        self.error = error

    @property
    def opened_the_app(self) -> bool:
        """A verified success: this candidate really opened the app, through the normal pipeline."""
        reading = console.preview(self.text)
        return bool(reading.is_command and reading.kind == OPEN_APP
                    and reading.equivalence_key[1].strip().lower() == APP
                    and self.reply is not None and self.reply.status is console.Status.RAN
                    and self.reply.result is not None and self.reply.result.ok
                    and self.reply.result.outcome is Outcome.DONE and self.reply.result.verified)

    def __repr__(self) -> str:  # the candidate is speech: described, never shown
        status = self.reply.status.value if self.reply is not None else f"raised {self.error!r}"
        return f"Handoff(characters={len(self.text)}, status={status})"


class Evidence:
    """What the test may assert on afterwards. Each field's meaning is stated, because mixing "first"
    and "last" is exactly what produced a false failure once."""

    def __init__(self):
        self.preload_completed_at = None  # FIRST successful ensure_model only (voice-mode startup)
        self.preload_status = None        # the FIRST ensure_model result, whatever it was
        self.ensure_model_calls = 0       # COUNT: startup preload, plus one reuse per transcription
        self.first_capture_at = None      # FIRST capture only
        self.captures = 0                 # COUNT of recordings
        self.transcriptions = 0           # COUNT of recognitions (never any text)
        self.displayed = []               # ALL candidates shown for acceptance, by identity, in order
        self.handoffs = []                # ALL accepted candidates paired with their reply, in order
        self.hotkey_entered = False       # the real hotkey context was entered
        self.hotkey_active = None         # hotkey.status().active, read from INSIDE that context
        self.hotkey_left = False          # and left again
        self.spoken = []                  # ALL speak() outcomes, in order: which voice said each
                                          # reply, or why none did. Evidence column D.

    @property
    def opened_the_app(self):
        """The LAST handoff that verifiably opened the app, or None. Chosen by evidence rather than by
        position, so an earlier rejected or refused attempt cannot be mistaken for the real one."""
        successes = [handoff for handoff in self.handoffs if handoff.opened_the_app]
        return successes[-1] if successes else None


def instrument(monkeypatch, clock=time.monotonic) -> Evidence:
    """Wrap - never replace - the places worth recording. Returns the Evidence they fill in.

    `clock` is injectable so the offline regression test can pin exact times."""
    seen = Evidence()
    real_model, real_capture = adapter.ensure_model, adapter.capture
    real_transcribe, real_handle = adapter.transcribe, console.handle_command
    real_line, real_listening = voice_console.command_line, hotkey.listening
    real_speak = speaker_adapter.speak

    def ensure_model(configured):
        status = real_model(configured)
        seen.ensure_model_calls += 1
        if seen.ensure_model_calls == 1:
            # ONLY the startup preload. Every later call is adapter.transcribe() reusing the loaded
            # model, and must not be able to move the preload timestamp forward.
            seen.preload_status = status
            if not isinstance(status, VoiceFailure):
                seen.preload_completed_at = clock()
        return status

    def capture(configured, **kwargs):
        seen.captures += 1
        if seen.first_capture_at is None:
            seen.first_capture_at = clock()
        return real_capture(configured, **kwargs)

    def transcribe(recording, configured):
        seen.transcriptions += 1
        return real_transcribe(recording, configured)

    def command_line(candidate):
        seen.displayed.append(candidate)   # the object itself, not a copy
        return real_line(candidate)

    def speak(text, configured):
        """Records WHICH voice spoke - never the text. Column D of the acceptance report."""
        outcome = real_speak(text, configured)
        seen.spoken.append(outcome)
        return outcome

    def handle_command(text, **kwargs):
        handoff = Handoff(text)
        seen.handoffs.append(handoff)      # recorded BEFORE the call, so a raise can't lose the pair
        try:
            handoff.reply = real_handle(text, **kwargs)
        except BaseException as exc:
            handoff.error = exc
            raise
        return handoff.reply

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
    monkeypatch.setattr(speaker_adapter, "speak", speak)
    monkeypatch.setattr(console, "handle_command", handle_command)
    monkeypatch.setattr(main.hotkey, "listening", listening)
    return seen


@pytest.fixture
def evidence(request, monkeypatch):
    if request.config.getoption("capture") != "no" or type(sys.stdin).__name__ == "DontReadFromInput":
        pytest.skip(NEEDS_DASH_S)
    try:
        settings = logic.listener_settings()
    except SettingsError as exc:
        pytest.skip(f"{exc}\n{NEEDS_ENABLED}")
    if settings.enabled is not True:
        pytest.skip(NEEDS_ENABLED)
    return instrument(monkeypatch)


@pytest.fixture(autouse=True)
def nothing_left_running():
    yield
    assert microphone.owner() is None, f"the microphone is still owned by {microphone.owner()!r}"
    worker = voice_console._worker
    assert worker is None or not worker.thread.is_alive(), "a capture worker is still running"


def test_a_spoken_command_opens_notepad_through_the_normal_safe_pipeline(evidence, tmp_path):
    """The whole path, with you at the keyboard. Read the script below before you start."""
    started = time.time()
    # BEFORE voice mode starts, the microphone opens, TTS speaks or you type anything: what audio
    # already exists under data/. A local model may legitimately ship static audio fixtures - the
    # sherpa keyword-spotting model carries test_wavs/*.wav with 2024 timestamps - so this folder can
    # never be asserted empty. What must hold is that THIS SESSION neither created nor removed one.
    audio_before = _audio_files(PROJECT_ROOT / "data")
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
    announce("    4. repeat 1-3 for any further utterances this acceptance calls for, then")
    announce("       type:  exit")
    announce("")
    announce("  Microphone audio is not saved. Replies may be spoken aloud. You do not need to")
    announce("  press Ctrl+Alt+Backspace. Do not say any `type ...` command: in voice mode it would")
    announce("  target this console window (see the limitations at the top of this file).")
    announce("=" * 78)
    announce("")

    exit_code = main.main(["--voice"])

    announce("")
    announce("-" * 78)
    if isinstance(evidence.preload_status, VoiceFailure):
        pytest.skip(f"the speech model is not ready, so nothing could be accepted: "
                    f"{evidence.preload_status.message}")
    assert isinstance(evidence.preload_status, ModelStatus), "voice mode never prepared a model"

    # 1-4: voice mode really started, with the hotkey up and the model ready before the microphone.
    assert evidence.hotkey_entered and evidence.hotkey_left, "the hotkey context did not wrap the run"
    assert evidence.hotkey_active is True, "the emergency-stop hotkey was not active during the run"
    assert logic.listener_settings().enabled is True
    assert evidence.captures >= 1, ("no recording was made, so there is nothing to accept - "
                                    "real acceptance remains open")
    assert evidence.preload_completed_at is not None and evidence.first_capture_at is not None
    assert evidence.preload_completed_at < evidence.first_capture_at, (
        "the microphone opened before the startup preload finished")

    # 5-12: something was recognized, shown, and explicitly accepted by a person.
    assert evidence.transcriptions >= 1, "nothing was recognized"
    assert evidence.displayed, "no candidate was ever shown for acceptance"
    assert evidence.handoffs, ("nothing was accepted, so nothing ran - a cancelled or abandoned "
                              "attempt is not acceptance evidence")

    # 13-15: the normal parser/safety/Executor/Verifier path opened the app, from a candidate that was
    # displayed and explicitly accepted. Chosen by evidence, so earlier rejected attempts don't count.
    opened = evidence.opened_the_app
    assert opened is not None, (
        f"no accepted candidate verifiably opened {APP} through the normal pipeline. Say "
        f"'{PHRASE}' and accept a candidate that reads as that command; the attempts were: "
        f"{evidence.handoffs}")
    announce(f"ACCEPTED CANDIDATE: [{opened.text}]")
    announce(f"PIPELINE SAID:      {opened.reply.status.value} - {opened.reply.message}")
    announce(f"recordings: {evidence.captures}, recognitions: {evidence.transcriptions}, "
             f"ensure_model calls: {evidence.ensure_model_calls}, "
             f"candidates shown: {len(evidence.displayed)}, accepted: {len(evidence.handoffs)}")
    announce("")
    announce("D. WHICH VOICE SPOKE EACH REPLY (categories only - never what was said):")
    if evidence.spoken:
        for number, outcome in enumerate(evidence.spoken, start=1):
            announce(f"   reply {number}: {outcome!r}")
    else:
        announce("   nothing was spoken - speaker.enabled is false, or every attempt was cancelled")

    # The exact string shown is the exact string that ran - identity, not equality.
    assert any(candidate is opened.text for candidate in evidence.displayed), (
        "the string handed to handle_command was not one that was displayed for acceptance")
    assert exit_code == 0

    # 16-18: nothing was kept, and nothing private was written down.
    # tmp_path belongs to this test, so it must be empty of audio outright. data/ is compared with the
    # snapshot taken before the session: equality catches a file this run CREATED and also one it
    # REMOVED, without excluding any model asset by name.
    assert _audio_files(tmp_path) == [], "this session wrote audio into its own temporary folder"
    assert _audio_files(PROJECT_ROOT / "data") == audio_before, (
        "this session created or removed a persistent audio file under data/ (compared with the "
        "snapshot taken before voice mode started)")
    logged = _log_text(tmp_path)
    assert opened.text.strip() not in logged and PHRASE not in logged.lower(), (
        "the accepted command reached the log file")
    announce("")
    announce("Notepad is still open - close it yourself; this test does not close anything.")
    announce(f"(run took {time.time() - started:.0f} s)")
    announce("-" * 78)


def _audio_files(folder):
    """Every audio-suffixed file under `folder`, sorted - so two snapshots compare deterministically
    however the filesystem happened to order them."""
    if not folder.is_dir():
        return []
    return sorted(str(path) for path in folder.rglob("*") if path.suffix.lower() in AUDIO_SUFFIXES)


def _log_text(tmp_path) -> str:
    """Whatever this run wrote to companion.log (the autouse fixture puts it under tmp_path)."""
    found = list(tmp_path.rglob("companion.log*"))
    return "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in found)
