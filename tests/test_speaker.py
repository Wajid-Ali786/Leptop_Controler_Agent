"""
Tests for app/speaker/ (docs/step4 Section 5, Phase 2 - Feature 1: config and boundary only).

Offline and silent: nothing here speaks, imports edge-tts or pyttsx3, or touches the network. The
speaker owns its own settings, like every other module, so app/listener never reads them.
The import-boundary rules for edge_tts/pyttsx3 live in tests/test_listener.py, which checks every
voice library in one place.
"""
import pytest

from app.speaker import logic
from app.speaker.models import SpeakerSettings
from config import settings
from config.settings import SettingsError

CONFIG = ("speaker:\n"
          "  enabled: false\n"
          "  engine: auto\n"
          '  voice: ""\n'
          "  rate: 0\n")


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_PATH", path)

    def rewrite(old, new):
        assert old in path.read_text(encoding="utf-8"), f"{old!r} is not in the test config"
        path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")

    return rewrite


def test_the_speaker_section_is_read_and_validated(config):
    chosen = logic.speaker_settings()
    assert chosen == SpeakerSettings(enabled=False, engine="auto", voice="", rate=0)


def test_the_real_config_file_is_valid():
    assert logic.speaker_settings().engine in logic.ENGINES


@pytest.mark.parametrize("old, new, part", [
    ("engine: auto", "engine: robot", "speaker.engine"),
    ("engine: auto", "engine: true", "speaker.engine"),
    ("rate: 0", "rate: 900", "speaker.rate"),
    ("rate: 0", "rate: 51", "speaker.rate"),
    ("rate: 0", "rate: -51", "speaker.rate"),
    ("rate: 0", "rate: 1.5", "speaker.rate"),
    ("rate: 0", "rate: true", "speaker.rate"),
    ("enabled: false", "enabled: sometimes", "speaker.enabled"),
    ('voice: ""', "voice: 7", "speaker.voice"),
])
def test_invalid_settings_are_refused_by_name(config, old, new, part):
    config(old, new)
    with pytest.raises(SettingsError, match=part.replace(".", r"\.")):
        logic.speaker_settings()


def test_an_empty_voice_means_the_engine_default(config):
    assert logic.speaker_settings().voice == ""


@pytest.mark.parametrize("value", [-50, -20, 0, 20, 50])
def test_rate_is_a_relative_percentage_in_both_directions(config, value):
    """0 is the engine's normal speed; the same number must mean the same thing on either engine,
    so it is a percentage here and each adapter maps it onto its own scale in Feature 9."""
    config("rate: 0", f"rate: {value}")
    assert logic.speaker_settings().rate == value


def test_the_accepted_rate_range_is_conservative():
    assert (logic.SLOWEST_PERCENT, logic.FASTEST_PERCENT) == (-50, 50)


def test_the_rate_error_explains_the_percentage_contract(config):
    config("rate: 0", "rate: 200")
    with pytest.raises(SettingsError, match="percentage"):
        logic.speaker_settings()


def test_the_speaker_owns_its_settings_not_the_listener():
    """Rule 4: a module's settings are read inside that module."""
    from app.listener import logic as listener_logic
    assert not hasattr(listener_logic, "speaker_settings")
