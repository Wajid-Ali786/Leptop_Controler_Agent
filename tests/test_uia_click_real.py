"""
Owner-run REAL desktop smoke for the Phase 5 UIA click path (Slice 2), plus the offline tests that
prove it can never run by accident.

WHAT IT PROVES, end to end, on the real machine:

    the existing Executor opens Calculator
        -> UI Automation resolves ONE harmless control by its accessible name
        -> the existing Safety confirmation appears and YOU type "yes"
        -> the control is re-identified AFTER your confirmation
        -> the existing Executor adapter sends exactly ONE click
        -> YOU look at Calculator and see 7

The display is never read. That is the point: this smoke validates UIA targeting and physical
delivery while leaving screen content unread, so it proves where the click landed and nothing about
what it achieved.

THE TARGET IS "Seven", NOT "7". Calculator's digit button exposes the accessible name "Seven", and
matching is exact. No alias is introduced here - Phase 4's application aliases are a different domain
(which app to launch) and must not be reused for control names. That a person would say "7" is a real
limitation of the current design, recorded rather than papered over.

HOW IT IS GATED - three independent locks, all of which must be open:
  1. the `real_desktop` marker (the existing one; no new marker category exists)
  2. RUN_REAL_DESKTOP_TEST=1 (that marker's existing gate)
  3. pytest's `-s`, with a real stdin, because YOU have to type "yes"
Without the third it skips, which is why `scripts/phase1_checklist.py` - which runs pytest with no
`-s` - is unaffected by this file.

NOTHING PHYSICAL HAPPENS IN A FIXTURE. Every desktop action is in the test body, so `--collect-only`
and `--setup-only` are provably safe ways to inspect this test, and the offline tests below rely on
that.

WHAT IT WILL NOT DO: it never closes a Calculator window you already had open, and it never closes
the one it opened - you need that window to look at. Cleanup is yours, and the test prints the exact
window it opened so there is no ambiguity about which one.
"""
import os
import sys

import pytest

import safety_guards
from app import console
from app.executor import adapter as executor_adapter
from app.executor import emergency_stop
from app.executor import logic as executor
from app.executor.models import OPEN_APP, ExecutorAction, Outcome
from app.safety.logic import ActionDeniedError
from app.verifier import adapter as verifier_adapter
from app.verifier import logic as verifier
from app.verifier import observation
from app.verifier.models import Ambiguous, Found, NotFound, Target
from tests.test_real_desktop import _describe, _new_windows

APP = "calculator"
TARGET_NAME = "Seven"          # Calculator's accessible name for the 7 button. Exact, no alias.
EXPECTED_DISPLAY = "7"

NEEDS_DASH_S = (
    "This smoke is interactive - you have to type \"yes\" at the real Safety confirmation, and pytest\n"
    "    hides every prompt and makes input() raise unless it is run with -s:\n\n"
    "    $env:RUN_REAL_DESKTOP_TEST='1'\n"
    "    .\\venv\\Scripts\\python.exe -m pytest tests/test_uia_click_real.py -m real_desktop -s -v")

ALREADY_OPEN = (
    "Calculator is already open, so this smoke would be ambiguous: Windows Calculator is\n"
    "    single-instance, so opening it again would just focus THAT window - the test could not prove\n"
    "    it owned what it clicked, and the display could already have something in it.\n\n"
    "    A window marked `cloaked` below is the usual case and is not a window you can see: Windows\n"
    "    keeps Calculator suspended in the background long after it is closed, with both its frame\n"
    "    and its hosted content window still titled \"Calculator\". It still has to go, because\n"
    "    reopening it would reuse its old display contents.\n\n"
    "    Clear it yourself, then run this again - this test NEVER closes a window it did not open:\n"
    "        Get-Process CalculatorApp -ErrorAction SilentlyContinue | Stop-Process\n\n"
    "    Already open: ")


def _blocking_windows(windows, expectation):
    """The matching windows that would make this smoke ambiguous, from a list already read.

    A pure function of what was read, so the rule can be tested offline without a desktop. Cloaked
    windows COUNT: a suspended Calculator still holds its old display contents, and reopening it would
    reuse them, so "the display now reads 7" would prove nothing. See ALREADY_OPEN."""
    return [window for window in windows if expectation.pattern.search(window.title)]


def announce(text=""):
    """Flushed, so the script you are reading stays in step with the prompts you are answering."""
    print(text, flush=True)


@pytest.fixture
def owner_at_the_keyboard(request):
    """Skip unless a person can actually answer the confirmation. Installs nothing and touches
    nothing - see the module docstring on why no fixture here may act on the desktop."""
    if request.config.getoption("capture") != "no" or type(sys.stdin).__name__ == "DontReadFromInput":
        pytest.skip(NEEDS_DASH_S)
    return True


@pytest.mark.real_desktop
def test_a_uia_resolved_click_lands_on_calculators_seven(owner_at_the_keyboard, monkeypatch):
    """The whole Phase 5 Slice 2 path, with you at the keyboard. Read the script as it prints."""
    emergency_stop.reset("real-uia-smoke")
    expectation = verifier.expect_window(APP)

    # 1. No pre-existing Calculator, or nothing afterwards is unambiguous.
    already = _blocking_windows(verifier_adapter.list_windows(), expectation)
    if already:
        pytest.skip(ALREADY_OPEN + "; ".join(_describe(w) for w in already))

    announce()
    announce("=" * 78)
    announce("PHASE 5 UIA CLICK SMOKE - you drive this. The test types nothing for you.")
    announce("")
    announce("  1. it opens Calculator through the normal Executor")
    announce(f"  2. it asks UI Automation for the control named {TARGET_NAME!r}")
    announce("  3. the REAL Safety confirmation appears - type  yes  to allow ONE real click")
    announce("  4. it re-identifies the control AFTER your answer, then clicks once")
    announce(f"  5. YOU check that Calculator shows  {EXPECTED_DISPLAY}  (the test never reads it)")
    announce("")
    announce("  Ctrl+Alt+Backspace is the emergency stop throughout.")
    announce("=" * 78)

    before = verifier.snapshot_windows(expectation)

    # 2 + 3. The existing launch path, which is also what establishes ownership.
    opened = executor.execute_with_recovery(ExecutorAction(OPEN_APP, APP))
    announce(f"\nopen: {opened.message}")
    assert opened.ok, opened.message

    new = _new_windows(expectation, before)
    for window in new:
        announce(f"open: now present {_describe(window)}")
    assert new, "Calculator reported open but no new matching window appeared"

    # The bridge names the window in its confirmation through verifier.window_by_handle(), which lists
    # TOP-LEVEL windows only. A Store app's content window is a CHILD of its frame, so the frame is the
    # only handle that can satisfy both the UIA read and that lookup.
    frame = next((w for w in new if w.class_name == "ApplicationFrameWindow"), new[0])
    announce(f"open: targeting {_describe(frame)}")

    target = Target(TARGET_NAME, frame.handle)

    # 4. Resolve. Only the RESOLUTION is printed - never a label read off the screen.
    found = observation.resolve_target(target)
    announce(f"resolve: {type(found).__name__} for the control you named")
    if not isinstance(found, Found):
        _explain_resolution_failure(found, new, frame)
    assert isinstance(found, Found), f"resolve_target returned {type(found).__name__}"
    seen = found.observed
    announce(f"resolve: control_type={seen.control_type!r} enabled={seen.enabled} "
             f"offscreen={seen.offscreen} bounds={seen.bounds} has_automation_id={bool(seen.automation_id)}")
    assert seen.enabled and not seen.offscreen, "the control is not in a clickable state"

    # Count the real clicks by WRAPPING the real adapter function - it still performs the real click.
    # This is evidence, not a bypass: remove the wrapper and the behaviour is identical.
    clicks = []
    real_click = executor_adapter.click

    def counting_click(x, y):
        clicks.append((x, y))
        return real_click(x, y)

    monkeypatch.setattr(executor_adapter, "click", counting_click)

    # 5 + 6. The REAL production confirmation: console.typed_confirmation() reads the keyboard and
    # allows only the exact word "yes". Nothing here answers it for you.
    announce("")
    announce("-" * 78)
    announce(f"The next prompt is the real Safety confirmation. Type  yes  to allow ONE click on")
    announce(f"{TARGET_NAME!r}. Anything else cancels and nothing is clicked.")
    announce("-" * 78)

    # 7 + 8. Re-identification happens inside this call, after your answer; then exactly one click.
    try:
        result = executor.click_target(target, seen, console.typed_confirmation())
    except ActionDeniedError as exc:
        announce(f"\nSTOPPED SAFELY: {exc}")
        announce(f"clicks sent: {len(clicks)} (expected 0)")
        assert clicks == [], "something was clicked even though the confirmation was not given"
        pytest.fail("STOPPED SAFELY - you did not type 'yes', so nothing was clicked. This is the "
                    "safe outcome, not a code defect. Run it again and type yes to produce evidence.")

    # 9. Report the action result.
    announce(f"\nclick: {result.message}")
    announce(f"click: ok={result.ok} outcome={result.outcome.value} verified={result.verified}")
    announce(f"click: clicks sent through the Executor adapter = {len(clicks)} at {clicks}")
    # What re-identification changed, if anything. The point clicked comes from the bounds read AFTER
    # the confirmation, so a shift here is the feature working, not a fault.
    before_centre = (seen.bounds[0] + (seen.bounds[2] - seen.bounds[0]) // 2,
                     seen.bounds[1] + (seen.bounds[3] - seen.bounds[1]) // 2)
    if clicks:
        announce(f"click: centre before you confirmed {before_centre}; clicked {clicks[0]}"
                 f"{' (re-identification moved it)' if clicks[0] != before_centre else ''}")
    else:
        # Re-identification refused AFTER you confirmed - a safe outcome, and the message above says
        # which rule refused. Guarded so this reports it rather than failing with an IndexError.
        announce(f"click: NOTHING was clicked. The control was found at {before_centre} before you "
                 f"confirmed, and was not clickable by the time you had.")

    assert result.ok, result.message
    assert len(clicks) == 1, f"expected exactly one click, got {len(clicks)}"
    assert result.outcome is Outcome.UNVERIFIED and not result.verified, (
        "a click must never report itself verified: landing on the right control is not proof of what "
        "the control then did")

    # 10. Your eyes are the verifier here, by design.
    announce("")
    announce("=" * 78)
    announce(f"LOOK AT CALCULATOR NOW. Its display should read:  {EXPECTED_DISPLAY}")
    announce("")
    announce(f"  If it does  -> PASS. The click was delivered to the UIA-resolved {TARGET_NAME!r}.")
    announce("  If it does not -> FAIL, and say so: the click was delivered somewhere else, which")
    announce("                    is exactly the kind of defect this smoke exists to catch.")
    announce("")
    announce("  The test did NOT read the display, and will NOT close this window. To close it:")
    announce(f"  click its X, or run:  Get-Process Calculator*,CalculatorApp | Stop-Process")
    announce(f"  The window this test opened: {_describe(frame)}")
    announce("=" * 78)


def _explain_resolution_failure(found, new, frame):
    """Say WHY, with enough detail to act on, before the assertion fails.

    The most likely cause is structural rather than a defect: if UI Automation only exposes
    Calculator's buttons under its hosted content window, the Slice 2 bridge cannot click them,
    because the window it names in the confirmation is looked up among TOP-LEVEL windows only. Reading
    the child windows here is a read, and no click can follow it - the preparer would refuse."""
    announce("")
    announce(f"resolve: FAILED - {getattr(found, 'reason', getattr(found, 'message', found))}")
    if isinstance(found, Ambiguous):
        announce(f"resolve: {len(found.candidates)} controls answered to that name; nothing was chosen.")
        return
    announce("resolve: checking whether the control is visible under a HOSTED window instead -")
    announce("         a read only; the bridge would refuse to click a non-top-level window.")
    for window in new:
        if window.handle == frame.handle:
            continue
        probe = observation.resolve_target(Target(TARGET_NAME, window.handle))
        announce(f"         {_describe(window)} -> {type(probe).__name__}")
    hosted = verifier.hosted_windows(
        verifier.expect_window(APP), frozenset(w.handle for w in new))
    for handle in hosted:
        probe = observation.resolve_target(Target(TARGET_NAME, handle))
        announce(f"         hosted child {handle:#x} -> {type(probe).__name__}")
    if isinstance(found, NotFound):
        announce("")
        announce(f"         If every window says NotFound, the accessible name is not {TARGET_NAME!r}")
        announce("         on this Windows build. Open Accessibility Insights or Inspect.exe, read the")
        announce("         Name of the 7 button, and use that exact text.")


# =====================================================================================================
# OFFLINE: the smoke cannot run by accident, and nothing above weakens Safety
# =====================================================================================================
# These run in the ordinary suite. They are deliberately in this file, beside what they describe.

def test_the_smoke_needs_the_existing_marker_and_no_new_gate_category():
    """Lock 1 and 2: the existing real_desktop marker and its existing gate - nothing new."""
    markers = {mark.name for mark in test_a_uia_resolved_click_lands_on_calculators_seven.pytestmark}
    assert markers == {"real_desktop"}, markers
    assert safety_guards.DESKTOP_EXEMPT["real_desktop"] == "RUN_REAL_DESKTOP_TEST"
    from tests.conftest import OPT_IN_GATES
    assert OPT_IN_GATES["real_desktop"][0] == "RUN_REAL_DESKTOP_TEST"


def test_the_gate_alone_is_not_enough_without_the_marker():
    """Lock 2 without lock 1 permits nothing: exemption needs BOTH, which is the existing rule."""
    marked = type("Node", (), {"get_closest_marker": lambda self, name: None})()
    assert safety_guards.exempt(marked, safety_guards.DESKTOP_EXEMPT) is False
    assert os.environ.get("RUN_REAL_DESKTOP_TEST") != "1", (
        "RUN_REAL_DESKTOP_TEST is set in this shell - the ordinary suite must not be run with it")


def test_the_marker_alone_is_not_enough_without_the_gate(monkeypatch):
    """Lock 1 without lock 2 permits nothing either."""
    monkeypatch.delenv("RUN_REAL_DESKTOP_TEST", raising=False)
    present = type("Node", (), {"get_closest_marker": lambda self, name: object()})()
    assert safety_guards.exempt(present, safety_guards.DESKTOP_EXEMPT) is False
    monkeypatch.setenv("RUN_REAL_DESKTOP_TEST", "1")
    assert safety_guards.exempt(present, safety_guards.DESKTOP_EXEMPT) is True


@pytest.mark.parametrize("capture, stdin_kind, expect_skip", [
    ("fd", "DontReadFromInput", True),      # ordinary pytest run: both reasons to skip
    ("no", "DontReadFromInput", True),       # -s given, but pytest still owns stdin
    ("fd", "TextIOWrapper", True),           # a real stdin, but every prompt would be hidden
    ("no", "TextIOWrapper", False),          # -s AND a real stdin: the only runnable combination
])
def test_lock_three_requires_dash_s_and_a_real_stdin(monkeypatch, capture, stdin_kind, expect_skip):
    """Lock 3, tested as the predicate rather than by running the smoke: this is what keeps
    scripts/phase1_checklist.py - which runs pytest with no -s - from ever reaching the desktop."""
    class Config:
        def getoption(self, name):
            assert name == "capture"
            return capture

    class Request:
        config = Config()

    monkeypatch.setattr(sys, "stdin", type(stdin_kind, (), {})())
    if expect_skip:
        with pytest.raises(pytest.skip.Exception):
            owner_at_the_keyboard.__wrapped__(Request())
    else:
        assert owner_at_the_keyboard.__wrapped__(Request()) is True


def test_the_phase_one_checklist_runs_pytest_without_dash_s():
    """The reason lock 3 is enough: the established real-desktop workflow cannot trip this smoke."""
    script = (__import__("config.settings", fromlist=["settings"]).PROJECT_ROOT
              / "scripts" / "phase1_checklist.py").read_text(encoding="utf-8")
    command = next(line for line in script.splitlines() if '"-m", "pytest"' in line)
    assert '"-s"' not in command, command
    assert "no:cacheprovider" in command, "this is the pytest invocation that was checked"


def _smoke_nodes():
    """The AST of the smoke and its helper, docstrings stripped - and nothing else in this file.

    Scoped this narrowly on purpose. The first version of the three tests below searched the whole
    module's TEXT for forbidden strings, and each one's own list of forbidden strings is in that text,
    so they matched themselves. A node in another function is not a node in this one."""
    import ast
    import inspect
    import textwrap
    source = textwrap.dedent(inspect.getsource(sys.modules[__name__]))
    module = ast.parse(source)
    wanted = ("test_a_uia_resolved_click_lands_on_calculators_seven", "_explain_resolution_failure")
    functions = [node for node in module.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    assert len(functions) == len(wanted), [f.name for f in functions]
    for node in functions:
        for inner in ast.walk(node):
            if isinstance(inner, (ast.FunctionDef, ast.ClassDef)) and inner.body \
                    and isinstance(inner.body[0], ast.Expr) \
                    and isinstance(inner.body[0].value, ast.Constant) \
                    and isinstance(inner.body[0].value.value, str):
                inner.body.pop(0)
                if not inner.body:
                    inner.body.append(ast.Pass())
    return functions


def _calls_in_smoke(name):
    """Every call to `name` (bare or attribute) made by the smoke."""
    import ast
    found = []
    for function in _smoke_nodes():
        for node in ast.walk(function):
            if isinstance(node, ast.Call):
                called = node.func.attr if isinstance(node.func, ast.Attribute) \
                    else getattr(node.func, "id", "")
                if called == name:
                    found.append(node)
    return found


def test_the_smoke_adds_no_safety_bypass():
    """It uses the REAL production confirmation, and that is the only thing it passes as one.

    Structural, not textual: the confirm argument of click_target() must BE a call to
    console.typed_confirmation(). A lambda, a literal True, or anything else fails here."""
    import ast
    calls = _calls_in_smoke("click_target")
    assert len(calls) == 1, f"the smoke must click exactly one way, found {len(calls)}"
    arguments = calls[0].args
    assert len(arguments) == 3, "click_target(target, observed, confirm)"
    confirm = arguments[2]
    assert isinstance(confirm, ast.Call), f"the confirmation must be built, not fabricated: {ast.dump(confirm)}"
    assert isinstance(confirm.func, ast.Attribute) and confirm.func.attr == "typed_confirmation" \
        and isinstance(confirm.func.value, ast.Name) and confirm.func.value.id == "console", \
        ast.dump(confirm)
    assert confirm.args == [] and confirm.keywords == [], "no read/write override: the keyboard decides"

    # No risk floor is passed, so the preparer's own MEDIUM stands untouched.
    assert [k.arg for k in calls[0].keywords] == [], calls[0].keywords
    # Nothing in the smoke calls the gate directly or constructs a Safety object.
    for forbidden in ("authorize", "Action", "RiskAssessment", "SafetyDecision", "assess"):
        assert _calls_in_smoke(forbidden) == [], f"the smoke must not call {forbidden}()"
    # ... and it never patches the Executor's own logic, only wraps the adapter to count.
    patched = [ast.dump(node.args[0]) for node in _calls_in_smoke("setattr") if node.args]
    assert all("executor_adapter" in dump for dump in patched), patched


def test_the_smoke_issues_no_action_other_than_opening_calculator():
    """Nothing is closed - not the owner's Calculator, and not the one the smoke opened, which is the
    evidence they are asked to look at. No second close mechanism is introduced because no close is
    issued at all. Checked by reading every ExecutorAction the smoke builds."""
    import ast
    kinds = []
    for node in _calls_in_smoke("ExecutorAction"):
        assert node.args, "an ExecutorAction with no kind"
        kinds.append(node.args[0].id if isinstance(node.args[0], ast.Name) else ast.dump(node.args[0]))
    assert kinds == ["OPEN_APP"], kinds
    for closer in ("CLOSE_APP", "WINDOW_CONTROL", "request_close", "PostMessageW",
                   "_close_windows_opened_since"):
        assert _names_in_smoke(closer) == 0, f"the smoke must not close anything: {closer}"


def _names_in_smoke(name):
    """How many times the smoke's code refers to `name` as an identifier or attribute."""
    import ast
    count = 0
    for function in _smoke_nodes():
        for node in ast.walk(function):
            if isinstance(node, ast.Name) and node.id == name:
                count += 1
            elif isinstance(node, ast.Attribute) and node.attr == name:
                count += 1
    return count


def test_the_smoke_never_reads_calculators_display():
    """The whole point: UIA targeting and physical delivery are proved while content stays unread.

    A click that lands on the right control proves delivery. Reading the display to check the digit
    would prove the outcome too - and would make this a content-reading test, which Phase 5 keeps out
    of reach until a layer exists that is allowed to do it with the owner's consent."""
    for content_read in ("read_text", "field_text_length", "count_text", "selection",
                         "clipboard_kinds", "rich_text", "wait_for_typed_text", "everything_selected"):
        assert _names_in_smoke(content_read) == 0, f"the smoke must not read screen content: {content_read}"
    # The accessible name is compared inside the verifier's adapter and never crosses that boundary,
    # so there is nothing named to print even by accident.
    assert _names_in_smoke("name") == 0, "no .name is read anywhere in the smoke"


def test_a_pre_existing_calculator_blocks_the_smoke():
    """Step 1 of the flow, tested as behaviour: Windows Calculator is single-instance, so a window
    that was already there makes ownership and the visual check both meaningless.

    This rule had no test until a mutation run removed it and nothing failed."""
    from app.verifier.models import WindowInfo
    expectation = verifier.expect_window(APP)
    mine = WindowInfo(1, "Calculator")
    cloaked = WindowInfo(2, "Calculator", class_name="ApplicationFrameWindow", cloaked=True)
    unrelated = WindowInfo(3, "Untitled - Notepad")

    assert _blocking_windows([], expectation) == []
    assert _blocking_windows([unrelated], expectation) == []
    assert _blocking_windows([mine], expectation) == [mine]
    # The common real case on this machine: Windows keeps a suspended Calculator with its frame AND
    # its hosted content window both titled "Calculator". Both must block.
    assert _blocking_windows([cloaked], expectation) == [cloaked], "a cloaked Calculator must block"
    assert _blocking_windows([unrelated, mine, cloaked], expectation) == [mine, cloaked]


def test_the_smoke_skips_rather_than_closing_a_calculator_it_found():
    """The structural half: the smoke must actually ACT on that predicate, and the action must be a
    skip - never a close. The mutant that replaced the condition with a constant survived until this
    test existed."""
    import ast
    guards = []
    for function in _smoke_nodes():
        for node in ast.walk(function):
            if not isinstance(node, ast.If):
                continue
            skips = [inner for inner in ast.walk(node)
                     if isinstance(inner, ast.Call)
                     and isinstance(inner.func, ast.Attribute) and inner.func.attr == "skip"]
            if skips:
                guards.append(node)
    assert guards, "the smoke must skip when a Calculator is already open"
    tested_on = {ast.dump(guard.test) for guard in guards}
    assert any("Name" in dump and "already" in dump for dump in tested_on), tested_on
    for guard in guards:
        assert not isinstance(guard.test, ast.Constant), "the skip must depend on what was read"
    assert _calls_in_smoke("_blocking_windows"), "the predicate must be the thing consulted"


def test_no_desktop_action_happens_in_a_fixture():
    """Why --collect-only and --setup-only are safe ways to inspect this test under the real gate:
    the only fixture this module defines decides whether to skip, and does nothing else."""
    import ast
    import inspect
    import textwrap
    source = textwrap.dedent(inspect.getsource(owner_at_the_keyboard.__wrapped__))
    calls = {node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
             for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call)}
    assert calls <= {"getoption", "skip", "type"}, calls
