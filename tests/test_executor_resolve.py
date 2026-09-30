"""
Target resolution (Phase 3 Slice 1A) - and proof that lifting it out of the preparers changed nothing.

resolve() is the one place that answers "can this target be used?". It existed before as eight separate
checks at the top of eight preparers; those checks now live here and the preparers call them. This file
therefore has two jobs:

  1. test resolve() itself, per kind, valid and invalid;
  2. prove the refactor is invisible - every user-visible failure message is BYTE-IDENTICAL to the one
     recorded from the code before the change. Those sentences were tuned during Phase 1 acceptance and
     are quoted in the closeout docs, so "equivalent" is not good enough.

Nothing here opens a window, reads the active window or executes anything: the invalid-target cases
return before the preparer's first verifier call, which is exactly the property resolve() formalises.
"""
import pytest

from app.executor import logic, shortcuts
from app.executor.models import (CLICK, CLOSE_APP, OPEN_APP, REFRESH, RESOLVE_BAD_FORMAT,
                                 RESOLVE_NO_TARGET, RESOLVE_OUT_OF_RANGE, RESOLVE_UNKNOWN_APP,
                                 RESOLVE_UNKNOWN_KIND, RESOLVE_UNWANTED_TARGET, SCROLL, SHORTCUT,
                                 TYPE_TEXT, WINDOW_CONTROL, ExecutorAction, Resolved, Unresolved)

# Recorded from app/executor/logic.py BEFORE resolve() existed, by calling each preparer with an
# invalid target (which returns before any verifier call). Byte-for-byte; do not reword.
BASELINE = {
    (OPEN_APP, ""): "Which app should I open?",
    (OPEN_APP, "the calculator"):
        "I don't know an app called 'the calculator'. Apps I can open: calculator, notepad.",
    (OPEN_APP, "it"): "I don't know an app called 'it'. Apps I can open: calculator, notepad.",
    (CLOSE_APP, ""): "Which app should I close?",
    (CLOSE_APP, "everything"):
        "I don't know an app called 'everything'. Apps I can close: calculator, notepad.",
    (CLOSE_APP, "the editor"):
        "I don't know an app called 'the editor'. Apps I can close: calculator, notepad.",
    (CLICK, ""): "Where should I click? Give screen coordinates as x, y (e.g. 500, 300).",
    (CLICK, "the blue button"):
        "I can't click at 'the blue button': give whole-number screen coordinates as x, y (e.g. 500, 300).",
    (CLICK, "500"):
        "I can't click at '500': give whole-number screen coordinates as x, y (e.g. 500, 300).",
    (SCROLL, ""): "Which way and how far should I scroll? For example: down 3.",
    (SCROLL, "down"): "How many notches? For example: down 3.",
    (SCROLL, "a bit"):
        "I can't read 'a bit': say up or down and a number of notches. For example: down 3.",
    (SCROLL, "left 3"): "Horizontal scrolling isn't supported yet.",
    (SCROLL, "-3"): "Say up or down instead of + or -. For example: down 3.",
    (SCROLL, "down 0"): "Scroll at least 1 notch.",
    (SCROLL, "down 21"): "That's 21 notches; I scroll at most 20 at once.",
    (SHORTCUT, ""): "Which shortcut should I press? For example: Ctrl+C.",
    (SHORTCUT, "copy"): "I don't know a key called 'copy'.",
    (SHORTCUT, "ctrl+"): "I can't read the shortcut 'ctrl+': join key names with +, e.g. Ctrl+C.",
    (WINDOW_CONTROL, ""): "Which window control? minimize, maximize, restore or close.",
    (WINDOW_CONTROL, "sideways"):
        "I can't do 'sideways' to a window. Window controls: minimize, maximize, restore or close.",
    (TYPE_TEXT, ""): "What should I type?",
    (TYPE_TEXT, "x" * 1001): "That's 1001 characters; I can type at most 1000 at once.",
    (TYPE_TEXT, "a\tb"):
        "I can't type that: it contains Tab or another control key, which isn't text "
        "(keyboard shortcuts come later).",
    (REFRESH, "the page"): "Refresh doesn't take a target; it refreshes the active window.",
}


def resolve(kind, target):
    return logic.resolve(ExecutorAction(kind, target))


# --- The messages did not move -------------------------------------------------------------------------

@pytest.mark.parametrize("case", list(BASELINE), ids=lambda case: f"{case[0]}-{case[1][:18]!r}")
def test_the_preparer_still_says_exactly_what_it_said_before_the_refactor(case):
    """The refactor's whole promise: same question, same words."""
    kind, target = case
    outcome = logic._PREPARERS[kind](ExecutorAction(kind, target))
    assert outcome.message == BASELINE[case]
    assert outcome.ok is False


@pytest.mark.parametrize("case", list(BASELINE), ids=lambda case: f"{case[0]}-{case[1][:18]!r}")
def test_resolve_gives_the_preparer_that_exact_message(case):
    """One source of truth: the preparer's sentence IS resolve()'s sentence."""
    kind, target = case
    outcome = resolve(kind, target)
    assert isinstance(outcome, Unresolved)
    assert outcome.message == BASELINE[case]


# --- resolve(), kind by kind ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", [OPEN_APP, CLOSE_APP])
@pytest.mark.parametrize("target, value", [("notepad", "notepad"), ("Notepad", "notepad"),
                                           ("  CALCULATOR  ", "calculator")])
def test_a_configured_app_resolves_to_its_canonical_name(kind, target, value):
    assert resolve(kind, target) == Resolved(value)


@pytest.mark.parametrize("kind", [OPEN_APP, CLOSE_APP])
def test_an_unknown_app_does_not_resolve(kind):
    outcome = resolve(kind, "photoshop")
    assert isinstance(outcome, Unresolved) and outcome.reason == RESOLVE_UNKNOWN_APP
    assert "photoshop" in outcome.message


@pytest.mark.parametrize("target, value", [("500, 300", (500, 300)), ("(500, 300)", (500, 300)),
                                           ("-4,-9", (-4, -9)), ("0 , 0", (0, 0))])
def test_valid_click_coordinates_resolve_to_numbers(target, value):
    assert resolve(CLICK, target) == Resolved(value)


@pytest.mark.parametrize("target", ["", "the blue button", "500", "500, 300, 700", "a, b", "500;300"])
def test_invalid_click_coordinates_do_not_resolve(target):
    assert isinstance(resolve(CLICK, target), Unresolved)


@pytest.mark.parametrize("target, value", [("down 3", ("down", 3)), ("UP 1", ("up", 1)),
                                           ("  down   20 ", ("down", 20))])
def test_valid_scroll_requests_resolve(target, value):
    assert resolve(SCROLL, target) == Resolved(value)


@pytest.mark.parametrize("target, reason", [("", RESOLVE_BAD_FORMAT), ("down", RESOLVE_BAD_FORMAT),
                                            ("left 3", RESOLVE_BAD_FORMAT),
                                            ("down 21", RESOLVE_OUT_OF_RANGE)])
def test_invalid_scroll_requests_do_not_resolve(target, reason):
    outcome = resolve(SCROLL, target)
    assert isinstance(outcome, Unresolved) and outcome.reason == reason


def test_a_valid_shortcut_resolves_to_the_parsed_shortcut():
    outcome = resolve(SHORTCUT, "ctrl+a")
    assert isinstance(outcome, Resolved)
    assert isinstance(outcome.value, shortcuts.Shortcut)
    assert outcome.value.name == "Ctrl+A"


@pytest.mark.parametrize("target", ["", "copy", "ctrl+", "ctrl+shift+"])
def test_an_unreadable_shortcut_does_not_resolve(target):
    assert isinstance(resolve(SHORTCUT, target), Unresolved)


@pytest.mark.parametrize("target", ["minimize", "maximize", "restore", "close", "  MINIMIZE "])
def test_a_known_window_operation_resolves(target):
    assert resolve(WINDOW_CONTROL, target) == Resolved(" ".join(target.lower().split()))


@pytest.mark.parametrize("target", ["", "sideways", "minimise"])
def test_an_unknown_window_operation_does_not_resolve(target):
    assert isinstance(resolve(WINDOW_CONTROL, target), Unresolved)


@pytest.mark.parametrize("target, value", [("hello", "hello"), ("  keep  spaces  ", "  keep  spaces  "),
                                           ("a\r\nb", "a\nb"), ("نوٹ پیڈ", "نوٹ پیڈ"),
                                           ("x" * 1000, "x" * 1000)])
def test_typed_text_resolves_verbatim_apart_from_line_endings(target, value):
    """The payload is the user's own words: only \\r\\n is normalised, nothing else is touched."""
    assert resolve(TYPE_TEXT, target) == Resolved(value)


@pytest.mark.parametrize("target, reason", [("", RESOLVE_NO_TARGET), ("x" * 1001, RESOLVE_OUT_OF_RANGE),
                                            ("a\tb", RESOLVE_BAD_FORMAT)])
def test_typed_text_that_breaks_a_current_limit_does_not_resolve(target, reason):
    outcome = resolve(TYPE_TEXT, target)
    assert isinstance(outcome, Unresolved) and outcome.reason == reason


def test_refresh_resolves_only_without_a_target():
    assert resolve(REFRESH, "") == Resolved(None)
    assert resolve(REFRESH, "   ") == Resolved(None)
    outcome = resolve(REFRESH, "the page")
    assert isinstance(outcome, Unresolved) and outcome.reason == RESOLVE_UNWANTED_TARGET


def test_an_unimplemented_kind_does_not_resolve():
    outcome = resolve("teleport", "mars")
    assert isinstance(outcome, Unresolved) and outcome.reason == RESOLVE_UNKNOWN_KIND


def test_every_implemented_kind_has_a_resolver():
    """A new Executor capability must not silently become unresolvable."""
    assert set(logic._RESOLVERS) == set(logic._PREPARERS)


# --- It must stay side-effect free ---------------------------------------------------------------------

def test_resolve_reads_no_window_and_runs_nothing(monkeypatch):
    """The property the router depends on: resolve() may read config and nothing else."""
    from app.verifier import logic as verifier

    def refuse(*args, **kwargs):
        raise AssertionError("resolve() must not read the desktop")

    for name in ("active_target", "screens", "window_at", "snapshot_windows", "find_open",
                 "modifiers_held", "control_chain_at_pointer", "window_state", "clipboard_kinds"):
        monkeypatch.setattr(verifier, name, refuse, raising=False)
    monkeypatch.setattr(logic.adapter, "launch_app", refuse, raising=False)
    monkeypatch.setattr(logic.adapter, "type_text", refuse, raising=False)
    for kind, target in (*BASELINE, (OPEN_APP, "notepad"), (CLICK, "10, 10"), (SCROLL, "down 2"),
                         (SHORTCUT, "ctrl+c"), (WINDOW_CONTROL, "minimize"), (TYPE_TEXT, "hi"),
                         (REFRESH, "")):
        logic.resolve(ExecutorAction(kind, target))     # must not raise


def test_resolve_never_touches_the_source_action():
    """It is a question about the action, not a change to it - the action is frozen anyway, and the
    canonical value comes back separately rather than being written into it."""
    action = ExecutorAction(OPEN_APP, "  Notepad  ")
    outcome = logic.resolve(action)
    assert outcome == Resolved("notepad")
    assert action.target == "  Notepad  ", "the caller's action is unchanged"
