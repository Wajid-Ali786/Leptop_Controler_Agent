"""
The ONE microphone owner in this process (docs/step4 Section 5, Phase 2).

Anything that opens the microphone - a command capture today, the StopGuard's "stop" listening
later - must own it first, through here, and give it back only after its stream is fully closed:

    if not microphone.acquire("capture"):
        ...                      # someone else has it: report device_busy, disturb nobody
    try:
        ...                      # open, record, and CLOSE the stream
    finally:
        microphone.release()     # only now can the next owner begin

- Exactly one owner at a time. A second owner is refused (after at most `wait` seconds, default
  none), never queued behind a capture that may run for many seconds.
- Nesting is a bug, not a wait: the owning thread asking again raises at once instead of
  deadlocking on itself.
- Only the owning thread may release, so one caller can't free a microphone another is using.
- It holds no audio and imports nothing but threading - it is only the rule. reset() exists for the
  test suite's cleanup (tests/conftest.py), in the same way as executor.logic.forget_session_windows().
"""
import threading

_held = threading.Lock()   # locked for exactly as long as someone owns the microphone
_guard = threading.Lock()  # keeps the owner's purpose and thread consistent for readers
_owner = {"purpose": None, "thread": None}


def acquire(purpose: str, *, wait: float = 0.0) -> bool:
    """Take the microphone for `purpose` (e.g. "capture"). False if someone else still owns it after
    `wait` seconds. Raises RuntimeError if THIS thread already owns it."""
    if not isinstance(purpose, str) or not purpose.strip():
        raise ValueError("say what the microphone is wanted for")
    if isinstance(wait, bool) or not isinstance(wait, (int, float)) or wait < 0:
        raise ValueError(f"wait must be a number of seconds, not {wait!r}")
    me = threading.get_ident()
    with _guard:
        if _owner["thread"] == me:
            raise RuntimeError(f"this thread already owns the microphone (for {_owner['purpose']}); "
                               f"taking it again would deadlock")
    taken = _held.acquire(timeout=wait) if wait > 0 else _held.acquire(blocking=False)
    if not taken:
        return False
    with _guard:
        _owner.update(purpose=purpose.strip(), thread=me)
    return True


def release() -> None:
    """Give the microphone back. Only the owning thread may, and only once its stream is closed."""
    with _guard:
        if _owner["thread"] != threading.get_ident():
            raise RuntimeError("only the thread that owns the microphone may release it")
        _owner.update(purpose=None, thread=None)
        _held.release()


def owner() -> str | None:
    """What the microphone is currently owned for, or None when it is free."""
    with _guard:
        return _owner["purpose"]


def reset() -> None:
    """Forget any owner. For the test suite's cleanup only - production code releases properly."""
    with _guard:
        _owner.update(purpose=None, thread=None)
        if _held.locked():
            _held.release()
