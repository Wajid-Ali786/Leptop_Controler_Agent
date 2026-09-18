"""
AI Desktop Companion - thin entry point only.

Wires the app/ modules together; contains no logic of its own (docs/step3 Section 1).
Sets up logging, runs the Phase 0 startup health check (docs/step4 Section 3), and
exits with 0 if all checks pass, 1 otherwise. --console starts the Phase 1 typed-command
console instead (docs/step4 Section 4).

    python main.py                 offline checks only (default)
    python main.py --check-claude  also make one real, minimal Claude request
    python main.py --console       type commands ("open notepad"); help lists them
"""
import argparse
import logging
import sys

from app.console import run_console
from app.logging_setup import setup_logging
from app.health import format_report, run_health_check

log = logging.getLogger("main")


def main(argv=()) -> int:
    """Entry point. Returns the process exit code."""
    parser = argparse.ArgumentParser(description="AI Desktop Companion - startup health check.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check-claude", action="store_true",
        help="also make one real, minimal Claude API request (costs a few tokens; passes the cost controls)",
    )
    mode.add_argument(
        "--console", action="store_true",
        help="start the typed-command console instead of the health check (type help for the commands)",
    )
    args = parser.parse_args(list(argv))
    setup_logging()
    if args.console:
        log.info("Typed-command console started")
        exit_code = run_console()
        log.info("Typed-command console finished (exit code %d)", exit_code)
        return exit_code
    log.info("Startup (check_claude=%s)", args.check_claude)
    results = run_health_check(check_claude=args.check_claude)
    print(format_report(results))
    exit_code = 0 if all(r.ok for r in results) else 1
    log.info("Exit code %d", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
