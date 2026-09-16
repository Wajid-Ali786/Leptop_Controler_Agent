"""
Permission & Safety gate - minimal Phase 0 version (docs/step4 Section 3). Every action
passes through authorize() before it may run (CLAUDE.md rule 5). Reasoning-aware in
Phase 3, full Low/Medium/High/Critical system in Phase 8 (docs/step4 Sections 6, 11).

Phase 0 rule: an action containing a configured risky keyword is Medium risk, and
Medium or above needs the user's confirmation (CLAUDE.md rule 6).

Fail closed throughout:
- Keywords match as whole words plus common verb forms (delete/deletes/deleted/deleting).
  Any other word that merely contains a keyword (e.g. "resend") is ambiguous and treated
  as risky, unless listed in safety.safe_words (e.g. "sender").
- Text in a script the keyword rule can't read, text with no words, or a missing/invalid
  safety configuration all count as risky.
- No confirmation function, a "no", anything other than an explicit True, or an error
  while confirming all deny the action.

Confirmation is passed in as a function, so console, voice (Phase 2) and tray (Phase 9)
prompts can all use this gate unchanged. Logs record the risk level and triggering rule
only - never the full action text.
"""
import logging
import re
from typing import Callable

from app.safety.models import Action, RiskAssessment, RiskLevel, SafetyDecision
from config.settings import SettingsError, get_setting

# CLAUDE.md rule 6. Deliberately a constant, not a setting: safety can't be configured off.
CONFIRMATION_REQUIRED_AT = RiskLevel.MEDIUM

Confirm = Callable[[Action, RiskAssessment], bool]

_WORD = re.compile(r"[^\W\d_]+")          # runs of letters, in any script
_KEYWORD = re.compile(r"[a-z]+( [a-z]+)*")  # a word or a phrase of words
_SAFE_WORD = re.compile(r"[a-z]+")

log = logging.getLogger(__name__)


class ActionDeniedError(Exception):
    """The safety gate refused an action; it must not run."""

    def __init__(self, message: str, assessment: RiskAssessment):
        super().__init__(message)
        self.assessment = assessment


def authorize(action: Action, confirm: Confirm | None = None) -> SafetyDecision:
    """Allow the action (returning the decision) or raise ActionDeniedError.

    confirm(action, assessment) is asked only for Medium risk and above; the action is
    allowed only if it returns exactly True.
    """
    assessment = assess(action)
    if assessment.level < CONFIRMATION_REQUIRED_AT:
        log.info("Safety: allowed - %s risk (%s)", assessment.level.name, assessment.rule)
        return SafetyDecision(assessment=assessment, confirmed=False)

    if confirm is None:
        failure = "no confirmation method is available"
    else:
        try:
            answer = confirm(action, assessment)
        except Exception as exc:  # a broken prompt must deny, never allow
            failure = f"confirmation failed ({type(exc).__name__})"
        else:
            failure = None if answer is True else "the user did not confirm"
    if failure:
        log.warning("Safety: denied - %s risk (%s): %s", assessment.level.name, assessment.rule, failure)
        raise ActionDeniedError(
            f"Action not run - {assessment.level.name} risk: {assessment.rule}; {failure}.", assessment
        )

    log.info("Safety: allowed - %s risk (%s), confirmed by the user", assessment.level.name, assessment.rule)
    return SafetyDecision(assessment=assessment, confirmed=True)


def assess(action: Action) -> RiskAssessment:
    """Classify an action's risk. Never raises; any doubt counts as risky."""
    try:
        keywords, safe_words = _load_rules()
    except Exception as exc:  # any config problem fails closed; never crash the gate
        detail = exc if isinstance(exc, SettingsError) else type(exc).__name__
        return _risky(f"safety configuration invalid ({detail}) - every action needs confirmation")

    text = action.description if isinstance(action.description, str) else ""
    tokens = _WORD.findall(text.lower())
    if not tokens:
        return _risky("no words to check (treated as risky)")
    matched_rule = _match_keywords(tokens, keywords, safe_words)
    if matched_rule:
        return _risky(matched_rule)
    if any(not token.isascii() for token in tokens):
        return _risky("text in a script the Phase 0 keyword rule can't check (treated as risky)")
    return RiskAssessment(RiskLevel.LOW, "no risky keywords found")


def configured_keywords() -> list[str]:
    """The risky keywords in effect, or SettingsError if the safety configuration is invalid
    (in which case assess() treats every action as risky). Used by the offline health check."""
    keywords, _ = _load_rules()
    return keywords


# --- Helpers ----------------------------------------------------------------------------

def _risky(rule: str) -> RiskAssessment:
    return RiskAssessment(RiskLevel.MEDIUM, rule)


def _match_keywords(tokens: list[str], keywords: list[str], safe_words: set[str]) -> str | None:
    """A clear keyword match wins; otherwise report the first ambiguous one, if any."""
    ambiguous = None
    for keyword in keywords:
        first, *rest = keyword.split()
        forms = _verb_forms(first)
        for i, token in enumerate(tokens):
            if token in forms and tokens[i + 1:i + 1 + len(rest)] == rest:
                return f"risky keyword '{keyword}' matched as '{token}'"
            if not rest and ambiguous is None and keyword in token and token not in safe_words:
                ambiguous = f"word '{token}' contains risky keyword '{keyword}' (ambiguous - treated as risky)"
    return ambiguous


def _verb_forms(word: str) -> set[str]:
    """Generous regular forms: extra non-words are harmless (they only make matching stricter)."""
    forms = {word, word + "s", word + "es", word + "d", word + "ed", word + "ing",
             word + word[-1] + "ed", word + word[-1] + "ing"}  # shut -> shutting
    if word.endswith("e"):
        forms.add(word[:-1] + "ing")  # delete -> deleting
    if word.endswith("y"):
        forms |= {word[:-1] + "ies", word[:-1] + "ied"}
    return forms


def _load_rules() -> tuple[list[str], set[str]]:
    keywords = get_setting("safety.risky_keywords")
    valid = isinstance(keywords, list) and keywords and all(isinstance(k, str) for k in keywords)
    normalized = [" ".join(k.lower().split()) for k in keywords] if valid else []
    if not valid or not all(_KEYWORD.fullmatch(k) for k in normalized):
        raise SettingsError(
            f"Setting 'safety.risky_keywords' must be a non-empty list of words or phrases, got {keywords!r}."
        )
    safe_words = get_setting("safety.safe_words")
    if not isinstance(safe_words, list) or not all(
            isinstance(w, str) and _SAFE_WORD.fullmatch(w.strip().lower()) for w in safe_words):
        raise SettingsError(f"Setting 'safety.safe_words' must be a list of single words, got {safe_words!r}.")
    return normalized, {w.strip().lower() for w in safe_words}
