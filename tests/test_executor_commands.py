"""
Tests for the typed-command grammar (app/executor/commands.py): text -> ExecutorAction.

The parser is pure text handling, so these tests need no desktop at all. They check that every
supported form maps to exactly one action, that ambiguous, malformed and unknown lines are refused
without guessing, that typed text keeps its spaces and never leaks, and that the parser can't act
on the computer: it imports nothing but the Executor's data shapes.
"""
import ast
import logging
from pathlib import Path

import pytest

from app.executor import adapter, commands
from app.executor import logic as executor_logic
from app.executor.commands import AMBIGUOUS, EMPTY, MALFORMED, UNKNOWN, CommandRefusal, parse
from app.executor.models import CLICK, CLOSE_APP, OPEN_APP, REFRESH, SCROLL, SHORTCUT, TYPE_TEXT, WINDOW_CONTROL, \
    ExecutorAction
from app.verifier import adapter as verifier_adapter

SECRET = "hunter2-correct-horse-battery"

SUPPORTED = [
    ("open notepad", OPEN_APP, "notepad"),
    ("open", OPEN_APP, ""),                              # the Executor asks which app
    ("close notepad", CLOSE_APP, "notepad"),
    ("close window", WINDOW_CONTROL, "close"),
    ("click 500, 300", CLICK, "500, 300"),
    ("click (500, 300)", CLICK, "(500, 300)"),
    ("click", CLICK, ""),
    ("type hello", TYPE_TEXT, "hello"),
    ("shortcut ctrl+a", SHORTCUT, "ctrl+a"),
    ("shortcut", SHORTCUT, ""),
    ("scroll down 3", SCROLL, "down 3"),
    ("scroll", SCROLL, ""),
    ("refresh", REFRESH, ""),
    ("minimize", WINDOW_CONTROL, "minimize"),
    ("minimize window", WINDOW_CONTROL, "minimize"),
    ("maximize", WINDOW_CONTROL, "maximize"),
    ("maximize window", WINDOW_CONTROL, "maximize"),
    ("restore", WINDOW_CONTROL, "restore"),
    ("restore window", WINDOW_CONTROL, "restore"),
]


@pytest.mark.parametrize("line, kind, target", SUPPORTED)
def test_every_supported_command_maps_to_one_action(line, kind, target):
    assert parse(line) == ExecutorAction(kind, target)


@pytest.mark.parametrize("line, kind, target", [
    ("  open   notepad  ", OPEN_APP, "notepad"),
    ("OPEN Notepad", OPEN_APP, "Notepad"),               # the command word only is case-insensitive
    ("Close WINDOW", WINDOW_CONTROL, "close"),
    ("CLOSE   Notepad", CLOSE_APP, "Notepad"),
    ("\tMINIMIZE\tWindow\t", WINDOW_CONTROL, "minimize"),
    ("click    500 ,    300", CLICK, "500 , 300"),
    ("Scroll DOWN 3", SCROLL, "DOWN 3"),
    ("ReFrEsH", REFRESH, ""),
])
def test_case_and_extra_whitespace(line, kind, target):
    assert parse(line) == ExecutorAction(kind, target)


# --- Nothing is guessed --------------------------------------------------------------------------

@pytest.mark.parametrize("line", ["", "   ", "\t", "\n", None, 5])
def test_nothing_to_do(line):
    refusal = parse(line)
    assert refusal == CommandRefusal(EMPTY, commands.EMPTY_MESSAGE)


def test_bare_close_is_ambiguous_and_never_guesses():
    refusal = parse("close")
    assert refusal.kind == AMBIGUOUS and refusal.message == commands.AMBIGUOUS_CLOSE
    assert "close notepad" in refusal.message and "close window" in refusal.message


@pytest.mark.parametrize("line, message", [
    ("minimize notepad", "minimize acts on the active window and takes nothing after it. Say minimize. "
                         "Nothing was done."),
    ("maximize that window please", "maximize acts on the active window and takes nothing after it. Say maximize. "
                                    "Nothing was done."),
    ("restore 2", "restore acts on the active window and takes nothing after it. Say restore. Nothing was done."),
    ("close window now", "close window acts on the active window and takes nothing after it. Say close window. "
                         "Nothing was done."),
])
def test_a_window_control_takes_nothing_after_it(line, message):
    refusal = parse(line)
    assert refusal.kind == MALFORMED and refusal.message == message


@pytest.mark.parametrize("line", [
    "opne notepad", "hello", "typehello", "minimise", "maximise window", "please open notepad", "open-notepad",
    "notepad kholo", "band karo", "закрыть", "exit", "help", "-", "500, 300", "openn", "clos window",
])
def test_unknown_commands_run_nothing_and_never_pick_the_closest(line):
    refusal = parse(line)
    assert refusal.kind == UNKNOWN
    assert refusal.message == commands.UNKNOWN_MESSAGE  # always the same words: no hint of a nearest match


def test_malformed_targets_are_the_executors_job_not_the_parsers():
    """The parser hands on the raw target; the Executor's own preparer refuses it (and asks nothing)."""
    for line, kind, target in [("click abc", CLICK, "abc"), ("click 500.5, 300", CLICK, "500.5, 300"),
                               ("scroll sideways", SCROLL, "sideways"), ("scroll 3", SCROLL, "3"),
                               ("shortcut ctrl+q", SHORTCUT, "ctrl+q"), ("shortcut alt+f4", SHORTCUT, "alt+f4"),
                               ("open nonexistentapp123", OPEN_APP, "nonexistentapp123"),
                               ("refresh now", REFRESH, "now")]:
        assert parse(line) == ExecutorAction(kind, target), line


# --- Type text -----------------------------------------------------------------------------------

@pytest.mark.parametrize("line, text", [
    ("type hello world", "hello world"),
    ("type hello  world", "hello  world"),          # spaces inside the text are kept exactly
    ("type   hello  ", "hello"),                    # stray spaces around it are not typed
    ('type "  hello  "', "  hello  "),              # quotes keep them
    ('type ""', ""),                                # the Executor then asks what to type
    ('type "say ""hi"""', 'say ""hi""'),            # only the outer pair is the quoting
    ('type ""quoted""', '"quoted"'),
    ('type she said "hi"', 'she said "hi"'),        # a quote that isn't first is just text
    ("Type Hello", "Hello"),                        # the text keeps its case
    ("TYPE ctrl+a", "ctrl+a"),
    ("type close window", "close window"),          # never re-read as a command
    ("type open notepad", "open notepad"),
    ("type", ""),
    ("type C:\\new\\file.txt", "C:\\new\\file.txt"),  # no escape characters, so a backslash is a backslash
    ("type \\n", "\\n"),
    ("type 5", "5"),
])
def test_type_text_rules(line, text):
    assert parse(line) == ExecutorAction(TYPE_TEXT, text)


@pytest.mark.parametrize("line", ['type "hello', 'type "', f'type "{SECRET}'])
def test_an_unclosed_quote_is_refused_without_repeating_the_text(line):
    refusal = parse(line)
    assert refusal.kind == MALFORMED and refusal.message == commands.UNCLOSED_QUOTE
    assert SECRET not in refusal.message


def test_a_tab_inside_typed_text_is_passed_on_for_the_executor_to_refuse():
    assert parse("type a\tb") == ExecutorAction(TYPE_TEXT, "a\tb")


# --- Privacy -------------------------------------------------------------------------------------

@pytest.mark.parametrize("line", [
    f"type {SECRET}", f'type "{SECRET}"', f'type "{SECRET}', f"tpye {SECRET}", f"minimize {SECRET}",
    f"TYPE {SECRET} and more", f'type "  {SECRET}  "',
])
def test_typed_text_never_reaches_logs_refusals_or_reprs(caplog, line):
    with caplog.at_level(logging.DEBUG):
        parsed = parse(line)
    written = caplog.text + repr(parsed) + str(getattr(parsed, "message", "")) + repr(caplog.records)
    assert SECRET not in written, written
    if isinstance(parsed, ExecutorAction) and parsed.kind == TYPE_TEXT:
        assert parsed.target and SECRET in parsed.target  # it really did parse the text it must not leak


def test_an_app_name_stays_in_the_action_as_the_executor_expects(caplog):
    """Only typed TEXT is private. An app name is the Executor's target, which it logs as it always
    has ("I don't know an app called ..."), so the parser passes it on unchanged."""
    with caplog.at_level(logging.INFO):
        action = parse(f"close {SECRET}")
    assert action == ExecutorAction(CLOSE_APP, SECRET)
    assert SECRET not in caplog.text  # the PARSER still logs the kind only


def test_the_privacy_check_would_catch_a_leak(caplog):
    with caplog.at_level(logging.DEBUG):
        logging.getLogger(__name__).info("planted leak: %s", SECRET)
    assert SECRET in caplog.text


def test_logs_record_only_the_kind(caplog):
    with caplog.at_level(logging.INFO):
        parse("open notepad")
        parse("close")
    assert "Typed command parsed: open_app" in caplog.text
    assert "Typed command refused: ambiguous" in caplog.text
    assert "notepad" not in caplog.text


# --- The parser cannot act on the computer -------------------------------------------------------

def test_the_parser_imports_nothing_that_can_act():
    source = Path(commands.__file__).read_text(encoding="utf-8")
    modules = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
    assert modules == {"logging", "re", "dataclasses", "app.executor.models"}, modules


def test_parsing_touches_no_adapter_and_runs_nothing(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the parser must not act on the computer")
    for module, names in ((adapter, ("launch_app", "request_close", "click", "type_text", "press_keys",
                                     "send_wheel_notch", "request_window_state")),
                          (verifier_adapter, ("active_target", "list_windows", "window_at", "cursor_position")),
                          (executor_logic, ("execute", "execute_with_recovery"))):
        for name in names:
            if hasattr(module, name):
                monkeypatch.setattr(module, name, forbidden)
    for line, _, _ in SUPPORTED:
        parse(line)
    for line in ("close", "minimize notepad", 'type "x', "nonsense", f"type {SECRET}"):
        parse(line)


def test_help_lists_every_command_word():
    for word in ("open", "close", "close window", "click", "type", "shortcut", "scroll", "refresh",
                 "minimize", "help", "exit"):
        assert word in commands.HELP
