"""
Startup health check (docs/step4 Section 3): running main.py reports either
"All systems OK" or names exactly what is missing.

By default every check is local only - no network or API calls - so it works offline.
The real Claude connection check runs only when explicitly requested
(run_health_check(check_claude=True), i.e. `python main.py --check-claude`); it goes
through the brain adapter and its cost controls like any other Claude request.

Settings are read through get_setting() only; no result or message ever contains a
secret's value or Claude's reply text. Each result is also logged.
"""
import logging
from dataclasses import dataclass

from app.brain import adapter
from app.brain.adapter import ClaudeError
from app.brain.cost_controls import CostLimitError
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
        return CheckResult(CLAUDE_CHECK_NAME, False, f"Unexpected error ({type(exc).__name__}).")
    return CheckResult(
        CLAUDE_CHECK_NAME, True,
        f"Connected ({reply.model}; {reply.input_tokens} input + {reply.output_tokens} output tokens).",
    )


def run_health_check(*, check_claude: bool = False) -> list[CheckResult]:
    """Run the startup checks. The real Claude check runs only if check_claude=True."""
    results = [check_api_key()]
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
