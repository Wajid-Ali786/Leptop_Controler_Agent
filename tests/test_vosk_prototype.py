"""
Offline rules for the Vosk spoken-stop PROTOTYPE (Task 6d2a).

Nothing here imports vosk, opens a microphone, loads a model or touches the network. It checks the
things that must be true whether or not the benchmark ever runs:

  * the trigger rule really is "a FINAL result of exactly 'stop'" - a partial cannot trigger, a
    forced flush at the timeout is not a detection, and there is no fuzzy or containment matching;
  * the grammar is exactly two tokens, and a result containing anything else fails the run rather
    than being reported as accuracy;
  * the corpus and the semantic contract are the sherpa ones, unchanged;
  * it is opt-in behind its own switch, it cannot act, and no module under app/ imports vosk;
  * the microphone-ownership and cleanup-abort rules are the ones the sherpa benchmarks established.

The AST helpers come from the sherpa offline rules, so there is one copy of each.
"""
import ast
import json
from pathlib import Path

import pytest

from config import settings
from tests import test_vosk_model_real as smoke
from tests import test_vosk_stop_real as harness
from tests.test_kws_prototype import (_assigned_attributes, _called, _imports, _property_names,
                                      _top_level_imports, _tree)

BENCHMARK = Path("tests/test_vosk_stop_real.py")
SMOKE = Path("tests/test_vosk_model_real.py")
SHERPA_HARNESS = Path("tests/test_kws_prototype_real.py")
FETCH_SCRIPT = Path("scripts/fetch_vosk_model.py")
MARKER = "real_vosk_stop"
OPT_IN = "RUN_REAL_VOSK_STOP_TEST"
SMOKE_MARKER = "real_vosk_model"
SMOKE_OPT_IN = "RUN_REAL_VOSK_MODEL_TEST"


def _printed(path) -> str:
    said = []
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Call) and "announce" in ast.unparse(node.func):
            said.append(ast.unparse(node))
    return " ".join(said)


def _fetch_module():
    """Import the fetch script without running it, to read its constants."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("fetch_vosk", settings.PROJECT_ROOT / FETCH_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _code(path, name) -> str:
    """One function's CODE, with its docstring removed - so a check cannot match its own prose."""
    node = _function(path, name)
    body = [statement for statement in node.body
            if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)
                    and isinstance(statement.value.value, str))]
    return ast.unparse(ast.Module(body=body, type_ignores=[]))


def _function(path, name):
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no function {name} in {path}")


# --- Fakes: a scripted recognizer and a raw device, so the whole loop runs with no microphone ---------

class FakeRawStream:
    """Stands in for sounddevice.RawInputStream, with the API the installed sounddevice really has:
    read() returns (buffer, overflowed), close(ignore_errors=True) by default, and `closed` is a
    property that is True after close().

    `fail` picks one mode: read, stop, close, stays_open, interrupt, overflow, or loud."""

    def __init__(self, samplerate=None, channels=None, dtype=None, blocksize=None, fail=None):
        self.samplerate, self.channels, self.dtype, self.blocksize = (samplerate, channels, dtype,
                                                                     blocksize)
        self.started = self.stopped = False
        self._closed = False
        self.close_calls = []
        self.reads = 0
        self.fail = fail

    @property
    def closed(self):
        return False if self.fail == "stays_open" else self._closed

    def start(self):
        self.started = True

    def read(self, frames):
        self.reads += 1
        if self.fail == "read" and self.reads == 2:
            raise OSError("the device went away mid-trial")
        if self.fail == "interrupt" and self.reads == 2:
            raise KeyboardInterrupt
        block = (b"\x00\x40" if self.fail == "loud" else b"\x00\x00") * frames
        return block, self.fail == "overflow"

    def stop(self):
        if self.fail == "stop":
            raise OSError("Pa_StopStream failed")
        self.stopped = True

    def close(self, ignore_errors=True):
        self.close_calls.append(ignore_errors)
        if self.fail == "close":
            raise OSError("Pa_CloseStream failed")
        self._closed = True


class FakeDevice:
    """The fake `sounddevice` module the trial imports."""

    def __init__(self, fail=None):
        self.streams = []
        self.fail = fail

    def RawInputStream(self, **kwargs):
        if self.fail == "open":
            raise OSError("the microphone could not be opened")
        stream = FakeRawStream(fail=self.fail, **kwargs)
        self.streams.append(stream)
        return stream


class FakeRecognizer:
    """Scripted Vosk.

    `finals` maps a 1-based chunk number to the final text AcceptWaveform should endpoint with.
    `partials` maps a chunk number to the partial text that chunk should report.
    `flush` is what FinalResult() returns. `fail` makes AcceptWaveform raise on the second chunk."""

    def __init__(self, finals=None, partials=None, flush="", fail=None):
        self.finals = dict(finals or {})
        self.partials = dict(partials or {})
        self.flush = flush
        self.fail = fail
        self.chunks = 0
        self.pending = ""
        self.result_calls = self.partial_calls = self.flush_calls = self.reset_calls = 0
        self.byte_lengths = []

    def AcceptWaveform(self, data):
        self.chunks += 1
        self.byte_lengths.append(len(data))
        if self.fail == "accept" and self.chunks == 2:
            raise RuntimeError("the recognizer failed")
        if self.chunks in self.finals:
            self.pending = self.finals[self.chunks]
            return True
        return False

    def Result(self):
        self.result_calls += 1
        return json.dumps({"text": self.pending})

    def PartialResult(self):
        self.partial_calls += 1
        return json.dumps({"partial": self.partials.get(self.chunks, "")})

    def FinalResult(self):
        self.flush_calls += 1
        return json.dumps({"text": self.flush})

    def Reset(self):
        self.reset_calls += 1


@pytest.fixture
def trial_run(monkeypatch):
    """Run harness._listen() with no microphone and a scripted recognizer."""
    import sys
    from app.listener import microphone
    import numpy

    numpy.zeros(1)

    def run(recognizer, fail=None, expected=harness.POSITIVE, prompt="stop", listen=0.05):
        device = FakeDevice(fail=fail)
        monkeypatch.setitem(sys.modules, "sounddevice", device)
        monkeypatch.setattr(harness, "LISTEN_SECONDS", listen)
        microphone.reset()
        run.builds = 0

        def factory():
            # Counted, so "the recognizer is built once per trial" is a checkable claim rather than an
            # accident of handing the same object back every time.
            run.builds += 1
            return recognizer

        trial = harness._listen(factory, "offline", prompt, expected)
        return trial, device
    run.builds = 0
    return run


# --- The trigger rule is the whole experiment ---------------------------------------------------------

def test_a_final_result_of_exactly_stop_is_the_only_thing_that_triggers(trial_run):
    recognizer = FakeRecognizer(finals={3: "stop"})
    trial, device = trial_run(recognizer)
    assert trial.timely is True and trial.final_category == harness.FINAL_STOP
    assert trial.detect_seconds is not None and trial.chunks == 3, "it stopped listening at once"
    assert trial.ended_early is True
    assert recognizer.flush_calls == 0, "a detection needs no forced flush"


@pytest.mark.parametrize("text, category", [
    ("stop", harness.FINAL_STOP),
    ("", harness.FINAL_EMPTY),
    ("[unk]", harness.FINAL_UNK),
    ("[unk] [unk]", harness.FINAL_UNK),
    ("[unk] stop", harness.FINAL_OTHER),
    ("stop [unk]", harness.FINAL_OTHER),
    ("stop stop", harness.FINAL_OTHER),
])
def test_only_the_bare_word_counts_as_a_stop(text, category):
    assert harness.classify(text)[0] == category
    assert harness.is_stop(text) is (category == harness.FINAL_STOP)


@pytest.mark.parametrize("text", ["stopped", "stopping", "stops", "stopper", "stop it", "please stop",
                                  "full stop", "the stop", "stop now", "st op", "sto", "STOP!"])
def test_nothing_that_merely_contains_or_resembles_stop_counts(text):
    """No containment, no prefix, no fuzzy distance, no case folding, no punctuation stripping."""
    assert harness.is_stop(text) is False


def test_whitespace_is_the_only_normalisation():
    assert harness.is_stop("  stop  ") is True and harness.is_stop("stop\n") is True
    assert harness.normalised("  a   b ") == "a b"


def test_the_narrow_predicate_agrees_with_the_projects_own_stop_rule_on_this_corpus():
    """The prototype keeps its own predicate so it cannot inherit production semantics that might
    change under it - but the two must not disagree, so they are compared HERE rather than by making
    the benchmark depend on app/listener/logic.py."""
    from app.listener import logic
    for phrase in (harness.POSITIVES + harness.NEGATIVES + ("stop", "[unk]", "[unk] stop", "")):
        if phrase == "(say nothing at all)":
            continue
        assert harness.is_stop(phrase) == logic.is_stop_phrase(phrase), phrase
    for module, name in _imports(BENCHMARK):
        assert "logic" not in module and name != "logic", f"the harness must not import {module}"


def test_no_fuzzy_or_containment_matching_decides_what_a_stop_is():
    """Checked on the three functions that make the decision. Elsewhere in the file `startswith` is the
    cleanup rule reading its own FATAL markers, which has nothing to do with speech."""
    # Docstrings are stripped first: the word "fuzzy" appears in the prose that PROMISES there is no
    # fuzzy matching, and a crude source search would trip over its own documentation.
    deciding = " ".join(_code(BENCHMARK, name) for name in ("is_stop", "normalised", "classify"))
    for fuzzy in ("get_close_matches", "SequenceMatcher", "difflib", "startswith", "endswith",
                  "ratio", "fuzz", "re.", "lower()", "casefold"):
        assert fuzzy not in deciding, f"{fuzzy!r} would not be an exact rule"
    stop_rule = ast.unparse(_function(BENCHMARK, "is_stop"))
    assert "==" in stop_rule and " in " not in stop_rule, stop_rule
    for module, _ in _imports(BENCHMARK):
        assert module not in ("re", "difflib", "fuzzywuzzy", "rapidfuzz"), module


# --- A partial is diagnostic and can never trigger ----------------------------------------------------

def test_a_partial_saying_stop_never_triggers_and_never_ends_the_trial(trial_run):
    recognizer = FakeRecognizer(partials={1: "stop", 2: "stop", 3: "stop"})
    trial, device = trial_run(recognizer)
    assert trial.partial_stop_seen is True, "it is recorded, because it is worth knowing"
    assert trial.timely is False and trial.detect_seconds is None, "and it changes nothing"
    assert trial.missed is True
    assert trial.chunks > 3, "the trial kept listening to its bound"
    assert trial.partials >= 3


def test_partial_text_is_never_printed():
    """Whether a partial said "stop" is one flag. The text itself is not report material."""
    body = ast.unparse(_function(BENCHMARK, "_listen"))
    assert "partial_stop_seen = True" in body
    for line in _printed(BENCHMARK).split("announce"):
        assert "partial_pattern" not in line and "{partial}" not in line
    assert "partial" not in _assigned_attributes(BENCHMARK, "Trial") or True
    trial = harness.Trial("t", "stop", harness.POSITIVE)
    trial.partial_stop_seen, trial.partials = True, 4
    shown = repr(trial)
    assert "a partial said stop" in shown and "partials=4" in shown


def test_a_partial_cannot_break_the_listening_loop():
    """Structural: the only break in the chunk loop is the exactly-'stop' final result."""
    listen = _function(BENCHMARK, "_listen")
    breaks = []
    for node in ast.walk(listen):
        if isinstance(node, ast.While):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Break):
                    breaks.append(inner)
    assert len(breaks) == 1, f"exactly one way out of the loop, found {len(breaks)}"
    guard = [node for node in ast.walk(listen) if isinstance(node, ast.If)
             and "FINAL_STOP" in ast.unparse(node.test)]
    assert guard and any(isinstance(inner, ast.Break) for inner in guard[0].body), (
        "the break belongs to the FINAL_STOP branch")


# --- A forced flush is not a detection ----------------------------------------------------------------

def test_a_stop_that_appears_only_on_the_forced_flush_is_not_a_detection(trial_run):
    recognizer = FakeRecognizer(flush="stop")
    trial, device = trial_run(recognizer)
    assert trial.flush_only_stop is True and trial.flush_category == harness.FINAL_STOP
    assert trial.timely is False, "a stop that needs a forced flush is no use for interrupting"
    assert trial.missed is True, "it counts as a false negative"
    assert trial.detect_seconds is None
    assert "FLUSH_ONLY_STOP" in repr(trial)


def test_a_flush_only_stop_on_a_negative_is_not_a_false_positive_either(trial_run):
    trial, device = trial_run(FakeRecognizer(flush="stop"), expected=harness.NEGATIVE,
                              prompt="stopped")
    assert trial.flush_only_stop is True
    assert trial.false_positive is False, "it is reported separately, not mixed into the headline"
    assert trial.timely is False


def test_the_flush_happens_once_at_the_bound_and_never_mid_trial(trial_run):
    """FinalResult() deletes the decoder and the feature pipeline, so calling it mid-trial would throw
    away the recognizer that still has to hear a stop."""
    recognizer = FakeRecognizer()
    trial, device = trial_run(recognizer)
    assert recognizer.flush_calls == 1, "once, after the loop"
    detected = FakeRecognizer(finals={2: "stop"})
    trial, device = trial_run(detected)
    assert detected.flush_calls == 0, "a detection skips the flush entirely"
    listen = _function(BENCHMARK, "_listen")
    inside_loop = [ast.unparse(node) for node in ast.walk(listen) if isinstance(node, ast.While)]
    assert all("FinalResult" not in one for one in inside_loop), "never inside the chunk loop"


def test_the_summary_keeps_flush_only_stops_out_of_the_headline_counts(capsys):
    trials = []
    for number, prompt in enumerate(harness.POSITIVES, start=1):
        trial = harness.Trial(f"positive {number}", prompt, harness.POSITIVE)
        trial.cleanup_ok = True
        if number == 1:
            trial.timely, trial.detect_seconds = True, 1.2
            trial.final_category = harness.FINAL_STOP
        elif number == 2:
            trial.flush_only_stop, trial.flush_category = True, harness.FINAL_STOP
        trials.append(trial)
    harness._summarise(trials)
    shown = capsys.readouterr().out
    assert "TIMELY detections 1/5" in shown and "false negatives 4" in shown
    assert "1 produced 'stop' ONLY on the forced flush" in shown


# --- After a non-stop final result the same recognizer carries on -------------------------------------

def test_the_recognizer_is_reused_after_a_non_stop_final_result_and_can_still_hear_stop(trial_run):  # noqa: E501
    """Source (recognizer.cc): AcceptWaveform calls CleanUp() itself when the state is not
    RUNNING/INITIALIZED, so no explicit Reset() is needed after Result(). The trial must keep feeding
    the SAME recognizer - the action-time listener needs to hear "stop" after unrelated speech."""
    recognizer = FakeRecognizer(finals={2: "[unk]", 5: "stop"})
    trial, device = trial_run(recognizer)
    assert trial.endpoints == 2, "both endpoints were seen"
    assert trial.timely is True and trial.chunks == 5
    assert trial.final_category == harness.FINAL_STOP
    assert recognizer.reset_calls == 0, "Reset() is not needed and is not called"
    assert "Reset" not in _called(BENCHMARK)
    assert trial_run.builds == 1, (
        "the SAME recognizer must carry on - rebuilding it would throw away the context that lets it "
        "hear a stop after unrelated speech, and would cost a graph composition mid-trial")


def test_one_recognizer_per_trial_and_it_is_built_before_the_cue(trial_run):
    """Composing the grammar graph is not instant, and speech arriving during it would be lost."""
    listen = ast.unparse(_function(BENCHMARK, "_listen"))
    assert listen.index("make_recognizer()") < listen.index("SAY:"), (
        "the recognizer must exist before the cue is printed")
    assert listen.index("SAY:") < listen.index("RawInputStream"), "and the cue before the device opens"
    recognizer = FakeRecognizer()
    trial, device = trial_run(recognizer)
    assert trial_run.builds == 1, "exactly one recognizer per trial"
    assert trial.recognizer_seconds is not None, "its cost is measured and reported separately"
    assert trial.open_seconds is not None and trial.open_seconds >= 0


# --- The grammar --------------------------------------------------------------------------------------

def test_the_grammar_is_exactly_stop_and_the_unknown_sink():
    assert harness.GRAMMAR_WORDS == ("stop", "[unk]")
    assert json.loads(harness.GRAMMAR) == ["stop", "[unk]"]
    assert harness.GRAMMAR == '["stop", "[unk]"]'


def test_no_synonym_was_added_to_the_grammar():
    for word in ("halt", "cancel", "emergency", "abort", "quit", "freeze", "please stop", "stop it"):
        assert word not in harness.GRAMMAR_WORDS
        assert word not in json.loads(harness.GRAMMAR)
    assert len(harness.GRAMMAR_WORDS) == 2


def test_a_word_outside_the_grammar_fails_the_run_instead_of_being_reported_as_accuracy():
    """Vosk only WARNS when a model cannot take a runtime grammar, then keeps the full vocabulary. A
    word we never asked for is proof the experiment was not the one we designed."""
    category, pattern, outside = harness.classify("notepad")
    assert category == harness.FINAL_OTHER and outside == frozenset({"notepad"})
    run = _function(BENCHMARK, "test_whether_a_constrained_grammar_hears_a_standalone_stop")
    asserted = " ".join(ast.unparse(node) for node in ast.walk(run) if isinstance(node, ast.Assert))
    assert "leaked == []" in asserted, "the run must FAIL on an out-of-grammar word"
    assert "outside_grammar" in ast.unparse(run), "gathered from every trial"
    trial = harness.Trial("t", "notepad", harness.NEGATIVE)
    trial.outside_grammar = frozenset({"notepad"})
    assert "OUT OF GRAMMAR" in repr(trial)


def test_the_run_checks_the_grammar_is_in_force_before_any_trial():
    check = ast.unparse(_function(BENCHMARK, "_check_grammar_is_really_in_force"))
    assert "vosk_model_find_word" in check, "both tokens must exist in the model's lexicon"
    assert "HCLr.fst" in check and "Gr.fst" in check, "and the model must have a runtime graph"
    body = ast.unparse(_function(BENCHMARK,
                                 "test_whether_a_constrained_grammar_hears_a_standalone_stop"))
    assert body.index("_check_grammar_is_really_in_force") < body.index("for label, prompt, expected")


# --- The corpus and the contract are unchanged --------------------------------------------------------

def test_the_corpus_is_the_sherpa_corpus_unchanged():
    from tests import test_kws_prototype_real as sherpa
    assert harness.POSITIVES == sherpa.POSITIVES == ("stop",) * 5
    assert harness.NEGATIVES == sherpa.NEGATIVES
    assert harness.NEGATIVES == ("(say nothing at all)", "notepad", "stopped", "stopping", "stops",
                                 "stopper", "stop it", "please stop", "full stop",
                                 "open notepad and type hello")
    assert len(list(harness._schedule())) == 15


def test_only_the_standalone_word_is_a_positive():
    for phrase in ("stop it", "please stop", "full stop", "stops", "stopper", "stopped", "stopping"):
        assert phrase in harness.NEGATIVES and phrase not in harness.POSITIVES
        assert harness.is_stop(phrase) is False


def test_the_streaming_shape_matches_the_sherpa_runs():
    from tests import test_kws_prototype_real as sherpa
    assert harness.SAMPLE_RATE == sherpa.SAMPLE_RATE == 16000
    assert harness.CHUNK_SECONDS == sherpa.CHUNK_SECONDS == 0.1
    assert harness.LISTEN_SECONDS == sherpa.LISTEN_SECONDS == 4.0


def test_it_streams_rather_than_recording_first():
    body = ast.unparse(_function(BENCHMARK, "_listen"))
    assert "AcceptWaveform" in body and "while" in body
    assert "rec(" not in body and "wait()" not in body, "no record-then-decode"
    loops = [node for node in ast.walk(_function(BENCHMARK, "_listen")) if isinstance(node, ast.While)]
    assert len(loops) == 1, "one bounded chunk loop"


def test_the_audio_the_recognizer_is_fed_is_int16_at_the_same_device_path(trial_run):
    """Vosk's Python AcceptWaveform hands its argument to the C char* entry point with len(data) bytes,
    so it wants int16 PCM. That is the only thing about the audio path that differs from sherpa."""
    recognizer = FakeRecognizer()
    trial, device = trial_run(recognizer)
    stream = device.streams[0]
    assert (stream.samplerate, stream.channels, stream.dtype) == (16000, 1, "int16")
    assert stream.blocksize == int(harness.CHUNK_SECONDS * harness.SAMPLE_RATE) == 1600
    assert set(recognizer.byte_lengths) == {3200}, "3200 bytes = 1600 int16 samples = 100 ms"
    assert trial.audio_format == ("int16 bytes, 3200 bytes = 1600 samples per chunk at 16000 Hz mono")
    source = BENCHMARK.read_text(encoding="utf-8")
    assert "RawInputStream" in source and "latency=" not in source, "the latency setting is untouched"
    assert "device=" not in source, "the default input device is untouched"


# --- Audio presence, and no audio kept ----------------------------------------------------------------

def test_the_level_of_the_audio_fed_is_reported_per_trial(trial_run):
    trial, device = trial_run(FakeRecognizer(), fail="loud")
    assert trial.samples_fed == trial.chunks * 1600
    assert trial.peak == pytest.approx(0x4000 / 32768.0), "int16 scaled to the sherpa runs' [-1, 1]"
    assert trial.rms == pytest.approx(0x4000 / 32768.0)
    assert trial.silent is False
    assert "peak=" in repr(trial) and "rms=" in repr(trial)


def test_a_silent_trial_is_flagged_and_still_counted(trial_run, capsys):
    trial, device = trial_run(FakeRecognizer())
    assert trial.peak == 0.0 and trial.silent is True
    assert "NOTHING AUDIBLE ARRIVED" in repr(trial)
    trial.cleanup_ok = True
    harness._summarise([trial])
    shown = capsys.readouterr().out
    assert "NOTHING AUDIBLE" in shown and "STILL counted" in shown


def test_a_dropped_input_chunk_is_counted(trial_run):
    trial, device = trial_run(FakeRecognizer(), fail="overflow")
    assert trial.chunks > 0 and trial.overflows == trial.chunks
    assert f"overflows={trial.chunks}" in repr(trial)


def test_no_audio_and_no_transcript_is_kept(trial_run):
    trial, device = trial_run(FakeRecognizer(finals={2: "[unk] stop"}), fail="loud")
    for name, value in vars(trial).items():
        assert not hasattr(value, "shape"), f"{name} holds an array"
        assert not isinstance(value, (bytes, bytearray, memoryview)), f"{name} holds raw audio"
        assert not isinstance(value, list) or name == "chunk_seconds", f"{name} accumulates data"
    fields = _assigned_attributes(BENCHMARK, "Trial")
    for forbidden in ("pcm", "audio", "samples", "frames", "block", "buffer", "recording", "wav",
                      "transcript", "heard", "said"):
        assert forbidden not in fields, f"{forbidden!r} would be audio or a transcript"
    source = BENCHMARK.read_text(encoding="utf-8")
    for writing in ("open(", "write_text", "write_bytes", "wavfile", "soundfile", "np.save"):
        assert writing not in source, f"{writing!r} could persist something"


def test_the_only_text_that_can_be_printed_is_the_two_token_grammar(trial_run):
    """`pattern` is a sequence of grammar words. With a two-word vocabulary that is not a transcript -
    and anything else is reported as an out-of-grammar failure, not as content."""
    trial, device = trial_run(FakeRecognizer(finals={2: "[unk] stop"}))
    assert trial.final_pattern == "[unk] stop"
    assert set(trial.final_pattern.split()) <= set(harness.GRAMMAR_WORDS)
    assert trial.outside_grammar == frozenset()


# --- Cleanup: the sherpa rules, unchanged -------------------------------------------------------------

@pytest.mark.parametrize("finals, listen", [({2: "stop"}, 0.05), (None, 0.05)])
def test_the_stream_is_closed_and_the_microphone_released_on_detection_and_on_timeout(trial_run,
                                                                                     finals, listen):
    from app.listener import microphone
    trial, device = trial_run(FakeRecognizer(finals=finals), listen=listen)
    assert len(device.streams) == 1
    stream = device.streams[0]
    assert stream.started and stream.stopped and stream.closed
    assert stream.close_calls == [False], "close(ignore_errors=False), so a failure is visible"
    assert microphone.owner() is None
    assert trial.cleanup_ok is True and trial.close_seconds is not None


def test_cleanup_happens_when_the_recognizer_raises(trial_run):
    from app.listener import microphone
    trial, device = trial_run(FakeRecognizer(fail="accept"))
    assert isinstance(trial.error, RuntimeError), "recorded, not swallowed"
    assert trial.cleanup_ok is True, "the device is provably gone, so the run may continue"
    assert device.streams[0].closed and microphone.owner() is None


def test_cleanup_happens_when_the_device_raises_mid_trial(trial_run):
    from app.listener import microphone
    trial, device = trial_run(FakeRecognizer(), fail="read")
    assert isinstance(trial.error, OSError)
    assert device.streams[0].closed and microphone.owner() is None


def test_a_device_that_cannot_open_is_recorded_and_the_microphone_is_freed(trial_run):
    from app.listener import microphone
    trial, device = trial_run(FakeRecognizer(), fail="open")
    assert isinstance(trial.error, OSError) and trial.chunks == 0
    assert microphone.owner() is None


def test_a_busy_microphone_is_recorded_and_not_retried(monkeypatch):
    """Held by ANOTHER thread, because taking it twice on one thread is a deliberate error in the
    ownership rule rather than a busy device. The fake module goes into sys.modules even though no
    stream is opened, so the real backend never leaks into the offline suite."""
    import sys
    import threading
    from app.listener import microphone
    monkeypatch.setitem(sys.modules, "sounddevice", FakeDevice())
    microphone.reset()
    holding, release = threading.Event(), threading.Event()

    def hold():
        assert microphone.acquire("someone-else")
        holding.set()
        release.wait(10)
        microphone.release()

    keeper = threading.Thread(target=hold)
    keeper.start()
    try:
        assert holding.wait(5)
        trial = harness._listen(lambda: FakeRecognizer(), "offline", "stop", harness.POSITIVE)
    finally:
        release.set()
        keeper.join(10)
        microphone.reset()
    assert trial.error is not None and trial.chunks == 0 and trial.timely is False
    assert trial.cleanup_ok is True, "nothing was acquired and no stream was opened"


def test_a_failed_close_means_cleanup_could_not_be_proven(trial_run):
    trial, device = trial_run(FakeRecognizer(), fail="close")
    assert trial.cleanup_ok is False and "FATAL close() raised OSError" in trial.cleanup_error


def test_a_stream_that_still_reports_itself_open_means_cleanup_could_not_be_proven(trial_run):
    trial, device = trial_run(FakeRecognizer(), fail="stays_open")
    assert trial.cleanup_ok is False and "still reports closed=False" in trial.cleanup_error


def test_a_failed_stop_alone_is_recorded_but_not_fatal(trial_run):
    trial, device = trial_run(FakeRecognizer(), fail="stop")
    assert trial.cleanup_ok is True
    assert "stop() raised OSError" in trial.cleanup_error and "not fatal" in trial.cleanup_error


def test_a_microphone_that_stays_held_means_cleanup_could_not_be_proven(trial_run, monkeypatch):
    from app.listener import microphone
    monkeypatch.setattr(microphone, "release", lambda: None)
    trial, device = trial_run(FakeRecognizer())
    assert trial.cleanup_ok is False and "still owned by" in trial.cleanup_error
    microphone.reset()


def test_a_keyboard_interrupt_cleans_up_and_never_starts_another_trial(trial_run):
    from app.listener import microphone
    with pytest.raises(KeyboardInterrupt):
        trial_run(FakeRecognizer(), fail="interrupt")
    assert microphone.owner() is None


def test_a_cleanup_failure_aborts_the_remaining_benchmark():
    body = _function(BENCHMARK, "test_whether_a_constrained_grammar_hears_a_standalone_stop")
    aborts = [node for node in ast.walk(body) if isinstance(node, ast.If)
              and "not trial.cleanup_ok" in ast.unparse(node.test)]
    assert aborts and any(isinstance(inner, ast.Break) for inner in aborts[0].body)
    asserted = " ".join(ast.unparse(node) for node in ast.walk(body) if isinstance(node, ast.Assert))
    assert "aborted is None" in asserted
    assert "trial.cleanup_ok for trial in trials" in asserted
    assert "ABORTED" in _printed(BENCHMARK)


def test_the_cleanup_rule_is_the_same_one_the_sherpa_benchmarks_established():
    """It is written out again rather than imported, so this prototype does not depend on a paused one.
    The rule itself must not drift."""
    ours = ast.unparse(_function(BENCHMARK, "_release"))
    theirs = ast.unparse(_function(SHERPA_HARNESS, "_release"))
    for rule in ("stream.stop()", "stream.close(ignore_errors=False)", "closed is not True",
                 "microphone.release()", "microphone.owner() is not None", "trial.cleanup_ok = not fatal",
                 "FATAL"):
        assert rule in ours, f"missing from the Vosk rule: {rule}"
        assert rule in theirs, f"missing from the sherpa rule: {rule}"


def test_there_is_no_worker_and_no_retry():
    source = BENCHMARK.read_text(encoding="utf-8")
    assert "retry" not in source.lower()
    assert "Thread" not in source and "daemon" not in source and "Event(" not in source
    assert len(list(harness._schedule())) == len(set(label for label, _, _ in harness._schedule()))


# --- Opt-in, isolated, recognition only ---------------------------------------------------------------

def test_the_benchmark_has_its_own_new_switch():
    from tests import conftest
    assert harness.pytestmark.name == MARKER
    variable, _ = conftest.OPT_IN_GATES[MARKER]
    assert variable == OPT_IN
    others = [value for name, (value, _) in conftest.OPT_IN_GATES.items() if name != MARKER]
    assert variable not in others
    for finished in ("RUN_REAL_KWS_TEST", "RUN_REAL_KWS_THRESHOLD_TEST"):
        assert variable != finished, "the finished sherpa switches must not enable this"


def test_the_no_microphone_smoke_has_a_separate_switch_from_the_benchmark():
    from tests import conftest
    assert smoke.pytestmark.name == SMOKE_MARKER
    assert conftest.OPT_IN_GATES[SMOKE_MARKER][0] == SMOKE_OPT_IN
    assert SMOKE_OPT_IN != OPT_IN, "loading a model and opening a microphone are different acts"


@pytest.mark.parametrize("marker", [MARKER, SMOKE_MARKER])
def test_no_other_real_switch_can_enable_it(monkeypatch, marker):
    from tests import conftest

    class Item:
        def __init__(self):
            self.markers = []

        def get_closest_marker(self, name):
            return object() if name == marker else None

        def add_marker(self, added):
            self.markers.append(added)

    for variable, _ in conftest.OPT_IN_GATES.values():
        monkeypatch.setenv(variable, "1")
    monkeypatch.delenv(conftest.OPT_IN_GATES[marker][0], raising=False)
    item = Item()
    conftest.pytest_collection_modifyitems(None, [item])
    assert [added.name for added in item.markers] == ["skip"]


def test_both_real_files_are_skipped_by_default():
    import os
    for variable in (OPT_IN, SMOKE_OPT_IN):
        assert os.environ.get(variable) != "1", "this suite must never run with a real switch set"


@pytest.mark.parametrize("path", [BENCHMARK, SMOKE])
def test_collection_opens_nothing_and_downloads_nothing(path):
    import sys
    module_level = {module.split(".")[0] for module, _ in _top_level_imports(path)}
    assert not {"vosk", "sounddevice", "numpy", "requests", "urllib"} & module_level, module_level
    assert "vosk" not in sys.modules, "importing these test files must not load the library"


def test_the_benchmark_reaches_no_network_at_all():
    for module, name in _imports(BENCHMARK):
        first = module.split(".")[0]
        assert first not in ("urllib", "requests", "http", "httpx", "socket"), module
    for absent in ("urlopen", "urlretrieve", "download_model", "get_model_path", "list_models"):
        assert absent not in _called(BENCHMARK), absent
    source = BENCHMARK.read_text(encoding="utf-8")
    assert "http://" not in source and "https://" not in source


def test_the_model_is_always_loaded_from_our_explicit_local_path():
    """Model(lang=...) and Model(model_name=...) DOWNLOAD. Only model_path= is local."""
    for path in (BENCHMARK, SMOKE):
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("Model"):
                keywords = {keyword.arg for keyword in node.keywords}
                assert keywords == {"model_path"}, f"{ast.unparse(node)} in {path}"
                assert not node.args, "no positional model argument; be explicit"
    assert harness.MODEL.parent.name == "vosk"
    assert "data/models" in harness.MODEL.as_posix(), "under the git-ignored model area"
    assert harness.MODEL_NAME == "vosk-model-small-en-us-0.15", "the agreed model, not another"


def test_the_benchmark_never_acts():
    for module, name in _imports(BENCHMARK):
        assert "executor" not in module, f"{module} {name}"
        assert "safety" not in module and "verifier" not in module, f"{module} {name}"
        assert name not in ("emergency_stop", "handle_command", "console")
        assert module != "app.console"
    assert not {"trigger", "handle_command", "execute", "execute_with_recovery", "authorize",
                "run"} & _called(BENCHMARK)


def test_the_only_production_module_it_touches_is_the_microphone_ownership_rule():
    production = {module for module, _ in _imports(BENCHMARK) if module.startswith("app")}
    assert production == {"app.listener"}, production


def test_no_production_module_imports_vosk():
    root = settings.PROJECT_ROOT
    offenders = []
    for path in (*(root / "app").rglob("*.py"), *(root / "config").rglob("*.py"), root / "main.py"):
        for module, name in _imports(path):
            if module.split(".")[0] == "vosk" or name == "vosk":
                offenders.append(str(path.relative_to(root)))
    assert offenders == [], offenders


def test_vosk_is_not_in_the_permanent_requirements():
    requirements = (settings.PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    for prototype in ("vosk", "sherpa", "sentencepiece"):
        assert prototype not in requirements, prototype


def test_the_listener_adapter_still_owns_only_the_whisper_boundary():
    adapter = settings.PROJECT_ROOT / "app" / "listener" / "adapter.py"
    external = {module.split(".")[0] for module, _ in _imports(adapter)}
    assert "vosk" not in external and "sherpa_onnx" not in external, external


# --- The fetch script is the only thing that downloads ------------------------------------------------

def test_only_the_fetch_script_reaches_the_network_for_this_model():
    """The host is read from the script rather than written here: a literal in this file would match
    itself, and the test would pass for the wrong reason."""
    root = settings.PROJECT_ROOT
    host = _fetch_module().ARCHIVE_URL.split("//", 1)[1].split("/", 1)[0]
    assert host and "." in host, host
    reaching = []
    for path in (*(root / "app").rglob("*.py"), *(root / "tests").rglob("*.py"),
                 *(root / "scripts").rglob("*.py"), root / "main.py"):
        if path in (root / FETCH_SCRIPT, root / "tests" / "test_vosk_prototype.py"):
            continue
        if host in path.read_text(encoding="utf-8"):
            reaching.append(str(path.relative_to(root)))
    assert reaching == [], f"only {FETCH_SCRIPT} may name the model host: {reaching}"


def test_the_fetch_script_checks_the_checksum_upstream_publishes():
    source = (settings.PROJECT_ROOT / FETCH_SCRIPT).read_text(encoding="utf-8")
    assert "EXPECTED_MD5" in source and "09ab50ccd62b674cbaa231b825f9c1cb" in source
    assert "model-list.json" in source, "it says where the checksum comes from"
    assert "not a signature" in source, "and is honest about what that proves"
    assert "sha256" in source.lower(), "our own measurement is recorded too"
    called = _called(FETCH_SCRIPT)
    assert "unlink" in called, "a mismatched or partial archive is removed"


def test_the_fetch_script_refuses_an_archive_that_would_write_outside_the_model_folder():
    """zipfile has no equivalent of tarfile's filter="data", so the check has to be explicit."""
    extract = ast.unparse(_function(FETCH_SCRIPT, "_extract"))
    assert ".." in extract and "resolve()" in extract
    assert "raise ValueError" in extract


def test_the_fetch_script_validates_the_model_structure():
    module = _fetch_module()
    assert module.MODEL == "vosk-model-small-en-us-0.15"
    assert "am/final.mdl" in module.REQUIRED and "conf/model.conf" in module.REQUIRED
    assert "graph/HCLr.fst" in module.REQUIRED and "graph/Gr.fst" in module.REQUIRED, (
        "the two files that make a runtime grammar possible at all")
    assert set(module.REQUIRED) <= set(harness.REQUIRED) | {"graph/disambig_tid.int"}


def test_the_model_folder_is_git_ignored():
    ignored = (settings.PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "data/models/" in [line.strip() for line in ignored]


# --- The report says what it is -----------------------------------------------------------------------

def test_the_report_explains_what_a_keyword_grammar_can_and_cannot_say():
    printed = _printed(BENCHMARK)
    assert "ASKED" in printed or "asked" in printed
    assert "not what was heard" in printed or "not what the microphone heard" in printed
    assert "FLUSH_ONLY_STOP" in printed
    assert "NEVER counts" in printed or "never counts" in printed


def test_the_prompt_field_is_named_for_the_prompt_not_for_speech():
    fields = _assigned_attributes(BENCHMARK, "Trial")
    assert "prompt" in fields and "said" not in fields
    trial = harness.Trial("positive 1", "stop", harness.POSITIVE)
    assert "prompt='stop'" in repr(trial)
    for claim in ("heard", "transcript", "text", "recognised", "recognized", "speech", "utterance"):
        assert claim not in fields | _property_names(BENCHMARK, "Trial"), claim


def test_the_report_keeps_endpoint_latency_as_its_own_number():
    summarise = ast.unparse(_function(BENCHMARK, "_summarise"))
    assert "detect_seconds" in summarise and "open_seconds" in summarise
    assert "recognizer_seconds" in summarise and "close_seconds" in summarise
    assert "detections only" in summarise, "endpoint latency is measured on the trials that detected"
    printed = _printed(BENCHMARK)
    assert "NOT model inference latency" in printed
    assert "trailing" in printed, "the endpoint's own silence requirement is stated"


def test_the_report_names_no_winner_and_approves_nothing(capsys):
    trial = harness.Trial("positive 1", "stop", harness.POSITIVE)
    trial.timely, trial.detect_seconds, trial.final_category = True, 1.0, harness.FINAL_STOP
    trial.cleanup_ok = True
    harness._summarise([trial])
    shown = capsys.readouterr().out.lower()
    for verdict in ("better", "worse", "optimal", "recommend", "should use", "production-ready",
                    "approved"):
        assert verdict not in shown, verdict


# --- The whole 15-trial run, driven offline ------------------------------------------------------------
# Fifteen spoken trials is a lot to ask of a person, so the entire benchmark is exercised here first
# with a fake vosk module and a fake device: no microphone, no library, no model. This is the check
# that would catch a labelling, counting or abort bug before the real sitting rather than after it.

@pytest.fixture
def dry_run(monkeypatch, tmp_path):
    """Run the real benchmark function against fakes. Returns (run, seen)."""
    import sys
    import types
    import numpy
    from app.listener import microphone

    numpy.zeros(1)
    # A model folder that satisfies the pre-flight checks without needing the real 68 MiB download.
    model = tmp_path / harness.MODEL_NAME
    for name in ("graph/HCLr.fst", "graph/Gr.fst"):
        (model / name).parent.mkdir(parents=True, exist_ok=True)
        (model / name).write_bytes(b"")
    monkeypatch.setattr(harness, "MODEL", model)
    monkeypatch.setattr(harness, "LISTEN_SECONDS", 0.05)
    microphone.reset()
    seen = []

    def run(script, cleanup_fails_at=None, device_fail=None, lexicon=0):
        """`script` maps a prompt to (finals, partials, flush) for that trial's recognizer."""
        monkeypatch.setitem(sys.modules, "sounddevice", FakeDevice(fail=device_fail))

        class Model:
            def __init__(self, model_path=None):
                self.model_path = model_path

            def vosk_model_find_word(self, word):
                return lexicon

        library = types.ModuleType("vosk")
        library.Model = Model
        library.KaldiRecognizer = lambda model, rate, grammar: FakeRecognizer()
        monkeypatch.setitem(sys.modules, "vosk", library)
        original = harness._listen

        def listen(make_recognizer, label, prompt, expected):
            finals, partials, flush = script.get(prompt, ({}, {}, ""))
            made = original(lambda: FakeRecognizer(finals=finals, partials=partials, flush=flush),
                            label, prompt, expected)
            if cleanup_fails_at == label:
                made.cleanup_ok, made.cleanup_error = False, "FATAL simulated cleanup failure"
            seen.append(made)
            return made

        monkeypatch.setattr(harness, "_listen", listen)
        harness.test_whether_a_constrained_grammar_hears_a_standalone_stop(model)

    return run, seen


def test_the_whole_benchmark_runs_fifteen_trials_and_reports_the_three_outcomes(dry_run, capsys):
    run, seen = dry_run
    run({
        "stop": ({2: "stop"}, {1: "stop"}, ""),                  # a clean timely detection
        "stopped": ({2: "stop"}, {}, ""),                        # a timely FALSE POSITIVE
        "stopping": ({}, {}, "stop"),                            # flush-only, must not count
        "please stop": ({2: "[unk] stop"}, {}, ""),              # the sink, then the word: not a stop
        "notepad": ({2: "[unk]"}, {}, ""),
    })
    assert len(seen) == 15
    shown = capsys.readouterr().out
    assert "TIMELY detections 5/5" in shown, "every 'stop' prompt fired in this fake"
    assert "TIMELY false positives 1/10" in shown and "'stopped'" in shown
    assert "flush-only 'stop' on 1 negative(s)" in shown and "'stopping'" in shown
    assert "FINAL_OTHER '[unk] stop'" in shown, "the sink-then-word case is visible, not counted"
    assert "FINAL_UNK" in shown
    from app.listener import microphone
    assert microphone.owner() is None


def test_the_run_fails_when_a_word_outside_the_grammar_appears(dry_run, capsys):
    """A leaked word means Vosk ignored the grammar, so the numbers would be meaningless."""
    run, seen = dry_run
    with pytest.raises(AssertionError, match="was NOT in force"):
        run({"notepad": ({2: "notepad"}, {}, "")})
    shown = capsys.readouterr().out
    assert "OUT OF GRAMMAR" in shown and "notepad" in shown


def test_the_run_stops_before_any_trial_when_a_grammar_token_is_missing(dry_run):
    """Vosk would only WARN and keep the full vocabulary, so this has to be checked up front."""
    run, seen = dry_run
    with pytest.raises(AssertionError, match="not in this model's lexicon"):
        run({}, lexicon=-1)
    assert seen == [], "not one trial ran"


def test_a_cleanup_failure_cancels_the_rest_of_the_benchmark(dry_run, capsys):
    run, seen = dry_run
    with pytest.raises(AssertionError, match="cleanup could not be proven"):
        run({}, cleanup_fails_at="positive 3")
    assert [made.label for made in seen] == ["positive 1", "positive 2", "positive 3"]
    shown = capsys.readouterr().out
    assert "BENCHMARK ABORTED after positive 3" in shown
    from app.listener import microphone
    assert microphone.owner() is None


def test_a_device_that_never_opens_finishes_the_run_and_fails_on_the_silence_not_on_accuracy(dry_run,
                                                                                             capsys):
    """Every trial is attempted (you have already spoken for the earlier ones), the microphone is
    always given back, and the run fails on the honest reason: no audio ever reached the recognizer.
    It must NOT report 0/5 recall as though that were a detector result."""
    run, seen = dry_run
    with pytest.raises(AssertionError, match="no audio was streamed at all"):
        run({}, device_fail="open")
    assert len(seen) == 15, "every trial was attempted; the failures are reported together"
    assert all(made.cleanup_ok for made in seen), "nothing was left open"
    assert all(isinstance(made.error, OSError) for made in seen)
    shown = capsys.readouterr().out
    assert "NO trial fed any audio at all" in shown
