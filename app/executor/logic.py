"""
Executor decision-making - the ONLY path by which the assistant acts on the computer.

execute(action, confirm) always runs in this order:
  1. emergency-stop checkpoint
  2. validate the action - no side effects (an unknown action, a missing or unknown app
     fails cleanly without asking the user anything)
  3. Permission & Safety gate: app/safety authorize() - every action, no exceptions (CLAUDE.md rule 5)
  4. emergency-stop checkpoint again (a confirmation prompt can take a while)
  5. the real action, through app/executor/adapter.py

A stop or a safety denial RAISES (EmergencyStopError / ActionDeniedError) so it can't be
silently ignored. An action that simply didn't work returns ActionResult(ok=False) with a
clear message, for the Verifier / recovery loop.

Phase 1 is built one action at a time; open_app is the first.
"""
import logging

from app.executor import adapter, emergency_stop
from app.executor.models import OPEN_APP, ActionResult, ExecutorAction
from app.safety.logic import Confirm, authorize
from app.safety.models import Action
from config.settings import SettingsError, get_setting

log = logging.getLogger(__name__)


def execute(action: ExecutorAction, confirm: Confirm | None = None) -> ActionResult:
    """Carry out one action, or explain why not. See the module docstring for the order."""
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

    def run() -> ActionResult:
        try:
            pid = adapter.launch_app(executable)
        except adapter.ExecutorAdapterError as exc:
            return _result(action, False, str(exc))
        return _result(action, True, f"Started {name} ({executable}, process {pid}).")

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


def _result(action: ExecutorAction, ok: bool, message: str) -> ActionResult:
    log.log(logging.INFO if ok else logging.WARNING,
            "Executor %s '%s': %s - %s", action.kind, action.target.strip(), "OK" if ok else "FAILED", message)
    return ActionResult(action, ok, message)
