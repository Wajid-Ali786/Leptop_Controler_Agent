"""
Tests for scripts/phase1_checklist.py - the Phase 1 checklist's own honesty.

The checklist's whole job is to report status truthfully, so these tests pin the rules that matter:
NOT RUN is never promoted to PASS, a renamed or uncollected mapped test fails visibly instead of
quietly reducing the evidence, a non-blocking regression failure can never produce exit 0, and the
safe default cannot select a real-desktop, elevated or clipboard test.

Everything here is offline: no pytest subprocess is started (run_group is replaced), and no window,
pointer, clipboard or key is touched.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import phase1_checklist as checklist  # noqa: E402

PASS, FAIL, NOT_RUN = checklist.PASS, checklist.FAIL, checklist.NOT_RUN
DEFERRED, DOCUMENTED, MAPPING_ERROR = checklist.DEFERRED, checklist.DOCUMENTED, checklist.MAPPING_ERROR
OFFLINE, DESKTOP, ELEVATED = checklist.OFFLINE, checklist.DESKTOP, checklist.ELEVATED


def row(group=OFFLINE, anchors=("tests/test_thing.py::test_one",), blocking=True, section=checklist.BUILD):
    return checklist.Row(section, "a requirement", group, anchors, blocking=blocking)


def group_result(cases=None, broken=""):
    return checklist.GroupResult(cases=cases or {}, broken=broken)


def passing(anchor="tests/test_thing.py::test_one"):
    return group_result({anchor: [(PASS, "")]})


# --- status derivation -----------------------------------------------------------------------------

def test_a_mapped_test_that_ran_and_passed_is_pass():
    assert checklist.evaluate(row(), {OFFLINE: passing()}).status == PASS


def test_a_mapped_test_that_failed_is_fail_with_the_message():
    result = group_result({"tests/test_thing.py::test_one": [(FAIL, "assert 1 == 2")]})
    outcome = checklist.evaluate(row(), {OFFLINE: result})
    assert outcome.status == FAIL and "assert 1 == 2" in outcome.detail


def test_a_failure_elsewhere_in_the_evidence_file_does_not_touch_the_row():
    """The mislabelling this fixes: an unrelated test in the same file is NOT this requirement's
    failure. It is reported separately and still stops the command succeeding."""
    result = group_result({"tests/test_thing.py::test_one": [(PASS, "")],
                           "tests/test_thing.py::test_something_else": [(FAIL, "boom")]})
    assert checklist.evaluate(row(), {OFFLINE: result}).status == PASS
    assert checklist.unmapped_failures({OFFLINE: result}) == [
        ("tests/test_thing.py::test_something_else", "boom", OFFLINE)]


def test_a_group_that_was_not_run_is_not_run():
    outcome = checklist.evaluate(row(group=DESKTOP), {OFFLINE: passing()})
    assert outcome.status == NOT_RUN


def test_not_run_is_never_upgraded_to_pass_by_another_groups_success():
    """A real-desktop row stays NOT RUN however green the offline run was."""
    outcomes = [(r, checklist.evaluate(r, {OFFLINE: passing()})) for r in
                (row(group=DESKTOP), row(group=ELEVATED))]
    assert [outcome.status for _, outcome in outcomes] == [NOT_RUN, NOT_RUN]
    assert PASS not in [outcome.status for _, outcome in outcomes]


def test_a_mapped_test_that_was_selected_but_skipped_is_never_pass():
    """The mode asked for this evidence and pytest skipped it: that is a configuration problem."""
    result = group_result({"tests/test_thing.py::test_one": [(NOT_RUN, "")]})
    outcome = checklist.evaluate(row(), {OFFLINE: result})
    assert outcome.status == FAIL and outcome.status != PASS and "SKIPPED" in outcome.detail


def test_some_parametrised_cases_skipping_is_still_a_pass():
    result = group_result({"tests/test_thing.py::test_one": [(PASS, ""), (NOT_RUN, "")]})
    assert checklist.evaluate(row(), {OFFLINE: result}).status == PASS


def test_deferred_rows_report_their_step4_reason():
    deferred = checklist.Row(checklist.DEFERRED_SECTION, "deferred thing", checklist.DEFER, (),
                             blocking=False, note="step4 says so")
    outcome = checklist.evaluate(deferred, {})
    assert outcome.status == DEFERRED and outcome.detail == "step4 says so"


def test_documented_rows_are_documented_not_pass():
    manual = checklist.Row(checklist.EVIDENCE, "manual thing", checklist.MANUAL, (), note="seen on 18 Sep")
    outcome = checklist.evaluate(manual, {})
    assert outcome.status == DOCUMENTED and outcome.status != PASS and "18 Sep" in outcome.detail


# --- mapping drift ---------------------------------------------------------------------------------

def test_a_renamed_mapped_test_is_a_visible_mapping_error_not_a_pass():
    result = group_result({"tests/test_thing.py::test_one": [(PASS, "")]})
    stale = row(anchors=("tests/test_thing.py::test_one", "tests/test_thing.py::test_renamed_away"))
    outcome = checklist.evaluate(stale, {OFFLINE: result})
    assert outcome.status == MAPPING_ERROR and "test_renamed_away" in outcome.detail


def test_a_row_whose_tests_were_never_collected_is_a_mapping_error():
    outcome = checklist.evaluate(row(), {OFFLINE: group_result({"tests/other.py::test_x": [(PASS, "")]})})
    assert outcome.status == MAPPING_ERROR


def test_pytest_failing_to_produce_results_is_a_failure():
    outcome = checklist.evaluate(row(), {OFFLINE: group_result(broken="pytest wrote no JUnit XML")})
    assert outcome.status == FAIL and "no JUnit XML" in outcome.detail


def test_every_mapped_anchor_names_a_test_file_that_exists():
    """Cheap guard against a typo in the mapping: the files must be real."""
    root = Path(__file__).resolve().parent.parent
    for mapped in checklist.ROWS:
        for anchor in mapped.anchors:
            file_name, _, test_name = anchor.partition("::")
            assert (root / file_name).exists(), f"{mapped.name}: {file_name} does not exist"
            assert test_name.startswith("test_"), f"{mapped.name}: {anchor} is not a test id"


# --- environmental failures ------------------------------------------------------------------------

def test_a_pointer_interference_failure_is_flagged_as_possibly_environmental():
    result = group_result({"tests/test_thing.py::test_one":
                           [(FAIL, "The mouse pointer isn't over the active window")]})
    outcome = checklist.evaluate(row(group=DESKTOP), {DESKTOP: result})
    assert outcome.status == FAIL and outcome.interference is True


def test_an_ordinary_failure_is_not_flagged_as_environmental():
    result = group_result({"tests/test_thing.py::test_one": [(FAIL, "expected done, got failed")]})
    outcome = checklist.evaluate(row(group=DESKTOP), {DESKTOP: result})
    assert outcome.interference is False and outcome.ambiguous is False


def test_interference_is_recognised_in_the_output_the_test_printed():
    """The reason usually isn't in the assertion - it's in what the test printed (e.g. held keys)."""
    message = "\n".join(("assert False", checklist.CAPTURED_MARKER,
                         "Didn't scroll: Shift and Ctrl are held down on the keyboard"))
    result = group_result({"tests/test_thing.py::test_one": [(FAIL, message)]})
    outcome = checklist.evaluate(row(group=DESKTOP), {DESKTOP: result})
    assert outcome.interference is True
    assert checklist.CAPTURED_MARKER not in outcome.detail  # the captured text is scanned, not printed


def test_a_trial_that_never_exercised_the_stop_is_reported_as_ambiguous_but_still_fails():
    result = group_result({"tests/test_thing.py::test_one":
                           [(FAIL, "the approved thresholds were not met: no stop was measured in trial(s) [19]")]})
    outcome = checklist.evaluate(row(group=DESKTOP), {DESKTOP: result})
    assert outcome.status == FAIL and outcome.ambiguous is True and outcome.interference is False


def test_only_the_first_line_of_a_failure_is_shown():
    assert checklist.headline("first line\nsecond line") == "first line"
    assert checklist.headline(f"boom\n{checklist.CAPTURED_MARKER}\nlots of output") == "boom"


# --- JUnit reading ---------------------------------------------------------------------------------

def test_junit_xml_is_read_into_per_test_results(tmp_path):
    xml = tmp_path / "results.xml"
    xml.write_text(
        '<testsuites><testsuite name="pytest">'
        '<testcase classname="tests.test_a" name="test_ok" file="tests/test_a.py"/>'
        '<testcase classname="tests.test_a" name="test_bad" file="tests/test_a.py">'
        '<failure message="assert False">trace</failure></testcase>'
        '<testcase classname="tests.test_a" name="test_err" file="tests/test_a.py">'
        '<error message="collection error">trace</error></testcase>'
        '<testcase classname="tests.test_b" name="test_skipped" file="tests/test_b.py">'
        '<skipped message="needs opt-in"/></testcase>'
        '<testcase classname="tests.test_b" name="test_param[1]" file="tests/test_b.py"/>'
        '</testsuite></testsuites>', encoding="utf-8")
    result = checklist.read_junit(str(xml))
    assert result.cases["tests/test_a.py::test_ok"] == [(PASS, "")]
    assert result.cases["tests/test_a.py::test_bad"][0][0] == FAIL
    assert result.cases["tests/test_a.py::test_err"][0][0] == FAIL
    assert result.cases["tests/test_b.py::test_skipped"] == [(NOT_RUN, "")]
    assert "tests/test_b.py::test_param" in result.cases  # parametrised cases collapse to the base id


# --- exit codes ------------------------------------------------------------------------------------

def outcomes_for(*pairs):
    return [(mapped, checklist.Outcome(status)) for mapped, status in pairs]


def test_exit_zero_only_when_every_blocking_item_is_satisfied(capsys):
    code = checklist.report(outcomes_for((row(), PASS), (row(group=checklist.MANUAL), DOCUMENTED),
                                         (row(blocking=False), DEFERRED)), [OFFLINE, DESKTOP, ELEVATED])
    printed = capsys.readouterr().out
    assert code == 0 and "FULL PHASE 1: PASS" in printed
    assert "documented manual evidence" in printed  # the human-attested part stays visible


def test_exit_two_when_blocking_evidence_was_not_run(capsys):
    code = checklist.report(outcomes_for((row(), PASS), (row(group=DESKTOP), NOT_RUN)), [OFFLINE])
    printed = capsys.readouterr().out
    assert code == 2
    assert "OFFLINE PASS" in printed and "NOT FULLY VERIFIED" in printed
    assert "FULL PHASE 1: PASS" not in printed


def test_exit_one_when_an_executed_check_failed(capsys):
    code = checklist.report(outcomes_for((row(), FAIL), (row(group=DESKTOP), NOT_RUN)), [OFFLINE])
    assert code == 1 and "FULL PHASE 1: FAIL" in capsys.readouterr().out


def test_a_non_blocking_regression_failure_can_never_exit_zero(capsys):
    """Everything Step 4 requires passed, but a regression row failed: that must still be non-zero."""
    code = checklist.report(outcomes_for((row(), PASS), (row(group=checklist.MANUAL), DOCUMENTED),
                                         (row(blocking=False, section=checklist.REGRESSION), FAIL)),
                            [OFFLINE, DESKTOP, ELEVATED])
    printed = capsys.readouterr().out
    assert code == 1 and "[regression]" in printed


def test_a_mapping_error_exits_one(capsys):
    code = checklist.report(outcomes_for((row(), MAPPING_ERROR)), [OFFLINE])
    assert code == 1 and MAPPING_ERROR in capsys.readouterr().out


def test_the_percentage_counts_only_blocking_rows_and_never_counts_not_run(capsys):
    checklist.report(outcomes_for((row(), PASS), (row(group=DESKTOP), NOT_RUN),
                                  (row(blocking=False), DEFERRED)), [OFFLINE])
    assert "satisfied: 1/2 (50%)" in capsys.readouterr().out


# --- what each mode may run ------------------------------------------------------------------------

def test_the_offline_group_excludes_every_real_marker():
    expression = checklist.GROUP_MARKERS[OFFLINE]
    for marker in ("real_desktop", "real_elevated", "real_clipboard", "real_api"):
        assert f"not {marker}" in expression


def test_the_real_desktop_group_excludes_elevated_clipboard_and_api_tests():
    expression = checklist.GROUP_MARKERS[DESKTOP]
    assert expression.startswith("real_desktop")
    for marker in ("real_elevated", "real_clipboard", "real_api"):
        assert f"not {marker}" in expression


def test_no_group_ever_enables_the_clipboard_opt_in():
    for group, environment in checklist.GROUP_ENVIRONMENT.items():
        assert "RUN_REAL_CLIPBOARD_TEST" not in environment, group


def test_the_offline_group_clears_inherited_opt_ins(monkeypatch):
    """An exported RUN_REAL_DESKTOP_TEST must not turn the safe default into a desktop run."""
    for name in checklist.OPT_IN_VARIABLES:
        monkeypatch.setenv(name, "1")
    environment = checklist.environment_for(OFFLINE)
    assert not any(name in environment for name in checklist.OPT_IN_VARIABLES)
    assert checklist.environment_for(DESKTOP)["RUN_REAL_DESKTOP_TEST"] == "1"
    assert "RUN_ELEVATED_TEST" not in checklist.environment_for(DESKTOP)
    elevated = checklist.environment_for(ELEVATED)
    assert elevated["RUN_ELEVATED_TEST"] == "1" and "RUN_REAL_CLIPBOARD_TEST" not in elevated


def selected_groups(argv, monkeypatch):
    """Which groups main() would run, without starting pytest."""
    asked = []

    def fake_run_group(group, xml_path, timeout=0):
        asked.append(group)
        return passing()

    monkeypatch.setattr(checklist, "run_group", fake_run_group)
    monkeypatch.setattr(checklist, "warn_before_real_tests", lambda groups, pause: None)
    checklist.main(argv)
    return asked


def test_the_default_run_is_offline_only(monkeypatch):
    assert selected_groups([], monkeypatch) == [OFFLINE]


def test_real_desktop_adds_only_the_desktop_group(monkeypatch):
    assert selected_groups(["--real-desktop", "--pause", "0"], monkeypatch) == [OFFLINE, DESKTOP]


def test_real_elevated_adds_only_the_elevated_group_as_help_says(monkeypatch):
    """--real-elevated ADDS its group; it does not imply --real-desktop. --help must say so."""
    assert selected_groups(["--real-elevated", "--pause", "0"], monkeypatch) == [OFFLINE, ELEVATED]
    assert selected_groups(["--real-desktop", "--real-elevated", "--pause", "0"], monkeypatch) == \
        [OFFLINE, DESKTOP, ELEVATED]


def test_help_documents_the_additive_flags_and_exit_codes(capsys):
    with pytest.raises(SystemExit):
        checklist.main(["--help"])
    printed = capsys.readouterr().out
    assert "ADDS" in printed and "BOTH --real-desktop and --real-elevated" in printed
    assert "Exit codes" in printed and "clipboard" in printed


# --- the mapping represents Step 4, not the implementation -----------------------------------------

def test_every_blocking_row_belongs_to_a_step4_section():
    for mapped in checklist.ROWS:
        if mapped.blocking:
            assert mapped.section in (checklist.BUILD, checklist.CASES, checklist.DONE, checklist.EVIDENCE)


def test_regression_and_deferred_rows_are_never_blocking():
    for mapped in checklist.ROWS:
        if mapped.section in (checklist.REGRESSION, checklist.DEFERRED_SECTION):
            assert not mapped.blocking, mapped.name


# --- related / unmapped failures --------------------------------------------------------------------

def test_an_unmapped_failure_is_listed_with_its_node_id_reason_and_group():
    message = "AssertionError: something broke" + chr(10) + "more detail"
    result = group_result({"tests/test_other.py::test_unrelated": [(FAIL, message)]})
    assert checklist.unmapped_failures({DESKTOP: result}) == [
        ("tests/test_other.py::test_unrelated", "AssertionError: something broke", DESKTOP)]


def test_an_unmapped_failure_without_a_reason_still_says_something():
    result = group_result({"tests/test_other.py::test_unrelated": [(FAIL, "")]})
    assert checklist.unmapped_failures({OFFLINE: result})[0][1] == "no reason reported"


def test_unmapped_skips_and_passes_are_not_failures():
    result = group_result({"tests/test_other.py::test_skipped": [(NOT_RUN, "")],
                           "tests/test_other.py::test_fine": [(PASS, "")]})
    assert checklist.unmapped_failures({OFFLINE: result}) == []


def test_a_row_anchor_is_never_reported_as_unmapped():
    anchor = checklist.ROWS[0].anchors[0]
    result = group_result({anchor: [(FAIL, "boom")]})
    assert checklist.unmapped_failures({OFFLINE: result}) == []


def test_an_unmapped_failure_cannot_exit_zero_and_leaves_the_row_passing(capsys):
    code = checklist.report(outcomes_for((row(), PASS), (row(group=checklist.MANUAL), DOCUMENTED)),
                            [OFFLINE, DESKTOP, ELEVATED],
                            [("tests/test_thing.py::test_something_else", "boom", OFFLINE)])
    printed = capsys.readouterr().out
    assert code == 1
    assert "[PASS] a requirement" in printed                     # the requirement stays truthful
    assert checklist.UNMAPPED_SECTION in printed
    assert "tests/test_thing.py::test_something_else" in printed and "boom" in printed
    assert "CHECKLIST FAILED DUE TO RELATED TEST FAILURE" in printed
    assert "FULL PHASE 1: FAIL" not in printed                   # no requirement failed
    assert "every Step 4 requirement in this run is satisfied" in printed


def test_the_percentage_is_unchanged_by_unmapped_failures(capsys):
    checklist.report(outcomes_for((row(), PASS), (row(group=DESKTOP), NOT_RUN)), [OFFLINE],
                     [("tests/test_thing.py::test_other", "boom", OFFLINE)])
    printed = capsys.readouterr().out
    assert "satisfied: 1/2 (50%)" in printed and "OFFLINE FAIL" in printed
