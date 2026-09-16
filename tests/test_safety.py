"""
Tests for app/safety/ - the minimal Phase 0 Permission & Safety gate.

Confirmation always goes through a scripted confirm function (the gate's test mode);
nothing bypasses authorize(). Settings point at a temp config.yaml.
"""
import logging

import pytest

from app.safety import logic
from app.safety.logic import ActionDeniedError, assess, authorize
from app.safety.models import Action, RiskLevel
from config import settings
from config.settings import get_setting

SAFETY_CONFIG = (
    "safety:\n"
    '  risky_keywords: [delete, shutdown, "shut down", send]\n'
    "  safe_words: [sender, senders]\n"
)


class ScriptedConfirm:
    """Test-mode confirmation: returns a fixed answer (or raises) and records each call."""

    def __init__(self, answer=True, error=None):
        self.answer, self.error, self.calls = answer, error, []

    def __call__(self, action, assessment):
        self.calls.append((action, assessment))
        if self.error:
            raise self.error
        return self.answer


@pytest.fixture
def safety_config(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(SAFETY_CONFIG, encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_PATH", config_path)
    return lambda text: config_path.write_text(text, encoding="utf-8")


# --- Classification: configured risky keywords ---

@pytest.mark.parametrize("text, keyword", [
    ("delete the file report.txt", "delete"),
    ("Deletes old logs", "delete"),
    ("the files were deleted", "delete"),
    ("deleting temp files", "delete"),
    ("DELETE everything", "delete"),
    ("shutdown the laptop", "shutdown"),
    ("shut down the laptop", "shut down"),
    ("shutting down now", "shut down"),
    ("shut-down the PC", "shut down"),
    ("send Ali a message", "send"),
    ("sends the report", "send"),
    ("sending the invoice", "send"),
    ("file delete kar do", "delete"),  # Roman Urdu with an English verb
])
def test_risky_keywords_are_medium_risk(safety_config, text, keyword):
    assessment = assess(Action(text))
    assert assessment.level == RiskLevel.MEDIUM
    assert f"risky keyword '{keyword}'" in assessment.rule


@pytest.mark.parametrize("text", [
    "open notepad",
    "type hello",
    "show the sender of the last email",
    "list all senders",
    "scroll down",
    "maximize the window",  # closing is risky in the real config - see the close tests below
])
def test_ordinary_actions_are_low_risk(safety_config, text):
    assert assess(Action(text)).level == RiskLevel.LOW


@pytest.mark.parametrize("text, token", [
    ("resend the invoice", "resend"),
    ("undelete my photo", "undelete"),
    ("autosend is on", "autosend"),
])
def test_ambiguous_words_containing_a_keyword_are_risky(safety_config, text, token):
    assessment = assess(Action(text))
    assert assessment.level == RiskLevel.MEDIUM
    assert f"word '{token}'" in assessment.rule and "ambiguous" in assessment.rule


def test_safe_words_cannot_exempt_a_real_verb_form(safety_config):
    safety_config(SAFETY_CONFIG.replace("[sender, senders]", "[sender, senders, delete, deleting]"))
    assert assess(Action("deleting the folder")).level == RiskLevel.MEDIUM


def test_unreadable_script_is_treated_as_risky(safety_config):
    assessment = assess(Action("فائل ڈیلیٹ کرو"))  # Urdu script
    assert assessment.level == RiskLevel.MEDIUM
    assert "script" in assessment.rule


@pytest.mark.parametrize("text", ["", "   ", "123 !!!"])
def test_text_without_words_is_treated_as_risky(safety_config, text):
    assert assess(Action(text)).level == RiskLevel.MEDIUM


def test_keywords_come_from_config(safety_config):
    assert assess(Action("format the drive")).level == RiskLevel.LOW
    safety_config(SAFETY_CONFIG.replace("send]", "send, format]"))
    assert assess(Action("format the drive")).level == RiskLevel.MEDIUM


# --- Confirmation behavior ---

def test_low_risk_passes_without_asking(safety_config):
    confirm = ScriptedConfirm()
    decision = authorize(Action("open notepad"), confirm)
    assert decision.assessment.level == RiskLevel.LOW
    assert decision.confirmed is False
    assert confirm.calls == []


def test_low_risk_passes_even_with_no_confirmation_method(safety_config):
    assert authorize(Action("open notepad"), None).assessment.level == RiskLevel.LOW


def test_medium_risk_runs_only_after_confirmation(safety_config):
    confirm = ScriptedConfirm(answer=True)
    decision = authorize(Action("delete old logs"), confirm)
    assert decision.confirmed is True
    assert len(confirm.calls) == 1
    asked_action, asked_assessment = confirm.calls[0]
    assert asked_action == Action("delete old logs")
    assert asked_assessment.level == RiskLevel.MEDIUM


def test_declined_confirmation_denies(safety_config):
    with pytest.raises(ActionDeniedError, match="the user did not confirm") as info:
        authorize(Action("delete old logs"), ScriptedConfirm(answer=False))
    assert info.value.assessment.level == RiskLevel.MEDIUM


def test_no_confirmation_method_denies(safety_config):
    with pytest.raises(ActionDeniedError, match="no confirmation method is available"):
        authorize(Action("shut down the laptop"))


@pytest.mark.parametrize("answer", ["yes", 1, "True", None, [True]])
def test_only_an_explicit_true_counts_as_confirmation(safety_config, answer):
    with pytest.raises(ActionDeniedError, match="did not confirm"):
        authorize(Action("send the report"), ScriptedConfirm(answer=answer))


def test_confirmation_error_denies_without_details(safety_config):
    confirm = ScriptedConfirm(error=RuntimeError("microphone unplugged: private detail"))
    with pytest.raises(ActionDeniedError, match=r"confirmation failed \(RuntimeError\)") as info:
        authorize(Action("send the report"), confirm)
    assert "private detail" not in str(info.value)
    assert info.value.__context__ is None and info.value.__cause__ is None


# --- Fail closed on missing/invalid safety configuration ---

@pytest.mark.parametrize("config", [
    "brain:\n  model: x\n",                                        # no safety section
    "safety:\n  risky_keywords: []\n  safe_words: []\n",           # empty keyword list
    "safety:\n  risky_keywords: delete\n  safe_words: []\n",       # not a list
    "safety:\n  risky_keywords: [delete, 42]\n  safe_words: []\n",  # non-text keyword
    'safety:\n  risky_keywords: ["rm -rf"]\n  safe_words: []\n',   # not plain words
    "safety:\n  risky_keywords: [delete]\n",                       # safe_words missing
    "safety:\n  risky_keywords: [delete]\n  safe_words: [two words]\n",
    "safety: [broken\n",                                           # malformed YAML
])
def test_invalid_safety_config_makes_every_action_need_confirmation(safety_config, config):
    safety_config(config)
    assessment = assess(Action("open notepad"))
    assert assessment.level == RiskLevel.MEDIUM
    assert "every action needs confirmation" in assessment.rule
    with pytest.raises(ActionDeniedError):
        authorize(Action("open notepad"))
    assert authorize(Action("open notepad"), ScriptedConfirm(answer=True)).confirmed is True


def test_confirmation_threshold_is_medium_and_not_a_setting():
    assert logic.CONFIRMATION_REQUIRED_AT == RiskLevel.MEDIUM


# --- Logging: level and rule, never the action text ---

def test_denial_log_has_level_and_rule_but_not_the_message(safety_config, caplog):
    caplog.set_level(logging.INFO, logger="app.safety.logic")
    with pytest.raises(ActionDeniedError):
        authorize(Action("send Ali PRIVATE-MESSAGE-TEXT"), ScriptedConfirm(answer=False))
    assert "Safety: denied - MEDIUM risk (risky keyword 'send' matched as 'send')" in caplog.text
    assert "PRIVATE-MESSAGE-TEXT" not in caplog.text
    assert "Ali" not in caplog.text


def test_allowed_actions_are_logged_without_their_text(safety_config, caplog):
    caplog.set_level(logging.INFO, logger="app.safety.logic")
    authorize(Action("open PRIVATE-FILE-NAME"), None)
    authorize(Action("delete PRIVATE-FILE-NAME"), ScriptedConfirm(answer=True))
    assert "Safety: allowed - LOW risk (no risky keywords found)" in caplog.text
    assert "confirmed by the user" in caplog.text
    assert "PRIVATE-FILE-NAME" not in caplog.text


# --- Real config ---

def test_real_config_has_valid_safety_rules():
    keywords, safe_words = logic._load_rules()
    assert {"delete", "shutdown", "shut down", "send", "close"} <= set(keywords)
    assert {"mita", "hata", "band", "bhej", "khatam", "saaf"} <= set(keywords)  # Roman Urdu stopgap
    assert {"sender", "husband", "bandwidth", "khata"} <= safe_words
    assert isinstance(get_setting("safety.risky_keywords"), list)


# --- Roman Urdu destructive verbs, with the REAL config.yaml (stopgap until Phase 3) ---

@pytest.mark.parametrize("text, keyword", [
    ("file mita do", "mita"),
    ("saari photos mitao", "mitao"),
    ("purani files mitado", "mitado"),
    ("ye folder hata do", "hata"),
    ("icon hatao", "hatao"),
    ("laptop band karo", "band"),
    ("Chrome band kar do", "band"),
    ("Ali ko message bhej do", "bhej"),
    ("report bhejo", "bhejo"),
    ("email bhejdo", "bhejdo"),
    ("session khatam karo", "khatam karo"),
    ("sab kuch khatam kar do", "khatam kar do"),
    ("process khatm karo", "khatm"),
    ("cache saaf karo", "saaf karo"),
    ("recycle bin saaf kar do", "saaf kar do"),
    ("downloads folder saaf", "saaf"),
])
def test_roman_urdu_destructive_verbs_are_risky(text, keyword):
    assessment = assess(Action(text))
    assert assessment.level == RiskLevel.MEDIUM
    assert f"risky keyword '{keyword}'" in assessment.rule  # a clear match, not merely ambiguous


def test_unlisted_roman_urdu_form_is_still_risky():
    assessment = assess(Action("sab kuch mitaiye"))  # polite form, not in the keyword list
    assert assessment.level == RiskLevel.MEDIUM
    assert "ambiguous" in assessment.rule


@pytest.mark.parametrize("text", [
    "call my husband",
    "Ali ko file dikhao",
    "notepad kholo",
    "check the wifi bandwidth",
    "broadband settings kholo",
    "mera bank khata dikhao",
    "open the safety settings",  # "saf" is deliberately NOT a keyword
])
def test_ordinary_roman_urdu_and_safe_words_stay_low_risk(text):
    assert assess(Action(text)).level == RiskLevel.LOW


def test_roman_urdu_verb_requires_confirmation_end_to_end():
    with pytest.raises(ActionDeniedError, match="risky keyword 'bhej'"):
        authorize(Action("Ali ko message bhej do"))  # no confirmation method: denied
    decision = authorize(Action("Ali ko message bhej do"), ScriptedConfirm(answer=True))
    assert decision.confirmed is True


# --- Closing, with the REAL config.yaml: Medium risk in English and Roman Urdu alike ---

@pytest.mark.parametrize("text, keyword", [
    ("close app notepad", "close"),       # exactly what the Executor's close_app asks the gate
    ("Close the calculator", "close"),
    ("closes all windows", "close"),
    ("the app closed", "close"),
    ("closing notepad now", "close"),
    ("notepad band karo", "band"),
])
def test_closing_is_medium_risk_in_both_languages(text, keyword):
    assessment = assess(Action(text))
    assert assessment.level == RiskLevel.MEDIUM
    assert f"risky keyword '{keyword}'" in assessment.rule


@pytest.mark.parametrize("text", [
    "open the closet photos",
    "disclose nothing",
    "show the enclosed file",
    "read the disclosure",
])
def test_words_that_merely_contain_close_stay_low_risk(text):
    assert assess(Action(text)).level == RiskLevel.LOW


def test_close_as_an_adjective_is_still_treated_as_risky():
    assessment = assess(Action("move closer"))  # stopgap until Phase 3, like English "band"
    assert assessment.level == RiskLevel.MEDIUM and "ambiguous" in assessment.rule


def test_close_app_requires_confirmation_end_to_end():
    with pytest.raises(ActionDeniedError, match="risky keyword 'close'"):
        authorize(Action("close app notepad"))
    assert authorize(Action("close app notepad"), ScriptedConfirm(answer=True)).confirmed is True
