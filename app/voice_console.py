"""
The spoken-command console (docs/step4 Section 5, Phase 2) - orchestration only.

One utterance at a time:

    listen -> Transcript -> derived forms -> YOU accept it -> app.console.handle_command()

Nothing here parses a command, classifies risk, activates a window or acts on the computer. Every
accepted command goes through the SAME typed pipeline as app/console.py, including the safety gate,
the emergency stop, the Executor and the Verifier. This module is deliberately not a second
Executor: the only Executor-facing calls it makes are app.console.handle_command() and the pure
app.console.preview().

Why every spoken command is accepted explicitly, even a Low-risk one
    Speech recognition is not a keyboard. The Feature 4 acceptance runs produced " open notepad
    calculator" and " click type click type" from phrases nobody said - and both of those parse as
    real commands. So nothing spoken runs until you have seen it and said so. Acceptance is not the
    safety gate: Medium risk and above still asks its own question, on the KEYBOARD, afterwards.

The one invariant everything else rests on
    The exact string shown as "COMMAND TO ACCEPT" is the exact string handed to handle_command().
    Not a tidied copy, not a stripped copy. If any layer normalized text between what you read and
    what runs, the gate would be theatre. That is why the pending candidate starts as the RAW
    transcript text, why a correction replaces one token's span instead of rebuilding the line, and
    why a mechanically proposed alternative has to be accepted again on its own.

Privacy: what was said is shown to YOU on purpose - that is the whole point of the gate - and is
never logged. Log lines here carry counts and command kinds only.
"""
import logging

from app import console
from app.listener import logic
from app.listener.models import PendingCommand, Transcript, VoiceFailure

log = logging.getLogger(__name__)

ACCEPT, CORRECT, REDICTATE, CANCEL = "accept", "correct", "redictate", "cancel"
# Full words, plus one unique short key each. "c" is deliberately not a key: it would be ambiguous
# between correct and cancel. "yes", "ok", "go" and "confirm" are not acceptance either - they are
# the words of the SAFETY confirmation, which happens later and on the keyboard.
CHOICES = {"accept": ACCEPT, "a": ACCEPT,
           "correct": CORRECT, "e": CORRECT,
           "redictate": REDICTATE, "r": REDICTATE,
           "cancel": CANCEL, "x": CANCEL}
CHOICE_PROMPT = "accept (a) / correct (e) / redictate (r) / cancel (x): "
CHOICE_AGAIN = f"Please answer with one of: {', '.join(sorted(set(CHOICES) - set('aerx')))}."

WELCOME = ("AI Desktop Companion - spoken commands (Phase 2). Type listen to say one command, "
           "exit to leave.\nNothing spoken ever runs until you accept it, and anything Medium risk "
           "or above still asks you to type yes.")
HELP = "Type listen to say one command, or exit to leave."
CANCELLED = "Cancelled - nothing was run."
NOTHING_TO_CORRECT = "There is nothing in that to correct."
NO_SUCH_TOKEN = "There is no part numbered {number} - nothing was changed."
EMPTY_REPLACEMENT = "A replacement can't be empty - nothing was changed."
NOT_A_COMMAND = ("That isn't a command I know, so nothing was run. You can correct it, say it "
                 "again, or cancel.")
AMBIGUOUS = ("Those two readings would do different things, so nothing was run. Choose which one "
             "you meant (or cancel) - I won't choose for you.")


def run_voice_console(listen, read=input, write=print, focus=None) -> int:
    """Take spoken commands one at a time until the user leaves. Returns an exit code.

    `listen` is the one injected seam: it returns a Transcript or a VoiceFailure for ONE bounded
    utterance, and owns the microphone, the model and the recording notices. Keeping it out of here
    is what makes this whole loop testable with no microphone and no speech model."""
    write(WELCOME)
    confirm = console.typed_confirmation(read, write)
    offer_retry = console.typed_retry_offer(read, write)
    while True:
        try:
            line = read("voice> ")
        except (EOFError, KeyboardInterrupt):
            write("")
            return 0
        word = line.strip().lower()
        if word in ("exit", "quit"):
            return 0
        if not word:
            continue
        if word != "listen":
            write(HELP)
            continue
        _one_utterance(listen, read, write, focus, confirm, offer_retry)


def _one_utterance(listen, read, write, focus, confirm, offer_retry) -> None:
    """Listen, show, let the user accept/correct/redictate/cancel, and run at most one command."""
    while True:  # redictating comes back here; nothing from the old attempt survives it
        heard = listen()
        if isinstance(heard, VoiceFailure):
            write(heard.message)
            return
        if not isinstance(heard, Transcript):
            raise TypeError(f"listen() must return a Transcript or a VoiceFailure, got "
                            f"{type(heard).__name__}")
        pending = PendingCommand(logic.derive_transcript(heard), heard.text)
        while True:  # accepting, correcting and reconsidering all come back to this same prompt
            _show(write, pending)
            choice = _choice(read, write)
            if choice == CANCEL:
                write(CANCELLED)
                return
            if choice == REDICTATE:
                break  # the pending candidate is dropped here, and can never be run
            if choice == CORRECT:
                pending = _correct(pending, read, write)
                continue
            decided, pending = _reconcile(pending, read, write)
            if decided == CANCEL:
                write(CANCELLED)
                return
            if decided is None:  # stay pending: nothing runs, the user decides what to do next
                continue
            _run(pending, write, focus, confirm, offer_retry)
            return


# --- Showing the pending command ------------------------------------------------------------------

def _show(write, pending: PendingCommand) -> None:
    """What was heard, and what would run. Both are shown between [ ] so spaces are visible."""
    write(f"HEARD:             [{pending.heard.transcript.text}]")
    write(command_line(pending.candidate))


def command_line(candidate: str) -> str:
    """The one line the user reads before accepting. Whatever is between the first [ and the last ]
    is EXACTLY what handle_command() receives - a test pins that."""
    return f"COMMAND TO ACCEPT: [{candidate}]"


def _choice(read, write) -> str:
    """One of ACCEPT / CORRECT / REDICTATE / CANCEL. Nothing else counts - not "yes", not "ok"."""
    while True:
        try:
            answer = read(CHOICE_PROMPT)
        except (EOFError, KeyboardInterrupt):
            return CANCEL
        chosen = CHOICES.get(answer.strip().lower())
        if chosen is not None:
            return chosen
        write(CHOICE_AGAIN)


# --- Correcting one part of it ---------------------------------------------------------------------

def _correct(pending: PendingCommand, read, write) -> PendingCommand:
    """Replace ONE numbered part of the candidate. Every other character stays exactly where it is.

    A correction is never acceptance: whatever happens here, the caller shows the whole candidate
    again and asks again."""
    parts = logic.tokens(pending.candidate)
    if not parts:
        write(NOTHING_TO_CORRECT)
        return pending
    write("  " + "  ".join(f"[{number}] {part}" for number, part in enumerate(parts, start=1)))
    number = _number(read, write, len(parts))
    if number is None:
        return pending
    try:
        replacement = read(f"replace [{number}] {parts[number - 1]} with: ")
    except (EOFError, KeyboardInterrupt):
        return pending
    if not replacement.strip():
        write(EMPTY_REPLACEMENT)
        return pending
    corrected = pending.corrected(logic.replace_token(pending.candidate, number, replacement.strip()))
    log.info("Spoken command corrected (%d so far)", corrected.corrections)  # a count, never the text
    return corrected


def _number(read, write, total: int) -> int | None:
    """Which part to replace, or None when the answer isn't one."""
    try:
        answer = read(f"which part (1-{total})? ")
    except (EOFError, KeyboardInterrupt):
        return None
    try:
        number = int(answer.strip())
    except ValueError:
        write(NO_SUCH_TOKEN.format(number=answer.strip() or "(nothing)"))
        return None
    if not 1 <= number <= total:
        write(NO_SUCH_TOKEN.format(number=number))
        return None
    return number


# --- Speech-final punctuation: propose, never apply ------------------------------------------------

def _reconcile(pending: PendingCommand, read, write):
    """(what to do, pending). ACCEPT means run the candidate as it stands; None means stay pending.

    Speech recognition ends sentences with punctuation that the command grammar reads as part of the
    command ("close window." is an app called "window."). So ONE mechanical alternative may be
    PROPOSED - never applied silently, and never to free-form payload. If the two readings would do
    different things, the user chooses; if neither is a command, nothing runs."""
    as_heard = console.preview(pending.candidate)
    if as_heard.free_form:  # `type`: the punctuation and spacing are what the user wants typed
        return ACCEPT, pending
    trimmed = logic.without_terminal_punctuation(pending.candidate)
    if trimmed == pending.candidate:
        return ACCEPT, pending
    without = console.preview(trimmed)
    if as_heard.same_action_as(without):
        return ACCEPT, pending  # the punctuation made no difference at all
    if not as_heard.is_command and not without.is_command:
        write(NOT_A_COMMAND)
        return None, pending
    return _choose_reading(pending, as_heard, trimmed, without, read, write)


def _choose_reading(pending, as_heard, trimmed, without, read, write):
    """Show both readings and let the user pick one. Neither runs until they do."""
    write(AMBIGUOUS)
    write(f"  [1] [{pending.candidate}] - {_reading(as_heard)}")
    write(f"  [2] [{trimmed}] - {_reading(without)}")
    while True:
        try:
            answer = read("which reading did you mean - 1 / 2 / cancel (x)? ")
        except (EOFError, KeyboardInterrupt):
            return CANCEL, pending
        chosen = answer.strip().lower()
        if chosen in ("x", "cancel"):
            return CANCEL, pending
        if chosen == "1":
            return ACCEPT, pending          # exactly what was already on screen and accepted
        if chosen == "2":
            # A changed command is never run on the strength of an earlier acceptance: the caller
            # shows this new candidate and asks for acceptance again.
            return None, pending.reconsidered(trimmed)
        write("Answer 1, 2 or x.")


def _reading(preview) -> str:
    return preview.safe_description if preview.is_command else f"not a command ({preview.refusal})"


# --- Handing it over, unchanged --------------------------------------------------------------------

def _run(pending: PendingCommand, write, focus, confirm, offer_retry) -> None:
    """The accepted candidate, byte for byte, into the same pipeline a typed command uses.

    `confirm` is the console's own KEYBOARD confirmation: a Medium-or-above action still asks the
    user to type yes, and no transcript can answer it."""
    log.info("Spoken command accepted after %d correction(s)", pending.corrections)  # never the text
    reply = console.handle_command(pending.candidate, confirm=confirm, offer_retry=offer_retry,
                                   focus=focus)
    write(reply.message)
