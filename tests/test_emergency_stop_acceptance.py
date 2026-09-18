"""
The emergency stop's approved Phase 1 acceptance thresholds (docs/step4 Section 4).

The thresholds were approved by the project owner on 18 September 2026 after reading the first
measurement, and they judge the SYNTHETIC path only: SendInput -> WM_HOTKEY -> the existing emergency
stop -> the Executor stops. They make no claim about physical keyboard or driver latency; that the
hotkey physically works is separate reachability evidence (PowerShell, Chrome and an elevated Notepad
in front).

Most of this file is OFFLINE: it builds trials with known latencies and checks that the judging in
scripts/measure_emergency_stop.py accepts exactly what was approved and refuses everything else, so a
later edit can't quietly loosen a threshold. The last test is the real acceptance run itself, opt-in
like every other real-desktop test:

    $env:RUN_REAL_DESKTOP_TEST='1'; pytest tests/test_emergency_stop_acceptance.py -m real_desktop -v -s

It calls the measurement script's own acceptance_run(), so there is one measurement system rather
than a second one that could drift away from it.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import measure_emergency_stop as measure  # noqa: E402


def trials(*latencies_ms, continued=(), retried=()):
    """Trials whose end-to-end latency C is known. None means no stop was measured."""
    rows = []
    for number, value in enumerate(latencies_ms, start=1):
        trial = measure.Trial(number)
        trial.c = None if value is None else value / 1000
        trial.continued = number in continued
        trial.retried = number in retried
        rows.append(trial)
    return rows


def scenario(key, *latencies_ms, **kwargs):
    return measure.Scenario(key, f"{key} scenario", trials(*latencies_ms, **kwargs))


# --- The approved numbers themselves ---------------------------------------------------------------

def test_the_thresholds_are_the_ones_that_were_approved():
    """If any of these change, the change was not the one the project owner approved."""
    assert measure.HARD_MAX_C_MS == 100.0
    assert measure.TYPICAL_MEDIAN_C_MS == 25.0
    assert measure.TYPICAL_P95_C_MS == 50.0


def test_only_the_two_twenty_trial_scenarios_carry_median_and_p95():
    """Rule 3: no statistically strong p95 requirement is invented from the 5 one-shot trials."""
    assert set(measure.TYPICAL_SCENARIOS) == {"typing", "scrolling"}
    assert [check.label for check in scenario("idle", 1.0, 2.0).checks()] == ["max C"]
    assert [check.label for check in scenario("one-shot", 1.0, 2.0).checks()] == ["max C"]
    for key in ("typing", "scrolling"):
        assert [check.label for check in scenario(key, 1.0, 2.0).checks()] == ["max C", "median C", "p95 C"]


# --- Rule 1: the hard maximum, every trial, every scenario -----------------------------------------

@pytest.mark.parametrize("key", ["idle", "typing", "scrolling", "one-shot"])
def test_one_trial_over_the_hard_maximum_fails_any_scenario(key):
    assert measure.Verdict([scenario(key, 1.0, 2.0, 100.1)]).passed is False
    assert "max C 100.1 ms exceeds the 100.0 ms threshold" in measure.Verdict([scenario(key, 100.1)]).failures[0]


@pytest.mark.parametrize("key", ["idle", "one-shot"])
def test_exactly_the_hard_maximum_is_allowed(key):
    """The rule is 'at most 100 ms', not 'under'. Checked on the two scenarios where the hard maximum
    is the only rule; Type Text and Scroll have their own limits below (a single 100 ms trial in 20
    would break their p95, which is the point of having a p95)."""
    assert measure.Verdict([scenario(key, 1.0, 100.0)]).passed is True


def test_a_slow_one_shot_or_idle_trial_still_only_faces_the_hard_maximum():
    """Rule 3: 40 ms would break the typical rules, but those don't apply to these two scenarios."""
    assert measure.Verdict([scenario("idle", 40.0, 40.0), scenario("one-shot", 40.0)]).passed is True


# --- Rule 2: median and p95 for Type Text and Scroll -----------------------------------------------

@pytest.mark.parametrize("key", ["typing", "scrolling"])
def test_median_above_25_ms_fails(key):
    twenty = [26.0] * 20
    verdict = measure.Verdict([scenario(key, *twenty)])
    assert verdict.passed is False
    assert any("median C 26.0 ms exceeds the 25.0 ms threshold" in reason for reason in verdict.failures)


@pytest.mark.parametrize("key", ["typing", "scrolling"])
def test_p95_above_50_ms_fails_even_when_the_median_is_fine(key):
    latencies = [1.0] * 18 + [51.0, 51.0]  # median 1 ms, p95 51 ms, max still under 100
    verdict = measure.Verdict([scenario(key, *latencies)])
    assert verdict.passed is False
    assert any("p95 C 51.0 ms exceeds the 50.0 ms threshold" in reason for reason in verdict.failures)
    assert not any("max C" in reason for reason in verdict.failures)


@pytest.mark.parametrize("key", ["typing", "scrolling"])
def test_exactly_the_typical_limits_are_allowed(key):
    assert measure.Verdict([scenario(key, *([25.0] * 19 + [50.0]))]).passed is True


# --- Rule 4: the non-timing rules ------------------------------------------------------------------

def test_input_continuing_after_the_stop_fails_however_fast_the_stop_was():
    verdict = measure.Verdict([scenario("typing", *([1.0] * 20), continued=(7,))])
    assert verdict.passed is False
    assert any("input continued after the stop in trial(s) [7]" in reason for reason in verdict.failures)


def test_a_retried_stopped_action_fails_however_fast_the_stop_was():
    verdict = measure.Verdict([scenario("scrolling", *([1.0] * 20), retried=(3,))])
    assert verdict.passed is False
    assert any("a stopped action was retried in trial(s) [3]" in reason for reason in verdict.failures)


def test_a_one_shot_that_sent_anything_fails():
    """For the one-shot scenario 'continued' means the click was sent at all (the pointer moved)."""
    verdict = measure.Verdict([scenario("one-shot", 1.0, 1.0, continued=(2,))])
    assert verdict.passed is False
    assert any("input continued after the stop" in reason for reason in verdict.failures)


# --- A run that didn't observe what it claims ------------------------------------------------------

def test_a_trial_with_no_measured_stop_is_not_a_pass():
    verdict = measure.Verdict([scenario("typing", *([1.0] * 19 + [None]))])
    assert verdict.passed is False
    assert any("no stop was measured in trial(s) [20]" in reason for reason in verdict.failures)


def test_a_scenario_with_nothing_timed_at_all_fails():
    verdict = measure.Verdict([scenario("idle", None, None)])
    assert verdict.passed is False
    assert any("max C not measured" in reason for reason in verdict.failures)


def test_a_verdict_with_no_scenarios_is_not_a_pass():
    assert measure.Verdict([]).passed is False


# --- The report the owner reads --------------------------------------------------------------------

def test_the_report_shows_counts_statistics_thresholds_and_the_verdict(capsys):
    verdict = measure.print_acceptance(measure.Verdict([
        scenario("typing", *([2.0] * 19 + [8.0])),
        scenario("one-shot", 3.0),
    ]))
    printed = capsys.readouterr().out
    assert verdict.passed is True
    assert "20 trials, 20 timed" in printed and "1 trials, 1 timed" in printed
    assert "min 2.0 | median 2.0 | p95 8.0 | max 8.0 ms" in printed
    for label in ("max C", "median C", "p95 C"):
        assert label in printed
    for limit in ("100.0 ms", "25.0 ms", "50.0 ms"):  # every threshold in force is printed
        assert f"<=  {limit:>9}" in printed or f"<= {limit:>10}" in printed
    assert "PASS" in printed
    assert "input continued after the stop: never" in printed
    assert "stopped action retried:         never" in printed
    assert "RESULT: PASS over 21 trials in 2 scenarios" in printed
    assert "make no claim about physical keyboard latency" in printed


def test_a_failing_report_names_every_broken_rule_and_says_not_to_tune(capsys):
    measure.print_acceptance(measure.Verdict([
        scenario("scrolling", *([1.0] * 19 + [120.0]), continued=(4,), retried=(5,)),
    ]))
    printed = capsys.readouterr().out
    assert "RESULT: FAIL" in printed
    assert "FAILED: scrolling scenario: max C 120.0 ms exceeds the 100.0 ms threshold" in printed
    assert "input continued after the stop in trial(s) [4]" in printed
    assert "a stopped action was retried in trial(s) [5]" in printed
    assert "Do not loosen a threshold and do not tune the code to chase one." in printed


# --- The real acceptance run (opt-in) ---------------------------------------------------------------

@pytest.mark.real_desktop
def test_the_emergency_stop_meets_the_approved_thresholds_on_this_machine():
    """Phase 1's emergency-stop acceptance run: 65 trials on the real desktop, judged against the
    approved thresholds. It opens and closes only its own Notepad and puts the pointer back."""
    verdict = measure.acceptance_run()
    assert verdict.passed, "the approved thresholds were not met:\n  " + "\n  ".join(verdict.failures)
