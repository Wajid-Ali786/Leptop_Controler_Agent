"""
AI Desktop Companion - thin entry point only.

Wires the app/ modules together; contains no logic of its own (docs/step3 Section 1).
Sets up logging, runs the Phase 0 startup health check (docs/step4 Section 3), and
exits with 0 if all checks pass, 1 otherwise.

    python main.py                 offline checks only (default)
    python main.py --check-claude  also make one real, minimal Claude request
"""
import argparse
import logging
import sys

from app.logging_setup import setup_logging
from app.health import format_report, run_health_check

log = logging.getLogger("main")


def main(argv=()) -> int:
    """Entry point. Returns the process exit code."""
    parser = argparse.ArgumentParser(description="AI Desktop Companion - startup health check.")
    parser.add_argument(
        "--check-claude", action="store_true",
        help="also make one real, minimal Claude API request (costs a few tokens; passes the cost controls)",
    )
    args = parser.parse_args(list(argv))
    setup_logging()
    log.info("Startup (check_claude=%s)", args.check_claude)
    results = run_health_check(check_claude=args.check_claude)
    print(format_report(results))
    exit_code = 0 if all(r.ok for r in results) else 1
    log.info("Exit code %d", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
