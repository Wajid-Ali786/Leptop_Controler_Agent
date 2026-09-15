"""
Tests for app/executor/ - Phase 0: the emergency-stop placeholder (emergency_stop.py).
Full Executor integration (halting real actions mid-step) arrives in Phase 1.
"""
import logging
import threading
import time

import pytest

from app.executor import emergency_stop
from app.executor.emergency_stop import EmergencyStopError, StopStatus

LOGGER = "app.executor.emergency_stop"


@pytest.fixture(autouse=True)
def clean_stop():
    """The stop flag is process-wide, so every test starts and ends un-stopped."""
    emergency_stop.reset("test-setup")
    yield
    emergency_stop.reset("test-teardown")


# --- Exists, triggers, reports, resets ---

def test_mechanism_exists_and_starts_clear():
    for name in ("trigger", "reset", "is_stopped", "status", "check", "wait"):
        assert callable(getattr(emergency_stop, name))
    assert emergency_stop.is_stopped() is False
    assert emergency_stop.status() == StopStatus(stopped=False, source=None, triggered_at=None)
    emergency_stop.check()  # does not raise while clear


def test_trigger_takes_effect_immediately():
    emergency_stop.trigger("hotkey")
    assert emergency_stop.is_stopped() is True
    status = emergency_stop.status()
    assert status.stopped and status.source == "hotkey"
    assert status.triggered_at == pytest.approx(time.time(), abs=5)


def test_check_raises_while_stopped():
    emergency_stop.trigger("dashboard")
    with pytest.raises(EmergencyStopError, match="triggered by dashboard"):
        emergency_stop.check()


def test_repeated_trigger_keeps_the_first_source():
    emergency_stop.trigger("hotkey")
    emergency_stop.trigger("dashboard")
    assert emergency_stop.status().source == "hotkey"


def test_reset_clears_the_stop_and_it_can_be_triggered_again():
    emergency_stop.trigger("hotkey")
    emergency_stop.reset("user")
    assert emergency_stop.is_stopped() is False
    assert emergency_stop.status() == StopStatus(stopped=False, source=None, triggered_at=None)
    emergency_stop.check()
    emergency_stop.trigger("hotkey")  # not permanently disabled
    assert emergency_stop.is_stopped() is True


def test_reset_when_not_stopped_is_harmless(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    emergency_stop.reset("user")
    assert emergency_stop.is_stopped() is False
    assert caplog.records == []


def test_trigger_and_reset_are_logged(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    emergency_stop.trigger("hotkey")
    emergency_stop.reset("user")
    assert [(r.levelname, r.getMessage()) for r in caplog.records if r.name == LOGGER] == [
        ("WARNING", "Emergency stop triggered (source: hotkey)"),
        ("INFO", "Emergency stop reset (source: user)"),
    ]


# --- Thread safety: triggered from a different thread than the Executor ---

def test_trigger_from_another_thread_is_seen_by_this_thread():
    hotkey = threading.Thread(target=emergency_stop.trigger, args=("hotkey-thread",))
    hotkey.start()
    hotkey.join(timeout=5)
    assert emergency_stop.is_stopped() is True
    assert emergency_stop.status().source == "hotkey-thread"


def test_worker_loop_halts_when_stopped_from_another_thread():
    """The Phase 1 pattern: an Executor loop calling check() halts when another thread triggers."""
    halted = threading.Event()

    def executor_loop():
        try:
            while True:
                emergency_stop.check()
                time.sleep(0.001)
        except EmergencyStopError:
            halted.set()

    worker = threading.Thread(target=executor_loop, daemon=True)
    worker.start()
    time.sleep(0.05)
    assert not halted.is_set()  # still running before the stop
    emergency_stop.trigger("hotkey")
    assert halted.wait(timeout=2)
    worker.join(timeout=2)
    assert not worker.is_alive()


def test_wait_wakes_early_when_stopped_from_another_thread():
    hotkey = threading.Timer(0.05, emergency_stop.trigger, args=("hotkey",))
    start = time.monotonic()
    hotkey.start()
    assert emergency_stop.wait(10) is True
    assert time.monotonic() - start < 2  # woke on the stop, not after 10 s
    hotkey.join()


def test_wait_times_out_normally_when_not_stopped():
    start = time.monotonic()
    assert emergency_stop.wait(0.05) is False
    assert time.monotonic() - start >= 0.04


def test_many_threads_triggering_at_once_leave_one_consistent_stop(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    barrier = threading.Barrier(20)

    def press(i):
        barrier.wait()
        emergency_stop.trigger(f"thread-{i}")

    threads = [threading.Thread(target=press, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    status = emergency_stop.status()
    assert status.stopped and status.source.startswith("thread-")
    assert caplog.text.count("Emergency stop triggered") == 1


def test_concurrent_trigger_and_reset_never_leave_inconsistent_state():
    errors = []

    def hammer(i):
        action = emergency_stop.trigger if i % 2 else emergency_stop.reset
        for _ in range(200):
            action(f"thread-{i}")
            s = emergency_stop.status()
            if s.stopped != (s.source is not None and s.triggered_at is not None):
                errors.append(s)

    threads = [threading.Thread(target=hammer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert errors == []
