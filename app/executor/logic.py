"""
Executor decision-making - the ONLY path by which the assistant acts on the computer.

execute(action, confirm) always runs in this order:
  1. emergency-stop checkpoint
  2. validate the action - no side effects (an unknown action, a missing or unknown app, or an
     app whose result can't be verified fails cleanly without asking the user anything)
  3. Permission & Safety gate: app/safety authorize() - every action, no exceptions (CLAUDE.md rule 5)
  4. emergency-stop checkpoint again (a confirmation prompt can take a while)
  5. the real action, through app/executor/adapter.py
  6. the Verifier confirms the expected result actually happened - success is never assumed

A stop or a safety denial RAISES (EmergencyStopError / ActionDeniedError) so it can't be
silently ignored. An action that simply didn't work returns ActionResult(ok=False) with a clear
message, marked retryable when trying again could help.

execute_with_recovery() adds the Action -> Result -> Recovery loop (docs/build-plan Section 2):
a retryable failure is OFFERED for retry - never silently continued - and every retry runs the
whole pipeline again, safety gate and verification included.

Phase 1 is built one action at a time; open_app is the first.
"""
import logging
from typing import Callable

from app.executor import adapter, emergency_stop
from app.executor.models import OPEN_APP, ActionResult, ExecutorAction
from app.safety.logic import Confirm, authorize
from app.safety.models import Action
from app.verifier import logic as verifier
from config.settings import SettingsError, get_setting

log = logging.getLogger(__name__)

OfferRetry = Callable[[ActionResult], bool]


def execute(action: ExecutorAction, confirm: Confirm | None = None) -> ActionResult:
    """Carry out one action once, or explain why not. See the module docstring for the order."""
    emergency_stop.check()
    prepare = _PREPARERS.get(action.kind)
    if prepare is None:
        return _result(action, False, f"I don't know how to do '{action.kind}' yet.")
    prepared = prepare(action)  # validation only - nothing happens on the computer here
    if isinstance(prepared, ActionResult):
        return prepared
    authorize(Action(action.description), confirm)  # raises ActionDeniedError when denied
    emergency_stop.check()
    return prepared()


def execute_with_recovery(action: ExecutorAction, confirm: Confirm | None = None,
                          offer_retry: OfferRetry | None = None) -> ActionResult:
    """Action -> Result -> Recovery. A retryable failure is offered via offer_retry(result), which
    must return exactly True to retry; without it, nothing is retried. Stops after
    executor.max_attempts attempts."""
    try:
        max_attempts = _max_attempts()
    except SettingsError as exc:
        return _result(action, False, str(exc))
    attempt = 1
    while True:
        result = execute(action, confirm)
        if result.ok or not result.retryable:
            return result
        if attempt >= max_attempts:
            return _result(action, False,
                           f"{result.message} Gave up after {attempt} attempt{'s' if attempt != 1 else ''}.")
        if not _retry_accepted(result, offer_retry):
            return _result(action, False, f"{result.message} Not retried.", retryable=True)
        attempt += 1
        log.info("Executor retrying %s '%s' (attempt %d of %d)",
                 action.kind, action.target.strip(), attempt, max_attempts)


# --- open_app ---------------------------------------------------------------------------

def _prepare_open_app(action: ExecutorAction):
    name = action.target.strip().lower()
    if not name:
        return _result(action, False, "Which app should I open?")
    try:
        apps = _configured_apps()
    except SettingsError as exc:
        return _result(action, False, str(exc))
    executable = apps.get(name)
    if executable is None:
        return _result(action, False, f"I don't know an app called '{action.target.strip()}'. "
                                      f"Apps I can open: {', '.join(sorted(apps))}.")
    try:
        expectation = verifier.expect_window(name)
    except SettingsError as exc:
        return _result(action, False, str(exc))

    def run() -> ActionResult:
        try:
            before = verifier.snapshot_windows(expectation)
        except verifier.VerifierUnavailableError as exc:
            return _result(action, False, f"Didn't open {name}: I can't check whether its window appears ({exc}).")
        try:
            adapter.launch_app(executable)
        except adapter.ExecutorAdapterError as exc:
            return _result(action, False, str(exc))
        check = verifier.wait_for_new_window(expectation, before)
        if not check.ok:
            return _result(action, False, check.message, retryable=check.retryable)
        return _result(action, True, f"Opened {name}; its window appeared after {check.elapsed_seconds:.1f}s.")

    return run


def _configured_apps() -> dict[str, str]:
    apps = get_setting("executor.apps")
    valid = isinstance(apps, dict) and apps and all(
        isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip() for k, v in apps.items())
    if not valid:
        raise SettingsError(f"Setting 'executor.apps' must map app names to executables, got {apps!r}.")
    return {k.strip().lower(): v.strip() for k, v in apps.items()}


# --- Helpers ----------------------------------------------------------------------------

_PREPARERS = {OPEN_APP: _prepare_open_app}


def _max_attempts() -> int:
    value = get_setting("executor.max_attempts")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SettingsError(f"Setting 'executor.max_attempts' must be a whole number of at least 1, got {value!r}.")
    return value


def _retry_accepted(result: ActionResult, offer_retry: OfferRetry | None) -> bool:
    if offer_retry is None:
        return False
    try:
        return offer_retry(result) is True
    except Exception as exc:  # a broken prompt must not crash the assistant or retry blindly
        log.warning("Retry prompt failed (%s); not retrying", type(exc).__name__)
        return False


def _result(action: ExecutorAction, ok: bool, message: str, retryable: bool = False) -> ActionResult:
    log.log(logging.INFO if ok else logging.WARNING,
            "Executor %s '%s': %s - %s", action.kind, action.target.strip(), "OK" if ok else "FAILED", message)
    return ActionResult(action, ok, message, retryable)
