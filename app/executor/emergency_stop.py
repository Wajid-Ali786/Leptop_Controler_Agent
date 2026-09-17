"""
Emergency stop - Phase 0 placeholder (docs/step4 Section 3); full Executor
integration arrives in Phase 1 (docs/step4 Section 4).

One process-wide stop flag, safe to trigger from any thread (e.g. a hotkey listener)
and visible to every thread immediately:

    from app.executor import emergency_stop
    emergency_stop.trigger("hotkey")   # any thread; takes effect before anything is logged
    emergency_stop.check()             # Executor checkpoints: raises EmergencyStopError if stopped
    emergency_stop.wait(2.0)           # interruptible sleep: returns True as soon as stopped
    emergency_stop.reset("user")       # explicit clear, so a stop never disables the assistant for good

The flag stays set until reset() is called - nothing clears it automatically.
"""
import logging
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

_stop = threading.Event()
_lock = threading.Lock()  # keeps the flag and its source/time consistent with each other
_info = {"source": None, "triggered_at": None}


class EmergencyStopError(Exception):
    """Raised at an Executor checkpoint while the emergency stop is active."""


class TypingInterruptedError(EmergencyStopError):
    """The emergency stop interrupted typing after some text was already sent. `result` is the
    Executor's ActionResult saying how much was typed (outcome PARTIAL, or UNVERIFIED if everything
    was sent and only the check was interrupted). The message never contains the text."""

    def __init__(self, message: str, result):
        super().__init__(message)
        self.result = result


@dataclass(frozen=True)
class StopStatus:
    stopped: bool
    source: str | None         # who triggered it, e.g. "hotkey", "dashboard"
    triggered_at: float | None  # time.time() when triggered


def trigger(source: str = "unknown") -> None:
    """Request an immediate stop. Safe from any thread; repeated calls keep the first trigger."""
    with _lock:
        if _stop.is_set():
            return
        _stop.set()
        _info.update(source=str(source), triggered_at=time.time())
    log.warning("Emergency stop triggered (source: %s)", source)


def reset(source: str = "unknown") -> None:
    """Clear the stop so the assistant can act again. Harmless if not stopped."""
    with _lock:
        if not _stop.is_set():
            return
        _stop.clear()
        _info.update(source=None, triggered_at=None)
    log.info("Emergency stop reset (source: %s)", source)


def is_stopped() -> bool:
    return _stop.is_set()


def status() -> StopStatus:
    with _lock:
        return StopStatus(_stop.is_set(), _info["source"], _info["triggered_at"])


def check() -> None:
    """Executor checkpoint: raise EmergencyStopError if the stop is active."""
    current = status()
    if current.stopped:
        raise EmergencyStopError(
            f"Emergency stop is active (triggered by {current.source}); reset it before acting again."
        )


def wait(timeout: float) -> bool:
    """Sleep up to `timeout` seconds, waking immediately if the stop is triggered.
    Returns True if stopped. The Executor should use this instead of time.sleep()."""
    return _stop.wait(timeout)
