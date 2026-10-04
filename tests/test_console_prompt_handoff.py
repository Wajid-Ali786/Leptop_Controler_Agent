"""
The last pre-Phase-5 usability fixes, from the owner's real typed-console session.

THREE THINGS THE REAL SESSION EXPOSED.

  A. "open powershell new window" came back with a MEDIUM advisory floor, so a plain open asked for
     confirmation. The floor mechanism was right; the system prompt never said opening an app is LOW.
  B. At "Try again? Type yes to retry", the owner typed `close powershell`. It was read as "not yes"
     and thrown away.
  C. At the correction prompt, `close explorer` became a PAID model call about a plan already abandoned.
  E. The Brain planned `shortcut Enter` for a new line, which is deliberately unsupported - while
     type_text already presses Enter for a line break.

B and C are one shape: a mini-prompt asking for information cannot tell an answer from a new command.
Safety's confirmation is NOT one of those prompts and is deliberately untouched - a command typed there
must never become a way to defer or bypass the yes-only rule, which the last section pins.
"""
import ast

import pytest

from app import console
from app.brain import logic as brain
from app.console import (CORRECTION_PROMPT, PendingCommand, Prompts, Status, is_fresh_command,
                         run_console)
from app.executor.logic import resolve
from app.executor.models import ActionResult, ExecutorAction, OPEN_APP
from app.safety.models import RiskLevel


class Script:
    """Scripted keyboard answers, plus everything written to the screen."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.lines = []
        self.asked = []

    def read(self, prompt=""):
        self.asked.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def write(self, text=""):
        self.lines.append(str(text))

    @property
    def output(self):
        return "\n".join(self.lines)


# --- A. The open_app risk guidance --------------------------------------------------------------------

def test_the_prompt_names_opening_an_app_as_a_low_example():
    """1. The gap that caused the phantom MEDIUM: `low` listed reading and moving a window, and said
    nothing about opening one, so the model had nothing to map an ordinary open onto."""
    prompt = brain.SYSTEM_PROMPT
    assert "low for opening an application" in prompt
    assert "ordinary and low" in prompt


def test_the_guidance_still_lets_the_model_raise_a_floor_for_a_real_reason():
    """The correction must not read as "never raise for an open". It says raise it when THAT intent
    carries its own reason - just not because the request was unusual or repeated."""
    prompt = brain.SYSTEM_PROMPT
    assert "raise it only when that particular intent carries its own" in prompt
    assert "not because the request was unusual or had to be repeated" in prompt
    assert "only ever a floor" in prompt, "the raise-only contract is still stated"


def test_nothing_caps_the_floor_in_code():
    """2, first half. The fix is prompt-only: no code clamps open_app to LOW, because that would also
    silence a floor the model raised for a good reason."""
    source = (console.__file__, brain.__file__)
    for path in source:
        text = open(path, encoding="utf-8").read()
        assert "RiskLevel.LOW)" not in text.replace("risk_floor: RiskLevel = RiskLevel.LOW", ""), path
    from app.executor import logic as executor_logic
    import inspect
    floor = inspect.getsource(executor_logic._with_advisory_floor)
    assert "advisory <= RiskLevel(safety_action.minimum_level)" in floor, "raise-only is unchanged"


def test_safety_remains_authoritative_and_the_floor_only_raises():
    """2, second half, behaviourally: a LOW advisory cannot soften a rule, and a MEDIUM one still has to
    pass the gate."""
    from app.executor import logic as executor_logic
    from app.safety.logic import assess
    from app.safety.models import Action
    low = executor_logic._with_advisory_floor(
        Action("press Ctrl+S", minimum_level=RiskLevel.HIGH, minimum_reason="saving"), RiskLevel.LOW)
    assert low.minimum_level is RiskLevel.HIGH, "an advisory LOW lowered a real rule"
    raised = executor_logic._with_advisory_floor(Action("open app notepad"), RiskLevel.MEDIUM)
    assert assess(raised).level is RiskLevel.MEDIUM


# --- E. The newline guidance --------------------------------------------------------------------------

def test_the_prompt_tells_the_model_to_use_type_text_for_a_new_line():
    """15. The capability already existed; the model reached for a shortcut instead."""
    prompt = brain.SYSTEM_PROMPT
    assert "use type_text with a line break in the text" in prompt
    assert "no Enter" in prompt and "shortcut is not how a line is ended" in prompt


def test_enter_was_not_added_to_the_supported_shortcuts():
    """16. Deliberately still unsupported: a bare Enter submits whatever happens to be focused."""
    from app.executor import shortcuts
    assert "Enter" not in shortcuts.SUPPORTED
    assert not any(name.lower() == "enter" for name in shortcuts.SUPPORTED)


def test_text_containing_a_line_break_keeps_its_higher_risk():
    """17. Unchanged: a line break is a real Enter press, so typing one stays the higher level."""
    from app.executor import logic as executor_logic
    assert executor_logic._ENTER_RISK is RiskLevel.HIGH
    assert "Enter" in executor_logic._ENTER_RISK_REASON
    assert executor_logic._TYPING_RISK is RiskLevel.MEDIUM


# --- B/C. What counts as a fresh command --------------------------------------------------------------

@pytest.mark.parametrize("line", ["open chrome", "close notepad", "open notepad", "help", "exit",
                                  "close powershell", "open the calculator", "  open chrome  "])
def test_a_recognisable_command_is_a_fresh_command(line):
    assert is_fresh_command(line) is True


@pytest.mark.parametrize("line", ["yes", "no", "cancel", "stop", "", "   ", "close",
                                  "use the other Ali", "the other one", "This text is mine",
                                  "Hello Wajid ka bad new line honi chaheya"])
def test_a_genuine_answer_is_not_mistaken_for_a_command(line):
    """The half that keeps the prompts usable. A correction and a clarification answer are free text, so
    treating every Brain-eligible line as a command would have broken both."""
    assert is_fresh_command(line) is False


def test_the_detector_reuses_the_existing_parser_and_routing():
    """"Do not invent a second command parser." It calls brain.route with the Executor's own resolve and
    reads the result - no regexes, no keyword list, no verb table of its own."""
    import inspect
    source = inspect.getsource(is_fresh_command)
    assert "brain.route(line, resolve)" in source
    tree = ast.parse(source.strip())
    called = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    assert "brain.route" in called
    for invented in ("re.compile", "re.match", "split", "startswith"):
        assert invented not in source, f"the detector does its own parsing: {invented}"


def test_only_two_words_are_the_consoles_own():
    """help and exit are handled by the loop itself, so the detector has to know them; nothing else is
    special-cased."""
    import inspect
    source = inspect.getsource(is_fresh_command)
    assert '("exit", "help")' in source


# --- The one-slot handoff -----------------------------------------------------------------------------

def test_the_slot_holds_one_line_and_gives_it_up_once():
    """4/6, at the mechanism. take() empties it, so a line can neither run twice nor be left behind."""
    pending = PendingCommand()
    assert pending.take() is None
    pending.put("open chrome")
    assert pending.take() == "open chrome"
    assert pending.take() is None, "the line survived being taken"


def test_a_later_line_replaces_an_untaken_one():
    pending = PendingCommand()
    pending.put("open chrome")
    pending.put("close chrome")
    assert pending.take() == "close chrome"
    assert pending.take() is None


# --- B. The retry prompt ------------------------------------------------------------------------------

def retry_prompt(*answers, pending=None):
    """The real _offer_retry, with a scripted keyboard."""
    script = Script(*answers)
    offer = console._offer_retry(script.read, script.write, pending)
    failed = ActionResult(ExecutorAction(OPEN_APP, "powershell"), False, "it didn't open")
    return offer(failed), script


def test_yes_still_retries():
    """3."""
    retried, _script = retry_prompt("yes")
    assert retried is True


@pytest.mark.parametrize("answer", ["no", "cancel", "stop", "anything else", ""])
def test_a_genuine_non_command_answer_still_stops(answer):
    """4. Unchanged semantics: only YES retries."""
    retried, _script = retry_prompt(answer, pending=PendingCommand())
    assert retried is False


def test_a_command_at_the_retry_prompt_is_handed_back_and_not_retried():
    """5 and 7. The owner's exact case: `close powershell` typed at the retry prompt. It must not be
    consumed as "not yes" and discarded, and the failed action must NOT be retried either."""
    pending = PendingCommand()
    retried, _script = retry_prompt("close powershell", pending=pending)
    assert retried is False, "the previous failed action was retried as well"
    assert pending.take() == "close powershell"


def test_without_a_slot_the_retry_prompt_behaves_exactly_as_before():
    """The voice console passes no slot, so its behaviour is untouched: a command there is still just
    "not yes"."""
    retried, _script = retry_prompt("close powershell", pending=None)
    assert retried is False


# --- C. The correction and clarification prompts ------------------------------------------------------

def ask_text(answer, prompt=CORRECTION_PROMPT, pending=None):
    script = Script(answer)
    prompts = Prompts(read=script.read, write=script.write, pending=pending)
    return console._ask_text(prompts, prompt), script


def test_a_genuine_correction_is_still_consumed_as_the_answer():
    """9. The free-text answer these prompts exist for."""
    answer, _script = ask_text("use the other Ali", pending=PendingCommand())
    assert answer == "use the other Ali"


def test_a_command_at_the_correction_prompt_is_handed_back_instead():
    """10. The owner's case: `close explorer` typed here became a paid model call about an abandoned
    plan. Now it is handed to the loop and the question reports "no answer"."""
    pending = PendingCommand()
    answer, _script = ask_text("close explorer", pending=pending)
    assert answer is None, "it was still used as the correction"
    assert pending.take() == "close explorer"


def test_a_brain_eligible_command_is_also_handed_back():
    """11. "close powershell" no longer resolves - powershell was removed from the config - so routing
    calls it Brain-eligible. It is still a recognised verb, so it is a command, not a correction."""
    pending = PendingCommand()
    route = brain.route("close powershell", resolve)
    assert isinstance(route, brain.BrainEligible) and route.parsed is not None
    answer, _script = ask_text("close powershell", pending=pending)
    assert answer is None
    assert pending.take() == "close powershell"


def test_handing_a_command_back_spends_nothing_on_the_abandoned_prompt():
    """12. _ask_text returning None is what the callers already treat as "no answer", which cancels -
    so no interpretation, no replan request, nothing billed."""
    import inspect
    source = inspect.getsource(console._ask_text)
    assert "prompts.pending.put(answer)" in source
    assert "return None" in source
    correction_site = inspect.getsource(console._offer_correction)
    assert "if not correction or not correction.strip():" in correction_site
    assert "session.cancel(context)" in correction_site, "a cancelled correction must not replan"


def test_without_a_slot_free_text_prompts_behave_exactly_as_before():
    answer, _script = ask_text("close explorer", pending=None)
    assert answer == "close explorer", "voice behaviour changed"


# --- The Safety boundary, deliberately untouched ------------------------------------------------------

def test_a_command_at_a_safety_confirmation_does_not_confirm_anything():
    """13. Only the exact word yes confirms. A command typed there is "anything else", which cancels -
    it must never become a way to defer or bypass the gate."""
    from app.safety.models import Action, RiskAssessment
    for typed in ("close explorer", "open chrome", "help", "exit", "no", "later"):
        script = Script(typed)
        confirm = console._confirm(script.read, script.write)
        allowed = confirm(Action("close app notepad"),
                          RiskAssessment(RiskLevel.MEDIUM, "closing can lose unsaved work"))
        assert allowed is False, f"{typed!r} confirmed a MEDIUM action"


def test_the_safety_confirmation_has_no_command_handoff_at_all():
    """14. Structural, not incidental: _confirm never sees the slot and cannot requeue anything, so the
    yes-only boundary has exactly one meaning."""
    import inspect
    source = inspect.getsource(console._confirm)
    assert "pending" not in source and "is_fresh_command" not in source
    signature = inspect.signature(console._confirm)
    assert list(signature.parameters) == ["read", "write"], signature


def test_only_the_two_information_prompts_consult_the_slot():
    """The handoff is narrow by construction: exactly two functions mention it."""
    import inspect
    source = inspect.getsource(console)
    users = [node.name for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.FunctionDef) and "is_fresh_command(" in ast.unparse(node)
             and node.name != "is_fresh_command"]
    assert sorted(users) == ["_ask_text", "_offer_retry", "offer_retry"], users
    assert "offer_retry" in users and "_offer_retry" in users, (
        "offer_retry is the closure inside _offer_retry, so both names appear")


# --- D. Exactly once, through the real loop -----------------------------------------------------------

def test_a_command_typed_at_the_retry_prompt_runs_once_through_the_real_loop(monkeypatch):
    """6, 8 and D, end to end through the REAL run_console loop.

    A failing action offers a retry; the user types a real command there; the loop must run it exactly
    once, must NOT retry the failed action, and - being deterministic - must cost no model call. The
    interpreter is deliberately NOT stubbed: if a deterministic line reached it, the autouse provider
    guard would raise rather than let it pass quietly."""
    ran = []

    def executor(action, confirm=None, offer_retry=None, *, risk_floor=RiskLevel.LOW):
        ran.append(action.target)
        if action.target == "calculator":
            result = ActionResult(action, False, "calculator didn't open", retryable=True)
            if offer_retry is not None:
                offer_retry(result)              # the real prompt reads the next scripted line
            return result
        return ActionResult(action, True, f"did {action.kind} {action.target}")

    monkeypatch.setattr(console, "execute_with_recovery", executor)
    monkeypatch.setattr(console.emergency_stop, "is_stopped", lambda: False)

    script = Script(
        "open calculator",     # fails, and offers a retry
        "close notepad",       # typed AT the retry prompt - a fresh command
        "exit",
    )
    code = run_console(read=script.read, write=script.write, focus=_NoFocus())

    assert code == 0
    assert ran == ["calculator", "notepad"], f"ran {ran}"
    assert ran.count("calculator") == 1, "the failed action was retried as well"
    assert ran.count("notepad") == 1, "the handed-back command ran more than once"
    assert "Try again?" in "".join(script.asked)
    assert script.asked[-1] == "> ", "the loop went back to its own prompt afterwards"


def test_the_handed_back_command_is_not_left_in_the_slot(monkeypatch):
    """D's last clause: nothing stays in a hidden queue after execution."""
    pending_seen = []

    def executor(action, confirm=None, offer_retry=None, *, risk_floor=RiskLevel.LOW):
        if action.target == "calculator" and offer_retry is not None:
            offer_retry(ActionResult(action, False, "no", retryable=True))
        return ActionResult(action, True, "ok")

    monkeypatch.setattr(console, "execute_with_recovery", executor)
    monkeypatch.setattr(console.emergency_stop, "is_stopped", lambda: False)
    real_pending = console.PendingCommand

    class Watched(console.PendingCommand):
        def take(self):
            line = super().take()
            pending_seen.append(line)
            return line

    monkeypatch.setattr(console, "PendingCommand", Watched)
    script = Script("open calculator", "close notepad", "exit")
    run_console(read=script.read, write=script.write, focus=_NoFocus())
    monkeypatch.setattr(console, "PendingCommand", real_pending)

    assert "close notepad" in pending_seen, "the command was never taken from the slot"
    assert pending_seen[-1] is None, f"something was left in the slot: {pending_seen}"


class _NoFocus:
    def note_console_window(self):
        pass

    def hand_over(self, *args, **kwargs):
        return True
