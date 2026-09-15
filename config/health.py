"""
Startup health check (docs/step4 Section 3): running main.py reports either
"All systems OK" or names exactly what is missing.

Checks are local only - no network or API calls - so they work offline.
Settings are read through get_setting() only; no result or message ever
contains a secret's value.
"""
from dataclasses import dataclass

from config.settings import MissingSettingError, get_setting

API_KEY_NAME = "ANTHROPIC_API_KEY"

# Obvious placeholder values (compared case-insensitively), including the one in .env.example.
PLACEHOLDER_API_KEYS = frozenset({
    "your-key-here", "your-api-key", "your_api_key", "your-api-key-here", "changeme", "replace-me",
})


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


def run_health_check() -> list[CheckResult]:
    """Run every startup check and return the results."""
    return [check_api_key()]


def format_report(results: list[CheckResult]) -> str:
    """Render results as a short, user-friendly report."""
    lines = ["AI Desktop Companion - health check"]
    lines += [f"  [{'OK' if r.ok else 'FAIL'}] {r.name}: {r.message}" for r in results]
    problems = sum(not r.ok for r in results)
    lines.append("All systems OK." if problems == 0 else f"{problems} problem(s) found - see above.")
    return "\n".join(lines)
