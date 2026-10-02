"""
The non-desktop boundaries an ordinary test must not reach: the provider, the microphone and the speech
model - plus the exemption rule that is the only way past any of them.

These exist because the Slice 3B forensics found three boundaries guarded only by per-test mocking. The
rule proved here is uniform: a test may exercise as much application logic as it likes, and may never
reach the real machine or the real network. Bypassing a guard needs a registered marker AND that
marker's RUN_REAL_* gate - never a file name, a class name or a function-name prefix.
"""
from pathlib import Path

import pytest

import safety_guards
from app.brain import adapter as brain_adapter
from app.executor import adapter as executor_adapter
from app.listener import adapter as listener_adapter
from safety_guards import (PhysicalAudioEscaped, PhysicalDesktopEscaped, PhysicalListenerEscaped,
                           ProviderEscaped, RealDatabaseEscaped)

FAKE_KEY = "sk-ant-api03-" + "A" * 80 + "-ZZtestZZ"      # shaped like a real key, and not one


# --- §4 The explicit adapter fixture: requesting it permits nothing -----------------------------------

def test_requesting_the_fixture_alone_permits_no_physical_io(os_primitives_faked):
    """The contract. The fixture hands back the boundary names and no permission whatsoever: every one
    of them still refuses, because no OS primitive has been faked yet."""
    assert os_primitives_faked == safety_guards.DESKTOP_BOUNDARIES
    with pytest.raises(PhysicalDesktopEscaped):
        executor_adapter.launch_app("notepad.exe")
    with pytest.raises(PhysicalDesktopEscaped):
        executor_adapter.send_character("a")
    with pytest.raises(PhysicalDesktopEscaped):
        executor_adapter.register_hotkey(1, 0, 0)


def test_launch_runs_against_a_fake_popen(os_primitives_faked, monkeypatch):
    """A: with subprocess.Popen faked, the real launch_app logic runs - path resolution, the no-shell
    argument, the pid - and Windows never starts anything."""
    import subprocess
    seen = {}

    class FakeProcess:
        pid = 4321

    def fake_popen(args, **kwargs):
        seen.update(args=args, kwargs=kwargs)
        return FakeProcess()

    monkeypatch.setattr(executor_adapter.shutil, "which", lambda name: r"C:\fake\notepad.exe")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    assert executor_adapter.launch_app("notepad.exe") == 4321
    assert seen["args"] == [r"C:\fake\notepad.exe"] and not seen["kwargs"].get("shell")


def test_the_window_path_runs_against_a_fake_win32_primitive(os_primitives_faked, monkeypatch):
    """B: request_close posts a real window message the instant it runs, so it proceeds only once
    ctypes.WinDLL is the test's own fake."""
    posted = []

    class FakeUser32:
        class PostMessageW:
            argtypes = restype = None

            def __new__(cls, handle, message, wparam, lparam):
                posted.append((handle, message))
                return 1

    monkeypatch.setattr(executor_adapter.ctypes, "WinDLL",
                        lambda name, use_last_error=False: FakeUser32, raising=False)
    executor_adapter.request_close(99)
    assert posted and posted[0][0] == 99


def test_the_keyboard_path_runs_against_a_fake_keyboard_primitive(os_primitives_faked, monkeypatch):
    """C: _keyboard_api is the seam every key press goes through."""
    sent = []

    class FakeApi:
        Input = type("Input", (), {})

        def SendInput(self, count, inputs, size):
            sent.append(count)
            return count

    monkeypatch.setattr(executor_adapter.sys, "platform", "win32")
    monkeypatch.setattr(executor_adapter, "_keyboard_api", lambda: FakeApi())
    with pytest.raises(Exception):      # it will fail on the fake structures, never on real input
        executor_adapter.send_character("a")
    # What matters: the refusal did NOT come from the guard.
    try:
        executor_adapter.send_character("a")
    except PhysicalDesktopEscaped:
        pytest.fail("the guard fired even though the keyboard primitive was faked")
    except Exception:
        pass


def test_the_hotkey_path_runs_against_a_fake_hotkey_primitive(os_primitives_faked, monkeypatch):
    """D: _hotkey is the seam the global emergency-stop registration goes through."""
    registered = []

    class FakeHotkey:
        class user32:
            @staticmethod
            def RegisterHotKey(window, identifier, modifiers, key):
                registered.append(identifier)
                return 1

    monkeypatch.setattr(executor_adapter, "_hotkey", lambda: FakeHotkey())
    executor_adapter.register_hotkey(7, 0, 0)
    assert registered == [7]


@pytest.mark.parametrize("boundary, call", [
    ("launch_app", lambda: executor_adapter.launch_app("notepad.exe")),
    ("request_close", lambda: executor_adapter.request_close(1)),
    ("request_window_state", lambda: executor_adapter.request_window_state(1, "minimize")),
    ("send_character", lambda: executor_adapter.send_character("a")),
    ("send_shortcut", lambda: executor_adapter.send_shortcut(("ctrl",), "c")),
    ("send_wheel_notch", lambda: executor_adapter.send_wheel_notch(True)),
    ("register_hotkey", lambda: executor_adapter.register_hotkey(1, 0, 0)),
])
def test_forgetting_the_primitive_fake_still_fails_closed(boundary, call, os_primitives_faked,
                                                          monkeypatch):
    """The whole point of the fixture being a contract rather than a permission."""
    monkeypatch.setattr(executor_adapter.sys, "platform", "win32")
    with pytest.raises(PhysicalDesktopEscaped) as escaped:
        call()
    assert "faked" in str(escaped.value) or "primitive" in str(escaped.value)


def test_there_is_no_blanket_allow_desktop_fixture():
    """A fixture that simply switched the guard off would undo all of this."""
    import inspect

    import conftest as root_conftest
    source = inspect.getsource(root_conftest)
    assert "allow_faked_primitives=True" in source, "the strict variant is what the fixture installs"
    installer = inspect.getsource(safety_guards.install_desktop_guard)
    assert "_primitive_refuser" in installer, "primitives are replaced even for adapter tests"


# --- §5 The provider, with a real-looking API key present ---------------------------------------------

def test_a_normal_test_cannot_reach_anthropic_even_with_an_api_key(monkeypatch):
    """MANDATORY PROOF. A syntactically valid-looking key is set, a GENUINE Anthropic client is built
    with it - the real default transport, not the project's fake - and a request is sent.

    The guard stops it at the TRANSPORT. Not at authentication, not at DNS, not at a missing credit:
    httpx2.HTTPTransport.handle_request is the function that would open the socket, and it never runs.
    Nothing leaves this machine, and the key appears nowhere."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    genuine = brain_adapter.anthropic.Anthropic(api_key=FAKE_KEY, timeout=5, max_retries=0)
    with pytest.raises(ProviderEscaped) as escaped:
        genuine.messages.create(model="claude-sonnet-5-5", max_tokens=16,
                                messages=[{"role": "user", "content": "hello"}])
    message = str(escaped.value)
    assert "httpx2.HTTPTransport" in message, "it was stopped at the transport"
    assert FAKE_KEY not in message and "sk-ant" not in message, "and the key is never shown"


def test_the_provider_guard_does_not_depend_on_the_key_being_absent():
    """The old protection was luck: no key in .env. This asserts the guard reads no credential at all."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(safety_guards.install_provider_guard))
    reads = {ast.unparse(node) for node in ast.walk(tree)}
    assert not any("ANTHROPIC" in text or "api_key" in text for text in reads), reads


def test_the_whole_brain_path_is_blocked_with_a_key_present(monkeypatch, fake_claude):
    """Not just a raw client: send_message() with the cost controls and everything above them."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    fake_claude.configure(max_input_tokens=100000)
    monkeypatch.setattr(brain_adapter, "get_client", lambda http_client=None: brain_adapter.anthropic
                        .Anthropic(api_key=FAKE_KEY, timeout=5, max_retries=0))
    with pytest.raises((ProviderEscaped, brain_adapter.ClaudeError)) as raised:
        brain_adapter.send_message("hello", max_tokens=16)
    assert FAKE_KEY not in str(raised.value)


# --- §6 The fake provider still works -----------------------------------------------------------------

def test_the_projects_fake_claude_transport_is_still_allowed(fake_claude):
    """The guard must tell a MockTransport from the real one, or every Brain test would need a real
    marker - which would be worse than no guard."""
    fake_claude.configure(max_input_tokens=100000)
    reply = brain_adapter.ping()
    assert reply.text, "the fake answered"
    assert fake_claude.requests, "and the request went to the fake transport"


def test_many_brain_tests_run_without_any_real_marker():
    """Evidence rather than assertion: the Brain suites pass in an ordinary run, which they could not do
    if the provider guard were blocking fakes too."""
    import subprocess
    import sys

    from config import settings
    finished = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_brain.py", "tests/test_brain_provider.py",
         "-q", "-p", "no:cacheprovider"],
        cwd=settings.PROJECT_ROOT, capture_output=True, text=True, timeout=600)
    assert finished.returncode == 0, finished.stdout[-3000:]
    assert " passed" in finished.stdout


# --- §7 The microphone ---------------------------------------------------------------------------------

def test_the_real_microphone_library_cannot_be_imported():
    with pytest.raises(PhysicalListenerEscaped) as escaped:
        import sounddevice                                            # noqa: F401
    assert "microphone" in str(escaped.value)
    assert "real_microphone" in str(escaped.value), "and it names the way out"


def test_the_production_capture_seam_cannot_open_the_microphone():
    """_audio() is the one place app/listener/adapter.py imports sounddevice, and capture() goes through
    it. Driving the real seam proves the guard sits below the production code, not beside it."""
    with pytest.raises(PhysicalListenerEscaped):
        listener_adapter._audio()


def test_fake_listener_tests_still_work(monkeypatch):
    """A test that installs its own fake never consults the import finder."""
    import sys
    fake = type(sys)("sounddevice")
    fake.RawInputStream = lambda **kwargs: None
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    assert listener_adapter._audio() is fake


# --- §8 The speech model -------------------------------------------------------------------------------

def test_the_real_speech_model_library_cannot_be_imported():
    with pytest.raises(PhysicalListenerEscaped) as escaped:
        import faster_whisper                                         # noqa: F401
    assert "speech-model loading" in str(escaped.value)


def test_the_production_model_seam_cannot_load_a_model():
    with pytest.raises(PhysicalListenerEscaped):
        listener_adapter._whisper()


def test_nothing_in_the_guard_can_download_a_model():
    """fetch_model is the one function allowed to download, and the guard never reaches it: the import
    is refused first, so no network and no disk write can follow."""
    import inspect
    assert "download" not in inspect.getsource(safety_guards.install_library_guard)


def test_a_fake_model_still_works(monkeypatch):
    import sys
    fake = type(sys)("faster_whisper")
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    assert listener_adapter._whisper() is fake


# --- §9 The speaker, after the move -------------------------------------------------------------------

@pytest.mark.parametrize("library", ["edge_tts", "pyttsx3"])
def test_the_speech_libraries_are_still_refused(library):
    with pytest.raises(PhysicalAudioEscaped) as escaped:
        __import__(library)
    assert "real_speaker" in str(escaped.value)


def test_mci_playback_is_still_refused():
    from app.speaker import adapter as speaker_adapter
    with pytest.raises(PhysicalAudioEscaped):
        speaker_adapter._mci("play something.mp3")


# --- §11 The exemption matrix -------------------------------------------------------------------------

CATEGORIES = {
    "desktop": safety_guards.DESKTOP_EXEMPT,
    "audio": safety_guards.AUDIO_EXEMPT,
    "microphone": safety_guards.MICROPHONE_EXEMPT,
    "model": safety_guards.MODEL_EXEMPT,
    "provider": safety_guards.PROVIDER_EXEMPT,
    "database": safety_guards.DATABASE_EXEMPT,
}


class Node:
    """A stand-in pytest node, so the exemption logic is tested with safe probes - never by actually
    exercising hardware or the provider to see what happens."""

    def __init__(self, *markers):
        self.markers = set(markers)

    def get_closest_marker(self, name):
        return object() if name in self.markers else None


@pytest.mark.parametrize("category", sorted(CATEGORIES))
def test_marker_alone_is_never_enough(category, monkeypatch):
    allowed = CATEGORIES[category]
    for marker, gate in allowed.items():
        monkeypatch.delenv(gate, raising=False)
        assert not safety_guards.exempt(Node(marker), allowed), f"{category}: {marker} alone"


@pytest.mark.parametrize("category", sorted(CATEGORIES))
def test_the_gate_alone_is_never_enough(category, monkeypatch):
    allowed = CATEGORIES[category]
    for marker, gate in allowed.items():
        monkeypatch.setenv(gate, "1")
        assert not safety_guards.exempt(Node(), allowed), f"{category}: {gate} alone"


@pytest.mark.parametrize("category", sorted(CATEGORIES))
def test_both_together_are_the_only_way_through(category, monkeypatch):
    allowed = CATEGORIES[category]
    for marker, gate in allowed.items():
        monkeypatch.setenv(gate, "1")
        assert safety_guards.exempt(Node(marker), allowed), f"{category}: {marker} + {gate}"


def test_every_real_category_this_project_has_is_in_the_matrix():
    """Desktop, elevated desktop, clipboard, microphone, voice, speaker, real model and the API - the
    eight §11 names, each mapped to a gate."""
    everything = {marker for allowed in CATEGORIES.values() for marker in allowed}
    for required in ("real_desktop", "real_elevated", "real_clipboard", "real_microphone",
                     "real_voice_console", "real_speaker", "real_model", "real_api", "real_database"):
        assert required in everything, required


@pytest.mark.parametrize("category", sorted(CATEGORIES))
def test_a_wrong_marker_does_not_open_another_category(category, monkeypatch):
    """A microphone test may record without being allowed to speak, launch a window or call Claude."""
    allowed = CATEGORIES[category]
    for other, other_allowed in CATEGORIES.items():
        if other == category:
            continue
        for marker, gate in allowed.items():
            monkeypatch.setenv(gate, "1")
            if marker in other_allowed:
                continue                      # real_voice_console deliberately spans several
            assert not safety_guards.exempt(Node(marker), other_allowed), f"{marker} opened {other}"


# --- The two databases that belong to the user ---------------------------------------------------------
# data/memory.db (structured memory) and data/claude_usage.db (the Claude usage ledger). Before the
# central guard existed the ledger was protected only by whichever fixture a test happened to request -
# the same per-test-mocking shape that opened real Notepad windows in Slice 3B.
#
# NOTE ON WHAT IS ASSERTED. Not "the real file must not exist": in normal use it SHOULD exist, and a test
# whose invariant is its absence would start failing the day the owner uses the feature. What is asserted
# is that this test neither created, modified nor read it - by snapshotting the file's state and by
# proving the configured path resolves inside this test's own temporary area.

def real_database(name):
    from config.settings import PROJECT_ROOT
    return PROJECT_ROOT / "data" / name


def snapshot(path):
    """Whether the file is there, and if so its size and modification time. Works either way."""
    if not path.exists():
        return ("absent",)
    stat = path.stat()
    return ("present", stat.st_size, stat.st_mtime_ns)


def test_the_memory_database_path_resolves_inside_this_test(tmp_path):
    """Item 19. The configured path is redirected, so the real file is not even named."""
    from app.memory import logic as memory_logic
    resolved = memory_logic.database_path()
    assert resolved.is_relative_to(tmp_path), resolved
    assert resolved != real_database("memory.db")


def test_the_usage_ledger_path_resolves_inside_this_test(tmp_path):
    """Item 20. The gap this guard closed: previously this depended on the test's own fixtures."""
    from app.brain import cost_controls
    resolved = cost_controls._ledger_path()
    assert resolved.is_relative_to(tmp_path), resolved
    assert resolved != real_database("claude_usage.db")


def test_real_memory_use_never_touches_the_users_memory_database():
    """Item 21. A full initialise/write/wipe cycle through production code, with the real file's state
    compared before and after - whether or not it exists."""
    import sqlite3

    from app.memory import logic as memory_logic
    from app.memory.models import MemoryDatabase
    real = real_database("memory.db")
    before = snapshot(real)

    created = memory_logic.initialize()
    assert isinstance(created, MemoryDatabase), getattr(created, "message", created)
    with sqlite3.connect(created.path) as conn:
        conn.execute("INSERT INTO people (name, created_at) VALUES ('Ali', 1.0)")
    memory_logic.wipe()

    assert snapshot(real) == before, "the user's own memory database changed"
    assert Path(created.path) != real


def test_real_ledger_use_never_touches_the_users_usage_database(fake_claude):
    """Item 22. The ledger genuinely exists on this machine, so this proves the point for a file that
    is present, not merely for a missing one."""
    from app.brain import cost_controls
    real = real_database("claude_usage.db")
    before = snapshot(real)

    request_id = cost_controls.authorize(model="test-model", prompt="hello", max_tokens=16)
    cost_controls.release_reservation(request_id)

    assert snapshot(real) == before, "the user's own usage ledger changed"


def test_a_database_inside_the_repository_is_refused_outright(tmp_path):
    """The second layer. The path redirect could be refactored around; this cannot - building a
    repository path by hand and connecting to it fails loudly instead of quietly reaching data/."""
    import sqlite3

    from config.settings import PROJECT_ROOT
    with pytest.raises(RealDatabaseEscaped):
        sqlite3.connect(PROJECT_ROOT / "data" / "memory.db")
    with pytest.raises(RealDatabaseEscaped):
        sqlite3.connect(str(PROJECT_ROOT / "data" / "claude_usage.db"))


def test_sqlite_itself_still_works_normally(tmp_path):
    """The guard redirects and refuses; it does not disable SQLite and does not mock it. A temporary
    database and an in-memory one both behave exactly as the driver does."""
    import sqlite3
    path = tmp_path / "ordinary.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t (x) VALUES (42)")
        assert conn.execute("SELECT x FROM t").fetchone()[0] == 42
    with sqlite3.connect(":memory:") as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1


def test_no_desktop_audio_or_provider_side_effect_accompanies_memory_work():
    """Item 23. Memory is a file boundary and must not have quietly become another kind."""
    import sys

    from app.memory import logic as memory_logic
    memory_logic.initialize()
    memory_logic.wipe()
    for library in ("sounddevice", "faster_whisper", "pyautogui", "pywinauto", "playwright"):
        assert library not in sys.modules, library
    with pytest.raises(PhysicalDesktopEscaped):
        executor_adapter.launch_app("notepad.exe")
    with pytest.raises(ProviderEscaped):
        brain_adapter.httpx2.HTTPTransport().handle_request(object())


# --- §12 The fail-closed sentinel ---------------------------------------------------------------------

@pytest.mark.parametrize("sentinel", [PhysicalDesktopEscaped, PhysicalAudioEscaped,
                                      PhysicalListenerEscaped, ProviderEscaped])
def test_no_sentinel_can_be_swallowed_by_except_exception(sentinel):
    assert issubclass(sentinel, BaseException) and not issubclass(sentinel, Exception)
    assert issubclass(sentinel, safety_guards.PhysicalBoundaryEscaped)


def test_a_production_shaped_handler_cannot_absorb_a_guard_hit():
    """app/executor/logic.py and app/speaker/adapter.py both catch Exception broadly by design. This is
    the shape that let the desktop leak pass three test runs as green."""
    def production_like(call):
        try:
            call()
        except Exception:                      # noqa: BLE001 - deliberately the broad handler
            return "swallowed"
        return "ran"

    for call in (lambda: executor_adapter.launch_app("notepad.exe"),
                 lambda: listener_adapter._audio(),
                 lambda: listener_adapter._whisper()):
        with pytest.raises(safety_guards.PhysicalBoundaryEscaped):
            production_like(call)
