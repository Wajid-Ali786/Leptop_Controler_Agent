"""
One Brain round-trip: send a request, and believe the answer only if it validates.

WHY THIS FILE EXISTS. It is the only place that joins the three halves of the Brain - the provider
boundary (adapter.py), the semantics (logic.py) and the configuration that sizes a request. Neither of
the other two may do it: adapter.py must hold no semantic policy, and logic.py is pure and imports no
adapter, which is what lets the Brain be reasoned about without the ability to call anything.

It exists as its own file rather than inside app/console.py for a concrete reason: the console is tested
to import NO adapter of any kind, and that invariant is worth more than the convenience of putting one
import there. A front end asks for an interpretation; it does not know there is an HTTP client.
(CLAUDE.md module-shape amendment, 2026-09-16: a module folder may hold focused extra files when a
concern fits neither adapter.py nor logic.py.)

What it is NOT: no lifecycle, no planning, no retry, no second attempt of any kind. One request, one
answer, one verdict.
"""
import logging
from dataclasses import dataclass

from app.brain import adapter, cost_controls
from app.brain import logic as brain
from app.brain.models import Interpretation, interpretation_schema
from config.settings import SettingsError, get_setting

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Unavailable:
    """The Brain could not be reached, is not configured, or is over budget.

    `reason` is a safe category for a log - an exception class name, never a message, never a prompt.
    The caller shows app/brain/logic.UNAVAILABLE_MESSAGE; it never shows this.
    """
    reason: str


def interpret(prompt: str) -> Interpretation | brain.InterpretationError | Unavailable:
    """Ask the Brain to read one request, and validate what comes back.

    Returns a typed Interpretation, an InterpretationError when the reply cannot be believed, or
    Unavailable when the request could not be made at all. Those three are the whole result space -
    there is no fourth case in which something happens anyway.

    Every cost control runs inside adapter.send_message(), before anything is sent.
    """
    try:
        max_type_characters = int(get_setting("executor.max_type_characters"))
        max_tokens = int(get_setting("cost.max_output_tokens_per_request"))
    except (SettingsError, TypeError, ValueError) as exc:
        log.warning("Brain request not made: configuration unusable (%s)", type(exc).__name__)
        return Unavailable(type(exc).__name__)
    try:
        reply = adapter.send_message(prompt, max_tokens=max_tokens, system=brain.SYSTEM_PROMPT,
                                    output_schema=interpretation_schema(max_type_characters))
    except (adapter.ClaudeError, cost_controls.CostLimitError, SettingsError) as exc:
        # The adapter has already logged the metadata it is allowed to log. Nothing here adds the prompt.
        log.info("Brain request unavailable (%s)", type(exc).__name__)
        return Unavailable(type(exc).__name__)
    outcome = brain.validate_interpretation(reply.text, max_type_characters=max_type_characters,
                                           stop_reason=reply.stop_reason)
    if isinstance(outcome, brain.InterpretationError):
        log.warning("Brain reply rejected: %s (%s)", outcome.reason, outcome.detail)
        return outcome
    log.info("Brain reply accepted: %s", type(outcome).__name__)
    return outcome
