"""
Tests for Phase 2 Feature 5 Task 5a: the derived layer above one Transcript - pure.

Nothing here has any I/O at all: no microphone, no model, no transcription, no fakes and no
monkeypatching. app/listener/logic.py is pure and has no logger, so every rule below is tested by
calling it.

The point of the feature is the SEPARATION: Transcript.text is raw provenance from the recognizer and
is never replaced, while tidy_text and matching_text are values computed from it. Feature 5
deliberately does NOT produce a general punctuation-stripped "command form": the period in
"type hello." is payload and the period in "close window." is noise, and a verb-blind rule cannot
tell them apart. The measured evidence for that decision lives in logic.FEATURE_6_PUNCTUATION and
in the audit note beside it.
"""
import ast
import dataclasses

import pytest

from app.listener import logic
from app.listener.models import DerivedTranscript, Transcript

SECRET = "Zarqonimbus  fourteen  Kholo!!  qwertyuiop"   # distinctive: must not appear in any repr
URDU = "نوٹ پیڈ کھولو"
HINDI = "नोटपैड खोलो"
URDU_STOP = "رک جاؤ"
HINDI_STOP = "रुको"


def derive(text, **metadata):
    return logic.derive_transcript(Transcript(text=text, **metadata))


def _tree():
    return ast.parse(open(logic.__file__, encoding="utf-8").read())


def _imports():
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Import):
            yield from ((alias.name, "") for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield from ((node.module or "", alias.name) for alias in node.names)


# --- Raw provenance is untouched ------------------------------------------------------------------

def test_the_original_transcript_object_comes_back_inside_the_result():
    heard = Transcript(text="  Open   Notepad.  ", language="en", language_probability=0.51,
                       audio_seconds=2.0)
    derived = logic.derive_transcript(heard)
    assert derived.transcript is heard, "the same object, not a copy that could drift"
    assert derived.transcript.text == "  Open   Notepad.  ", "raw text is never replaced"


def test_deriving_twice_never_changes_the_transcript():
    heard = Transcript(text="  Open   Notepad.  ")
    before = heard.text
    for _ in range(3):
        logic.derive_transcript(heard)
    assert heard.text == before and dataclasses.asdict(heard)["text"] == before


def test_the_result_has_no_text_field_to_confuse_with_the_raw_one():
    derived = derive("open notepad")
    assert not hasattr(derived, "text"), "callers must name which string they mean"
    assert set(vars(derived)) == {"transcript", "tidy_text", "matching_text", "stop_match"}


def test_the_result_carries_no_meaning():
    derived = derive("open notepad")
    for forbidden in ("intent", "action", "command", "corrected", "translation", "normalized_text"):
        assert not hasattr(derived, forbidden), f"{forbidden} is not Feature 5's business"


def test_the_result_is_immutable_and_checks_its_parts():
    derived = derive("open notepad")
    with pytest.raises(Exception):
        derived.tidy_text = "something else"
    with pytest.raises(TypeError):
        DerivedTranscript(transcript="not a transcript", tidy_text="a", matching_text="a",
                          stop_match=False)
    with pytest.raises(TypeError):
        DerivedTranscript(transcript=Transcript("a"), tidy_text=None, matching_text="a",
                          stop_match=False)
    with pytest.raises(TypeError):
        DerivedTranscript(transcript=Transcript("a"), tidy_text="a", matching_text="a", stop_match=1)
    with pytest.raises(TypeError):
        logic.derive_transcript("just a string")


# --- Exactly what the two mechanical forms are ----------------------------------------------------

@pytest.mark.parametrize("raw, tidy_text, matching_text", [
    ("open notepad", "open notepad", "open notepad"),
    ("   open notepad   ", "open notepad", "open notepad"),                      # outer whitespace
    ("open    notepad", "open notepad", "open notepad"),                         # repeated spaces
    ("open\tnotepad\nplease", "open notepad please", "open notepad please"),     # unicode whitespace
    ("open notepad", "open notepad", "open notepad"),                       # non-breaking space
    ("Open NotePad", "Open NotePad", "open notepad"),                            # casing
    ("open notepad...  really??!!", "open notepad. really?!", "open notepad. really?!"),
    ("don't open Dad's notepad", "don't open Dad's notepad", "don't open dad's notepad"),
    ("don’t open it", "don’t open it", "don’t open it"),          # curly apostrophe
    ("re-open the well-known file", "re-open the well-known file",
     "re-open the well-known file"),                                             # hyphens
    ("open file 42 and  scroll 3 times", "open file 42 and scroll 3 times",
     "open file 42 and scroll 3 times"),                                         # ascii digits
    ("۴۲", "۴۲", "۴۲"),                            # urdu-indic digits
    ("", "", ""),
    ("    ", "", ""),
    (" ... ", ".", "."),
])
def test_the_mechanical_forms_are_exactly_this(raw, tidy_text, matching_text):
    derived = derive(raw)
    assert derived.tidy_text == tidy_text
    assert derived.matching_text == matching_text
    assert derived.tidy_text == logic.tidy(raw) and derived.matching_text == logic.for_matching(raw)


@pytest.mark.parametrize("raw", ["type hello.", "type hello,", "close window.", "minimize.",
                                 "Scroll down 3.", "open notepad!", "type what?", "type wow!"])
def test_a_single_terminal_mark_is_never_removed(raw):
    """Feature 5 must not decide what a full stop MEANS. The period in "type hello." is what the user
    wants typed; the one in "close window." is noise. A verb-blind rule cannot tell them apart, so
    the information is preserved and Feature 6 reconciles it (logic.FEATURE_6_PUNCTUATION)."""
    derived = derive(raw)
    assert derived.tidy_text == raw
    assert derived.matching_text == raw.lower()
    assert derived.tidy_text[-1] in ".,!?"


def test_repeated_punctuation_still_collapses_exactly_as_before():
    assert derive("wait...  what??").tidy_text == "wait. what?"
    assert derive("hmm;;  ok::").tidy_text == "hmm; ok:"


def test_matching_differs_from_tidy_only_by_case():
    for raw in ("Open Notepad.", "TYPE Hello", "Notepad Kholo", URDU, HINDI):
        derived = derive(raw)
        assert derived.matching_text == derived.tidy_text.lower()


# --- Every script survives ------------------------------------------------------------------------

@pytest.mark.parametrize("raw, tidy_text", [
    (f"  {URDU}  ", URDU),
    (f" {HINDI} ", HINDI),
    ("نوٹ  پیڈ   کھولو", "نوٹ پیڈ کھولو"),
    ("Notepad Kholo Phir Type Karo", "Notepad Kholo Phir Type Karo"),
    (f"Notepad {URDU} please", f"Notepad {URDU} please"),
])
def test_urdu_hindi_roman_urdu_and_mixed_script_are_only_respaced(raw, tidy_text):
    derived = derive(raw)
    assert derived.tidy_text == tidy_text
    assert derived.matching_text == tidy_text.lower()


def test_roman_urdu_is_never_translated_or_transliterated():
    """Roman Urdu is a written representation, not a language. "kholo" stays "kholo" - deciding that
    it means "open" is Phase 3's job, with context."""
    derived = derive("Notepad kholo phir type karo")
    for form in (derived.tidy_text, derived.matching_text):
        assert "kholo" in form and "karo" in form
        assert "open" not in form and "khol" == form[form.index("kholo"):form.index("kholo") + 4]
    assert derived.transcript.text == "Notepad kholo phir type karo"


def test_urdu_script_is_never_turned_into_latin():
    derived = derive(URDU)
    assert derived.tidy_text == URDU and derived.matching_text == URDU
    assert not any(character.isascii() and character.isalpha() for character in derived.tidy_text)


def test_nothing_is_repaired_or_guessed():
    """A mishearing stays a mishearing: no spell-check, no closest-command, no synonyms."""
    for raw in ("opon notepud", "click type click type", "open notepad calculator"):
        assert derive(raw).tidy_text == raw


# --- Language metadata is carried, never acted on -------------------------------------------------

@pytest.mark.parametrize("probability", [0.506, 0.603, 0.789, 0.01, 1.0, None])
def test_every_language_probability_is_preserved_and_decides_nothing(probability):
    """The real acceptance produced CORRECT transcripts at 0.506 and 0.603, so no threshold exists.
    It is a language probability, never word confidence."""
    derived = derive("open notepad", language="en", language_probability=probability)
    assert derived.transcript.language_probability == probability
    assert derived.tidy_text == "open notepad" and derived.stop_match is False


def test_a_missing_probability_means_not_measured_not_low():
    """None comes from an explicitly configured language, where faster-whisper's 1 is a placeholder."""
    derived = derive("open notepad", language="en", language_probability=None)
    assert derived.transcript.language_probability is None
    assert derived.tidy_text == "open notepad", "nothing is refused or flagged because of it"


@pytest.mark.parametrize("language", ["en", "ur", "hi", "pa", "ar", "", "yue"])
def test_no_transcript_is_rejected_for_the_language_that_was_detected(language):
    derived = derive("kuch bhi", language=language, language_probability=0.3)
    assert derived.transcript.language == language
    assert derived.tidy_text == "kuch bhi", "the words survive whatever language was reported"


def test_code_switched_speech_is_not_relabelled_per_word():
    """One detected code describes the utterance the recognizer reported - nothing here claims more,
    and no second detector runs."""
    derived = derive(f"Notepad {URDU} please", language="ur", language_probability=0.44)
    assert derived.transcript.language == "ur"
    assert not hasattr(derived, "languages") and not hasattr(derived, "segments")


def test_there_is_no_language_policy_object_to_represent_doing_nothing():
    assert not hasattr(logic, "LanguagePolicy") and not hasattr(logic, "language_policy")


# --- The one narrow spoken-stop rule --------------------------------------------------------------

@pytest.mark.parametrize("raw", ["stop", "Stop", "STOP", " stop ", " Stop. ", "STOP!", "stop?",
                                 "stop,", "stop.", "  stop.  ", "stop..."])
def test_the_whole_utterance_being_the_word_stop_matches(raw):
    assert logic.is_stop_phrase(raw) is True
    assert derive(raw).stop_match is True


@pytest.mark.parametrize("raw", [
    "please stop", "stop please", "stop it", "stop notepad", "stopped", "stopping", "don't stop",
    "non-stop", "stop and close notepad", "full stop", "s t o p", "stap", "stpo", "sto",
    URDU_STOP, HINDI_STOP, "ruk jao", "band karo", "", "   ", "...",
])
def test_everything_else_is_not_the_stop_phrase(raw):
    assert logic.is_stop_phrase(raw) is False
    assert derive(raw).stop_match is False


def test_the_stop_rule_is_not_a_substring_search():
    for raw in ("stop notepad", "please stop", "stopwatch", "a stop b"):
        assert "stop" in logic.for_matching(raw) and logic.is_stop_phrase(raw) is False


def test_the_stop_rule_is_not_fuzzy():
    for raw in ("stap", "stopp", "sotp", "shtop", "estop"):
        assert logic.is_stop_phrase(raw) is False


def test_the_stop_phrase_is_fixed_in_code():
    assert logic.STOP_PHRASE == "stop"
    settings_fields = {field.name for field in dataclasses.fields(
        __import__("app.listener.models", fromlist=["ListenerSettings"]).ListenerSettings)}
    assert "stop_phrase" not in settings_fields, "not configurable in this task"


def test_the_stop_punctuation_rule_never_became_a_general_command_form():
    """A private helper for one comparison, deliberately not a public normalized command string."""
    assert not hasattr(logic, "command_text") and not hasattr(logic, "command_form")
    assert logic.tidy("close window.") == "close window.", "tidy is untouched by the stop rule"


def test_matching_a_stop_phrase_triggers_nothing():
    """Feature 5 returns a boolean and nothing else: no Executor, no emergency stop, no side effect."""
    assert logic.is_stop_phrase("stop") is True
    imported = [f"{module}.{name}".rstrip(".") for module, name in _imports()]
    assert not [name for name in imported
                if name.split(".")[0] == "app" and "executor" in name], imported
    assert "emergency_stop" not in {name.split(".")[-1] for name in imported}


# --- Privacy ---------------------------------------------------------------------------------------

def test_no_representation_of_the_derived_result_contains_speech(capsys):
    derived = derive(SECRET, language="ur", language_probability=0.51, audio_seconds=3.0)
    for shown in (repr(derived), str(derived), f"{derived}", "%s" % (derived,)):
        for forbidden in ("Zarqonimbus", "qwertyuiop", "Kholo", "kholo", "fourteen"):
            assert forbidden not in shown, f"{forbidden!r} is in a repr"
        assert f"characters={len(SECRET)}" in shown and "language='ur'" in shown
        assert "stop_match=False" in shown and "differs_from_raw=True" in shown
    print(derived)
    assert "Zarqonimbus" not in capsys.readouterr().out
    assert derived.tidy_text.startswith("Zarqonimbus"), "the strings are still there to be used"


def test_the_transcripts_own_repr_is_still_safe_inside_the_result():
    derived = derive(SECRET)
    assert "Zarqonimbus" not in repr(derived.transcript)


def test_the_pure_module_has_no_logger_at_all():
    """Nothing in the derived layer can log a transcript, because there is nowhere to log it to."""
    assert "logging" not in {module.split(".")[0] for module, _ in _imports()}
    assert not hasattr(logic, "log") and not hasattr(logic, "logger")
    calls = [node.func.value.id for node in ast.walk(_tree())
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and isinstance(node.func.value, ast.Name)]
    assert not {"log", "logger", "logging"} & set(calls), calls


def test_the_measured_feature_six_evidence_is_recorded():
    """Feature 6 starts from evidence rather than re-deriving it."""
    text = open(logic.__file__, encoding="utf-8").read()
    for measured in ("open_app 'Notepad.'", "unknown", "close_app 'window.'", "scroll 'down 3.'"):
        assert measured in text, f"the measured example {measured!r} is not recorded"
    assert "WRONG ACTION KIND" in text
    assert "Feature 6 must reconcile" in logic.FEATURE_6_PUNCTUATION
