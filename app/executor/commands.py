"""
The typed-command grammar: text -> ExecutorAction (docs/step4 Section 4, Phase 1).

    parse("open notepad")  -> ExecutorAction(OPEN_APP, "notepad")
    parse("close")         -> CommandRefusal("ambiguous", "Close what? ...")

Deliberately deterministic: a line either matches the grammar or is refused. There is no Claude,
no fuzzy matching and no closest-command guessing here, and no Urdu, Roman Urdu or Hindi yet -
understanding loosely worded commands is Phase 3 (Brain + Planner).

This module only chooses the action and hands on the raw target. Whether that target is usable
stays the Executor's job, so "click abc", "shortcut ctrl+q" or a bare "open" become actions whose
own preparer refuses them, with no side effects and nothing asked. The parser checks only what the
grammar itself decides: a bare "close" (close an app, or the active window?), words after a window
control, and the quoting rule for typed text.

It is pure text handling: it imports nothing but the Executor's data shapes, reads no window and
sends no input. Nothing runs from here - app/console.py passes what comes back to the normal
Executor pipeline (validation, safety gate, emergency stop, Verifier).

Privacy: the text after `type` is never logged, never repeated in a refusal and never put in a
repr (see ExecutorAction.log_label). No refusal message quotes the line it refused, so a mistyped
command can't leak its contents either. Logs record the action kind, or the kind of refusal.
"""
import logging
import re
from dataclasses import dataclass

from app.executor.models import CLICK, CLOSE_APP, OPEN_APP, REFRESH, SCROLL, SHORTCUT, TYPE_TEXT, WINDOW_CONTROL, \
    ExecutorAction

EMPTY = "empty"          # nothing but whitespace
UNKNOWN = "unknown"      # not a command
AMBIGUOUS = "ambiguous"  # a command that could mean two things - never guessed
MALFORMED = "malformed"  # the right command word, wrongly written

TYPE_VERB = "type"
WINDOW_WORD = "window"  # reserved: "close window" is never an app called "window"
QUOTE = '"'

_LINE = re.compile(r"(\S+)(?:\s+(.*))?", re.DOTALL)
_SIMPLE_VERBS = {"open": OPEN_APP, "click": CLICK, "shortcut": SHORTCUT, "scroll": SCROLL, "refresh": REFRESH}
_WINDOW_OPERATIONS = ("minimize", "maximize", "restore")  # "close" is handled with close_app

HELP = """Commands (one per line; the command word is not case-sensitive):
  open <app>                    open notepad
  close <app>                   close notepad
  close window                  close the active window (only windows I opened this session)
  click <x>, <y>                click 500, 300
  type <text>                   type hello world
  type "<text>"                 type "  spaces kept exactly  "
  shortcut <keys>               shortcut ctrl+a
  scroll up|down <notches>      scroll down 3
  refresh                       refresh the active window
  minimize [window]             the active window; also maximize, restore
  help                          this list
  exit                          leave the console"""

UNKNOWN_MESSAGE = "I don't recognise that command, so I did nothing. Type help to see the commands."
EMPTY_MESSAGE = "Type a command, or help to see the commands."
AMBIGUOUS_CLOSE = ("Close what? Say close <app> (for example: close notepad), or close window for the active "
                   "window. Nothing was done.")
UNCLOSED_QUOTE = ("The text starts with a quote but doesn't end with one. To type text that starts with a quote, "
                  "put all of it in quotes. Nothing was typed.")

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandRefusal:
    """The line is not a command that can be run. Nothing happens."""
    kind: str     # empty | unknown | ambiguous | malformed - safe to log
    message: str  # plain English, safe to show; never contains the line it refused


def parse(text: str) -> ExecutorAction | CommandRefusal:
    """The action `text` asks for, or a CommandRefusal saying why there isn't one."""
    if not isinstance(text, str) or not text.strip():
        return _refuse(EMPTY, EMPTY_MESSAGE)
    match = _LINE.fullmatch(text.strip())
    verb, rest = match.group(1).lower(), match.group(2) or ""
    if verb == TYPE_VERB:  # everything after "type" is text, so it is never split into words
        return _parse_type(rest)
    target = " ".join(rest.split())  # runs of whitespace count as one; the target's own case is kept
    if verb == "close":
        if not target:
            return _refuse(AMBIGUOUS, AMBIGUOUS_CLOSE)
        if target.lower().split()[0] == WINDOW_WORD:
            return _window_control("close", target)
        return _parsed(ExecutorAction(CLOSE_APP, target))
    if verb in _WINDOW_OPERATIONS:
        return _window_control(verb, target)
    if verb in _SIMPLE_VERBS:
        return _parsed(ExecutorAction(_SIMPLE_VERBS[verb], target))
    return _refuse(UNKNOWN, UNKNOWN_MESSAGE)


def _parse_type(rest: str) -> ExecutorAction | CommandRefusal:
    """`type hello` types hello; `type "  hello  "` keeps the spaces. No escape characters, so a line
    break can't be typed from a command in Phase 1."""
    if rest.startswith(QUOTE):
        if len(rest) < 2 or not rest.endswith(QUOTE):
            return _refuse(MALFORMED, UNCLOSED_QUOTE)
        return _parsed(ExecutorAction(TYPE_TEXT, rest[1:-1]))
    return _parsed(ExecutorAction(TYPE_TEXT, rest.strip()))


def _window_control(operation: str, target: str) -> ExecutorAction | CommandRefusal:
    """A window control acts on the active window, so nothing may follow it except the word "window"."""
    written = f"{operation} {WINDOW_WORD}" if operation == "close" else operation
    if target.lower() not in ("", WINDOW_WORD):
        return _refuse(MALFORMED, f"{written} acts on the active window and takes nothing after it. "
                                  f"Say {written}. Nothing was done.")
    return _parsed(ExecutorAction(WINDOW_CONTROL, operation))


def _parsed(action: ExecutorAction) -> ExecutorAction:
    log.info("Typed command parsed: %s", action.kind)  # the kind only - never the line or the text
    return action


def _refuse(kind: str, message: str) -> CommandRefusal:
    log.info("Typed command refused: %s", kind)
    return CommandRefusal(kind, message)
