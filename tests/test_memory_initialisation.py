"""
ACCEPTING THE OFFER: the explicit "start fresh memory" command.

The gap this closes was found by the owner on the first real smoke of the Phase 4 wiring slice. The
privacy half worked - "remember Mujahid Hussain's whatsapp is ..." was caught locally, refused locally,
and the number was not transmitted - but the refusal offered "you can start a fresh memory" and there was
no way to accept it. initialize() had existed since Memory Slice 1 with NO production caller, so the
database could never come into existence and nothing could ever be remembered.

THE FROZEN RULE THIS MUST NOT BREAK, from docs/phase4-closeout.md Section 8: "The database is created
only when memory is initialised explicitly: a missing memory.db is reported with a recovery direction,
never silently replaced by an empty one."

So the hard part is not creating a database. It is the three states:

    missing                  -> create it, and confirm all fifteen structures before saying so
    present and sound        -> refuse, untouched
    present but DAMAGED or   -> refuse, untouched. A damaged database is not a missing one, and
    INCOMPLETE                  replacing it would destroy the data an export exists to recover

Every database here lives under tmp_path. NOTHING in this file may run against the real memory database:
the fixtures repoint logic.database_path() before any call, and the one test that reads a path from
configuration only asserts it is NOT the one being written.
"""
import ast
import inspect
import sqlite3

import pytest

from app import console
from app.console import Status
from app.executor import commands
from app.memory import adapter as memory_adapter
from app.memory import logic as memory
from app.memory.models import (SCHEMA_VERSION, TABLE_NAMES, MemoryDatabase, MemoryUnavailable, NotFound,
                               PersistenceDecision)
from config.settings import PROJECT_ROOT
from tests.test_console_brain import Brainless, Script, executor, no_stop, run  # noqa: F401

COMMAND = "start fresh memory"
ADDRESS = "+92-300-0000001"
REMEMBER = f"remember Ali's whatsapp is {ADDRESS}"
RECALL = "what is Ali's whatsapp"


@pytest.fixture
def memory_at(tmp_path, monkeypatch):
    """Point Memory at this test's own path, WITHOUT creating anything there."""
    path = tmp_path / "memory" / "memory.db"
    monkeypatch.setattr(memory, "database_path", lambda: path)
    return path


def must_not_be_called(prompt):
    raise AssertionError("starting a memory database must not cost a provider call")


def tables_at(path):
    with sqlite3.connect(path) as database:
        rows = database.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    return {name for (name,) in rows}


def people_at(path):
    """Read the people table with a RAW connection - nothing of the application in the way."""
    with sqlite3.connect(path) as database:
        return database.execute("SELECT name FROM people").fetchall()


# =======================================================================================================
# 1. THE COMMAND CREATES THE DATABASE
# =======================================================================================================

def test_the_command_creates_the_database_with_all_fifteen_structures(memory_at):
    """The gap, closed. And the count is asserted against the frozen list rather than a number typed
    here, so a sixteenth structure cannot be added without this test following it."""
    assert not memory_at.exists(), "the premise: there is nothing there yet"
    reply, _context = run(COMMAND, interpret=must_not_be_called)
    assert reply.status is Status.ANSWERED, reply.message
    assert memory_at.is_file()
    assert tables_at(memory_at) >= set(TABLE_NAMES)
    assert len(TABLE_NAMES) == 15


def test_the_reply_says_what_exists_and_that_it_is_local(memory_at):
    """The owner asked to be told plainly what now exists and that it is on their machine."""
    reply, _context = run(COMMAND, interpret=must_not_be_called)
    assert "15" in reply.message
    assert str(memory_at) in reply.message, reply.message
    assert "this machine" in reply.message


def test_it_costs_no_provider_call(memory_at):
    """The fake RAISES, so "no call" cannot be a count a stray call was absorbed into."""
    reply, _context = run(COMMAND, interpret=must_not_be_called)
    assert reply.status is Status.ANSWERED
    brainless = Brainless()      # no answers prepared: being called at all raises
    run(COMMAND, interpret=brainless)
    assert brainless.calls == 0


@pytest.mark.parametrize("line", [COMMAND, "START FRESH MEMORY", "  start   fresh   memory  ",
                                  "start a fresh memory"])
def test_the_phrase_is_recognised_as_the_user_would_type_it(line, memory_at):
    """normalize() is the project's one comparison form, so capitals and spacing are forgiven - and
    nothing else is."""
    reply, _context = run(line, interpret=must_not_be_called)
    assert reply.status is Status.ANSWERED, reply.message


@pytest.mark.parametrize("line", ["start fresh memories", "don't start a fresh memory",
                                  "start fresh", "memory", "fresh memory please"])
def test_a_near_miss_creates_nothing(line, memory_at):
    """EXACT, deliberately. This is the only command in the project that brings a database into
    existence, and a loose match is how "don't start a fresh memory" would create one."""
    assert console._is_start_memory(line) is False
    assert not memory_at.exists(), "a line that is not the command must create nothing"


# =======================================================================================================
# 2. THE THREE STATES
# =======================================================================================================

def test_a_sound_database_is_refused_and_its_data_survives(memory_at):
    """initialize() is idempotent and would have reported success here. "You already have one" is a
    different answer from "here is a new one", which is why the command asks a different question."""
    run(COMMAND, interpret=must_not_be_called)
    run(REMEMBER, interpret=must_not_be_called)
    assert people_at(memory_at) == [("Ali",)]
    before = memory_at.read_bytes()

    again, _context = run(COMMAND, interpret=must_not_be_called)
    assert again.status is Status.REFUSED
    assert memory.ALREADY_HAVE in again.message
    assert people_at(memory_at) == [("Ali",)], "the remembered contact did not survive"
    assert memory_at.read_bytes() == before, "the file was rewritten"

    recalled, _context = run(RECALL, interpret=must_not_be_called)
    assert ADDRESS in recalled.message, recalled.message


def test_a_damaged_database_is_refused_and_not_replaced(memory_at):
    """A DAMAGED DATABASE IS NOT A MISSING ONE. Replacing it with an empty one would destroy exactly the
    data a restore exists to recover, so it is refused and left byte-for-byte alone."""
    memory_at.parent.mkdir(parents=True, exist_ok=True)
    memory_at.write_bytes(b"this is not a database, it is a damaged file")
    before = memory_at.read_bytes()

    reply, _context = run(COMMAND, interpret=must_not_be_called)
    assert reply.status is Status.REFUSED
    assert memory.ALREADY_HAVE not in reply.message, "a damaged file was called a working memory"
    assert memory_at.read_bytes() == before, "the damaged file was replaced or repaired"


def test_a_database_that_opens_but_is_incomplete_is_refused_and_not_repaired(memory_at):
    """THE THIRD STATE, and the one open_memory() cannot see: its integrity check and its version read
    both pass on a database whose tables were never finished. Without the structure check this would
    have been reported as "you already have one" and every later remember would have failed."""
    assert isinstance(memory.initialize(), MemoryDatabase)
    with sqlite3.connect(memory_at) as database:
        database.execute("DROP TABLE contacts")
    assert isinstance(memory.open_memory(), MemoryDatabase), (
        "the premise: it still OPENS cleanly, which is why the version number is not enough")

    reply, _context = run(COMMAND, interpret=must_not_be_called)
    assert reply.status is Status.REFUSED
    assert "missing 1 of the 15" in reply.message, reply.message
    assert "contacts" not in tables_at(memory_at), "the missing table was quietly recreated"


def test_a_creation_that_does_not_finish_is_never_reported_as_success(memory_at, monkeypatch):
    """Reported honestly rather than claimed. The real creation runs in ONE transaction that rolls back,
    so a half-written database cannot come from a SQLite error - this simulates the other way it could
    happen, which is a future change that commits an incomplete set of tables."""
    real = memory_adapter._create_everything

    def leaves_one_out(conn, path):
        real(conn, path)
        conn.execute("DROP TABLE contacts")

    monkeypatch.setattr(memory_adapter, "_create_everything", leaves_one_out)
    reply, _context = run(COMMAND, interpret=must_not_be_called)
    assert reply.status is Status.REFUSED, reply.message
    assert "didn't finish" in reply.message, reply.message
    assert "Memory is ready" not in reply.message


def test_a_failure_before_anything_is_written_reports_it(memory_at, monkeypatch):
    """The other failure shape: creation refused by the adapter outright."""
    def refuses(*args, **kwargs):
        raise memory_adapter.MemoryAdapterError("the disk is read-only")

    monkeypatch.setattr(memory_adapter, "initialize", refuses)
    reply, _context = run(COMMAND, interpret=must_not_be_called)
    assert reply.status is Status.REFUSED
    assert "read-only" in reply.message


def test_something_that_is_not_a_file_is_still_not_absent(memory_at):
    """database_is_absent() asks whether anything EXISTS, not whether a FILE exists: a directory at the
    configured path is still something, and creating over it is not this command's business."""
    memory_at.mkdir(parents=True)
    reply, _context = run(COMMAND, interpret=must_not_be_called)
    assert reply.status is Status.REFUSED
    assert memory_at.is_dir(), "the directory was removed"


# =======================================================================================================
# 3. NOTHING CREATES A DATABASE ON THE USER'S BEHALF
# =======================================================================================================

def test_remember_does_not_auto_create(memory_at):
    """The frozen separation. A remember with no database reports the problem and names the command -
    it does not quietly make one."""
    reply, _context = run(REMEMBER, interpret=must_not_be_called)
    assert reply.status is Status.REFUSED
    assert not memory_at.exists(), "remember created a database on the user's behalf"
    assert COMMAND in reply.message, f"the offer does not name the command: {reply.message}"


def test_recall_does_not_auto_create(memory_at):
    reply, _context = run(RECALL, interpret=must_not_be_called)
    assert not memory_at.exists()
    assert COMMAND in reply.message, reply.message


def test_the_offer_and_the_command_are_the_same_words(memory_at):
    """PINNED TO EACH OTHER. The missing-database message tells the user what to type, so the phrase in
    that message must be one the command actually accepts. Two constants that drift apart is how an
    assistant offers something it cannot do - which is the defect this slice exists to fix."""
    reply, _context = run(REMEMBER, interpret=must_not_be_called)
    offered = [phrase for phrase in console.START_MEMORY_WORDS if phrase in reply.message]
    assert offered, f"the message offers no phrase the command accepts: {reply.message}"
    for phrase in offered:
        assert console._is_start_memory(phrase) is True
    assert COMMAND in memory.MISSING_RECOVERY


def test_no_other_production_code_creates_a_database():
    """STRUCTURAL. initialize() may be reached from exactly two places: the explicit command's own
    function, and a restore (which creates the fresh database it restores into). Checked on the AST so
    that a call added anywhere else - including at application start-up - fails this test."""
    callers = set()
    for module in (memory, memory_adapter, console):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if any(isinstance(inner, ast.Call) and ast.unparse(inner.func).endswith("initialize")
                   for inner in ast.walk(node)):
                callers.add(node.name)
    assert callers == {"initialize", "start_fresh_memory", "write_all_rows"}, callers


def test_application_start_up_touches_no_database(memory_at):
    """main.py is the application entry point; nothing on the way in may create a memory database."""
    source = (PROJECT_ROOT / "main.py").read_text(encoding="utf-8")
    assert "initialize" not in source and "start_fresh_memory" not in source
    assert not memory_at.exists()


# =======================================================================================================
# 4. ROUTING, AND THE NESTED PROMPTS
# =======================================================================================================

def test_the_command_is_a_fresh_command_at_every_prompt(memory_at):
    """Slice 1's rules. The missing-database message can be printed at a nested prompt, so the one
    command it names has to be handed back to the loop there rather than consumed as correction text and
    sent to the provider as prose."""
    assert console.is_fresh_command(COMMAND) is True
    for shape in (console.CLOSED_QUESTION, console.FREE_TEXT, console.CLARIFICATION):
        assert console.classify_answer(COMMAND, shape) is console.Answer.COMMAND, shape


def test_it_is_listed_in_help():
    """A command nobody can discover is half a fix."""
    assert COMMAND in commands.HELP


def test_the_command_reaches_the_seam_before_the_brain(memory_at):
    """Positional, like the rest of this guard: inside handle_typed_line the check comes before the call
    that can reach a provider. On the AST, with the docstring discarded."""
    tree = ast.parse(inspect.getsource(console.handle_typed_line).strip())
    function = tree.body[0]
    body = function.body[1:] if isinstance(function.body[0], ast.Expr) else function.body
    called = [ast.unparse(node.func) for node in ast.walk(ast.Module(body=body, type_ignores=[]))
              if isinstance(node, ast.Call)]
    assert called.index("_is_start_memory") < called.index("_ask_the_brain"), called


def test_the_command_never_becomes_an_action(memory_at, executor):
    """It writes a database and no action: nothing reaches the Executor."""
    run(COMMAND, interpret=must_not_be_called)
    reply, _context = run(COMMAND, interpret=must_not_be_called)
    assert executor.actions == []
    assert reply.action is None and reply.result is None


# =======================================================================================================
# 5. IT IS ON DISK - THE RESTART SEQUENCE
# =======================================================================================================

def test_a_contact_survives_being_read_by_something_that_is_not_the_application(memory_at):
    """THE TEST THAT MATTERS MOST, in the owner's order: initialise, remember, drop every connection,
    read the contact back from the SAME path with a fresh reader, recall.

    WHAT "A FRESH SERVICE" IS HERE, SAID PRECISELY RATHER THAN IMPLIED. A real process restart is not
    simulated inside this suite, and the owner's own console smoke is where that is proven. What is
    proven here is the pair of facts that make a restart uninteresting:

      1. the row is read back through a connection the application does not own and did not open - its
         own SQL, its own cursor - so "it is on disk" is OBSERVED, not inferred from the API agreeing
         with itself;
      2. there is no connection, row or path cache that could have answered in its place, which
         test_memory_holds_no_connection_or_row_cache checks structurally.

    AND WHY NOT importlib.reload: reloading app.memory.logic would discard the monkeypatch that
    safety_guards.install_database_guard puts on database_path - layer 1 of the two-layer guard keeping
    tests off the owner's REAL memory.db. Layer 2 (sqlite3.connect refusing repository files) would still
    hold, but deliberately stripping a guard layer to make a test look more impressive is the wrong
    trade. Reloading app.console was worse: it builds a SECOND Status enum, so every `is Status.X`
    comparison in every other test module silently stops matching."""
    run(COMMAND, interpret=must_not_be_called)
    stored, _context = run(REMEMBER, interpret=must_not_be_called)
    assert stored.status is Status.ANSWERED, stored.message

    # Every connection is closed already - the adapter opens and closes one per call - so this reads the
    # file exactly as a separate program would.
    with sqlite3.connect(memory_at) as database:
        rows = database.execute(
            "SELECT people.name, contacts.channel, contacts.address FROM contacts "
            "JOIN people ON people.id = contacts.person_id").fetchall()
    assert rows == [("Ali", "whatsapp", ADDRESS)], "the contact is not on disk"
    assert memory_adapter.open_existing(memory_at) == SCHEMA_VERSION

    # And the recall, through the real console path with a brand-new TurnContext - which is all the
    # per-line state the console has; it is documented as never persisted.
    recalled, _context = run(RECALL, interpret=must_not_be_called)
    assert recalled.status is Status.ANSWERED, recalled.message
    assert ADDRESS in recalled.message, recalled.message


def test_memory_holds_no_connection_or_row_cache():
    """WHY THE RESTART TEST CANNOT PASS FALSELY, stated structurally.

    Every adapter entry point opens its own connection through _connect() and closes it in a finally, so
    no connection outlives a call; and neither module holds a cache of rows, people or contacts. The only
    module-level mutable names in either file are the two clock hooks tests replace. If a cache is ever
    added, this fails and the restart test above must be re-examined."""
    for module in (memory, memory_adapter):
        tree = ast.parse(inspect.getsource(module))
        assigned = {target.id for node in tree.body if isinstance(node, ast.Assign)
                    for target in node.targets if isinstance(target, ast.Name)}
        private = {name for name in assigned if name.startswith("_") and name.islower()}
        assert private <= {"_now", "_clock"}, f"{module.__name__} grew module-level state: {private}"
        cached = [ast.unparse(node) for node in ast.walk(tree)
                  if isinstance(node, ast.Name) and "cache" in node.id.lower()]
        assert not cached, cached


def test_initialising_again_after_a_remember_refuses_and_keeps_the_contact(memory_at):
    """The owner's last requirement for this sequence, spelled out separately from the sound-database
    case because the thing that must survive is a REMEMBERED CONTACT, not just a file."""
    run(COMMAND, interpret=must_not_be_called)
    run(REMEMBER, interpret=must_not_be_called)
    again, _context = run(COMMAND, interpret=must_not_be_called)
    assert again.status is Status.REFUSED
    recalled, _context = run(RECALL, interpret=must_not_be_called)
    assert ADDRESS in recalled.message


# =======================================================================================================
# 6. THE PREVIOUS SLICE IS UNCHANGED
# =======================================================================================================

def test_the_write_gate_is_untouched(memory_at):
    """start_fresh_memory() creates a database; it is not a way past the persistence decision."""
    run(COMMAND, interpret=must_not_be_called)
    refused = memory.remember_contact("Ali", "whatsapp", ADDRESS, PersistenceDecision.DENY)
    assert isinstance(refused, NotFound) and refused.message == memory.NOT_APPROVED
    assert people_at(memory_at) == []


def test_initialize_itself_is_unchanged_and_still_idempotent(memory_at):
    """The frozen separation between explicit initialisation and ordinary opening stays: initialize() is
    still idempotent and still refuses to create over something it cannot open. The new command is a
    caller with a stricter question, not a change to this one."""
    first = memory.initialize()
    assert isinstance(first, MemoryDatabase)
    again = memory.initialize()
    assert isinstance(again, MemoryDatabase), "initialize() stopped being idempotent"

    memory_at.write_bytes(b"damaged")
    assert isinstance(memory.initialize(), MemoryUnavailable)
    assert memory_at.read_bytes() == b"damaged", "initialize() wrote over a file it could not open"
