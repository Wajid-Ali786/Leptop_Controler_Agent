"""
Offline regression tests for the MANUAL acceptance harness's evidence (tests/test_voice_console_real).

These test the harness, not the product - but the harness is what decides whether a real run counts,
and it got one thing wrong: adapter.transcribe() calls ensure_model() itself to reuse the loaded
model, so a wrapper that stamped "the model became ready" on every ensure_model call recorded the
LAST reuse instead of the startup preload. In a twelve-minute session that made a correct run look as
though the microphone had opened before the model was ready.

Everything here is offline: no microphone, no model, no action, no real transcription. The adapter
functions are replaced by fakes first, and the harness's own wrappers are then put around those - so
the ordering logic under test is exactly the one a real run uses, with a clock the test controls.
"""
import struct

import pytest

from app import console, voice_console
from app.listener import adapter
from app.listener.models import LIMIT, ListenerSettings, ModelStatus, Recording, Transcript, VoiceFailure
from tests import test_voice_console_real as harness

SETTINGS = ListenerSettings(
    enabled=True, model_size="small", model_dir="data/models", local_files_only=True, device="auto",
    compute_type="auto", language="auto", sample_rate=16000, input_device="", vad_filter=True,
    min_silence_ms=800, max_utterance_seconds=15.0, initial_prompt_terms=("notepad",),
    voice_stop_enabled=True)
LOADED = ModelStatus("small", "cpu", "int8", reused=False, load_seconds=7.9)
REUSED = ModelStatus("small", "cpu", "int8", reused=True, load_seconds=None)
RECORDING = Recording(struct.pack("<h", 900) * 1600, LIMIT)


class Clock:
    """A clock the test owns: every reading is the next number, so ordering is exact."""

    def __init__(self, *times):
        self.times = list(times)
        self.last = None

    def __call__(self):
        self.last = self.times.pop(0) if self.times else (self.last or 0) + 1
        return self.last


@pytest.fixture
def world(monkeypatch):
    """The real adapter functions replaced by fakes, with the harness's wrappers around them."""
    state = type("State", (), {})()
    state.models = [LOADED, REUSED, REUSED, REUSED]
    state.transcribe_reuses = True   # the real transcribe() asks ensure_model() for the model

    def ensure_model(settings):
        return state.models.pop(0) if state.models else REUSED

    def capture(settings, **kwargs):
        return RECORDING

    def transcribe(recording, settings):
        if state.transcribe_reuses:
            adapter.ensure_model(settings)   # exactly what the real adapter.transcribe does
        return Transcript(" open notepad", language="en")

    monkeypatch.setattr(adapter, "ensure_model", ensure_model)
    monkeypatch.setattr(adapter, "capture", capture)
    monkeypatch.setattr(adapter, "transcribe", transcribe)
    state.clock = Clock(100.0, 200.0, 300.0, 400.0, 500.0, 600.0)
    state.seen = harness.instrument(monkeypatch, clock=state.clock)
    return state


def voice_mode_session(world, recordings=1):
    """Replay the real order of events: one startup preload, then a recording and a recognition for
    each utterance - each of which asks ensure_model() for the model again."""
    adapter.ensure_model(SETTINGS)                    # run_voice_mode's preload
    for _ in range(recordings):
        adapter.capture(SETTINGS)                     # the recording worker
        adapter.transcribe(RECORDING, SETTINGS)       # which reuses the model


# --- The bug that produced a false failure -----------------------------------------------------------

def test_the_preload_timestamp_is_the_startup_one_not_the_last_reuse(world):
    voice_mode_session(world, recordings=3)
    seen = world.seen
    assert seen.preload_completed_at == 100.0, "the startup preload's own moment, and no other"
    assert seen.first_capture_at == 200.0, "the first recording"
    assert seen.ensure_model_calls == 4, "one preload plus one reuse per recognition"
    assert seen.preload_completed_at < seen.first_capture_at


def test_later_reuse_calls_cannot_move_the_preload_forward(world):
    adapter.ensure_model(SETTINGS)
    first = world.seen.preload_completed_at
    for _ in range(5):
        adapter.ensure_model(SETTINGS)
    assert world.seen.preload_completed_at == first
    assert world.seen.ensure_model_calls == 6


def test_the_first_capture_timestamp_is_the_first_one(world):
    voice_mode_session(world, recordings=4)
    assert world.seen.first_capture_at == 200.0
    assert world.seen.captures == 4 and world.seen.transcriptions == 4


def test_the_long_session_that_failed_before_now_reads_correctly(world):
    """The real run: seven recordings over twelve minutes, the last recognition long after the first
    recording. The ordering assertion must still say the preload came first."""
    world.clock = Clock(*[1.0] + [10.0 * step for step in range(1, 20)])
    world.seen = harness.instrument(pytest.MonkeyPatch(), clock=world.clock)
    voice_mode_session(world, recordings=7)
    assert world.seen.preload_completed_at == 1.0
    assert world.seen.first_capture_at == 10.0
    assert world.seen.preload_completed_at < world.seen.first_capture_at


def test_a_failed_preload_records_no_completion_time(world):
    world.models = [VoiceFailure("model_unavailable", "The 'small' model isn't downloaded yet.")]
    adapter.ensure_model(SETTINGS)
    assert world.seen.preload_completed_at is None
    assert isinstance(world.seen.preload_status, VoiceFailure), "the skip message still comes from it"
    assert world.seen.ensure_model_calls == 1


# --- Every other field's meaning ----------------------------------------------------------------------

def test_each_evidence_field_means_exactly_one_thing(world):
    seen = harness.Evidence()
    assert (seen.preload_completed_at, seen.preload_status, seen.first_capture_at) == (None, None, None)
    assert (seen.ensure_model_calls, seen.captures, seen.transcriptions) == (0, 0, 0)
    assert seen.displayed == [] and seen.handoffs == []
    assert (seen.hotkey_entered, seen.hotkey_active, seen.hotkey_left) == (False, None, False)
    assert not hasattr(seen, "model_at") and not hasattr(seen, "capture_at"), "the ambiguous names"


def test_the_accepted_candidate_and_its_reply_can_never_drift_apart(world, monkeypatch):
    """Two parallel lists would misalign the moment one call raised. A pair cannot."""
    def handle_command(text, **kwargs):
        if "boom" in text:
            raise RuntimeError("something went wrong in the pipeline")
        return console.CommandReply(console.Status.REFUSED, "no")
    monkeypatch.setattr(console, "handle_command", handle_command)
    seen = harness.instrument(monkeypatch)
    console.handle_command("open notepad")
    with pytest.raises(RuntimeError):
        console.handle_command("boom")
    console.handle_command("refresh")
    assert [len(handoff.text) for handoff in seen.handoffs] == [12, 4, 7]
    assert seen.handoffs[1].reply is None and isinstance(seen.handoffs[1].error, RuntimeError)
    assert seen.handoffs[0].reply is not None and seen.handoffs[2].reply is not None


def test_the_displayed_candidates_are_kept_by_identity(world, monkeypatch):
    seen = harness.instrument(monkeypatch)
    candidate = "  open   notepad  "
    voice_console.command_line(candidate)
    assert seen.displayed[-1] is candidate


# --- Which handoff counts as the successful one --------------------------------------------------------

def reply(status=console.Status.RAN, ok=True, outcome=None, message="Opened notepad."):
    from app.executor.models import ActionResult, ExecutorAction, Outcome
    action = ExecutorAction("open_app", "notepad")
    result = ActionResult(action, ok, message, outcome=outcome or (Outcome.DONE if ok else Outcome.FAILED))
    return console.CommandReply(status, message, action, result)


def handoff(text, **kwargs):
    return harness.Handoff(text, reply(**kwargs))


def test_only_a_verified_open_of_the_app_counts(world):
    seen = harness.Evidence()
    seen.handoffs = [handoff(" open notepad")]
    assert seen.opened_the_app is seen.handoffs[0]
    assert seen.handoffs[0].opened_the_app is True


@pytest.mark.parametrize("text, kwargs", [
    (" open note bag", {}),                                    # a different app: refused downstream
    (" open calculator", {}),                                  # the wrong app
    (" openmobile.txt file in notepad", {}),                   # not a command at all
    (" close notepad", {}),                                    # not an open
    (" open notepad", dict(ok=False, status=console.Status.RAN)),   # ran but failed
    (" open notepad", dict(status=console.Status.REFUSED)),     # refused
])
def test_everything_else_does_not_count(text, kwargs):
    assert harness.Handoff(text, reply(**kwargs)).opened_the_app is False


def test_an_unverified_success_does_not_count():
    from app.executor.models import Outcome
    assert harness.Handoff(" open notepad", reply(outcome=Outcome.UNVERIFIED)).opened_the_app is False


def test_the_successful_handoff_is_found_among_earlier_failed_attempts(world):
    """The permitted path: bad recognition accepted and refused, then the good one accepted."""
    seen = harness.Evidence()
    seen.handoffs = [handoff(" open note bag"), handoff(" open notepad"),
                     harness.Handoff(" open file manager", reply(ok=False))]
    assert seen.opened_the_app is seen.handoffs[1], "chosen by evidence, not by position"


def test_the_short_procedure_leaves_exactly_one_handoff(world):
    seen = harness.Evidence()
    seen.handoffs = [handoff(" open notepad")]
    assert len(seen.handoffs) == 1 and seen.opened_the_app is seen.handoffs[0]


def test_a_handoff_never_shows_the_command_it_carried():
    spoken = " open notepad phir type karo"
    shown = repr(handoff(spoken))
    assert "notepad" not in shown and "karo" not in shown
    assert f"characters={len(spoken)}" in shown


# --- The harness stays opt-in and inert ----------------------------------------------------------------

def test_the_harness_module_imports_no_backend_and_opens_nothing():
    import sys
    assert not {"faster_whisper", "sounddevice", "ctranslate2"} & set(sys.modules)
    assert harness.pytestmark.name == "real_voice_console"
    assert adapter._loaded is None


def test_the_harness_insists_on_dash_s():
    assert "-s" in harness.NEEDS_DASH_S and "RUN_REAL_VOICE_CONSOLE_TEST" in harness.NEEDS_DASH_S


def test_the_harness_never_edits_the_configuration():
    source = open(harness.__file__, encoding="utf-8").read()
    assert "config.yaml" in harness.NEEDS_ENABLED
    for forbidden in ("write_text(", "CONFIG_PATH =", "setenv", "monkeypatch.setattr(logic"):
        assert forbidden not in source, f"the harness must not change settings ({forbidden})"
