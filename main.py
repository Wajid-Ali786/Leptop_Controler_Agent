"""
AI Desktop Companion - thin entry point only.

Wires the app/ modules together; contains no logic of its own (docs/step3 Section 1).
Currently runs the Phase 0 startup health check (docs/step4 Section 3) and exits
with 0 if all checks pass, 1 otherwise.
"""
import sys

from config.health import format_report, run_health_check


def main() -> int:
    """Entry point. Returns the process exit code."""
    results = run_health_check()
    print(format_report(results))
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
