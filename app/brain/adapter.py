"""
The ONLY file in this project allowed to call the Claude API directly.
Every other module asks brain.logic for a decision - never imports anthropic itself.
(docs/step3 Section 2; CLAUDE.md rule 2)

Every request goes through send_message(), which first passes the three cost
controls in app/brain/cost_controls.py (rate limit, token limit, money budget).
A blocked request raises CostLimitError and never reaches the API. An authorized
request's worst-case cost is reserved; it is replaced by the actual cost on success,
released only if the request certainly wasn't billed, and otherwise kept (fail closed).

Failures surface as ClaudeError subclasses with plain-English messages. The API key
never appears in a message, and SDK exceptions are not chained onto ours because
they carry the raw request (headers included). Logs record request metadata only -
never the prompt or Claude's reply text.
"""
import json
import logging

import anthropic
import httpx2

from app.brain import cost_controls
from app.brain.models import ClaudeReply
from config.settings import get_setting

PING_PROMPT = "Reply with the single word: OK"

# Transport failures that happen before the request reaches Claude - it certainly wasn't billed.
_NOT_SENT = (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout)

log = logging.getLogger(__name__)


class ClaudeError(Exception):
    """Base class for clean, user-facing Claude failures.

    may_have_been_billed is False only when the request certainly wasn't billed - it never
    reached Claude, or Claude answered with an error status - so its cost reservation can
    be released. Anything uncertain stays True (fail closed).
    """

    def __init__(self, message: str, *, may_have_been_billed: bool = True):
        super().__init__(message)
        self.may_have_been_billed = may_have_been_billed


class ClaudeAuthError(ClaudeError):
    """The API key was rejected or lacks permission."""


class ClaudeUnavailableError(ClaudeError):
    """Claude can't be reached right now (network, timeout, rate limit, server error)."""


class ClaudeRequestError(ClaudeError):
    """Claude rejected the request or returned something unusable."""


def get_client(http_client=None) -> anthropic.Anthropic:
    """Build a Claude client from settings. http_client is for tests/proxies only."""
    return anthropic.Anthropic(
        api_key=get_setting("ANTHROPIC_API_KEY"),
        timeout=float(get_setting("brain.timeout_seconds")),
        max_retries=int(get_setting("brain.max_retries")),
        http_client=http_client,
    )


def send_message(prompt: str, max_tokens: int, *, system: str | None = None,
                 output_schema: dict | None = None) -> ClaudeReply:
    """Send one user message to Claude and return its reply. The only request path.

    `system` is the system prompt and `output_schema` the JSON schema the reply must match. Both are
    written by app/brain/logic.py: this function knows how to send them, not what they should say.

    Raises cost_controls.CostLimitError (request not sent) if a cost control blocks it.
    """
    model = get_setting("brain.model")
    client = get_client()
    billable, overhead = _billable_input(prompt, system, output_schema)
    try:
        request_id = cost_controls.authorize(model=model, prompt=billable, max_tokens=max_tokens,
                                            overhead_tokens=overhead)
    except cost_controls.CostLimitError as exc:
        log.warning("Claude request blocked by cost controls: %s", exc)
        raise
    log.info("Claude request #%d sent: model=%s max_tokens=%d structured=%s estimated_input_tokens=%d",
             request_id, model, max_tokens, output_schema is not None,
             cost_controls.estimate_input_tokens(billable) + overhead)
    try:
        reply = _to_reply(_call_api(client, model=model, prompt=prompt, max_tokens=max_tokens,
                                   system=system, output_schema=output_schema))
    except ClaudeError as exc:
        _settle_failed_request(request_id, exc)
        raise
    cost_usd = cost_controls.record_usage(request_id, reply)
    log.info(
        "Claude request #%d done: input_tokens=%d output_tokens=%d cost_usd=%.6f stop_reason=%s",
        request_id, reply.input_tokens, reply.output_tokens, cost_usd, reply.stop_reason,
    )
    return reply


def ping() -> ClaudeReply:
    """Minimal request for the Phase 0 health/test path."""
    return send_message(PING_PROMPT, max_tokens=int(get_setting("brain.ping_max_tokens")))


def _settle_failed_request(request_id: int, exc: ClaudeError) -> None:
    """Release the cost reservation only if the request certainly wasn't billed; else keep it."""
    name = type(exc).__name__
    if exc.may_have_been_billed:
        log.warning("Claude request #%d failed (%s): %s - worst-case cost stays reserved", request_id, name, exc)
        return
    log.warning("Claude request #%d failed (%s): %s - not billed, reservation released", request_id, name, exc)
    try:
        cost_controls.release_reservation(request_id)
    except cost_controls.CostLimitError as ledger_exc:  # the original error matters more; the reservation stays
        log.error("Claude request #%d: reservation could not be released, it stays counted: %s",
                  request_id, ledger_exc)


# --- SDK boundary -------------------------------------------------------------------

def _billable_input(prompt: str, system: str | None, output_schema: dict | None) -> tuple[str, int]:
    """Everything the request will be billed for on the way in, plus what we cannot see as text.

    Anthropic's pricing documentation is explicit that schemas sent with a request are billed as input
    tokens, and the structured-outputs documentation says the API also injects its own system prompt
    describing the format, which "costs you tokens like any other system prompt". That injected prompt's
    size is not published, so brain.structured_output_overhead_tokens is a deliberate over-count rather
    than a measurement - the estimator's whole philosophy is to over-count and fail closed.
    """
    parts = [prompt]
    if system:
        parts.append(system)
    overhead = 0
    if output_schema is not None:
        parts.append(json.dumps(output_schema, ensure_ascii=False, separators=(",", ":")))
        overhead = int(get_setting("brain.structured_output_overhead_tokens"))
    return "\n".join(parts), overhead


def estimated_input_tokens(prompt: str, system: str | None = None,
                           output_schema: dict | None = None) -> int:
    """What this adapter would reserve for that request, before sending it.

    Public so the opt-in real smoke test can compare our estimate with the provider's own reported
    input_tokens and fail if we ever under-count. Reads settings, sends nothing.
    """
    billable, overhead = _billable_input(prompt, system, output_schema)
    return cost_controls.estimate_input_tokens(billable) + overhead


def _call_api(client: anthropic.Anthropic, *, model: str, prompt: str, max_tokens: int,
              system: str | None = None, output_schema: dict | None = None):
    request = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        # Thinking costs output tokens and counts toward max_tokens, so leaving it at the model's
        # default would make the output cap underivable from the schema. The values are config, not
        # policy decided here; see brain.thinking / brain.effort in config/config.yaml for the
        # verification behind them. No tools are ever sent on this path, which is the condition under
        # which between_tools means the reply is text only.
        "thinking": {"type": get_setting("brain.thinking")},
    }
    effort = get_setting("brain.effort")
    if system:
        request["system"] = system
    # Verified against the installed SDK: anthropic 1.5.0 types OutputConfigParam with `effort` and
    # `format`, and JSONOutputFormatParam requires exactly "type" and "schema". The older top-level
    # output_format parameter does not exist on messages.create() in this version.
    output_config = {"effort": effort}
    if output_schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": output_schema}
    request["output_config"] = output_config
    try:
        return client.messages.create(**request)
    # Error statuses: Claude answered with an error, so nothing was billed.
    except anthropic.AuthenticationError:
        raise ClaudeAuthError("Claude rejected the API key (401). Check ANTHROPIC_API_KEY in .env.",
                              may_have_been_billed=False) from None
    except anthropic.PermissionDeniedError:
        raise ClaudeAuthError("The API key is not permitted to make this request (403).",
                              may_have_been_billed=False) from None
    except anthropic.NotFoundError:
        raise ClaudeRequestError(
            f"Model '{model}' was not found (404). Check brain.model in config/config.yaml.",
            may_have_been_billed=False,
        ) from None
    except anthropic.RateLimitError:
        raise ClaudeUnavailableError("Claude is rate-limiting requests right now (429). Try again shortly.",
                                     may_have_been_billed=False) from None
    except anthropic.BadRequestError as exc:
        raise ClaudeRequestError(f"Claude rejected the request (400): {exc.message}",
                                 may_have_been_billed=False) from None
    except anthropic.APIStatusError as exc:
        if exc.status_code >= 500:
            raise ClaudeUnavailableError(f"Claude had a server error ({exc.status_code}). Try again later.",
                                         may_have_been_billed=False) from None
        raise ClaudeRequestError(f"Claude returned an unexpected error ({exc.status_code}).",
                                 may_have_been_billed=False) from None
    # Transport failures: unbilled only if the request never left this machine.
    except anthropic.APITimeoutError as exc:
        not_sent = isinstance(exc.__cause__, _NOT_SENT)
        raise ClaudeUnavailableError("Claude did not respond in time. Check your internet connection.",
                                     may_have_been_billed=not not_sent) from None
    except anthropic.APIConnectionError as exc:
        not_sent = isinstance(exc.__cause__, _NOT_SENT)
        raise ClaudeUnavailableError("Can't reach Claude. Check your internet connection.",
                                     may_have_been_billed=not not_sent) from None
    except anthropic.APIError as exc:
        raise ClaudeRequestError(f"Unexpected Claude API error ({type(exc).__name__}).") from None


def _to_reply(response) -> ClaudeReply:
    try:
        text = "".join(block.text for block in response.content if block.type == "text")
        return ClaudeReply(
            text=text,
            model=response.model,
            stop_reason=response.stop_reason,
            input_tokens=int(response.usage.input_tokens),
            output_tokens=int(response.usage.output_tokens),
        )
    except (AttributeError, TypeError, ValueError):
        # Claude answered 200 (billed) but the usage is unreadable: keep the reservation.
        raise ClaudeRequestError("Claude returned a response in an unexpected shape.") from None
