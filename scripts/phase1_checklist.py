"""
Phase 1 pass/fail checklist (docs/step4 Sections 4 and 15) - explicit and repeatable.

    python scripts/phase1_checklist.py                   # offline evidence only (the safe default)
    python scripts/phase1_checklist.py --real-desktop    # ALSO the unattended real-desktop acceptance
    python scripts/phase1_checklist.py --real-elevated   # ALSO the manual elevated permission-denied check

docs/step4 Section 4 is the source of truth: every blocking row below is a Step 4 Build bullet, a row
of its test-case table, or a clause of its Done-when sentence. Nothing else blocks completion. The
architecture rows at the end are regression information, deliberately NOT completion requirements -
but a regression that FAILS still makes this command exit non-zero.

Each row names the pytest tests that are its evidence. This script runs pytest once per selected
group, reads the JUnit XML it produces, and maps real test results back to requirements. It parses no
prose, counts no totals, and contains no Executor, action or Verifier logic of its own. If a mapped
test has been renamed or is no longer collected, that is a CHECKLIST MAPPING ERROR, never a PASS.

Statuses: PASS (ran in THIS invocation and passed), FAIL (ran and failed), NOT RUN (not executed in
this mode - never promoted to PASS from an earlier run), DEFERRED (Step 4 explicitly defers it),
DOCUMENTED (a human observed it and Step 4 records it, with a date and a way to reproduce).

The safe default opens no window, moves no pointer, sends no input, touches no clipboard and waits for
nothing. It also clears the RUN_* opt-in variables for its own pytest run, so an exported
RUN_REAL_DESKTOP_TEST can't turn the safe default into a desktop run.
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

PASS, FAIL, NOT_RUN, DEFERRED, DOCUMENTED = "PASS", "FAIL", "NOT RUN", "DEFERRED", "DOCUMENTED"
MAPPING_ERROR = "MAPPING ERROR"
OFFLINE, DESKTOP, ELEVATED, MANUAL, DEFER = "offline", "real-desktop", "real-elevated", "manual", "deferred"

OPT_IN_VARIABLES = ("RUN_REAL_DESKTOP_TEST", "RUN_ELEVATED_TEST", "RUN_REAL_CLIPBOARD_TEST",
                   "RUN_REAL_CLAUDE_TEST")
# Marker expressions, so no mode can select a test another mode owns. The clipboard test carries
# real_desktop AND real_clipboard, and the elevated test carries real_desktop AND real_elevated, so
# both are excluded by name rather than by trusting the opt-in variables alone.
GROUP_MARKERS = {
    OFFLINE: "not real_desktop and not real_elevated and not real_clipboard and not real_api",
    DESKTOP: "real_desktop and not real_elevated and not real_clipboard and not real_api",
    ELEVATED: "real_elevated",
}
GROUP_ENVIRONMENT = {
    OFFLINE: {},
    DESKTOP: {"RUN_REAL_DESKTOP_TEST": "1"},
    ELEVATED: {"RUN_REAL_DESKTOP_TEST": "1", "RUN_ELEVATED_TEST": "1"},
}
# Failure text that means the desktop was being used while a real test ran, rather than the
# requirement being unmet. Reported as FAIL with a note - never hidden, never auto-retried.
INTERFERENCE_SIGNS = (
    "isn't over the active window",
    "the scroll fixture isn't in front",
    "held down on the keyboard",
    "isn't the active window",
    "didn't come to the front",
    "INVALID SETUP",
    "cursor_position",
    "are held down on the keyboard",
    "is held down on the keyboard",
)
# Failures that could be either: a trial where the action never ran (so no stop could be exercised)
# reads the same whether the desktop was in use or the stop genuinely regressed.
AMBIGUOUS_SIGNS = (
    "no stop was measured",
    "NOT INTERRUPTED",
)
CAPTURED_MARKER = "--- captured output ---"

BUILD, CASES, DONE = "BUILD / CAPABILITY", "TEST CASES", "DONE-WHEN"
EVIDENCE, DEFERRED_SECTION = "MANUAL EVIDENCE", "DEFERRED"
REGRESSION = "NON-BLOCKING REGRESSION CHECKS"

DESKTOP_HINT = "use --real-desktop"
ELEVATED_HINT = "use --real-elevated (needs an elevated Notepad you start and focus by hand)"


@dataclass(frozen=True)
class Row:
    """One Step 4 requirement and the tests that are its evidence."""
    section: str
    name: str
    group: str
    anchors: tuple = ()
    blocking: bool = True
    note: str = ""
    hint: str = ""


ROWS = (
    # --- Build bullets (Step 4 Section 4, "Build") ---
    Row(BUILD, "Typed commands parse to ExecutorActions and run through the console", OFFLINE,
        ("tests/test_executor_commands.py::test_bare_close_is_ambiguous_and_never_guesses",
         "tests/test_console.py::test_every_action_kind_is_classified_for_hand_over")),
    Row(BUILD, "Open and close applications", OFFLINE,
        ("tests/test_executor_logic.py::test_open_known_app_passes_safety_launches_and_is_verified",
         "tests/test_executor_close_app.py::test_missing_or_unknown_app_fails_cleanly")),
    Row(BUILD, "Mouse click by coordinate", OFFLINE,
        ("tests/test_executor_click.py::test_missing_coordinates_ask_where",)),
    Row(BUILD, "Type text", OFFLINE,
        ("tests/test_executor_type_text.py::test_text_is_confirmed_typed_and_verified",
         "tests/test_executor_type_text.py::test_focus_change_part_way_is_partial_with_progress")),
    Row(BUILD, "Keyboard shortcuts", OFFLINE,
        ("tests/test_executor_shortcut.py::test_malformed_unknown_unsupported_and_reserved_are_refused",)),
    Row(BUILD, "Scroll", OFFLINE,
        ("tests/test_executor_scroll.py::test_invalid_input_fails_cleanly_and_asks_nothing",)),
    Row(BUILD, "Refresh", OFFLINE,
        ("tests/test_executor_refresh.py::test_supported_apps_and_their_risk_levels",)),
    Row(BUILD, "Window controls (minimize/maximize/restore/close)", OFFLINE,
        ("tests/test_executor_window_control.py::test_state_that_doesnt_change_is_a_failure_not_success",)),
    Row(BUILD, "Executor adapter boundary (only the adapter touches the computer)", OFFLINE,
        ("tests/test_executor_logic.py::test_only_executor_adapter_controls_the_computer",
         "tests/test_executor_logic.py::test_only_the_two_adapters_use_the_windows_api_through_ctypes",
         "tests/test_executor_logic.py::test_only_each_modules_logic_uses_its_adapter")),
    Row(BUILD, "Verifier confirms each action's expected result", OFFLINE,
        ("tests/test_verifier.py::test_expectation_comes_from_config",)),
    Row(BUILD, "Emergency stop implemented and wired (global hotkey)", OFFLINE,
        ("tests/test_executor.py::test_check_raises_while_stopped",
         "tests/test_executor.py::test_worker_loop_halts_when_stopped_from_another_thread",
         "tests/test_executor_hotkey.py::test_the_configured_hotkey_is_the_one_registered")),

    # --- Test-case table (Step 4 Section 4) ---
    Row(CASES, "Happy path: open Notepad and type into it", DESKTOP,
        ("tests/test_real_desktop.py::test_open_app_really_opens_a_window_and_the_test_closes_it",
         "tests/test_real_desktop.py::test_type_text_into_a_notepad_the_test_opened"), hint=DESKTOP_HINT),
    Row(CASES, "Wrong input: an unknown app fails cleanly", OFFLINE,
        ("tests/test_executor_logic.py::test_unknown_app_fails_cleanly",)),
    Row(CASES, "Missing info: 'open' with nothing asks which app", OFFLINE,
        ("tests/test_executor_logic.py::test_missing_app_name_asks_which_app",)),
    Row(CASES, "App unavailable: not installed fails without starting anything", OFFLINE,
        ("tests/test_executor_logic.py::test_adapter_reports_a_missing_executable_without_starting_anything",)),
    Row(CASES, "Permission denied: an elevated window is refused clearly", ELEVATED,
        ("tests/test_permission_denied_acceptance.py::"
         "test_acting_on_an_elevated_window_is_refused_clearly_and_never_reported_as_done",), hint=ELEVATED_HINT),
    Row(CASES, "Recovery: Action -> Result -> Recovery offers retry, never silent", OFFLINE,
        ("tests/test_executor_logic.py::test_failed_attempt_is_offered_for_retry_and_the_retry_succeeds",
         "tests/test_executor_logic.py::test_without_a_retry_prompt_nothing_is_retried_silently",
         "tests/test_executor_logic.py::test_retries_stop_at_max_attempts")),
    Row(CASES, "Emergency stop halts the Executor mid-action", OFFLINE,
        ("tests/test_executor_type_text.py::test_stop_before_the_first_character_is_a_plain_stop",
         "tests/test_executor_type_text.py::test_stop_during_confirmation_types_nothing")),

    # --- Done-when (Step 4 Section 4) ---
    Row(DONE, "10 different typed commands run back-to-back with no code changes", DESKTOP,
        ("tests/test_real_desktop.py::test_ten_typed_commands_back_to_back_through_the_console",),
        hint=DESKTOP_HINT),
    Row(DONE, "Emergency stop meets the approved measured thresholds", DESKTOP,
        ("tests/test_emergency_stop_acceptance.py::"
         "test_the_emergency_stop_meets_the_approved_thresholds_on_this_machine",), hint=DESKTOP_HINT),
    Row(DONE, "Verifier catches a deliberately-broken action instead of false success", DESKTOP,
        ("tests/test_verifier_acceptance.py::"
         "test_a_deliberately_broken_maximize_is_caught_instead_of_reported_as_done",), hint=DESKTOP_HINT),

    # --- Manual evidence: real, but not reproducible by any script ---
    Row(EVIDENCE, "Physical Ctrl+Alt+Backspace reaches the listener", MANUAL, (),
        note="PowerShell, Chrome and an elevated Notepad in front - step4 Section 4 notes, "
             "18 September 2026. Reproduce: python scripts/measure_emergency_stop.py --physical"),

    # --- Deferred by explicit Step 4 notes (non-blocking) ---
    Row(DEFERRED_SECTION, "Real browser refresh test", DEFER, (), blocking=False,
        note='step4 Section 4 / tests/test_real_desktop.py: "There is no browser test: it would open '
             'your real browser profile."'),
    Row(DEFERRED_SECTION, "Click effect verification", DEFER, (), blocking=False,
        note="step4 Section 4: a click is honestly `unverified` in Phase 1; there is no screen "
             "understanding, OCR or element detection until Phase 5"),
    Row(DEFERRED_SECTION, "Refresh effect verification", DEFER, (), blocking=False,
        note='step4 Section 4: "A refresh is never DONE in Phase 1" - see "What would make refresh '
             'verifiable (Phase 5)"'),
    Row(DEFERRED_SECTION, "Real clipboard test (Ctrl+C / Ctrl+V)", DEFER, (), blocking=False,
        note="optional: it REPLACES your clipboard, so it stays behind RUN_REAL_CLIPBOARD_TEST=1 and "
             "is not required by any Step 4 Phase 1 line"),

    # --- Regression information: NOT Step 4 completion requirements ---
    Row(REGRESSION, "Safety pipeline order: nothing acts unauthorized", OFFLINE,
        ("tests/test_executor_logic.py::test_risky_action_without_confirmation_is_denied_and_nothing_launches",
         "tests/test_executor_logic.py::test_every_retry_passes_the_safety_gate_again"), blocking=False),
    Row(REGRESSION, "Typed console cannot bypass Executor, Safety or Verifier", OFFLINE,
        ("tests/test_console.py::test_the_executors_own_refusal_comes_back_as_a_result",
         "tests/test_console.py::test_an_unclassified_action_kind_fails_safe_by_asking_for_hand_over"),
        blocking=False),
    Row(REGRESSION, "Shortcuts cannot bypass Refresh or the close mechanism", OFFLINE,
        ("tests/test_executor_shortcut.py::test_malformed_unknown_unsupported_and_reserved_are_refused",),
        blocking=False),
)


ALL_ANCHORS = frozenset(anchor for mapped in ROWS for anchor in mapped.anchors)
UNMAPPED_SECTION = "RELATED / UNMAPPED TEST FAILURES"


@dataclass
class Outcome:
    status: str
    detail: str = ""
    interference: bool = False
    ambiguous: bool = False


@dataclass
class GroupResult:
    """What one pytest run produced: per-anchor cases, per-file failures, and how pytest itself did."""
    cases: dict = field(default_factory=dict)  # base node id -> list of (status, message)
    broken: str = ""                           # set when pytest itself could not produce results


def environment_for(group):
    environment = dict(os.environ)
    for name in OPT_IN_VARIABLES:  # a safe default stays safe even if these are exported
        environment.pop(name, None)
    environment.update(GROUP_ENVIRONMENT[group])
    return environment


def run_group(group, xml_path, timeout=3600):
    """Run one pytest group and read its JUnit XML. Never parses pytest's prose for pass/fail."""
    command = [PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-m", GROUP_MARKERS[group],
               f"--junitxml={xml_path}", "-o", "junit_logging=system-out"]
    try:
        process = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=timeout,
                                 env=environment_for(group))
    except subprocess.TimeoutExpired:
        return GroupResult(broken=f"pytest for the {group} group timed out after {timeout}s")
    if not Path(xml_path).exists():
        tail = (process.stdout or process.stderr or "").strip().splitlines()[-5:]
        return GroupResult(broken=f"pytest for the {group} group wrote no JUnit XML (exit "
                                  f"{process.returncode}): {' | '.join(tail)}")
    try:
        return read_junit(xml_path)
    except ElementTree.ParseError as exc:
        return GroupResult(broken=f"the {group} group's JUnit XML could not be read ({exc})")


def read_junit(xml_path):
    result = GroupResult()
    for case in ElementTree.parse(xml_path).getroot().iter("testcase"):
        file_name = (case.get("file") or case.get("classname", "").replace(".", "/") + ".py").replace("\\", "/")
        name = case.get("name", "")
        base = f"{file_name}::{name.split('[')[0]}"
        status, message, captured = PASS, "", ""
        for child in case:
            if child.tag in ("failure", "error"):
                status = FAIL
                message = (child.get("message") or child.text or "").strip()
            elif child.tag == "skipped":
                status = NOT_RUN
            elif child.tag == "system-out":
                captured = (child.text or "").strip()
        if status == FAIL and captured:  # the reason usually lives in what the test printed
            message = "\n".join((message, CAPTURED_MARKER, captured[-4000:]))
        result.cases.setdefault(base, []).append((status, message))
    return result


def headline(message, limit=200):
    """The first line of a failure, for display: the captured output is kept for scanning only."""
    first = message.split(CAPTURED_MARKER)[0].strip().splitlines()
    return (first[0] if first else "")[:limit]


def looks_environmental(messages):
    """Whether these failures name something that means the desktop was in use. Scans the tests' own
    captured output too, which is where reasons like held-down modifier keys are printed."""
    return any(sign in message for message in messages for sign in INTERFERENCE_SIGNS)


def looks_ambiguous(messages):
    """A failure that is EITHER environmental OR a real regression, and cannot be told apart here."""
    return any(sign in message for message in messages for sign in AMBIGUOUS_SIGNS)


def evaluate(row, results):
    """This row's status, from the group results actually gathered."""
    if row.group == MANUAL:
        return Outcome(DOCUMENTED, row.note)
    if row.group == DEFER:
        return Outcome(DEFERRED, row.note)
    result = results.get(row.group)
    if result is None:
        return Outcome(NOT_RUN, row.hint)
    if result.broken:
        return Outcome(FAIL, result.broken)

    missing = [anchor for anchor in row.anchors if anchor not in result.cases]
    if missing:  # the group ran, so these should have been in it: renamed, removed or not collected
        return Outcome(MAPPING_ERROR, f"mapped test(s) not collected in this run: {', '.join(missing)}")

    # ONLY this row's own anchors decide its status. A failure elsewhere in the same file is real and
    # is reported under RELATED / UNMAPPED TEST FAILURES, but it is not this requirement's failure.
    statuses = [(status, message) for anchor in row.anchors for status, message in result.cases[anchor]]
    failures = [message for status, message in statuses if status == FAIL]
    if failures:
        shown = "; ".join(dict.fromkeys(headline(failure) for failure in failures))[:400]
        return Outcome(FAIL, shown, looks_environmental(failures), looks_ambiguous(failures))
    if all(status == NOT_RUN for status, _ in statuses):
        return Outcome(FAIL, "its mapped tests were selected in this mode but SKIPPED - an opt-in gate "
                             "or platform skip. Not a PASS: the evidence was asked for and not produced.")
    return Outcome(PASS, ", ".join(f"{len(result.cases[anchor])} case(s) in {anchor.split('::')[1]}"
                                   for anchor in row.anchors))


def unmapped_failures(results):
    """Failures in the selected runs that are NOT any row's evidence. They belong to no Step 4
    requirement, so they must not change a requirement's status - but they are real, so they are
    listed and they stop this command returning success."""
    found = []
    for group, result in results.items():
        if result.broken:  # already reported against every row in that group
            continue
        for base, cases in sorted(result.cases.items()):
            if base in ALL_ANCHORS:
                continue
            for status, message in cases:
                if status == FAIL:
                    found.append((base, headline(message) or "no reason reported", group))
                    break  # one line per test, however many parametrised cases failed
    return found


def warn_before_real_tests(groups, pause):
    print("\n" + "!" * 78)
    print("REAL DESKTOP TESTS ARE ABOUT TO RUN.")
    print("Do not touch the mouse or keyboard until the run finishes.")
    print("Test-owned Notepad/Explorer windows may open temporarily; they are closed again.")
    if ELEVATED in groups:
        print("The elevated check needs a Notepad YOU started with 'Run as administrator', in front.")
        print("UAC is never automated. Start and focus it yourself.")
    print("The clipboard test and any browser test are NOT included.")
    print("!" * 78)
    for remaining in range(pause, 0, -1):
        print(f"  starting in {remaining}...", flush=True)
        time.sleep(1)


def report(outcomes, groups, unmapped=()):
    print("\nPhase 1 Checklist (docs/step4 Section 4)")
    print("=" * 78)
    for section in (BUILD, CASES, DONE, EVIDENCE, DEFERRED_SECTION, REGRESSION):
        rows = [(row, outcome) for row, outcome in outcomes if row.section == section]
        if not rows:
            continue
        print(f"\n{section}" + ("   (not Step 4 completion requirements)" if section == REGRESSION else ""))
        for row, outcome in rows:
            print(f"[{outcome.status}] {row.name}")
            if outcome.detail:
                print(f"         {outcome.detail}")
            if outcome.interference:
                print("         NOTE: this failure names desktop interference (the pointer or focus moved, "
                      "or a modifier key was held), not a broken requirement. Re-run on an idle desktop.")
            elif outcome.ambiguous:
                print("         NOTE: this failure is AMBIGUOUS - a trial where the action never ran looks "
                      "the same whether the desktop was in use or the stop regressed. It is reported as "
                      "FAIL. Re-run on a completely idle desktop; if it repeats, treat it as real.")

    if unmapped:
        print(f"\n{UNMAPPED_SECTION}   (real failures that are not any requirement's evidence)")
        for node, reason, group in unmapped:
            print(f"[{FAIL}] {node}   [{group} run]")
            print(f"         {reason}")

    blocking = [(row, outcome) for row, outcome in outcomes if row.blocking]
    satisfied = [(row, outcome) for row, outcome in blocking if outcome.status in (PASS, DOCUMENTED)]
    failures = [(row, outcome) for row, outcome in outcomes if outcome.status in (FAIL, MAPPING_ERROR)]
    not_run = [(row, outcome) for row, outcome in blocking if outcome.status == NOT_RUN]
    documented = [row for row, outcome in blocking if outcome.status == DOCUMENTED]
    deferred = [row for row, outcome in outcomes if outcome.status == DEFERRED]

    print("\nSUMMARY")
    print("=" * 78)
    for group, label in ((OFFLINE, "Offline"), (DESKTOP, "Real desktop"), (ELEVATED, "Manual/special")):
        rows = [outcome for row, outcome in outcomes if row.group == group]
        ran = [outcome for outcome in rows if outcome.status in (PASS, FAIL, MAPPING_ERROR)]
        passed = [outcome for outcome in ran if outcome.status == PASS]
        state = "not selected" if group not in groups else f"{len(passed)}/{len(rows)} PASS"
        print(f"  {label:<16} {state}"
              + (f", {len(ran) - len(passed)} problem(s)" if len(ran) - len(passed) else ""))
    print(f"  {'Documented':<16} {len(documented)} manual item(s), counted as satisfied but not executed")
    print(f"  {'Deferred':<16} {len(deferred)} non-blocking item(s) explicitly deferred by Step 4")
    if unmapped:
        print(f"  {'Unmapped':<16} {len(unmapped)} test failure(s) outside the requirement mapping - "
              f"real, but no requirement's evidence")
    print(f"\n  Step 4 blocking requirements satisfied: {len(satisfied)}/{len(blocking)} "
          f"({100 * len(satisfied) // len(blocking)}%) - DOCUMENTED counted, NOT RUN never counted")

    print("\nOverall:")
    if failures or unmapped:
        offline_broken = (any(row.group == OFFLINE for row, _ in failures)
                          or any(group == OFFLINE for _, _, group in unmapped))
        print("  OFFLINE FAIL" if offline_broken else "  OFFLINE PASS")
        if failures:
            print("  FULL PHASE 1: FAIL")
        else:  # every requirement is satisfied; something else in the run is broken
            print("  CHECKLIST FAILED DUE TO RELATED TEST FAILURE"
                  + (" - every Step 4 requirement in this run is satisfied" if not not_run else ""))
        for row, outcome in failures:
            kind = "regression" if not row.blocking else "requirement"
            print(f"    {outcome.status}: [{kind}] {row.name}")
        for node, reason, _ in unmapped:
            print(f"    FAIL: [unmapped] {node}")
        if not_run:
            for row, outcome in not_run:
                print(f"    NOT RUN: {row.name} ({outcome.detail})")
        return 1
    if not_run:
        print("  OFFLINE PASS")
        print(f"  FULL PHASE 1: NOT FULLY VERIFIED - {len(not_run)} blocking item(s) NOT RUN")
        for row, outcome in not_run:
            print(f"    NOT RUN: {row.name} ({outcome.detail})")
        return 2
    print("  OFFLINE PASS")
    print("  FULL PHASE 1: PASS"
          + (f" ({len(documented)} item(s) on documented manual evidence)" if documented else ""))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Phase 1 pass/fail checklist (docs/step4 Section 4).",
        epilog="The default runs OFFLINE evidence only: no window is opened, no input is sent, no "
               "clipboard is touched, and nothing waits for you. --real-desktop ADDS the unattended "
               "real-desktop acceptance tests. --real-elevated ADDS the manual elevated "
               "permission-denied check (you start and focus an elevated Notepad; UAC is never "
               "automated). Each flag only ADDS its own group, so full Phase 1 verification needs "
               "BOTH --real-desktop and --real-elevated. The optional clipboard test and any browser "
               "test are never run. Exit codes: 0 = every blocking requirement satisfied, 1 = "
               "something executed FAILED (including a regression row or a broken mapping), 2 = "
               "nothing failed but blocking evidence was NOT RUN.")
    parser.add_argument("--real-desktop", action="store_true",
                        help="also run the unattended real-desktop acceptance tests")
    parser.add_argument("--real-elevated", action="store_true",
                        help="also run the manual elevated permission-denied acceptance test")
    parser.add_argument("--pause", type=int, default=5,
                        help="seconds of warning before real tests start (default 5)")
    args = parser.parse_args(argv)

    groups = [OFFLINE]
    if args.real_desktop:
        groups.append(DESKTOP)
    if args.real_elevated:
        groups.append(ELEVATED)

    results, warned = {}, False
    with tempfile.TemporaryDirectory(prefix="phase1-checklist-") as workspace:
        for group in groups:
            if group != OFFLINE and not warned:  # warn once, before the first real group
                warn_before_real_tests(groups, args.pause)
                warned = True
            print(f"\nRunning the {group} evidence...", flush=True)
            results[group] = run_group(group, str(Path(workspace) / f"{group}.xml"))
            if results[group].broken:
                print(f"  PROBLEM: {results[group].broken}")

    outcomes = [(row, evaluate(row, results)) for row in ROWS]
    return report(outcomes, groups, unmapped_failures(results))


if __name__ == "__main__":
    sys.exit(main())
