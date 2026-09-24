"""
Tests for Phase 2 Feature 6 Task 6a: the spoken-command acceptance gate - offline.

No microphone, no speech model, no real action. The one injected seam is `listen`, which returns a
Transcript or a VoiceFailure, so the whole loop runs on scripted text; app.console.handle_command is
replaced by a recorder, so the boundary is observed rather than executed.

The invariant every other test serves: the exact string shown as "COMMAND TO ACCEPT" is the exact
string handed to handle_command. If anything normalized text between the two, the gate would be
theatre - the user would approve one string and another would run.
"""
import ast

import pytest

from app import console, voice_console
from app.listener import logic
from app.listener.models import (NO_SPEECH, PendingCommand, Transcript, VoiceFailure)

URDU = "نوٹ پیڈ"
MESSY = "  Open   Notepad and type hello.  "
SECRET = "Zarqonimbus qwertyuiop"


class Script:
    """The user's keyboard: each read() returns the next scripted answer."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError("the script ran out")
        return self.answers.pop(0)


class Screen:
    def __init__(self):
        self.lines = []

    def __call__(self, text=""):
        self.lines.append(str(text))

    @property
    def text(self):
        return "\n".join(self.lines)

    def command_shown(self):
        """Exactly what was between the first [ and the last ] on the COMMAND TO ACCEPT line."""
        line = [one for one in self.lines if one.startswith("COMMAND TO ACCEPT:")][-1]
        return line[line.index("[") + 1:line.rindex("]")]


class Ear:
    """The injected listen(): hands back queued Transcripts / VoiceFailures, one per call."""

    def __init__(self, *heard):
        self.heard = list(heard)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if not self.heard:
            raise AssertionError("listen() was called more times than the test expected")
        return self.heard.pop(0)


@pytest.fixture
def ran(monkeypatch):
    """Records every app.console.handle_command call instead of running it."""
    calls = []

    def handle_command(text, confirm=None, offer_retry=None, focus=None):
        calls.append(dict(text=text, confirm=confirm, offer_retry=offer_retry, focus=focus))
        return console.CommandReply(console.Status.RAN, "pretend it ran")
    monkeypatch.setattr(console, "handle_command", handle_command)
    return calls


def say(text, **metadata):
    return Transcript(text=text, **metadata)


def run(heard, *answers, ran=None, focus=None):
    """One voice-console session: hear `heard`, answer `answers`, then leave."""
    screen, script = Screen(), Script(*answers)
    ear = heard if isinstance(heard, Ear) else Ear(heard)
    code = voice_console.run_voice_console(ear, read=script, write=screen, focus=focus)
    return screen, script, ear, code


# --- Nothing runs until it is accepted ------------------------------------------------------------

def test_a_heard_command_is_shown_and_nothing_runs_before_acceptance(ran):
    screen, _, _, code = run(say(" open notepad"), "listen", "x", "exit")
    assert ran == [], "cancel must never reach the command pipeline"
    assert "HEARD:             [ open notepad]" in screen.lines
    assert screen.command_shown() == " open notepad"
    assert voice_console.CANCELLED in screen.lines and code == 0


def test_accepting_runs_it_exactly_once(ran):
    run(say(" open notepad"), "listen", "accept", "exit")
    assert len(ran) == 1 and ran[0]["text"] == " open notepad"


@pytest.mark.parametrize("answer", ["a", "accept"])
def test_both_ways_of_accepting_work(ran, answer):
    run(say("refresh"), "listen", answer, "exit")
    assert len(ran) == 1


def test_a_low_risk_command_is_gated_just_like_any_other(ran):
    """`refresh` is about as harmless as Phase 1 gets, and it still waits to be accepted."""
    run(say("refresh"), "listen", "x", "exit")
    assert ran == []
    run(say("refresh"), "listen", "a", "exit")
    assert len(ran) == 1


def test_redictating_throws_the_first_attempt_away(ran):
    ear = Ear(say("open notepad"), say("refresh"))
    screen, _, _, _ = run(ear, "listen", "r", "a", "exit")
    assert ear.calls == 2
    assert len(ran) == 1 and ran[0]["text"] == "refresh", "the discarded candidate never ran"


def test_a_voice_failure_is_reported_and_nothing_runs(ran):
    failure = VoiceFailure(NO_SPEECH, "Nothing was said, so there was nothing to act on.")
    screen, _, ear, _ = run(failure, "listen", "exit")
    assert ran == [] and failure.message in screen.lines
    assert "COMMAND TO ACCEPT" not in screen.text


def test_leaving_without_listening_runs_nothing(ran):
    screen, _, _, code = run(Ear(), "exit")
    assert ran == [] and code == 0 and voice_console.WELCOME in screen.lines


# --- Only the documented answers accept ------------------------------------------------------------

@pytest.mark.parametrize("answer", ["yes", "y", "ok", "okay", "go", "confirm", "do it", "c", "",
                                    "sure", "run it"])
def test_nothing_else_is_acceptance(ran, answer):
    """"c" is not a key at all: it would be ambiguous between correct and cancel. And the words of
    the SAFETY confirmation are not acceptance either - that question is asked later, on paper keys."""
    screen, _, _, _ = run(say("open notepad"), "listen", answer, "x", "exit")
    assert ran == []
    assert voice_console.CHOICE_AGAIN in screen.lines


def test_the_short_keys_are_unique():
    keys = [key for key in voice_console.CHOICES if len(key) == 1]
    assert sorted(keys) == ["a", "e", "r", "x"] and len(set(keys)) == 4
    assert "c" not in voice_console.CHOICES
    for word in ("yes", "y", "ok", "okay", "go", "confirm", "do it"):
        assert word not in voice_console.CHOICES


def test_end_of_input_at_the_prompt_cancels(ran):
    screen, _, _, _ = run(say("open notepad"), "listen")   # the script runs out at the choice
    assert ran == [] and voice_console.CANCELLED in screen.lines


# --- The invariant: what you read is what runs -----------------------------------------------------

def test_the_accepted_string_is_byte_for_byte_what_runs(ran):
    """Leading and trailing whitespace, repeated internal spaces, terminal punctuation and mixed
    script, all at once - and the identity of the object itself, not equality after normalizing."""
    spoken = f"  type   Hello   {URDU}  world...  "
    run(say(spoken), "listen", "accept", "exit")
    screen = None
    assert len(ran) == 1
    assert ran[0]["text"] == spoken
    assert ran[0]["text"] is spoken, "not a copy, not a normalized version - the same string"


def test_what_is_displayed_is_what_runs(ran):
    spoken = f"  type   Hello   {URDU}  world...  "
    screen, _, _, _ = run(say(spoken), "listen", "accept", "exit")
    assert screen.command_shown() == spoken
    assert screen.command_shown() == ran[0]["text"]


def test_the_invariant_holds_for_a_command_that_needed_a_choice(ran):
    """Even after being shown two readings and picking the one as heard, the exact bytes run."""
    spoken = "  Open   Notepad.  "
    screen, _, _, _ = run(say(spoken), "listen", "accept", "1", "exit")
    assert ran[0]["text"] == spoken and ran[0]["text"] is spoken
    assert screen.command_shown() == spoken


def test_the_candidate_starts_as_the_raw_transcript_not_a_tidied_copy():
    heard = say("  type   hello...  ")
    pending = PendingCommand(logic.derive_transcript(heard), heard.text)
    assert pending.candidate == "  type   hello...  "
    assert pending.candidate != pending.heard.tidy_text, "tidy() is a derived value, not the command"
    assert pending.heard.transcript.text == "  type   hello...  "


# --- Correcting one part ----------------------------------------------------------------------------

def test_correcting_replaces_only_the_chosen_part(ran):
    screen, _, _, _ = run(say(MESSY), "listen", "e", "2", "Calculator", "x", "exit")
    assert screen.command_shown() == "  Open   Calculator and type hello.  "
    assert ran == [], "a correction is never an acceptance"


def test_a_correction_must_be_accepted_on_its_own(ran):
    run(say(MESSY), "listen", "e", "2", "Calculator", "a", "1", "exit")
    assert len(ran) == 1 and ran[0]["text"] == "  Open   Calculator and type hello.  "


def test_the_whole_corrected_command_is_shown_again(ran):
    screen, _, _, _ = run(say(MESSY), "listen", "e", "2", "Calculator", "x", "exit")
    shown = [line for line in screen.lines if line.startswith("COMMAND TO ACCEPT:")]
    assert len(shown) == 2, "the pending command is shown again after the correction"
    assert "Calculator" in shown[1] and "Notepad" in shown[0]


def test_the_parts_are_numbered_with_their_punctuation_attached(ran):
    screen, _, _, _ = run(say(MESSY), "listen", "e", "9", "x", "exit")
    numbered = [line for line in screen.lines if "[1] Open" in line][0]
    assert "[5] hello." in numbered, "punctuation belongs to its word"
    assert voice_console.NO_SUCH_TOKEN.format(number=9) in screen.lines


@pytest.mark.parametrize("candidate, number, replacement, expected", [
    ("  Open   Notepad and type hello.  ", 2, "Calculator", "  Open   Calculator and type hello.  "),
    ("open\tnotepad", 2, "calculator", "open\tcalculator"),
    ("open notepad\nplease", 1, "close", "close notepad\nplease"),
    ("type don't stop", 3, "go", "type don't go"),
    (f"open {URDU} now", 2, "notepad", f"open notepad {URDU.split()[1]} now"),
    ("open کھولو now", 2, "notepad", "open notepad now"),
    ("नोटपैड खोलो", 1, "notepad", "notepad खोलो"),
    ("notepad kholo phir type karo", 2, "open", "notepad open phir type karo"),
    (f"  Notepad {URDU}   please.  ", 4, "now", f"  Notepad {URDU}   now  "),
])
def test_every_character_outside_the_replaced_part_survives(candidate, number, replacement, expected):
    """Tabs, newlines, runs of spaces, apostrophes, Urdu, Hindi, Roman Urdu and mixed script - the
    span is replaced, nothing else is even looked at."""
    assert logic.replace_token(candidate, number, replacement) == expected


def test_a_correction_never_rebuilds_the_line():
    """The difference that matters: " ".join(tokens) would quietly normalize the spacing."""
    candidate = "  Open   Notepad  "
    rebuilt = " ".join(logic.tokens(candidate)).replace("Notepad", "Calculator")
    assert logic.replace_token(candidate, 2, "Calculator") == "  Open   Calculator  "
    assert logic.replace_token(candidate, 2, "Calculator") != rebuilt


def test_an_empty_replacement_changes_nothing(ran):
    screen, _, _, _ = run(say(MESSY), "listen", "e", "2", "   ", "x", "exit")
    assert voice_console.EMPTY_REPLACEMENT in screen.lines
    assert screen.command_shown() == MESSY


def test_correcting_something_with_no_parts(ran):
    screen, _, _, _ = run(say("   "), "listen", "e", "x", "exit")
    assert voice_console.NOTHING_TO_CORRECT in screen.lines and ran == []


# --- Speech-final punctuation ------------------------------------------------------------------------

def test_a_conflicting_reading_is_never_chosen_for_the_user(ran):
    """The measured case: "close window." parses as an app called "window.", while "close window" is
    the window control. Two different actions, so the user decides."""
    screen, _, _, _ = run(say("close window."), "listen", "a", "x", "exit")
    assert ran == [] and voice_console.AMBIGUOUS in screen.lines
    assert "[1] [close window.]" in screen.text and "[2] [close window]" in screen.text
    assert "close app window." in screen.text and "window control close" in screen.text


def test_choosing_the_trimmed_reading_needs_a_fresh_acceptance(ran):
    screen, _, _, _ = run(say("close window."), "listen", "a", "2", "x", "exit")
    assert ran == [], "a transformed command is not run on the strength of the earlier acceptance"
    assert screen.command_shown() == "close window", "and the new candidate is shown in full"


def test_the_trimmed_reading_runs_only_after_that_second_acceptance(ran):
    run(say("close window."), "listen", "a", "2", "a", "exit")
    assert len(ran) == 1 and ran[0]["text"] == "close window"


def test_keeping_the_reading_as_heard_runs_exactly_what_was_shown(ran):
    run(say("close window."), "listen", "a", "1", "exit")
    assert len(ran) == 1 and ran[0]["text"] == "close window."


@pytest.mark.parametrize("spoken, trimmed", [
    ("Open Notepad.", "Open Notepad"),
    ("Scroll down 3.", "Scroll down 3"),
    ("close window.", "close window"),
])
def test_the_measured_punctuation_cases_all_ask(ran, spoken, trimmed):
    screen, _, _, _ = run(say(spoken), "listen", "a", "x", "exit")
    assert ran == []
    assert f"[1] [{spoken}]" in screen.text and f"[2] [{trimmed}]" in screen.text


def test_a_line_that_is_not_a_command_either_way_runs_nothing(ran):
    """"minimize." is unknown, and so is "minimize"... no: trimmed IS a command, so this one asks.
    A line that is nothing either way just reports."""
    screen, _, _, _ = run(say("kuch bhi bolo."), "listen", "a", "x", "exit")
    assert ran == [] and voice_console.NOT_A_COMMAND in screen.lines


def test_minimize_with_a_full_stop_offers_the_command_it_almost_was(ran):
    screen, _, _, _ = run(say("minimize."), "listen", "a", "2", "a", "exit")
    assert len(ran) == 1 and ran[0]["text"] == "minimize"


def test_a_command_without_terminal_punctuation_is_never_questioned(ran):
    screen, _, _, _ = run(say("open notepad"), "listen", "a", "exit")
    assert len(ran) == 1 and voice_console.AMBIGUOUS not in screen.lines


def test_punctuation_that_makes_no_difference_is_not_questioned(ran):
    """An equivalence that holds: both readings are the same action, so there is nothing to ask."""
    assert console.preview("refresh").same_action_as(console.preview("refresh"))
    screen, _, _, _ = run(say("refresh"), "listen", "a", "exit")
    assert len(ran) == 1 and voice_console.AMBIGUOUS not in screen.lines


# --- `type` payloads are never touched ----------------------------------------------------------------

@pytest.mark.parametrize("spoken", [
    "type hello.", "type hello...", "type hello,", "type what?", "type wow!",
    'type "  spaces  "', "type Hello   World.", f"type {URDU}...", "type don't stop.",
])
def test_a_typed_payload_reaches_the_pipeline_exactly_as_accepted(ran, spoken):
    """Free-form payload: the punctuation and the spacing ARE the command. No alternative is even
    offered, and nothing is trimmed."""
    screen, _, _, _ = run(say(spoken), "listen", "accept", "exit")
    assert len(ran) == 1 and ran[0]["text"] == spoken
    assert voice_console.AMBIGUOUS not in screen.lines
    assert screen.command_shown() == spoken


def test_two_actions_are_compared_by_what_they_would_do_not_by_their_description():
    """The description deliberately hides a typed payload, so two different commands can describe
    themselves identically. Equivalence must not be decided on that text."""
    one, other = console.preview("type hello."), console.preview("type world,")
    assert one.safe_description == other.safe_description == "type text (6 characters)"
    assert one.equivalence_key != other.equivalence_key
    assert one.same_action_as(other) is False, "same words on screen, different text typed"
    assert console.preview("type hello.").same_action_as(console.preview("type hello.")) is True


def test_a_refusal_is_never_equivalent_to_anything():
    refused = console.preview("kuch bhi")
    assert refused.same_action_as(console.preview("kuch bhi")) is False
    assert refused.same_action_as(console.preview("refresh")) is False
    assert refused.equivalence_key == () and refused.refusal == "unknown"


def test_type_is_the_free_form_kind():
    assert console.preview("type hello.").free_form is True
    assert console.preview("open notepad").free_form is False
    assert console.preview("blah").free_form is False


# --- Safety stays typed --------------------------------------------------------------------------------

def test_the_confirmation_handed_over_is_the_consoles_own_typed_one(ran, monkeypatch):
    """Not a copy, not a voice version: the object the typed console uses, reading the keyboard."""
    built = []
    real = console.typed_confirmation
    monkeypatch.setattr(console, "typed_confirmation",
                        lambda read, write: built.append(real(read, write)) or built[-1])
    run(say("open notepad"), "listen", "a", "exit")
    assert len(built) == 1 and ran[0]["confirm"] is built[0]


def test_a_spoken_yes_is_only_ever_a_command_that_is_not_understood(ran):
    screen, _, _, _ = run(say("yes"), "listen", "a", "exit")
    assert len(ran) == 1 and ran[0]["text"] == "yes"
    assert console.preview("yes").is_command is False, "and the parser refuses it"


@pytest.mark.parametrize("answer, allowed", [("yes", True), ("YES", True), ("  yes  ", True),
                                             ("y", False), ("yeah", False), ("", False),
                                             ("ok", False), ("confirm", False)])
def test_the_typed_confirmation_still_takes_only_the_exact_word(answer, allowed):
    screen = Screen()
    confirm = console.typed_confirmation(Script(answer), screen)
    action = console.commands.parse("close notepad")
    assessment = type("A", (), {"level": type("L", (), {"name": "MEDIUM"})(), "rule": "close"})()
    assert confirm(action, assessment) is allowed


def test_the_voice_console_never_builds_a_confirmation_of_its_own():
    """An AST rule: nothing here may define or pass a confirm built from what was heard."""
    tree = _voice_tree()
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "authorize" not in names and "execute" not in names and "execute_with_recovery" not in names
    source = open(voice_console.__file__, encoding="utf-8").read()
    assert "typed_confirmation" in source and "_says_yes" not in source
    assert "YES" not in names, "the voice console does not re-implement the confirmation rule"


# --- Privacy ---------------------------------------------------------------------------------------------

def test_the_pending_command_never_shows_what_was_said():
    pending = PendingCommand(logic.derive_transcript(say(SECRET, language="ur")), SECRET)
    for shown in (repr(pending), str(pending), f"{pending}", "%s" % (pending,)):
        assert "Zarqonimbus" not in shown and "qwertyuiop" not in shown
        assert f"candidate_characters={len(SECRET)}" in shown and "corrections=0" in shown
    assert pending.candidate == SECRET


def test_a_preview_never_shows_a_typed_payload():
    preview = console.preview(f"type {SECRET}")
    for shown in (repr(preview), str(preview), f"{preview}", "%s" % (preview,)):
        assert "Zarqonimbus" not in shown and "qwertyuiop" not in shown
    assert "Zarqonimbus" not in preview.safe_description
    assert preview.equivalence_key[1] == SECRET, "it is still compared exactly - just never shown"


def test_nothing_spoken_reaches_the_log(ran, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    run(say(f"type {SECRET}"), "listen", "e", "2", "Zaphod", "a", "exit")
    logged = "\n".join(record.getMessage() for record in caplog.records)
    for forbidden in ("Zarqonimbus", "qwertyuiop", "Zaphod"):
        assert forbidden not in logged, f"{forbidden!r} reached the log"
    # Phase 1 already logs the command KIND ("type_text"), never its payload - that stays true here.
    assert "type_text" in logged and "hello" not in logged
    assert "correction" in logged or "accepted" in logged


def test_what_was_said_is_shown_on_purpose_and_only_on_screen(ran):
    """The gate only works if the user can read what was heard - that is a deliberate display, and
    the only place it happens."""
    screen, _, _, _ = run(say(SECRET), "listen", "x", "exit")
    assert "Zarqonimbus" in screen.text


# --- Boundaries --------------------------------------------------------------------------------------------

def _voice_tree():
    return ast.parse(open(voice_console.__file__, encoding="utf-8").read())


def _imports(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from ((alias.name, "") for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield from ((node.module or "", alias.name) for alias in node.names)


def test_the_voice_console_reaches_the_computer_only_through_the_typed_console():
    imported = list(_imports(_voice_tree()))
    assert ("app", "console") in imported, "it must use app.console"
    offenders = [f"{module} {name}".strip() for module, name in imported
                 if module.split(".")[0] in ("app",) and "executor" in module]
    assert offenders == [], offenders
    for module, name in imported:
        assert "emergency_stop" not in (module, name), "no stop path exists yet (Task 6a)"


def test_the_voice_console_does_not_parse_or_act_by_itself():
    tree = _voice_tree()
    called = {node.func.attr for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert "parse" not in called and "authorize" not in called
    reached = {f"{node.func.value.id}.{node.func.attr}" for node in ast.walk(tree)
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and isinstance(node.func.value, ast.Name)}
    assert reached & {"console.handle_command"}, "the one way anything runs"
    # The only adapter it may reach is the Listener's, and only for listening (D-6). Any Executor
    # adapter is out of the question - the import rule above already forbids importing one.
    assert {name for name in reached if name.startswith("adapter.")} <= {
        "adapter.capture", "adapter.transcribe", "adapter.ensure_model"}, reached


def test_preview_does_nothing_at_all(monkeypatch):
    """No window is read, nothing is executed, nothing is authorized - it only parses."""
    for name in ("execute_with_recovery",):
        monkeypatch.setattr(console, name, _explode)
    for text in ("open notepad", "type hello.", "close window.", "blah", ""):
        console.preview(text)


def _explode(*args, **kwargs):
    raise AssertionError("preview() must not run anything")


def test_the_listener_package_still_knows_nothing_of_the_executor():
    from config import settings
    for path in (settings.PROJECT_ROOT / "app" / "listener").rglob("*.py"):
        for module, name in _imports(ast.parse(path.read_text(encoding="utf-8"))):
            assert "executor" not in module and name != "emergency_stop", f"{path}: {module} {name}"
