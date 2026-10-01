"""
Repository-root pytest configuration: the outer safety boundary.

pytest loads this file for EVERY test under this repository, wherever it sits. tests/conftest.py is not
the outer boundary and must not be treated as one - a test file placed anywhere else never sees it, and
that is exactly how two real Notepad windows were opened during the Slice 3B investigation.

Only the guards live here. Everything else - the fake Claude transport, the scripted consoles, logging
isolation, the RUN_REAL_* skip logic - stays in tests/conftest.py, which is where the tests are.

Every guard follows one rule: a test may exercise as much application logic as it likes, and may never
reach the real machine or the real network. Bypassing one needs a registered marker AND that marker's
RUN_REAL_* environment gate. See safety_guards.py for the reasoning behind each.
"""
import pytest

import safety_guards
from app.executor import adapter as executor_adapter
from app.speaker import adapter as speaker_adapter

# Re-exported so tests can import the sentinels from either conftest.
PhysicalBoundaryEscaped = safety_guards.PhysicalBoundaryEscaped
PhysicalDesktopEscaped = safety_guards.PhysicalDesktopEscaped
PhysicalAudioEscaped = safety_guards.PhysicalAudioEscaped
PhysicalListenerEscaped = safety_guards.PhysicalListenerEscaped
ProviderEscaped = safety_guards.ProviderEscaped


def pytest_configure(config):
    """Register the real markers here too, so a test file outside tests/ can still be marked."""
    for marker, gate in sorted({**safety_guards.DESKTOP_EXEMPT, **safety_guards.AUDIO_EXEMPT,
                                **safety_guards.MICROPHONE_EXEMPT, **safety_guards.MODEL_EXEMPT,
                                **safety_guards.PROVIDER_EXEMPT}.items()):
        config.addinivalue_line("markers", f"{marker}: uses this computer or the network for real; "
                                           f"needs {gate}=1 as well as this marker")


@pytest.fixture(autouse=True)
def no_physical_desktop(request, monkeypatch):
    """Block every boundary in app/executor/adapter.py that can touch this computer.

    A test that is ABOUT the adapter requests `os_primitives_faked` instead, which keeps the block but
    lets a call through once the test has replaced the Windows primitive underneath it."""
    if safety_guards.exempt(request.node, safety_guards.DESKTOP_EXEMPT):
        return
    if "os_primitives_faked" in request.fixturenames:
        return                                  # that fixture installs the stricter version itself
    safety_guards.install_desktop_guard(executor_adapter, monkeypatch, allow_faked_primitives=False)


@pytest.fixture
def os_primitives_faked(request, monkeypatch):
    """For tests of app/executor/adapter.py itself: run the real adapter logic, never the real OS.

    The contract, and it fails closed: each adapter entry point stays blocked until this test has
    replaced the Windows primitive beneath it - subprocess.Popen, ctypes.WinDLL, _keyboard_api, _hotkey,
    _use_physical_pixels or the pyautogui module. Replace it first, then call the adapter. Forget, and
    the call raises PhysicalDesktopEscaped exactly as it would in any other test.

    This is NOT a blanket "allow physical desktop" switch: requesting it permits nothing by itself."""
    safety_guards.install_desktop_guard(executor_adapter, monkeypatch, allow_faked_primitives=True)
    return safety_guards.DESKTOP_BOUNDARIES


@pytest.fixture(autouse=True)
def no_physical_speaker_or_listener(request, monkeypatch):
    """Refuse, at import, the libraries that make a sound, open the microphone, or load a speech model.

    Each is exempt separately: a microphone test may record without being allowed to speak."""
    safety_guards.install_library_guard(
        monkeypatch,
        speaker=not safety_guards.exempt(request.node, safety_guards.AUDIO_EXEMPT),
        microphone=not safety_guards.exempt(request.node, safety_guards.MICROPHONE_EXEMPT),
        model=not safety_guards.exempt(request.node, safety_guards.MODEL_EXEMPT),
    )
    if not safety_guards.exempt(request.node, safety_guards.AUDIO_EXEMPT):
        monkeypatch.setattr(speaker_adapter, "_mci", safety_guards.refuse_playback)


@pytest.fixture(autouse=True)
def no_real_provider(request, monkeypatch):
    """Block httpx2's real HTTP transport - the one the Anthropic client sends over.

    Deliberately independent of whether ANTHROPIC_API_KEY is set: a missing key was never isolation."""
    if safety_guards.exempt(request.node, safety_guards.PROVIDER_EXEMPT):
        return
    safety_guards.install_provider_guard(monkeypatch)
