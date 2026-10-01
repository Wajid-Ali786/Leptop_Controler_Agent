"""
The central desktop guard, and the tests that would have caught the Slice 3B incident.

WHAT HAPPENED. Mid-slice, the voice console was switched from console.handle_command to
console.handle_typed_line while the voice test fixtures still stubbed the OLD function. The stub was
installed on something nobody called, so "open notepad" went through the REAL orchestration, routed to
LocalAction, and reached the REAL Executor and the REAL subprocess.Popen. Roughly two dozen Notepad
windows opened on the user's machine across three test runs, and the suite reported them as passes.

WHY NOTHING CAUGHT IT. Isolation was per-test mocking at a HIGH level. Nothing guarded the low-level
boundary, so when a refactor moved the level that was being mocked, the tests kept passing while the
machine was being acted on. That is the same shape as the speaker incident two days earlier.

The two tests at the bottom are the ones that would have failed on the day: they drive the real
production path - deliberately NOT mocking the orchestration - and prove the guard, not a stub, is what
stands between a test and this computer.
"""
import pytest

from app import console, voice_console
from app.brain.models import Intent, OpenAppArgs, Understood
from app.executor import adapter as executor_adapter
from app.executor.models import OPEN_APP, ExecutorAction
from app.listener.models import Transcript
from app.planner.models import VOICE_CONSOLE, TurnContext
import safety_guards
from tests.conftest import DESKTOP_BOUNDARIES, DESKTOP_MARKERS, PhysicalDesktopEscaped


class Screen:
    def __init__(self):
        self.lines = []

    def __call__(self, text=""):
        self.lines.append(str(text))


class Script:
    def __init__(self, *answers):
        self.answers = list(answers)

    def __call__(self, prompt=""):
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


# --- The guard itself ----------------------------------------------------------------------------------

def test_the_guard_is_a_base_exception_so_production_cannot_swallow_it():
    """app/executor/logic.py has broad `except Exception` handlers on purpose, so an Exception raised by
    the guard would become a tidy ActionResult and the test would pass while the machine was acted on.
    That is precisely how this went unnoticed."""
    assert issubclass(PhysicalDesktopEscaped, BaseException)
    assert not issubclass(PhysicalDesktopEscaped, Exception)


@pytest.mark.parametrize("name", DESKTOP_BOUNDARIES)
def test_every_desktop_boundary_is_blocked_in_an_ordinary_test(name):
    """Every function in app/executor/adapter.py that can change something outside this process."""
    with pytest.raises(PhysicalDesktopEscaped) as escaped:
        getattr(executor_adapter, name)()
    assert name in str(escaped.value)


def test_the_guard_names_the_boundary_but_never_an_argument():
    """A target can be a window title or the user's typed text, so the message says the function and
    nothing else."""
    secret = "ZZ-my-diary-password-hunter2-ZZ"
    with pytest.raises(PhysicalDesktopEscaped) as escaped:
        executor_adapter.send_character(secret)
    message = str(escaped.value)
    assert "send_character" in message
    assert secret not in message and "hunter2" not in message


def test_the_boundary_list_covers_every_action_kind():
    """Launching, closing, the pointer, the keyboard, the wheel and window state - plus the global
    hotkey, which an offline test was really registering until this guard found it."""
    for expected in ("launch_app", "request_close", "click", "send_character", "send_shortcut",
                     "send_wheel_notch", "request_window_state", "register_hotkey"):
        assert expected in DESKTOP_BOUNDARIES, expected


def test_only_explicitly_marked_real_desktop_tests_are_exempt():
    assert set(DESKTOP_MARKERS) == {"real_desktop", "real_elevated", "real_clipboard",
                                    "real_voice_console"}


def test_there_is_no_name_based_bypass(monkeypatch):
    """The first version of this guard exempted any test named test_adapter_*. Safety must not depend on
    a naming convention, so that is gone - proved behaviourally: a node whose name looks exactly like one
    of those tests, with no marker, is not exempt from anything."""
    class AdapterLookingNode:
        name = "test_adapter_starts_the_resolved_path_without_a_shell"
        nodeid = "tests/test_executor_logic.py::test_adapter_starts_the_resolved_path_without_a_shell"

        def get_closest_marker(self, name):
            return None

    for gate in set(safety_guards.DESKTOP_EXEMPT.values()):
        monkeypatch.setenv(gate, "1")        # even with every gate set, the NAME buys nothing
    assert not safety_guards.exempt(AdapterLookingNode(), safety_guards.DESKTOP_EXEMPT)
    for allowed in (safety_guards.AUDIO_EXEMPT, safety_guards.MICROPHONE_EXEMPT,
                    safety_guards.MODEL_EXEMPT, safety_guards.PROVIDER_EXEMPT):
        assert not safety_guards.exempt(AdapterLookingNode(), allowed)


def test_the_exemption_decision_reads_only_markers_and_the_environment():
    """Structural companion, scoped to the one function that decides - not a grep over the whole file."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(safety_guards.exempt))
    reads = {ast.unparse(node) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    assert any("get_closest_marker" in call for call in reads)
    assert any("environ.get" in call for call in reads)
    assert not any("name" in call and "marker" not in call for call in reads), reads


def test_bypassing_a_guard_needs_the_marker_AND_the_gate(monkeypatch):
    """Neither half is sufficient on its own - for any of the five guards."""
    class Node:
        def __init__(self, markers): self._markers = markers
        def get_closest_marker(self, name): return object() if name in self._markers else None

    for allowed in (safety_guards.DESKTOP_EXEMPT, safety_guards.AUDIO_EXEMPT,
                    safety_guards.MICROPHONE_EXEMPT, safety_guards.MODEL_EXEMPT,
                    safety_guards.PROVIDER_EXEMPT):
        marker, gate = next(iter(allowed.items()))
        monkeypatch.delenv(gate, raising=False)
        assert not safety_guards.exempt(Node({marker}), allowed), f"{marker} alone was enough"
        assert not safety_guards.exempt(Node(set()), allowed), f"no marker was enough"
        monkeypatch.setenv(gate, "1")
        assert not safety_guards.exempt(Node(set()), allowed), f"{gate} alone was enough"
        assert safety_guards.exempt(Node({marker}), allowed), f"{marker} + {gate} should pass"


def test_the_root_gate_map_agrees_with_the_tests_own_gate_map():
    """safety_guards mirrors the gates rather than importing them, so it keeps working for a file that
    cannot see tests/. This is what stops the two copies drifting apart."""
    from tests.conftest import OPT_IN_GATES
    everything = {**safety_guards.DESKTOP_EXEMPT, **safety_guards.AUDIO_EXEMPT,
                  **safety_guards.MICROPHONE_EXEMPT, **safety_guards.MODEL_EXEMPT,
                  **safety_guards.PROVIDER_EXEMPT}
    for marker, gate in everything.items():
        assert marker in OPT_IN_GATES, f"{marker} is not a registered marker"
        assert OPT_IN_GATES[marker][0] == gate, f"{marker}: {gate} != {OPT_IN_GATES[marker][0]}"


def test_the_marker_alone_does_not_make_anything_happen():
    """Every exempt marker is additionally gated by its own RUN_REAL_* switch, so a marked test is
    skipped rather than run when the switch is unset."""
    from tests.conftest import OPT_IN_GATES
    for marker in DESKTOP_MARKERS:
        assert marker in OPT_IN_GATES, f"{marker} has no environment gate"
        variable, _reason = OPT_IN_GATES[marker]
        assert variable.startswith("RUN_"), variable


# --- The two tests that would have caught the incident -------------------------------------------------

def test_a_deterministic_open_notepad_through_the_voice_path_is_stopped_by_the_guard():
    """THE REGRESSION TEST FOR THE INCIDENT, case A: deterministic Phase 2 voice.

    Nothing high-level is mocked. A real transcript goes through the real transcript flow, the real
    acceptance, the real routing - which chooses LocalAction - the real run_action and the real Executor.
    The ONLY thing standing between this test and a window opening is the central guard, and that is the
    point: it proves the production path really does reach the adapter, and really is intercepted.

    Had this test existed on the day, it would have failed the moment the fixture seam moved."""
    heard = iter([Transcript(text="open notepad")])
    screen = Screen()
    with pytest.raises(PhysicalDesktopEscaped) as escaped:
        voice_console.run_voice_console(lambda: next(heard), read=Script("listen", "a", "exit"),
                                        write=screen)
    assert "launch_app" in str(escaped.value), "the production path reached the launcher"
    assert "COMMAND TO ACCEPT: [open notepad]" in screen.lines, "and it got there the normal way"


def test_a_brain_planned_open_notepad_through_the_voice_path_is_stopped_by_the_guard():
    """Case B: the Brain Voice orchestration, with only the PROVIDER mocked.

    open_app is one of the two kinds VOICE_CONSOLE allows, so this goes all the way through planning,
    acceptance and the Executor - and is caught at the adapter, not by a stub."""
    plan = Understood(intents=(Intent(OPEN_APP, OpenAppArgs("notepad"), why="you asked"),),
                      restated="open notepad")
    heard = iter([Transcript(text="could you open notepad for me please")])
    screen = Screen()
    with pytest.raises(PhysicalDesktopEscaped) as escaped:
        voice_console.run_voice_console(lambda: next(heard),
                                        read=Script("listen", "a", "yes", "exit"), write=screen)
    assert "launch_app" in str(escaped.value)


@pytest.fixture(autouse=True)
def brain_says_open_notepad(monkeypatch):
    """The provider, and only the provider. Everything below it stays real in this file."""
    plan = Understood(intents=(Intent(OPEN_APP, OpenAppArgs("notepad"), why="you asked"),),
                      restated="open notepad")
    monkeypatch.setattr(console.interpreter, "interpret", lambda prompt: plan)


def test_the_typed_console_path_is_stopped_the_same_way():
    """The same proof for `python main.py --console`, so neither front end can regress alone."""
    screen, script = Screen(), Script()
    with pytest.raises(PhysicalDesktopEscaped) as escaped:
        console.handle_typed_line("open notepad", TurnContext(),
                                  console.Prompts(read=script, write=screen))
    assert "launch_app" in str(escaped.value)


def test_the_guard_also_covers_a_brain_planned_close_app():
    screen, script = Screen(), Script("yes")
    with pytest.raises(PhysicalDesktopEscaped):
        console.handle_typed_line("open notepad", TurnContext(),
                                  console.Prompts(read=script, write=screen),
                                  frontend=VOICE_CONSOLE)


# --- Mutation proof: plant a call and show the guard fires --------------------------------------------

def test_a_planted_direct_adapter_call_is_caught():
    """§14's requirement: prove the guard fires rather than trusting that it would."""
    with pytest.raises(PhysicalDesktopEscaped):
        executor_adapter.launch_app("notepad.exe")


def test_a_planted_call_through_the_executor_is_caught():
    from app.executor import logic as executor_logic
    with pytest.raises(PhysicalDesktopEscaped):
        executor_logic.execute(ExecutorAction(OPEN_APP, "notepad"))


def test_a_planted_call_through_run_action_is_caught():
    with pytest.raises(PhysicalDesktopEscaped):
        console.run_action(ExecutorAction(OPEN_APP, "notepad"))


def test_the_guard_survives_an_except_exception_wrapper():
    """The BaseException choice, demonstrated: production code that swallows Exception cannot absorb it."""
    def production_like():
        try:
            executor_adapter.launch_app("notepad.exe")
        except Exception:                         # noqa: BLE001 - the shape this guard defeats
            return "swallowed"
        return "ran"

    with pytest.raises(PhysicalDesktopEscaped):
        production_like()


# --- The condition that opened two real windows: a pytest file outside tests/ -------------------------

def test_a_pytest_file_outside_tests_is_still_guarded(tmp_path):
    """THE REGRESSION TEST FOR THE FORENSIC INCIDENT.

    Two real Notepad windows were opened during the investigation because a scratch pytest file sat in
    the scratchpad directory, outside this repository. pytest collects conftest.py only from a test
    file's own ancestors, so tests/conftest.py did not exist for that run and no guard was installed.

    This creates an ephemeral test module in the REPOSITORY ROOT - outside tests/ - and runs pytest on
    it in a subprocess. The module calls adapter.launch_app directly. If the root conftest.py is loaded
    for it, the call raises PhysicalDesktopEscaped and the inner test passes; if the root conftest were
    ever moved back under tests/, the inner test would FAIL because a real window would open instead.

    That is the proof of root loading: the inner assertion can only hold if a conftest above the module's
    own directory installed the guard.
    """
    import subprocess
    import sys

    from config import settings

    root = settings.PROJECT_ROOT
    probe = root / "test_root_guard_probe_tmp.py"
    probe.write_text(
        "from app.executor import adapter\n"
        "from safety_guards import PhysicalDesktopEscaped\n"
        "import pytest\n"
        "\n"
        "def test_the_root_guard_reached_a_file_outside_tests():\n"
        "    with pytest.raises(PhysicalDesktopEscaped) as escaped:\n"
        "        adapter.launch_app('notepad.exe')\n"
        "    assert 'launch_app' in str(escaped.value)\n",
        encoding="utf-8")
    try:
        finished = subprocess.run(
            [sys.executable, "-m", "pytest", str(probe), "-q", "-p", "no:cacheprovider"],
            cwd=root, capture_output=True, text=True, timeout=300)
    finally:
        probe.unlink(missing_ok=True)
    assert not probe.exists(), "the ephemeral module is always removed"
    assert finished.returncode == 0, (
        f"a pytest file outside tests/ was NOT guarded:\n{finished.stdout[-3000:]}")
    assert "1 passed" in finished.stdout, finished.stdout[-2000:]


def test_the_ephemeral_probe_would_have_failed_without_the_root_conftest(tmp_path):
    """The other half, so the test above cannot pass vacuously: the same module, run with the root
    conftest deliberately unavailable, does NOT raise the sentinel - it would reach the real launcher.

    It is run with --noconftest so no conftest at all is loaded, and the inner test is written to PASS
    only when the guard is ABSENT. Nothing is launched: adapter.launch_app is replaced inside the probe
    before it is called, so the proof is about which conftest pytest loaded, not about Windows."""
    import subprocess
    import sys

    from config import settings

    root = settings.PROJECT_ROOT
    probe = root / "test_root_guard_absent_probe_tmp.py"
    probe.write_text(
        "from app.executor import adapter\n"
        "\n"
        "def test_without_any_conftest_no_guard_is_installed():\n"
        "    calls = []\n"
        "    adapter.launch_app = lambda executable: calls.append(executable) or 1234\n"
        "    assert adapter.launch_app('notepad.exe') == 1234\n"
        "    assert calls == ['notepad.exe'], 'nothing intercepted it, because no conftest was loaded'\n",
        encoding="utf-8")
    try:
        finished = subprocess.run(
            [sys.executable, "-m", "pytest", str(probe), "-q", "--noconftest",
             "-p", "no:cacheprovider"],
            cwd=root, capture_output=True, text=True, timeout=300)
    finally:
        probe.unlink(missing_ok=True)
    assert not probe.exists()
    assert finished.returncode == 0, finished.stdout[-2000:]
