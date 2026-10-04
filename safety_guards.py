"""
Project-wide test safety guards: the boundaries an ordinary test must never cross.

WHY THIS FILE IS AT THE REPOSITORY ROOT. It is imported by the root conftest.py, which pytest loads for
EVERY test file under this repository - not only files under tests/. That matters because a test file
placed anywhere else bypasses tests/conftest.py entirely, and that is exactly how two real Notepad
windows were opened during the Slice 3B investigation: a scratch pytest file outside tests/ meant none
of the guards existed for that run.

WHAT IT GUARDS, and the rule for all of them: a test may exercise as much application logic as it
likes, and may never reach the real machine or the real network.

    desktop     app/executor/adapter.py - launching, closing, pointer, keyboard, wheel, window state,
                and the global hotkey
    speaker     the text-to-speech libraries and the winmm/MCI playback call
    microphone  sounddevice, which app/listener/adapter.py imports to open a stream
    model       faster_whisper, which is heavy and can load or fetch a model
    provider    httpx2's real HTTP transport, which is how the Anthropic client reaches the network

THE SENTINELS ARE BaseException SUBCLASSES, deliberately. app/executor/logic.py and
app/speaker/adapter.py both contain broad `except Exception` handlers by design, so a guard raising an
ordinary Exception would be swallowed into a tidy failure message and the test would PASS while the
boundary was being hit. That is precisely how the desktop leak went unnoticed for three test runs.

EXEMPTION RULE, uniform for every guard: a test bypasses a guard only when BOTH its registered marker
is present AND that marker's RUN_REAL_* environment gate is set. A marker alone is not enough, an
environment variable alone is not enough, and a file name, class name or function-name prefix is NEVER
enough - the desktop guard's first version used a `test_adapter_` prefix and that was rejected for this
reason.
"""
import os
import subprocess
import sys


# --- Sentinels ----------------------------------------------------------------------------------------

class PhysicalBoundaryEscaped(BaseException):
    """A test reached something real. See the module docstring for why this is not an Exception."""


class PhysicalDesktopEscaped(PhysicalBoundaryEscaped):
    """It would have launched, closed, clicked, typed, scrolled or re-stated a window on this computer,
    or registered the global hotkey."""


class PhysicalAudioEscaped(PhysicalBoundaryEscaped):
    """It would have made a real sound, or called a network text-to-speech service."""


class PhysicalListenerEscaped(PhysicalBoundaryEscaped):
    """It would have opened the real microphone, or loaded the real speech model."""


class ProviderEscaped(PhysicalBoundaryEscaped):
    """It would have sent a real HTTP request - the path the Anthropic client uses."""


class RealDatabaseEscaped(PhysicalBoundaryEscaped):
    """It would have opened a SQLite database inside the repository - the user's own memory or usage
    ledger - instead of the test's own temporary copy."""


# --- Which markers may bypass which guard, and the gate each one needs ---------------------------------
# Mirrored from tests/conftest.OPT_IN_GATES rather than imported, so this module keeps working for a test
# file that cannot see tests/. A test asserts the two agree, so they cannot drift apart silently.

DESKTOP_EXEMPT = {
    "real_desktop": "RUN_REAL_DESKTOP_TEST",
    "real_elevated": "RUN_ELEVATED_TEST",
    "real_clipboard": "RUN_REAL_CLIPBOARD_TEST",
    "real_voice_console": "RUN_REAL_VOICE_CONSOLE_TEST",
}

AUDIO_EXEMPT = {
    "real_speaker": "RUN_REAL_SPEAKER_TEST",
    "real_voice_console": "RUN_REAL_VOICE_CONSOLE_TEST",
}

MICROPHONE_EXEMPT = {
    "real_microphone": "RUN_REAL_MICROPHONE_TEST",
    "real_recording": "RUN_REAL_RECORDING_TEST",
    "real_transcription": "RUN_REAL_TRANSCRIPTION_TEST",
    "real_voice_console": "RUN_REAL_VOICE_CONSOLE_TEST",
    "real_stop_latency": "RUN_REAL_STOP_LATENCY_TEST",
    "real_kws_latency": "RUN_REAL_KWS_TEST",
    "real_kws_threshold": "RUN_REAL_KWS_THRESHOLD_TEST",
    "real_vosk_stop": "RUN_REAL_VOSK_STOP_TEST",
}

MODEL_EXEMPT = {
    "real_model": "RUN_REAL_MODEL_TEST",
    "real_transcription": "RUN_REAL_TRANSCRIPTION_TEST",
    "real_voice_console": "RUN_REAL_VOICE_CONSOLE_TEST",
    "real_stop_latency_synthetic": "RUN_REAL_STOP_LATENCY_SYNTHETIC_TEST",
    "real_stop_latency": "RUN_REAL_STOP_LATENCY_TEST",
    "real_kws_latency": "RUN_REAL_KWS_TEST",
    "real_kws_threshold": "RUN_REAL_KWS_THRESHOLD_TEST",
    "real_vosk_stop": "RUN_REAL_VOSK_STOP_TEST",
    "real_vosk_model": "RUN_REAL_VOSK_MODEL_TEST",
}

PROVIDER_EXEMPT = {
    "real_api": "RUN_REAL_CLAUDE_TEST",
}

DATABASE_EXEMPT = {
    "real_database": "RUN_REAL_DATABASE_TEST",
}


def exempt(node, allowed: dict) -> bool:
    """True only when a registered marker is present AND its own gate is set. Both, always."""
    for marker, gate in allowed.items():
        if node.get_closest_marker(marker) is not None and os.environ.get(gate) == "1":
            return True
    return False


# --- Desktop ------------------------------------------------------------------------------------------
# Every function in app/executor/adapter.py that can change something outside this process, mapped to the
# OS primitive that makes it real. A test may run the adapter FUNCTION only once it has replaced that
# primitive - which is what its own unit tests already do. Checked when the function is called, not when
# the fixture is set up, because a test installs its fake inside its body.

DESKTOP_BOUNDARIES = (
    "launch_app",            # subprocess.Popen - the one that opened real Notepad windows
    "request_close",
    "tag_window",            # SetPropW on another process's window
    "window_token",          # GetPropW on another process's window
    "untag_window",          # RemovePropW on another process's window
    "click",
    "send_character",
    "send_shortcut",
    "release_keys",
    "send_wheel_notch",
    "request_window_state",
    "register_hotkey",
    "unregister_hotkey",
    "create_message_queue",
    "wait_for_hotkey_message",
    "post_quit_to_thread",
)

# The OS primitives that make a desktop boundary real. An adapter unit test replaces one of these and
# then exercises the genuine adapter function; everything else never gets that far, because the boundary
# function itself is replaced.
_PRIMITIVE_NAMES = ("subprocess.Popen", "ctypes.WinDLL", "adapter._keyboard_api", "adapter._hotkey",
                    "adapter._use_physical_pixels")
_MOUSE_LIBRARY = "pyautogui"

_ORIGINALS = {}


def remember_originals(adapter) -> None:
    """Record the genuine boundary functions once, so they can be restored behind a primitive guard."""
    if _ORIGINALS:
        return
    for name in DESKTOP_BOUNDARIES:
        _ORIGINALS[name] = getattr(adapter, name, None)


def _desktop_refusal(name: str, what: str) -> PhysicalDesktopEscaped:
    return PhysicalDesktopEscaped(
        f"offline test attempted a REAL desktop action: it reached {what}. "
        f"Patch app.console.execute_with_recovery for an orchestration test; if this test is about "
        f"app/executor/adapter.py itself, request the `os_primitives_faked` fixture and replace the "
        f"Windows primitive ({name}) before calling the adapter. No argument is shown: a target can be "
        f"a window title or typed text.")


def install_desktop_guard(adapter, monkeypatch, *, allow_faked_primitives: bool) -> None:
    """Two strictnesses, one rule: the real machine is never reached.

    allow_faked_primitives=False - every test. The adapter's public functions are replaced, so nothing
    above them can reach the machine and the refusal arrives early with a clear message.

    allow_faked_primitives=True - the `os_primitives_faked` fixture, for tests OF the adapter. The
    functions stay genuine, so their validation, their platform checks and their error translation can
    all be exercised; what is replaced is the Windows primitive underneath. A test that installs its own
    fake first wins (monkeypatch applies in order); a test that forgets hits the refuser instead of the
    machine. Requesting the fixture therefore permits nothing by itself."""
    remember_originals(adapter)
    if not allow_faked_primitives:
        for name in DESKTOP_BOUNDARIES:
            monkeypatch.setattr(adapter, name, _boundary_refuser(name), raising=False)
        return
    monkeypatch.setattr(subprocess, "Popen", _primitive_refuser("subprocess.Popen"))
    for name in ("_keyboard_api", "_hotkey", "_use_physical_pixels"):
        monkeypatch.setattr(adapter, name, _primitive_refuser(f"adapter.{name}"), raising=False)
    # ctypes.WinDLL is NOT replaced: merely LOADING user32 is harmless, and a test fixture legitimately
    # constructs adapter._KeyboardApi(), which loads it. Only the functions that build user32 inline and
    # immediately act on another process's window can reach the machine, so those stay wrapped and
    # delegate only once the test has replaced WinDLL itself.
    loaded = getattr(adapter.ctypes, "WinDLL", None)
    for name in ("request_close", "request_window_state", "tag_window", "window_token", "untag_window"):
        monkeypatch.setattr(adapter, name, _needs_faked_windll(adapter, name, loaded), raising=False)
    # click does `import pyautogui` inside the function; a test replaces sys.modules["pyautogui"] first.
    monkeypatch.setattr(sys, "meta_path",
                        [RefuseLibraries({_MOUSE_LIBRARY: (PhysicalDesktopEscaped,
                                                           "mouse-library access", DESKTOP_EXEMPT)}),
                         *sys.meta_path])


def _boundary_refuser(name: str):
    def refuse(*args, **kwargs):
        raise _desktop_refusal(", ".join(_PRIMITIVE_NAMES), f"app.executor.adapter.{name}()")
    return refuse


def _needs_faked_windll(adapter, name: str, loaded):
    """These reach another process's window the moment they run - posting a message, or reading, setting
    or removing a window property - so they may only proceed once the test has replaced ctypes.WinDLL
    with its own fake."""
    original = _ORIGINALS[name]

    def call(*args, **kwargs):
        if getattr(adapter.ctypes, "WinDLL", None) is not loaded:
            return original(*args, **kwargs)
        if getattr(adapter.sys, "platform", "") != "win32":
            # The test has said this is not Windows, so the function refuses on its own before it can
            # reach anything - which is the behaviour those tests exist to check.
            return original(*args, **kwargs)
        raise _desktop_refusal("ctypes.WinDLL", f"app.executor.adapter.{name}()")
    return call


def _primitive_refuser(name: str):
    def refuse(*args, **kwargs):
        raise _desktop_refusal(name, f"the real {name}")
    return refuse


# --- Speaker, microphone and the speech model: refused at import --------------------------------------
# Refusing the import reaches the barrier BEFORE aiohttp opens a socket, before comtypes builds a SAPI
# object, before PortAudio claims the microphone and before a model is read from disk. A test that
# installs its own fake is found in sys.modules first and never consults the finder.

SPEECH_LIBRARIES = ("edge_tts", "pyttsx3")
MICROPHONE_LIBRARIES = ("sounddevice",)
MODEL_LIBRARIES = ("faster_whisper",)


class RefuseLibraries:
    """A sys.meta_path finder that refuses to load a real-world library."""

    def __init__(self, blocked: dict):
        self._blocked = blocked      # top-level name -> (sentinel, what it would do, allowed markers)

    def find_spec(self, fullname, path=None, target=None):
        entry = self._blocked.get(fullname.split(".")[0])
        if entry is not None:
            sentinel, what, markers = entry
            raise sentinel(
                f"offline test attempted real {what}: it tried to import {fullname!r}. Install a fake "
                f"for the module under test, or mark the test {' or '.join(sorted(markers))} AND set "
                f"that marker's RUN_REAL_* gate if it is genuinely meant to use this computer.")
        return None


def install_library_guard(monkeypatch, *, speaker: bool, microphone: bool, model: bool) -> None:
    blocked = {}
    if speaker:
        blocked.update({name: (PhysicalAudioEscaped, "speaker/network TTS access", AUDIO_EXEMPT)
                        for name in SPEECH_LIBRARIES})
    if microphone:
        blocked.update({name: (PhysicalListenerEscaped, "microphone access", MICROPHONE_EXEMPT)
                        for name in MICROPHONE_LIBRARIES})
    if model:
        blocked.update({name: (PhysicalListenerEscaped, "speech-model loading", MODEL_EXEMPT)
                        for name in MODEL_LIBRARIES})
    if blocked:
        monkeypatch.setattr(sys, "meta_path", [RefuseLibraries(blocked), *sys.meta_path])


def refuse_playback(command):
    raise PhysicalAudioEscaped(
        f"offline test attempted real speaker access: it reached winmm/MCI playback "
        f"({str(command).split()[0]!r} command). Replace app.speaker.adapter._mci with a fake, or mark "
        f"the test real_speaker / real_voice_console with its RUN_REAL_* gate set.")


# --- Provider -----------------------------------------------------------------------------------------
# The lowest boundary that tells a fake transport from a real one: httpx2.MockTransport is what the
# fake-Claude fixture installs, and httpx2.HTTPTransport is what actually opens a socket. Blocking the
# real transport leaves loopback, subprocess and every fake untouched - and it does not depend on
# ANTHROPIC_API_KEY being absent, which was never isolation, only luck.

def install_provider_guard(monkeypatch) -> None:
    import httpx2

    def refuse(self, request, *args, **kwargs):
        raise ProviderEscaped(
            "offline test attempted a REAL provider request: it reached httpx2.HTTPTransport, the "
            "transport the Anthropic client sends over. Use the `fake_claude` fixture (httpx2."
            "MockTransport), or mark the test real_api AND set RUN_REAL_CLAUDE_TEST=1. The URL is not "
            "shown, and neither is the request body.")

    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", refuse)
    if hasattr(httpx2, "AsyncHTTPTransport"):
        async def refuse_async(self, request, *args, **kwargs):
            raise ProviderEscaped(
                "offline test attempted a REAL provider request over the async transport.")

        monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", refuse_async)


# --- Databases ----------------------------------------------------------------------------------------
# SQLite writes a FILE; it does not act on the world, so this guard redirects rather than refuses. An
# ordinary test gets a real SQLite database in its own temporary directory - full coverage of the real
# adapter - while the two databases that belong to the USER stay unreachable:
#
#     data/memory.db          structured memory (app/memory/)
#     data/claude_usage.db    the Claude usage ledger (app/brain/cost_controls.py)
#
# WHY THIS IS CENTRAL AND AUTOUSE. The ledger was previously protected only because the tests that
# exercise it happen to request the `fake_claude` fixture, which points the setting at a temporary file.
# That is per-test mocking - the same shape that opened real Notepad windows in Slice 3B - so a test that
# reached cost_controls without that fixture would have used the real ledger. The redirect below does not
# depend on any test remembering anything.
#
# Two layers, because the first alone could be refactored around:
#   1. the two functions that resolve a configured database path return a path inside this test's tmp_path
#   2. sqlite3.connect refuses any file inside the repository, so inlining the resolution fails loudly
#      instead of quietly reaching data/. SQLite itself is NOT disabled and NOT mocked: every other
#      connection, including :memory:, passes straight through to the real driver.

# (module path, function name, file name the test gets) - resolved lazily so importing this file never
# imports the application.
_DATABASE_PATHS = (
    ("app.memory.logic", "database_path", "memory.db"),
    ("app.brain.cost_controls", "_ledger_path", "claude_usage.db"),
)


def install_database_guard(tmp_path, monkeypatch) -> None:
    """Point every configured database at `tmp_path`, and make a repository database unreachable."""
    import importlib
    from pathlib import Path

    area = Path(tmp_path) / "databases"
    area.mkdir(parents=True, exist_ok=True)
    repository_root = Path(__file__).resolve().parent
    for module_name, function_name, file_name in _DATABASE_PATHS:
        module = importlib.import_module(module_name)
        original = getattr(module, function_name)
        monkeypatch.setattr(module, function_name,
                            _redirected(original, area / file_name, repository_root), raising=True)

    import sqlite3
    real_connect = sqlite3.connect
    repository = Path(__file__).resolve().parent

    def connect(database, *args, **kwargs):
        target = _database_file(database)
        if target is not None and _inside(target, repository):
            raise RealDatabaseEscaped(
                f"offline test attempted to open a REAL database inside the repository "
                f"({target.name}). Normal tests get their own SQLite file under tmp_path - resolve the "
                f"path through the configured setting instead of building it, or mark the test "
                f"real_database AND set RUN_REAL_DATABASE_TEST=1. No row contents are shown.")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)


def _redirected(original, safe_path, repository_root):
    """Keep a path the test chose for itself; replace one that points inside the repository.

    A fixture that already configured a temporary database (`fake_claude` does, for the ledger) must win,
    or its own assertions would read a different file from the one production writes. Only a path that
    would reach the user's real database is replaced. A settings error is NOT swallowed: a test that
    checks a missing setting still sees it."""
    from pathlib import Path

    def resolve():
        resolved = Path(original())
        return safe_path if _inside(resolved.resolve(), repository_root) else resolved

    resolve.__wrapped__ = original   # so a test can still reach the genuine resolver
    return resolve


def _database_file(database):
    """The file a connect() target names, or None when it is not a file (`:memory:`, a URI for it, or a
    connection object the driver accepts)."""
    from pathlib import Path
    if isinstance(database, Path):
        return database.resolve()
    if not isinstance(database, (str, bytes)):
        return None
    text = database.decode("utf-8", "replace") if isinstance(database, bytes) else database
    if not text or text.startswith(":") or ":memory:" in text or "mode=memory" in text:
        return None
    if text.startswith("file:"):
        text = text[len("file:"):].split("?", 1)[0]
        if not text:
            return None
    return Path(text).resolve()


def _inside(path, directory) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


# --- Verifier input state -----------------------------------------------------------------------------
# app/verifier/adapter.py::modifier_keys_down() calls GetAsyncKeyState, so it reads the REAL keyboard.
# Nothing centrally replaced it, and only some test files faked it for themselves - so whether a shortcut
# test passed depended on whether a modifier key happened to be held while the suite ran. That is how
# test_a_low_brain_floor_cannot_soften_a_real_executor_rule failed once in six identical runs.
#
# This guard REDIRECTS rather than refuses, because unlike the executor boundaries this is a read: it
# sends nothing and changes nothing, so there is no escape to prevent - only non-determinism to remove.
# [] (no modifier held) is the state every offline test already assumes.
#
# DELIBERATELY NARROW. The verifier's other observation reads - active_target, cursor_position,
# window_at, list_windows, the clipboard reads and read_text - are NOT covered here. They are a real
# test-isolation and privacy question, and they belong to Phase 5, which is the phase that expands
# screen observation and should define one coherent policy for all of them at once.

# The verifier's reads, split by what each one can expose. STRUCTURE REDIRECTS, CONTENT REFUSES:
#
#   redirected  environment state that hundreds of tests need constantly. A deterministic fake removes
#               the non-determinism without reducing any protection - these say nothing about the user.
#   refused     the reads that return ARBITRARY CONTENT from whatever window is in front. A test that
#               silently reads the user's open document is the actual harm, and few tests need these, so
#               the default is a loud refusal exactly like the executor's boundaries.
#
# Phase 5 adds UI Automation to the refused set: an accessibility tree carries every label and field in
# a window, and pywinauto is blocked at IMPORT so an ordinary test cannot even load a UIA stack.
VERIFIER_CONTENT_READS = (
    "read_text",            # WM_GETTEXT on any control - the single most exposing read in the project
    "selection",            # which characters are selected in that control
    "text_length",          # how much text it holds
    "uia_find_by_name",     # Phase 5: walks the accessibility tree and compares every control's name
    "uia_window_bounds",    # Phase 5: the same UIA entry, for one window's own rectangle
)

UIA_LIBRARIES = ("pywinauto",)


def install_verifier_input_guard(verifier_adapter, monkeypatch) -> None:
    """Make the verifier's live keyboard read deterministic for an ordinary test.

    A test that needs a modifier to look held patches the same function in its own body; that runs after
    this fixture, so it wins. A real-desktop test keeps the genuine read through the existing
    marker-and-gate pair, because this guard is simply not installed for it."""
    monkeypatch.setattr(verifier_adapter, "modifier_keys_down", _no_modifiers_held)


def install_observation_guard(verifier_adapter, monkeypatch) -> None:
    """Refuse every read that could return the user's own screen content, and block the UIA library.

    Not a second safety system: the same marker-and-gate rule, the same sentinel, and the same shape as
    the executor guard - a refuser in place of the function, so the refusal arrives early and says what
    to do instead. A test that is ABOUT one of these replaces it with a fake in its own body, which runs
    after this fixture."""
    for name in VERIFIER_CONTENT_READS:
        monkeypatch.setattr(verifier_adapter, name, _content_refuser(name), raising=False)
    monkeypatch.setattr(sys, "meta_path",
                        [RefuseLibraries({library: (PhysicalDesktopEscaped, "UI Automation access",
                                                    DESKTOP_EXEMPT)
                                          for library in UIA_LIBRARIES}),
                         *sys.meta_path])


def _content_refuser(name: str):
    def refuse(*args, **kwargs):
        raise PhysicalDesktopEscaped(
            f"offline test attempted to READ REAL SCREEN CONTENT: it reached "
            f"app.verifier.adapter.{name}(), which returns whatever is in the window that happens to be "
            f"in front. Replace it with a fake in the test, or mark the test real_desktop AND set "
            f"RUN_REAL_DESKTOP_TEST=1. No argument is shown: one of them can be a window title or the "
            f"user's own text.")
    return refuse


def _no_modifiers_held() -> list:
    """Named rather than a lambda so a test can tell the stand-in from the real implementation."""
    return []
