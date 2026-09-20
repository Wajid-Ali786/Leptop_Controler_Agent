"""
Tests for Phase 2 Feature 2 Task 2a: finding microphones and deciding which one is meant.

Task 2a records NOTHING. There is no capture() yet, and these tests open no stream: the offline
ones replace app.listener.adapter._audio() with a fake backend that has no stream class at all, so
a stray attempt to open one would fail loudly rather than quietly work.

The split under test is the point. app/listener/adapter.py enumerates and reports; the matching
rules live in the pure app/listener/logic.py, so every rule below - exact before substring, the
default-host-API tie-break, and refusing anything still ambiguous - is checked with no microphone,
no backend and no I/O.
"""
import logging

import pytest

from app.listener import adapter, logic
from app.listener.models import CAPTURE_UNAVAILABLE, NO_DEVICE, InputDevice, VoiceFailure

MME, DIRECTSOUND, WASAPI, WDMKS = "MME", "Windows DirectSound", "Windows WASAPI", "Windows WDM-KS"
HOST_APIS = (MME, DIRECTSOUND, WASAPI, WDMKS)


# --- A fake backend: the shape sounddevice reports, with nothing that could open a stream ---------

class FakePortAudioError(Exception):
    """Stands in for sounddevice.PortAudioError."""


class FakeDefaults:
    def __init__(self, input_index, host_api):
        self.device = [input_index, 99]  # [input, output] - only the input half is ever read
        self.hostapi = host_api


class FakeBackend:
    """Answers the three questions the adapter asks and offers nothing else on purpose."""

    PortAudioError = FakePortAudioError

    def __init__(self, devices, *, default_input=0, default_host_api=0, fails=None):
        self.devices = devices
        self.defaults_kwargs = (default_input, default_host_api)
        self.default = FakeDefaults(default_input, default_host_api)
        self.fails = fails
        self.calls = []

    def query_devices(self):
        self.calls.append("query_devices")
        if self.fails:
            raise self.fails
        return self.devices

    def query_hostapis(self):
        self.calls.append("query_hostapis")
        return [{"name": name} for name in HOST_APIS]


def device(name, *, host_api=0, inputs=1, rate=44100.0):
    return {"name": name, "hostapi": host_api, "max_input_channels": inputs,
            "max_output_channels": 0 if inputs else 2, "default_samplerate": rate}


def use(monkeypatch, backend):
    monkeypatch.setattr(adapter, "_audio", lambda: backend)
    return backend


def built(name="Microphone", *, index=0, host_api=MME, inputs=1, rate=44100.0,
          default=False, default_api=True):
    """An InputDevice as the adapter would have built it - for the pure choose_device() tests."""
    return InputDevice(index=index, name=name, host_api=host_api, max_input_channels=inputs,
                       default_samplerate=rate, is_default=default, is_default_host_api=default_api)


# --- list_input_devices() -------------------------------------------------------------------------

def test_only_input_capable_endpoints_are_returned(monkeypatch):
    use(monkeypatch, FakeBackend([device("Speakers", inputs=0), device("Microphone", inputs=2),
                                  device("Headphones", inputs=0)], default_input=1))
    found = adapter.list_input_devices()
    assert [d.name for d in found] == ["Microphone"]
    assert found[0].index == 1, "the index must stay the backend's own, not a position in our list"


def test_every_field_comes_from_the_backend(monkeypatch):
    use(monkeypatch, FakeBackend([device("Mic (Realtek)", host_api=2, inputs=2, rate=48000.0)],
                                 default_input=0, default_host_api=2))
    only = adapter.list_input_devices()[0]
    assert only == InputDevice(index=0, name="Mic (Realtek)", host_api=WASAPI, max_input_channels=2,
                               default_samplerate=48000.0, is_default=True, is_default_host_api=True)


def test_the_default_device_and_the_default_host_api_are_marked_separately(monkeypatch):
    use(monkeypatch, FakeBackend([device("A", host_api=0), device("B", host_api=2)],
                                 default_input=1, default_host_api=0))
    a, b = adapter.list_input_devices()
    assert (a.is_default, a.is_default_host_api) == (False, True)
    assert (b.is_default, b.is_default_host_api) == (True, False), \
        "the default microphone need not sit on the default host API"


def test_no_device_is_marked_default_when_the_backend_has_no_default_input(monkeypatch):
    use(monkeypatch, FakeBackend([device("A"), device("B")], default_input=-1))
    assert [d.is_default for d in adapter.list_input_devices()] == [False, False]


def test_a_computer_with_no_microphone_returns_an_empty_tuple_not_a_failure(monkeypatch):
    use(monkeypatch, FakeBackend([device("Speakers", inputs=0)]))
    found = adapter.list_input_devices()
    assert found == (), "the backend worked and found nothing - that is an answer, not a failure"


def test_listing_never_opens_anything(monkeypatch):
    backend = use(monkeypatch, FakeBackend([device("Microphone")]))
    adapter.list_input_devices()
    assert backend.calls == ["query_devices", "query_hostapis"]
    for opener in ("InputStream", "RawInputStream", "rec", "Stream"):
        assert not hasattr(backend, opener), "the fake deliberately cannot open a stream"


def test_the_device_list_writes_no_log_line_at_all(monkeypatch, caplog):
    use(monkeypatch, FakeBackend([device("Microphone (Wajid's Headset)")]))
    with caplog.at_level(logging.DEBUG):
        adapter.list_input_devices()
    assert caplog.records == [], "device names must never reach a log; the list returns them instead"


# --- When the backend itself is unusable (D-5: NOT no_device) --------------------------------------

@pytest.mark.parametrize("problem", [ImportError("No module named 'sounddevice'"),
                                     OSError("PortAudio library not found")])
def test_a_missing_backend_is_capture_unavailable(monkeypatch, problem):
    def explode():
        raise problem
    monkeypatch.setattr(adapter, "_audio", explode)
    failure = adapter.list_input_devices()
    assert isinstance(failure, VoiceFailure) and failure.kind == CAPTURE_UNAVAILABLE


def test_a_backend_that_cannot_start_its_host_apis_is_capture_unavailable(monkeypatch):
    use(monkeypatch, FakeBackend([], fails=FakePortAudioError("host API initialisation failed")))
    failure = adapter.list_input_devices()
    assert isinstance(failure, VoiceFailure) and failure.kind == CAPTURE_UNAVAILABLE


def test_capture_unavailable_is_not_the_same_fact_as_no_device(monkeypatch):
    """'There is no microphone' and 'there is no way to look for one' are different answers."""
    monkeypatch.setattr(adapter, "_audio", lambda: (_ for _ in ()).throw(ImportError("nope")))
    failure = adapter.list_input_devices()
    assert failure.kind != NO_DEVICE
    assert "sound backend" in failure.message


def test_the_failure_message_does_not_quote_the_raw_backend_error(monkeypatch):
    secret = "C:\\Users\\Wajid\\some\\private\\path\\libportaudio.dll"
    monkeypatch.setattr(adapter, "_audio", lambda: (_ for _ in ()).throw(OSError(secret)))
    assert secret not in adapter.list_input_devices().message


def test_an_unexpected_exception_propagates_instead_of_becoming_a_hardware_failure(monkeypatch):
    """A defect in this project must surface as a defect - never be relabelled as broken hardware."""
    use(monkeypatch, FakeBackend([], fails=ValueError("a bug in our own enumeration code")))
    with pytest.raises(ValueError, match="a bug in our own enumeration code"):
        adapter.list_input_devices()


# --- choose_device(): the default ------------------------------------------------------------------

@pytest.mark.parametrize("selector", ["", "   ", "\t"])
def test_an_empty_selector_means_the_backends_default_microphone(selector):
    devices = [built("A", index=0), built("B", index=1, default=True)]
    assert logic.choose_device(selector, devices) is devices[1]


def test_no_devices_at_all_is_refused_clearly():
    refusal = logic.choose_device("", [])
    assert isinstance(refusal, VoiceFailure) and refusal.kind == NO_DEVICE
    assert "no microphone" in refusal.message.lower()


def test_devices_exist_but_none_is_the_default():
    refusal = logic.choose_device("", [built("A"), built("B", index=1)])
    assert isinstance(refusal, VoiceFailure) and "default microphone" in refusal.message
    assert "listener.input_device" in refusal.message, "the refusal must say what to do about it"


# --- choose_device(): an index ---------------------------------------------------------------------

def test_an_index_selects_that_exact_device():
    devices = [built("A", index=0), built("B", index=7)]
    assert logic.choose_device(7, devices) is devices[1]


def test_an_index_that_is_not_there_is_refused_and_recommends_a_name():
    refusal = logic.choose_device(7, [built("A", index=0)])
    assert isinstance(refusal, VoiceFailure) and refusal.kind == NO_DEVICE
    assert "7" in refusal.message and "name" in refusal.message.lower()


def test_an_output_only_endpoints_index_is_not_selectable():
    """The adapter never lists one, so an index pointing at speakers simply isn't found."""
    refusal = logic.choose_device(3, [built("Microphone", index=1)])
    assert isinstance(refusal, VoiceFailure)


@pytest.mark.parametrize("selector", [True, False])
def test_a_boolean_never_names_a_device(selector):
    refusal = logic.choose_device(selector, [built("A", index=0, default=True)])
    assert isinstance(refusal, VoiceFailure) and refusal.kind == NO_DEVICE


# --- choose_device(): a name -----------------------------------------------------------------------

def test_an_exact_name_wins_even_when_another_name_contains_it():
    exact = built("Headset", index=1)
    devices = [built("Headset (TWS Hands-Free AG Audio)", index=0), exact]
    assert logic.choose_device("Headset", devices) is exact


def test_an_exact_match_ignores_case_and_surrounding_space():
    only = built("Microphone (Realtek High Definition Audio)", index=4)
    assert logic.choose_device("  microphone (REALTEK high definition audio)  ", [only]) is only


def test_a_substring_matches_when_nothing_matches_exactly():
    only = built("Microphone (Realtek High Definition Audio)", index=4)
    assert logic.choose_device("realtek", [only]) is only


def test_a_name_nothing_matches_is_refused():
    refusal = logic.choose_device("Blue Yeti", [built("Microphone (Realtek)")])
    assert isinstance(refusal, VoiceFailure) and refusal.kind == NO_DEVICE
    assert "Blue Yeti" in refusal.message


def test_the_same_microphone_seen_through_several_host_apis_is_resolved_by_the_default_one():
    """The real duplication this rule exists for: one physical microphone, four sound paths."""
    devices = [built("Microphone (Realtek High Defini", index=1, host_api=MME, default_api=True),
               built("Microphone (Realtek High Definition Audio)", index=8, host_api=DIRECTSOUND,
                     default_api=False),
               built("Microphone (Realtek High Definition Audio)", index=18, host_api=WASAPI,
                     default_api=False),
               built("Microphone (Realtek HD Audio Mic input)", index=20, host_api=WDMKS,
                     default_api=False)]
    chosen = logic.choose_device("realtek", devices)
    assert isinstance(chosen, InputDevice) and chosen.index == 1 and chosen.host_api == MME


def test_ambiguity_the_default_host_api_cannot_settle_is_refused_not_guessed():
    devices = [built("Microphone A", index=0, host_api=MME, default_api=True),
               built("Microphone B", index=1, host_api=MME, default_api=True)]
    refusal = logic.choose_device("microphone", devices)
    assert isinstance(refusal, VoiceFailure) and refusal.kind == NO_DEVICE
    assert "matches 2 microphones" in refusal.message
    assert "[0]" in refusal.message and "[1]" in refusal.message, "the user must see the candidates"


def test_ambiguity_with_no_candidate_on_the_default_host_api_is_refused():
    devices = [built("Mic X", index=17, host_api=WASAPI, default_api=False),
               built("Mic Y", index=24, host_api=WDMKS, default_api=False)]
    assert isinstance(logic.choose_device("mic", devices), VoiceFailure)


def test_a_refusal_never_lists_an_unbounded_number_of_candidates():
    devices = [built(f"Microphone {n}", index=n, default_api=True) for n in range(9)]
    message = logic.choose_device("microphone", devices).message
    assert "and 4 more" in message and message.count("] Microphone") == logic.MAX_LISTED_CANDIDATES


# --- Device names are hostile strings: real evidence from this machine -----------------------------

WDMKS_REAL_NAME = "Headset (@System32\\drivers\\bthhfenum.sys,#2;%1 Hands-Free AG Audio%0\r\n;(TWS))"


def test_a_driver_name_containing_a_line_break_is_flattened_for_display():
    """Device 24 on this computer really is called this, newline included."""
    shown = logic.readable(WDMKS_REAL_NAME)
    assert "\r" not in shown and "\n" not in shown
    assert "  " not in shown, "runs of whitespace collapse, so the name stays one readable line"


def test_a_very_long_name_is_cut_rather_than_flooding_the_message():
    shown = logic.readable("X" * 500)
    assert len(shown) == logic.MAX_NAME_CHARACTERS and shown.endswith("...")


def test_a_refusal_listing_an_ugly_name_stays_on_one_line():
    devices = [built(WDMKS_REAL_NAME, index=24, default_api=True),
               built(WDMKS_REAL_NAME + " (2)", index=25, default_api=True)]
    message = logic.choose_device("headset", devices).message
    assert "\n" not in message and "\r" not in message


def test_a_truncated_mme_name_still_matches_by_substring():
    """MME cuts names at 31 characters, so the full name never matches it exactly."""
    mme = built("Microphone (Realtek High Defini", index=1, host_api=MME, default_api=True)
    assert logic.choose_device("Microphone (Realtek High Defini", [mme]) is mme


# --- The real backend on this computer: read-only, records nothing ---------------------------------

@pytest.mark.real_microphone
def test_this_computer_really_has_a_microphone_we_can_describe():
    found = adapter.list_input_devices()
    assert isinstance(found, tuple), getattr(found, "message", "")
    assert found, "no input-capable device was found on this computer"
    assert all(d.max_input_channels >= 1 and d.default_samplerate > 0 for d in found)
    assert sum(1 for d in found if d.is_default) <= 1, "there can only be one default microphone"


@pytest.mark.real_microphone
def test_the_default_microphone_accepts_canonical_mono_16k_without_opening_a_stream():
    """check_input_settings() asks the host API whether the format would be accepted. It opens no
    stream and captures nothing. This is the Feature 1 contract - mono 16000 Hz - tested against the
    path capture would really use: the default device on the default host API."""
    import sounddevice  # imported here so the offline tests above never need the backend

    found = adapter.list_input_devices()
    assert isinstance(found, tuple) and found
    chosen = logic.choose_device("", found)
    assert isinstance(chosen, InputDevice), getattr(chosen, "message", "")
    assert chosen.is_default_host_api, (
        f"the default microphone is reached through {chosen.host_api}, which is not the default "
        f"host API - the 16 kHz evidence below would then be for a path capture would not use")
    sounddevice.check_input_settings(device=chosen.index, samplerate=16000, channels=1, dtype="int16")
    sounddevice.check_input_settings(samplerate=16000, channels=1, dtype="int16")  # unnamed default
