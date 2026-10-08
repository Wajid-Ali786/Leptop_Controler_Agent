"""
The advisory risk floor (Phase 3 Slice 3A): a caller may ask the Executor to be MORE careful about one
action, and can never ask it to be less.

The seam exists because the safety Action - and the floor on it - is built inside the Executor's own
preparer, from what it learned while preparing: the real window title, whether the typed text ends in
Enter, which shortcut it is. There was no way for a caller to add to that, so the Brain's opinion about
how careful to be had nowhere to go. Now execute() and execute_with_recovery() take `risk_floor`, and
_with_advisory_floor() keeps the higher of the two.

Nothing here touches a real desktop: the adapter is replaced throughout, and most of these tests never
get past the safety gate anyway, which is the point.
"""
import pytest

from app.executor import logic as executor
from app.executor.logic import ADVISORY_FLOOR_REASON
from app.executor.models import CLICK, OPEN_APP, REFRESH, SHORTCUT, TYPE_TEXT, ExecutorAction
from app.safety.logic import ActionDeniedError
from app.safety.models import Action, RiskLevel


# --- The combination rule, on its own ------------------------------------------------------------------

@pytest.mark.parametrize("preparer, advisory, expected", [
    (RiskLevel.LOW, RiskLevel.LOW, RiskLevel.LOW),
    (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.MEDIUM),
    (RiskLevel.LOW, RiskLevel.HIGH, RiskLevel.HIGH),
    (RiskLevel.LOW, RiskLevel.CRITICAL, RiskLevel.CRITICAL),
    (RiskLevel.MEDIUM, RiskLevel.LOW, RiskLevel.MEDIUM),
    (RiskLevel.MEDIUM, RiskLevel.MEDIUM, RiskLevel.MEDIUM),
    (RiskLevel.MEDIUM, RiskLevel.CRITICAL, RiskLevel.CRITICAL),
    (RiskLevel.HIGH, RiskLevel.LOW, RiskLevel.HIGH),
    (RiskLevel.HIGH, RiskLevel.MEDIUM, RiskLevel.HIGH),
    (RiskLevel.CRITICAL, RiskLevel.LOW, RiskLevel.CRITICAL),
    (RiskLevel.CRITICAL, RiskLevel.HIGH, RiskLevel.CRITICAL),
])
def test_the_higher_of_the_two_floors_wins(preparer, advisory, expected):
    """RiskLevel is an IntEnum, so this is the same comparison app/safety/logic.assess() already uses."""
    combined = executor._with_advisory_floor(Action("do something", minimum_level=preparer,
                                                    minimum_reason="because of the action itself"),
                                             advisory)
    assert combined.minimum_level is expected


def test_an_advisory_floor_that_does_not_raise_changes_nothing_at_all():
    """Including the reason. The preparer's reason says something true about THIS action - "pastes
    clipboard content the assistant can't see" - and a generic sentence must not replace it."""
    original = Action("press Ctrl+V", minimum_level=RiskLevel.HIGH,
                      minimum_reason="pastes clipboard content the assistant can't see")
    for advisory in (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH):
        combined = executor._with_advisory_floor(original, advisory)
        assert combined is original, "the same object, untouched"
        assert combined.minimum_reason == "pastes clipboard content the assistant can't see"


def test_a_raised_floor_gets_a_generic_reason_with_no_model_prose_in_it():
    combined = executor._with_advisory_floor(Action("open notepad"), RiskLevel.HIGH)
    assert combined.minimum_level is RiskLevel.HIGH
    assert combined.minimum_reason == ADVISORY_FLOOR_REASON
    assert combined.minimum_reason == "the reasoning step asked for extra care"
    assert combined.description == "open notepad", "the description is the Executor's, not the caller's"


def test_the_combination_never_mutates_the_action_it_was_given():
    original = Action("open notepad", minimum_level=RiskLevel.LOW, minimum_reason="")
    executor._with_advisory_floor(original, RiskLevel.CRITICAL)
    assert original.minimum_level is RiskLevel.LOW and original.minimum_reason == ""


@pytest.mark.parametrize("nonsense", [0, 5, 99, -1])
def test_an_unreadable_advisory_floor_fails_closed(nonsense):
    """Like assess() does with an invalid minimum: doubt means more caution, never less."""
    combined = executor._with_advisory_floor(Action("open notepad"), nonsense)
    assert combined.minimum_level is RiskLevel.CRITICAL


# --- Through execute(), with the gate watching --------------------------------------------------------

@pytest.fixture
def gate(monkeypatch):
    """Records what the safety gate was asked, and denies everything so nothing can run."""
    asked = []

    def confirm(action, assessment):
        asked.append((action, assessment))
        return False

    return asked, confirm


@pytest.fixture
def no_desktop(monkeypatch):
    """Every real action raises, so a test that gets past the gate fails loudly instead of acting."""
    def refuse(*args, **kwargs):
        raise AssertionError("no real desktop action may happen in these tests")

    for name in ("launch_app", "request_close", "click", "send_character", "send_shortcut",
                 "send_wheel_notch", "request_window_state"):
        monkeypatch.setattr(executor.adapter, name, refuse, raising=False)
    return refuse


def test_a_default_call_is_unchanged_and_asks_for_no_confirmation(monkeypatch, no_desktop):
    """A: the existing caller passes no floor, so a LOW action still runs without being asked."""
    ran = []
    monkeypatch.setattr(executor, "_PREPARERS",
                        {**executor._PREPARERS,
                         "fake": lambda action: executor._Prepared(lambda: ran.append(1) or
                                                                   executor._result(action, True, "done"))})
    asked = []
    result = executor.execute(ExecutorAction("fake", ""),
                              confirm=lambda action, assessment: asked.append(1) or True)
    assert result.ok and ran == [1]
    assert asked == [], "LOW risk is not asked about"


def test_a_raised_floor_makes_the_existing_gate_ask(monkeypatch, no_desktop, gate):
    """B: preparer LOW + advisory HIGH -> HIGH, and the EXISTING confirmation mechanism is what asks.
    No second confirmation system was added anywhere."""
    asked, confirm = gate
    monkeypatch.setattr(executor, "_PREPARERS",
                        {**executor._PREPARERS,
                         "fake": lambda action: executor._Prepared(
                             lambda: pytest.fail("must not run when the confirmation is denied"))})
    with pytest.raises(ActionDeniedError) as denied:
        executor.execute(ExecutorAction("fake", ""), confirm=confirm, risk_floor=RiskLevel.HIGH)
    assert len(asked) == 1, "asked exactly once"
    action, assessment = asked[0]
    assert assessment.level is RiskLevel.HIGH
    assert assessment.rule == ADVISORY_FLOOR_REASON
    assert denied.value.assessment.level is RiskLevel.HIGH


def test_a_critical_floor_uses_the_same_mechanism_as_medium(monkeypatch, no_desktop, gate):
    """D-ish: CONFIRMATION_REQUIRED_AT is MEDIUM, so MEDIUM, HIGH and CRITICAL all take one path."""
    asked, confirm = gate
    monkeypatch.setattr(executor, "_PREPARERS",
                        {**executor._PREPARERS,
                         "fake": lambda action: executor._Prepared(lambda: pytest.fail("denied"))})
    with pytest.raises(ActionDeniedError):
        executor.execute(ExecutorAction("fake", ""), confirm=confirm, risk_floor=RiskLevel.CRITICAL)
    assert asked[0][1].level is RiskLevel.CRITICAL


def test_a_denied_raised_floor_reaches_no_adapter_function(monkeypatch, no_desktop, gate):
    """E: the whole point. A denial happens before anything is done to the computer."""
    asked, confirm = gate
    with pytest.raises(ActionDeniedError):
        executor.execute(ExecutorAction(OPEN_APP, "notepad"), confirm=confirm,
                         risk_floor=RiskLevel.CRITICAL)
    assert len(asked) == 1
    # no_desktop would have raised AssertionError if launch_app had been reached


def test_a_lower_advisory_floor_cannot_soften_a_real_executor_rule(monkeypatch, gate):
    """C, on a real rule rather than a fake one: a coordinate click is always at least MEDIUM, whatever
    the Brain thinks, and the prompt still says why."""
    asked, confirm = gate
    screens = [executor.Screen(0, 0, 1920, 1080, True)] if hasattr(executor, "Screen") else None
    if screens is None:
        pytest.skip("Screen shape not exposed here; covered by the executor's own click tests")
    monkeypatch.setattr(executor.verifier, "screens", lambda: screens)
    monkeypatch.setattr(executor.verifier, "window_at", lambda x, y: None)
    with pytest.raises(ActionDeniedError):
        executor.execute(ExecutorAction(CLICK, "500, 300"), confirm=confirm, risk_floor=RiskLevel.LOW)
    assert asked, "a click is always confirmed"
    assert asked[0][1].level >= RiskLevel.MEDIUM
    assert asked[0][1].rule != ADVISORY_FLOOR_REASON, "the click's own reason is preserved"


def test_a_lower_advisory_floor_cannot_soften_the_shortcut_table(monkeypatch, no_desktop, gate):
    """C: Ctrl+V is HIGH because it pastes something the assistant cannot see. An advisory LOW must not
    turn that into a silent action."""
    asked, confirm = gate
    with pytest.raises(ActionDeniedError):
        executor.execute(ExecutorAction(SHORTCUT, "ctrl+v"), confirm=confirm, risk_floor=RiskLevel.LOW)
    assert asked[0][1].level is RiskLevel.HIGH
    assert asked[0][1].rule != ADVISORY_FLOOR_REASON, "the shortcut's own reason is preserved"


# --- The retry path keeps the floor -------------------------------------------------------------------

def test_every_retry_is_asked_for_with_the_same_floor(monkeypatch, no_desktop):
    """F, and mandatory: a retry runs the whole pipeline again, so it must be as careful as the first
    attempt. A floor that leaked back to LOW on attempt two would be a silent downgrade."""
    levels = []

    def confirm(action, assessment):
        levels.append(assessment.level)
        return True

    attempts = []

    def prepare(action):
        attempts.append(1)
        return executor._Prepared(
            lambda: executor._result(action, False, "didn't work", retryable=True))

    monkeypatch.setattr(executor, "_PREPARERS", {**executor._PREPARERS, "fake": prepare})
    monkeypatch.setattr(executor, "_max_attempts", lambda: 3)
    result = executor.execute_with_recovery(ExecutorAction("fake", ""), confirm=confirm,
                                            offer_retry=lambda outcome: True,
                                            risk_floor=RiskLevel.HIGH)
    assert not result.ok
    assert len(attempts) == 3, "it really did retry"
    assert levels == [RiskLevel.HIGH] * 3, f"a retry lost the floor: {levels}"


def test_a_retry_without_a_floor_stays_at_the_preparers_own_level(monkeypatch, no_desktop):
    """The mirror image: no floor supplied means nothing changes on any attempt."""
    levels = []
    monkeypatch.setattr(executor, "_max_attempts", lambda: 2)
    monkeypatch.setattr(executor, "_PREPARERS",
                        {**executor._PREPARERS,
                         "fake": lambda action: executor._Prepared(
                             lambda: executor._result(action, False, "no", retryable=True),
                             Action("do the fake thing", minimum_level=RiskLevel.MEDIUM,
                                    minimum_reason="its own reason"))})
    executor.execute_with_recovery(ExecutorAction("fake", ""),
                                   confirm=lambda action, assessment: levels.append(assessment) or True,
                                   offer_retry=lambda outcome: True)
    assert [assessment.level for assessment in levels] == [RiskLevel.MEDIUM, RiskLevel.MEDIUM]
    assert {assessment.rule for assessment in levels} == {"its own reason"}


# --- The seam is additive --------------------------------------------------------------------------

def test_both_entry_points_keep_their_positional_signatures():
    """H: every existing caller - console, voice console, 600-odd tests - passes action and confirm
    positionally or by name, and must keep working untouched."""
    import inspect
    for function in (executor.execute, executor.execute_with_recovery):
        parameters = list(inspect.signature(function).parameters.values())
        assert parameters[0].name == "action"
        assert parameters[1].name == "confirm" and parameters[1].default is None
        floor = inspect.signature(function).parameters["risk_floor"]
        assert floor.kind is inspect.Parameter.KEYWORD_ONLY, "keyword-only, so it cannot be passed by accident"
        assert floor.default is RiskLevel.LOW, "the default must leave existing behaviour alone"


def test_the_advisory_floor_is_not_part_of_the_action_contract():
    """It is not something the assistant DOES, so it does not belong on ExecutorAction."""
    from dataclasses import fields
    # `control` carries the user's own word for an on-screen control for click_target, and is a field
    # rather than part of `target` so that log_label and plan_summary cannot see it. It is still not a
    # risk decision, which is what this test is about: the advisory floor stays out of the contract.
    # `url` joined in usability Slice 5, for the same privacy reason `control` exists: log_label
    # reports `target`, so an address that must stay out of the log cannot live there. The point of
    # this assertion is unchanged - risk_floor is still not a field on the action.
    assert [field.name for field in fields(ExecutorAction)] == ["kind", "target", "control", "url"]
    assert "risk_floor" not in [field.name for field in fields(ExecutorAction)]
    assert not any("risk" in field.name or "floor" in field.name
                   for field in fields(ExecutorAction))
    assert not hasattr(ExecutorAction(REFRESH, ""), "risk_floor")


def test_type_text_keeps_its_own_enter_floor(monkeypatch, gate):
    """One more real rule: text ending in Enter is HIGH because Enter can submit something. An advisory
    LOW cannot reduce it."""
    asked, confirm = gate
    monkeypatch.setattr(executor.verifier, "active_target",
                        lambda: (_ for _ in ()).throw(executor.verifier.VerifierUnavailableError("no window")))
    outcome = executor.execute(ExecutorAction(TYPE_TEXT, "hello\n"), confirm=confirm,
                               risk_floor=RiskLevel.LOW)
    # Either the gate was asked at HIGH, or preparation failed before the gate for want of a window -
    # both are acceptable here; what must never happen is being asked at LOW, or not asked at all.
    if asked:
        assert asked[0][1].level >= RiskLevel.HIGH
        assert asked[0][1].rule != ADVISORY_FLOOR_REASON
    else:
        assert not outcome.ok, "no window to type into, so nothing ran"
