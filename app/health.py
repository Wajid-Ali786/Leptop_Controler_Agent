"""
Startup health check (docs/step4 Section 3): running main.py reports either
"All systems OK" or names exactly what is missing.

By default every check is local only - no network or API calls - so it works offline:
API key, cost limits, model price, usage ledger (read-only), safety rules, and - once
setup_logging() has run - whether logging fell back to the console. Every check runs,
so all problems are listed at once instead of surfacing later at the first request.

The real Claude connection check runs only when explicitly requested
(run_health_check(check_claude=True), i.e. `python main.py --check-claude`); it goes
through the brain adapter and its cost controls like any other Claude request.

Settings are read through get_setting() and each module's own validation, so these
checks can't drift from the rules the app enforces. No result or message ever contains
a secret's value or Claude's reply text. Each result is also logged.
"""
import logging
from dataclasses import dataclass

from app import logging_setup
from app.brain import adapter, cost_controls
from app.brain.adapter import ClaudeError
from app.brain.cost_controls import CostLimitError
from app.safety import logic as safety
from config.settings import MissingSettingError, SettingsError, get_setting

API_KEY_NAME = "ANTHROPIC_API_KEY"
CLAUDE_CHECK_NAME = "Claude connection"

# Obvious placeholder values (compared case-insensitively), including the one in .env.example.
PLACEHOLDER_API_KEYS = frozenset({
    "your-key-here", "your-api-key", "your_api_key", "your-api-key-here", "changeme", "replace-me",
})

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    message: str


def _unexpected(name: str, exc: Exception) -> CheckResult:
    # Type name only: an unexpected error's message could contain sensitive details.
    return CheckResult(name, False, f"Unexpected error ({type(exc).__name__}).")


def check_api_key() -> CheckResult:
    """Confirm ANTHROPIC_API_KEY is present in .env and is not a placeholder."""
    try:
        key = get_setting(API_KEY_NAME)
    except MissingSettingError as exc:
        return CheckResult("API key", False, str(exc))
    except (OSError, ValueError) as exc:
        # Type name only: some decode errors quote raw file bytes, which could include the key.
        return CheckResult("API key", False, f"Could not read .env ({type(exc).__name__}).")
    if key.lower() in PLACEHOLDER_API_KEYS:
        return CheckResult(
            "API key", False,
            f"{API_KEY_NAME} is still a placeholder value. Replace it in .env with your real Anthropic API key.",
        )
    return CheckResult("API key", True, f"{API_KEY_NAME} is set (not yet verified with Claude).")


def check_cost_limits() -> CheckResult:
    """Rate, token and budget settings are valid, and the health-check ping fits the token limit."""
    name = "Cost limits"
    try:
        limits = cost_controls.check_limits()
        ping_tokens = get_setting("brain.ping_max_tokens")
    except SettingsError as exc:
        return CheckResult(name, False, str(exc))
    except Exception as exc:
        return _unexpected(name, exc)
    max_output = limits["cost.max_output_tokens_per_request"]
    if not isinstance(ping_tokens, int) or isinstance(ping_tokens, bool) or not 0 < ping_tokens <= max_output:
        return CheckResult(
            name, False,
            f"Setting 'brain.ping_max_tokens' ({ping_tokens!r}) must be a positive whole number no larger than "
            f"cost.max_output_tokens_per_request ({max_output:g}), or --check-claude would always be blocked.",
        )
    return CheckResult(
        name, True,
        f"rate {limits['cost.rate_limit_per_minute']:g}/min; "
        f"tokens per request {limits['cost.max_input_tokens_per_request']:g} in / {max_output:g} out; "
        f"budget ${limits['cost.daily_budget_usd']:.2f}/day, ${limits['cost.monthly_budget_usd']:.2f}/month.",
    )


def check_model_price() -> CheckResult:
    """The configured model has a price, so the money budget can be enforced for it."""
    name = "Model price"
    try:
        model = get_setting("brain.model")
        input_price, output_price = cost_controls.model_prices(model)
    except SettingsError as exc:
        return CheckResult(name, False, str(exc))
    except CostLimitError:
        return CheckResult(
            name, False,
            f"No valid price for model '{model}' in cost.prices_usd_per_million_tokens - every Claude "
            f"request would be blocked until one is added.",
        )
    except Exception as exc:
        return _unexpected(name, exc)
    return CheckResult(
        name, True,
        f"{model}: ${input_price:g} per million input tokens, ${output_price:g} per million output tokens "
        f"(price snapshot - verify at anthropic.com/pricing).",
    )


def check_usage_ledger() -> CheckResult:
    """The usage ledger is usable. Read-only: never creates or modifies the ledger."""
    name = "Usage ledger"
    try:
        return CheckResult(name, True, cost_controls.check_ledger())
    except (CostLimitError, SettingsError) as exc:
        return CheckResult(name, False, str(exc))
    except Exception as exc:
        return _unexpected(name, exc)


def check_safety_rules() -> CheckResult:
    """The safety gate's configuration is valid (otherwise it fails closed on every action)."""
    name = "Safety rules"
    try:
        keywords = safety.configured_keywords()
    except Exception as exc:
        detail = exc if isinstance(exc, SettingsError) else f"Unexpected error ({type(exc).__name__})."
        return CheckResult(name, False, f"{detail} Until this is fixed, every action will require confirmation.")
    return CheckResult(name, True, f"{len(keywords)} risky keyword(s): {', '.join(keywords)}.")


def check_logging() -> CheckResult | None:
    """Whether file logging works or fell back to the console; None if logging isn't set up."""
    status = logging_setup.logging_status()
    if status is None:
        return None
    ok, message = status
    return CheckResult("Logging", ok, message)


def check_claude_connection() -> CheckResult:
    """Make one real, minimal Claude request through the brain adapter and its cost controls.

    Costs a few tokens, so it only runs when explicitly requested. Reports the model and
    token counts only - never the reply text.
    """
    try:
        reply = adapter.ping()
    except CostLimitError as exc:
        return CheckResult(CLAUDE_CHECK_NAME, False, f"Blocked by cost controls - {exc}")
    except (ClaudeError, SettingsError) as exc:
        return CheckResult(CLAUDE_CHECK_NAME, False, str(exc))
    except Exception as exc:  # never crash the health check; type name only, details may be sensitive
        return _unexpected(CLAUDE_CHECK_NAME, exc)
    return CheckResult(
        CLAUDE_CHECK_NAME, True,
        f"Connected ({reply.model}; {reply.input_tokens} input + {reply.output_tokens} output tokens).",
    )


def run_health_check(*, check_claude: bool = False) -> list[CheckResult]:
    """Run the startup checks. The real Claude check runs only if check_claude=True."""
    results = [check_api_key(), check_cost_limits(), check_model_price(), check_usage_ledger(),
               check_safety_rules()]
    logging_result = check_logging()
    if logging_result:
        results.append(logging_result)
    if check_claude:
        if results[0].ok:
            results.append(check_claude_connection())
        else:
            results.append(CheckResult(CLAUDE_CHECK_NAME, False, "Not run - fix the API key problem above first."))
    for r in results:
        log.log(logging.INFO if r.ok else logging.WARNING,
                "Health check %s: %s - %s", r.name, "OK" if r.ok else "FAIL", r.message)
    return results


def format_report(results: list[CheckResult]) -> str:
    """Render results as a short, user-friendly report."""
    lines = ["AI Desktop Companion - health check"]
    lines += [f"  [{'OK' if r.ok else 'FAIL'}] {r.name}: {r.message}" for r in results]
    problems = sum(not r.ok for r in results)
    lines.append("All systems OK." if problems == 0 else f"{problems} problem(s) found - see above.")
    return "\n".join(lines)
