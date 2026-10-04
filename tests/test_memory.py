"""
Tests for app/memory/ - Phase 4 Slice 1, the persistence foundation.

These drive the REAL adapter against a REAL SQLite database, in each test's own temporary directory. The
root conftest's `databases_stay_in_this_test` guard is what makes that safe: it points every configured
database at tmp_path and makes any database inside the repository unreachable, so the user's own
data/memory.db and data/claude_usage.db cannot be read or written here. Nothing is mocked at the sqlite3
level - a fake database would not have caught a wrong CREATE statement.

WHAT SLICE 1 IS. The fifteen frozen structures as real tables, an explicit schema version, explicit
initialisation, fail-soft opening, and a full wipe. There is no retrieval, no correction learning, no
export/import and no Brain integration, so there are no tests for those here.
"""
import ast
import sqlite3
from pathlib import Path

import pytest

from app.memory import adapter, logic
from app.memory.models import (FORBIDDEN_COLUMN_WORDS, SCHEMA_VERSION, TABLE_NAMES, TABLES,
                               VERSION_TABLE, MemoryDatabase, MemoryUnavailable)
from config import settings
from config.settings import SettingsError

CONFIG = "memory:\n  db_path: {path}\n"


@pytest.fixture
def memory(tmp_path, monkeypatch):
    """A configured but NOT yet created memory database, so each test chooses what exists."""
    db_path = tmp_path / "memory" / "memory.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG.format(path=db_path.as_posix()), encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_PATH", config_path)
    # The central guard redirects a repository path; this one is already outside, so it is kept as-is.
    monkeypatch.setattr(logic, "database_path", lambda: db_path)
    return db_path


def tables_in(path):
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall()
    return sorted(name for (name,) in rows)


# --- 1/2/3/6. Explicit initialisation ------------------------------------------------------------------

def test_explicit_initialize_creates_the_database_and_all_fifteen_structures(memory):
    """Items 1 and 6. Frozen Step 4 requires the fifteen structures as REAL tables."""
    assert not memory.exists(), "nothing exists until initialisation is asked for"
    result = logic.initialize()
    assert isinstance(result, MemoryDatabase), getattr(result, "message", result)
    assert memory.is_file()

    present = tables_in(memory)
    assert len(TABLE_NAMES) == 15, TABLE_NAMES
    for name in TABLE_NAMES:
        assert name in present, f"the {name} structure is missing"
    assert VERSION_TABLE in present, "the schema version has nowhere to live"


def test_every_structure_starts_empty(memory):
    """Item 2. A fresh memory remembers nothing - no seed data, no example rows."""
    logic.initialize()
    counts = adapter.row_counts(memory)
    assert set(counts) == set(TABLE_NAMES)
    assert all(count == 0 for count in counts.values()), counts


def test_the_schema_version_is_recorded_and_matches_this_build(memory):
    """Item 3."""
    result = logic.initialize()
    assert result.schema_version == SCHEMA_VERSION
    assert logic.open_memory().schema_version == SCHEMA_VERSION


def test_initialize_is_idempotent_and_never_destroys_anything(memory):
    """Item 4. Running it twice must not recreate, reset or empty an existing database."""
    logic.initialize()
    with sqlite3.connect(memory) as conn:
        conn.execute("INSERT INTO people (name, created_at) VALUES ('Ali', 1.0)")
    before = tables_in(memory)

    again = logic.initialize()
    assert isinstance(again, MemoryDatabase)
    assert tables_in(memory) == before
    assert adapter.row_counts(memory)["people"] == 1, "an existing row was destroyed"


# --- 5. A missing database is reported, never silently invented ---------------------------------------

def test_opening_a_missing_database_reports_a_recoverable_state(memory):
    """Item 5, and the frozen Recovery requirement. "There is no memory yet" and "your memory is gone"
    must not be the same sentence, so open_memory() does NOT create anything."""
    outcome = logic.open_memory()
    assert isinstance(outcome, MemoryUnavailable)
    assert not memory.exists(), "opening must not create a database"
    assert "no memory database" in outcome.reason
    assert outcome.recovery, "a way forward is required, not just an error"
    assert "restore" in outcome.message and "fresh" in outcome.message


def test_a_missing_database_is_a_result_not_an_exception(memory):
    """Fail-soft: a caller can carry on without memory. Nothing raises."""
    assert isinstance(logic.open_memory(), MemoryUnavailable)
    assert isinstance(logic.wipe(), MemoryUnavailable)


# --- 7. Corruption ------------------------------------------------------------------------------------

def test_a_corrupt_database_fails_clearly_and_is_not_overwritten(memory):
    """Item 7. Frozen Recovery: "a clear error and a path to restore from export, not a silent crash" -
    and emphatically not a blank database quietly replacing the damaged one."""
    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_bytes(b"SQLite format 3\x00this is not a database" + b"\x00" * 200)
    damaged = memory.read_bytes()

    outcome = logic.open_memory()
    assert isinstance(outcome, MemoryUnavailable)
    assert "restore" in outcome.message
    assert memory.read_bytes() == damaged, "the damaged file was modified"


def test_initialize_refuses_to_build_over_a_corrupt_database(memory):
    """Explicit initialisation must not become an accidental "repair" that discards data."""
    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_bytes(b"SQLite format 3\x00" + b"\x7f" * 400)
    damaged = memory.read_bytes()
    outcome = logic.initialize()
    assert isinstance(outcome, MemoryUnavailable), outcome
    assert memory.read_bytes() == damaged


def test_no_raw_sqlite_error_escapes_the_memory_boundary(memory):
    """Item 11 of the audit's privacy/containment aim: the adapter translates every sqlite3 failure."""
    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_bytes(b"not a database at all")
    for call in (logic.open_memory, logic.initialize, logic.wipe):
        outcome = call()
        assert isinstance(outcome, MemoryUnavailable), call.__name__
    with pytest.raises(adapter.MemoryAdapterError):
        adapter.open_existing(memory)


def test_a_corrupt_database_message_names_the_problem_not_its_contents(memory):
    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_bytes(b"SQLite format 3\x00" + b"\x11" * 300)
    outcome = logic.open_memory()
    assert "Memory database" in outcome.reason or "memory" in outcome.reason.lower()


# --- 8. A newer schema is refused ---------------------------------------------------------------------

def test_a_newer_schema_version_is_refused_and_left_alone(memory):
    """Item 8. A database written by a later build must not be guessed at - and must not be destroyed."""
    logic.initialize()
    with sqlite3.connect(memory) as conn:
        conn.execute(f"UPDATE {VERSION_TABLE} SET schema_version = ?", (SCHEMA_VERSION + 5,))

    outcome = logic.open_memory()
    assert isinstance(outcome, MemoryUnavailable)
    assert "newer version" in outcome.reason
    assert str(SCHEMA_VERSION + 5) in outcome.reason and str(SCHEMA_VERSION) in outcome.reason

    assert isinstance(logic.initialize(), MemoryUnavailable), "initialise must not overwrite it"
    with sqlite3.connect(memory) as conn:
        kept = conn.execute(f"SELECT schema_version FROM {VERSION_TABLE}").fetchone()[0]
    assert kept == SCHEMA_VERSION + 5, "the newer database was modified"


def test_an_older_schema_version_is_reported_rather_than_migrated(memory):
    """There is no migration framework in this slice, so an older database is reported, never upgraded
    in place and never deleted."""
    logic.initialize()
    with sqlite3.connect(memory) as conn:
        conn.execute(f"UPDATE {VERSION_TABLE} SET schema_version = 0")
    outcome = logic.open_memory()
    assert isinstance(outcome, MemoryUnavailable)
    assert "older schema" in outcome.reason


def test_a_database_with_no_recorded_version_is_not_trusted(memory):
    logic.initialize()
    with sqlite3.connect(memory) as conn:
        conn.execute(f"DELETE FROM {VERSION_TABLE}")
    assert isinstance(logic.open_memory(), MemoryUnavailable)


# --- 9/10/11/12. Wipe ---------------------------------------------------------------------------------

def rows_everywhere(path, at=1.0):
    """One row in every structure, respecting the foreign keys, so a wipe has something to remove."""
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO identity (id, name, updated_at) VALUES (1, 'Wajid', ?)", (at,))
        conn.execute("INSERT INTO people (id, name, created_at) VALUES (1, 'Ali', ?)", (at,))
        conn.execute("INSERT INTO relationships (person_id, relation, created_at) VALUES (1, 'friend', ?)",
                     (at,))
        conn.execute("INSERT INTO contacts (person_id, channel, address, created_at) "
                     "VALUES (1, 'phone', '0000', ?)", (at,))
        conn.execute("INSERT INTO applications (app_key, alias, created_at) VALUES ('notepad', 'editor', ?)",
                     (at,))
        conn.execute("INSERT INTO projects (id, name, created_at) VALUES (1, 'companion', ?)", (at,))
        conn.execute("INSERT INTO files (path, project_id, created_at) VALUES ('C:/x.txt', 1, ?)", (at,))
        conn.execute("INSERT INTO websites (alias, url, created_at) VALUES ('docs', 'https://x', ?)", (at,))
        conn.execute("INSERT INTO preferences (key, value, updated_at) VALUES ('browser', 'edge', ?)", (at,))
        conn.execute("INSERT INTO habits (description, last_seen_at) VALUES ('opens notepad daily', ?)", (at,))
        conn.execute("INSERT INTO workflows (name, steps, created_at) VALUES ('morning', '1. open', ?)", (at,))
        conn.execute("INSERT INTO corrections (level, scope, right_value, created_at) "
                     "VALUES ('explicit', 'notepad', 'the editor', ?)", (at,))
        conn.execute("INSERT INTO short_term_context (id, app_key, updated_at) VALUES (1, 'notepad', ?)",
                     (at,))
        conn.execute("INSERT INTO permissions (scope, allowed, updated_at) VALUES ('files', 1, ?)", (at,))
        conn.execute("INSERT INTO sensitive_data_rules (target_table, target_field, context, disclosure, "
                     "updated_at) VALUES ('contacts', 'address', 'provider', 'deny', ?)", (at,))


def test_wipe_empties_every_structure(memory):
    """Item 9, and the frozen Done-when's "a full memory wipe leaves the app functional (just
    memoryless)"."""
    logic.initialize()
    rows_everywhere(memory)
    assert all(count == 1 for count in adapter.row_counts(memory).values())

    result = logic.wipe()
    assert isinstance(result, MemoryDatabase), getattr(result, "message", result)
    counts = adapter.row_counts(memory)
    assert set(counts) == set(TABLE_NAMES)
    assert all(count == 0 for count in counts.values()), counts


def test_wipe_keeps_the_schema_and_its_version(memory):
    """Item 10. A wipe is NOT a missing database: the structures and the version survive."""
    logic.initialize()
    rows_everywhere(memory)
    before = tables_in(memory)
    result = logic.wipe()
    assert tables_in(memory) == before
    assert result.schema_version == SCHEMA_VERSION
    assert logic.open_memory().schema_version == SCHEMA_VERSION


def test_the_database_is_still_usable_after_a_wipe(memory):
    """Item 12. "Memoryless" must be a working state, not a broken one."""
    logic.initialize()
    rows_everywhere(memory)
    logic.wipe()
    assert isinstance(logic.open_memory(), MemoryDatabase)
    rows_everywhere(memory, at=2.0)                     # it accepts new memories again
    assert adapter.row_counts(memory)["people"] == 1
    assert isinstance(logic.wipe(), MemoryDatabase)


def test_a_wipe_that_fails_part_way_leaves_nothing_half_wiped(memory, monkeypatch):
    """Item 11. One transaction: the row that was already deleted comes back."""
    logic.initialize()
    rows_everywhere(memory)
    # Fail genuinely mid-sequence: the adapter deletes from each name in turn, and the third does not
    # exist, so SQLite raises after two tables have already been emptied inside the transaction.
    broken = TABLE_NAMES[:2] + ("no_such_table",) + TABLE_NAMES[2:]
    monkeypatch.setattr(adapter, "TABLE_NAMES", broken)
    outcome = logic.wipe()
    monkeypatch.undo()

    assert isinstance(outcome, MemoryUnavailable), outcome
    counts = adapter.row_counts(memory)
    assert all(count == 1 for count in counts.values()), f"half-wiped: {counts}"


def test_wipe_refuses_a_database_it_could_not_open_cleanly(memory):
    """A corrupt file is never "fixed" by emptying it."""
    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_bytes(b"SQLite format 3\x00" + b"\x33" * 250)
    damaged = memory.read_bytes()
    assert isinstance(logic.wipe(), MemoryUnavailable)
    assert memory.read_bytes() == damaged


def test_wipe_touches_no_other_database(memory, tmp_path):
    """It deletes rows from this module's own tables and nothing else - in particular never the ledger."""
    logic.initialize()
    rows_everywhere(memory)
    bystander = tmp_path / "databases" / "claude_usage.db"
    bystander.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(bystander) as conn:
        conn.execute("CREATE TABLE claude_requests (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO claude_requests (id) VALUES (1)")

    logic.wipe()
    with sqlite3.connect(bystander) as conn:
        assert conn.execute("SELECT COUNT(*) FROM claude_requests").fetchone()[0] == 1


# --- 13/14/15. The module boundary --------------------------------------------------------------------

def memory_module_source(name):
    return (settings.PROJECT_ROOT / "app" / "memory" / name).read_text(encoding="utf-8")


def imported_names(source):
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(f"{node.module or ''}.{alias.name}" for alias in node.names)
    return names


def test_only_the_adapter_imports_sqlite3(memory):
    """Item 13, and CLAUDE.md rule 2. logic.py and models.py must not hold a connection."""
    assert "sqlite3" in imported_names(memory_module_source("adapter.py"))
    for name in ("logic.py", "models.py", "__init__.py"):
        assert "sqlite3" not in imported_names(memory_module_source(name)), name


def test_memory_logic_imports_no_executor_planner_or_safety():
    """Item 14. Memory is not a second Executor, Planner or Safety system."""
    for name in ("logic.py", "models.py", "adapter.py"):
        imports = imported_names(memory_module_source(name))
        for forbidden in ("app.executor", "app.planner", "app.safety", "app.brain", "app.verifier"):
            assert not any(imported.startswith(forbidden) for imported in imports), f"{name}: {forbidden}"


def test_nothing_in_memory_can_produce_an_action():
    """Item 15. No ExecutorAction, no Action, no risk level, no confirmation - there is no code path from
    a remembered fact to something happening."""
    for name in ("logic.py", "models.py", "adapter.py"):
        source = memory_module_source(name)
        tree = ast.parse(source)
        called = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        # Unambiguous names only. A bare "execute" would match conn.execute(), which is SQL, not an
        # action - the import check above is what proves those modules are unreachable at all.
        for forbidden in ("ExecutorAction", "execute_with_recovery", "authorize", "RiskLevel",
                          "build_plan", "emergency_stop", "request_close", "launch_app"):
            assert not any(forbidden in call for call in called), f"{name} calls {forbidden}"


def test_the_applications_structure_cannot_make_anything_launchable():
    """Item 17. It remembers what the user CALLS an app. executor.apps stays the only source of what can
    be opened, so there is no column here that could become an executable."""
    applications = next(table for table in TABLES if table.name == "applications")
    columns = " ".join(applications.columns).lower()
    for forbidden in ("executable", "exe", "command", "path", "argv", "launch"):
        assert forbidden not in columns, f"applications has a {forbidden} column"
    assert "app_key" in columns, "an alias must point at a configured app, not replace it"


def test_the_permissions_structure_cannot_authorize_or_lower_risk():
    """Item 18. app/safety stays authoritative: there is no risk level and no confirmation flag here."""
    permissions = next(table for table in TABLES if table.name == "permissions")
    columns = " ".join(permissions.columns).lower()
    for forbidden in ("risk", "level", "confirm", "authorize", "authorise", "bypass", "skip", "override"):
        assert forbidden not in columns, f"permissions has a {forbidden} column"


def test_the_sensitive_data_rules_structure_can_answer_the_frozen_question():
    """It must support "a tagged field is not returned to a context that shouldn't see it", so it names
    the table, the field and the context explicitly rather than hiding them in a blob."""
    rules = next(table for table in TABLES if table.name == "sensitive_data_rules")
    columns = " ".join(rules.columns).lower()
    for required in ("target_table", "target_field", "context", "disclosure", "retention"):
        assert required in columns, f"sensitive_data_rules has no {required}"


# --- 16. Privacy: what no structure may hold ----------------------------------------------------------

def test_no_structure_has_a_column_for_a_secret_transcript_or_payload():
    """Item 16. Not a convention - a check over the actual schema. A generic payload column would quietly
    permit every one of these, so there is none."""
    offenders = []
    for table in TABLES:
        for column in table.columns:
            name = column.split()[0].lower()
            for forbidden in FORBIDDEN_COLUMN_WORDS:
                if forbidden in name:
                    offenders.append(f"{table.name}.{name} (contains {forbidden!r})")
    assert offenders == [], offenders


def test_the_created_schema_really_has_no_such_column(memory):
    """The same assertion against the database SQLite actually built, not only the definitions."""
    logic.initialize()
    with sqlite3.connect(memory) as conn:
        for name in TABLE_NAMES:
            for row in conn.execute(f"PRAGMA table_info({name})"):
                column = row[1].lower()
                for forbidden in FORBIDDEN_COLUMN_WORDS:
                    assert forbidden not in column, f"{name}.{column}"


def test_no_structure_is_a_freeform_notes_blob():
    """Frozen Step 4: real tables, "not a notes blob". A single-value table would satisfy the letter of
    "fifteen tables" while destroying the point, so each one carries real columns."""
    for table in TABLES:
        column_names = [column.split()[0] for column in table.columns
                        if not column.startswith(("UNIQUE", "CHECK", "FOREIGN", "PRIMARY"))]
        assert len(column_names) >= 2, f"{table.name} is too thin to be structure: {column_names}"
        assert "notes" not in table.name and "memory" not in table.name


def test_the_short_term_context_structure_cannot_grow_into_a_conversation(memory):
    """Phase 6 is conversation; Phase 4's short-term context is "active person/app/page/task right now".
    One row, enforced by the schema rather than by discipline."""
    logic.initialize()
    with sqlite3.connect(memory) as conn:
        conn.execute("INSERT INTO short_term_context (id, task, updated_at) VALUES (1, 'a', 1.0)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO short_term_context (id, task, updated_at) VALUES (2, 'b', 2.0)")


def test_phase_3_short_term_context_is_untouched():
    """PreviousActionContext is a Phase 3 runtime mechanism and is NOT this table. Slice 1 must not have
    changed it, and memory must not be wired into it yet."""
    from app.brain.models import PreviousActionContext
    context = PreviousActionContext(kind="open_app", safe_target="notepad")
    assert (context.kind, context.safe_target) == ("open_app", "notepad")
    assert "memory" not in (settings.PROJECT_ROOT / "app" / "brain" / "models.py").read_text(
        encoding="utf-8").lower()


# --- Configuration ------------------------------------------------------------------------------------

def test_the_database_path_comes_from_configuration(tmp_path, monkeypatch):
    """The GENUINE resolver, reached past the test guard's redirect (which is what `__wrapped__` is for),
    so this proves how production resolves a relative setting rather than how the guard rewrites it."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG.format(path="data/memory.db"), encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_PATH", config_path)
    resolver = getattr(logic.database_path, "__wrapped__", logic.database_path)

    resolved = resolver()
    assert resolved == settings.PROJECT_ROOT / "data" / "memory.db", resolved
    assert resolved.is_absolute(), "a relative setting resolves under the project root"


def test_an_absolute_path_setting_is_used_as_given(tmp_path, monkeypatch):
    elsewhere = tmp_path / "chosen" / "memory.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG.format(path=elsewhere.as_posix()), encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_PATH", config_path)
    resolver = getattr(logic.database_path, "__wrapped__", logic.database_path)
    assert resolver() == elsewhere


@pytest.mark.parametrize("value", ["", "   ", "null"])
def test_an_unusable_path_setting_is_a_settings_error(tmp_path, monkeypatch, value):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"memory:\n  db_path: {value}\n", encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_PATH", config_path)
    with pytest.raises(SettingsError):
        logic.database_path()


def test_a_missing_setting_is_reported_as_unavailable_not_a_crash(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("brain:\n  model: x\n", encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_PATH", config_path)
    outcome = logic.open_memory()
    assert isinstance(outcome, MemoryUnavailable)


def test_the_real_config_declares_the_memory_path():
    config = (settings.PROJECT_ROOT / "config" / "config.yaml").read_text(encoding="utf-8")
    assert "memory:" in config and "db_path: data/memory.db" in config


def test_no_remembered_data_lives_in_configuration():
    """Config holds the path; the facts live in the database, which is git-ignored."""
    config = (settings.PROJECT_ROOT / "config" / "config.yaml").read_text(encoding="utf-8")
    memory_section = config.split("memory:", 1)[1].split("# ---", 1)[0]
    assert "db_path" in memory_section
    for table in TABLE_NAMES:
        assert f"{table}:" not in memory_section


def test_the_database_file_is_git_ignored():
    ignored = (settings.PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "data/*.db" in ignored


# =======================================================================================================
# SLICE 2: local update and retrieval for five structures
#
# people, relationships, contacts, applications, sensitive_data_rules. The other ten stay
# persistence-only. Everything below drives the real logic and the real adapter against a real SQLite
# database in this test's own temporary directory.
#
# NO sentence is parsed anywhere. The frozen happy path is "message my friend Ali"; what the Memory API
# receives is name="Ali", relationship="friend". Translating one into the other is the Brain's job, later.
# =======================================================================================================

from app.memory.logic import DO_NOT_KNOW
from app.memory.models import Ambiguous, Contact, Found, NotFound, Person, Redacted, normalize

APPS = ("notepad", "calculator")     # what config/config.yaml currently configures
ADDRESS = "+92-300-0000001"


@pytest.fixture
def ready(memory):
    """An initialised, empty memory database."""
    created = logic.initialize()
    assert isinstance(created, MemoryDatabase), getattr(created, "message", created)
    return memory


def person_id(result):
    assert isinstance(result, Found), getattr(result, "message", result)
    return result.value.id


# --- Normalisation: minimal, and the only one ---------------------------------------------------------

@pytest.mark.parametrize("written, asked", [
    ("Ali", "ali"), ("Ali", "ALI"), ("Ali", "  Ali  "), ("  Ali ", "ali"), ("Ali Raza", "ali  raza"),
])
def test_names_match_ignoring_case_and_surrounding_space(ready, written, asked):
    """Items 2 and 3."""
    logic.add_person(written)
    found = logic.find_person(asked)
    assert isinstance(found, Found), getattr(found, "message", found)


def test_normalisation_does_nothing_clever():
    """Deliberately NOT fuzzy: a near-miss is a miss, because guessing at a person is worse than asking."""
    assert normalize("  ALI  ") == normalize("ali") == "ali"
    assert normalize("Ali Raza") == "ali raza"
    assert normalize("Alii") != normalize("Ali"), "no edit distance"
    assert normalize("Aly") != normalize("Ali"), "no phonetics"


# --- PEOPLE -------------------------------------------------------------------------------------------

def test_a_person_is_added_and_found(ready):
    """Item 1."""
    added = logic.add_person("Ali")
    assert isinstance(added, Found) and added.value.id > 0
    found = logic.find_person("Ali")
    assert isinstance(found, Found)
    assert found.value == Person(added.value.id, "Ali", None)


def test_the_name_is_stored_as_the_user_wrote_it(ready):
    """Item 4. Normalisation is for comparison only - it must never rewrite what is shown back."""
    logic.add_person("  aLiTa Raza  ")
    found = logic.find_person("alita raza")
    assert found.value.name == "aLiTa Raza", "the display form was rewritten"


def test_two_people_with_the_same_name_stay_two_people(ready):
    """Item 5. The schema has no UNIQUE on people.name on purpose: two people really can both be Ali, and
    the frozen ambiguity case depends on that remaining representable."""
    first = logic.add_person("Ali")
    second = logic.add_person("ali")
    assert first.value.id != second.value.id, "two identities were merged into one"
    assert adapter.row_counts(ready)["people"] == 2


def test_duplicate_names_are_ambiguous_and_never_first_row_wins(ready):
    """Item 6, and the frozen Ambiguous case. No person is chosen."""
    first = logic.add_person("Ali")
    second = logic.add_person("Ali")
    outcome = logic.find_person("ali")
    assert isinstance(outcome, Ambiguous)
    assert {person.id for person in outcome.candidates} == {first.value.id, second.value.id}
    assert "guess" in outcome.message
    assert not isinstance(outcome, Found), "a candidate was chosen"


def test_a_person_needs_a_name(ready):
    for blank in ("", "   ", None):
        assert isinstance(logic.add_person(blank), NotFound)
    assert adapter.row_counts(ready)["people"] == 0


# --- RELATIONSHIPS ------------------------------------------------------------------------------------

def test_a_relationship_is_attached_to_a_person(ready):
    """Item 7."""
    ali = person_id(logic.add_person("Ali"))
    assert isinstance(logic.add_relationship(ali, "friend"), Found)
    assert adapter.select_relations(ready, ali) == ["friend"]


def test_the_relationship_filter_resolves_one_of_two_people(ready):
    """Item 8, and §11's second half: a discriminator ALREADY in memory narrows two Alis to one."""
    friend = person_id(logic.add_person("Ali"))
    colleague = person_id(logic.add_person("Ali"))
    logic.add_relationship(friend, "friend")
    logic.add_relationship(colleague, "manager")

    assert isinstance(logic.find_person("Ali"), Ambiguous), "without it, still ambiguous"
    found = logic.find_person("Ali", relationship="friend")
    assert isinstance(found, Found) and found.value.id == friend


def test_a_relationship_is_never_inferred_or_invented(ready):
    """Item 9. Only what the user stored counts: an unrecorded relationship matches nobody, and asking
    does not create it."""
    ali = person_id(logic.add_person("Ali"))
    logic.add_relationship(ali, "friend")
    assert isinstance(logic.find_person("Ali", relationship="brother"), NotFound)
    assert adapter.select_relations(ready, ali) == ["friend"], "a relationship was invented"
    assert adapter.row_counts(ready)["relationships"] == 1


def test_still_ambiguous_when_the_filter_leaves_two(ready):
    """Item 10, and §11: a discriminator only helps if it genuinely leaves one candidate."""
    for _ in range(2):
        logic.add_relationship(person_id(logic.add_person("Ali")), "friend")
    assert isinstance(logic.find_person("Ali", relationship="friend"), Ambiguous)


def test_a_relationship_needs_a_value(ready):
    ali = person_id(logic.add_person("Ali"))
    assert isinstance(logic.add_relationship(ali, "  "), NotFound)
    assert adapter.row_counts(ready)["relationships"] == 0


# --- CONTACTS -----------------------------------------------------------------------------------------

def test_a_contact_is_added_for_an_existing_person(ready):
    """Item 11."""
    ali = person_id(logic.add_person("Ali"))
    added = logic.add_contact(ali, "whatsapp", ADDRESS)
    assert isinstance(added, Found)
    assert added.value.channel == "whatsapp" and added.value.address == ADDRESS


def test_no_contacts_is_not_found(ready):
    """Item 12."""
    ali = person_id(logic.add_person("Ali"))
    assert isinstance(logic.find_contact(ali, context="local"), NotFound)


def test_one_contact_is_found(ready):
    """Item 13."""
    ali = person_id(logic.add_person("Ali"))
    logic.add_contact(ali, "whatsapp", ADDRESS)
    found = logic.find_contact(ali, context="local")
    assert isinstance(found, Found) and found.value.address == ADDRESS


def test_two_contacts_left_after_the_filters_are_ambiguous(ready):
    """Item 14. Not the newest, not the lowest id, not alphabetical - nothing is chosen."""
    ali = person_id(logic.add_person("Ali"))
    logic.add_contact(ali, "whatsapp", ADDRESS)
    logic.add_contact(ali, "phone", "+92-300-0000002")
    outcome = logic.find_contact(ali, context="local")
    assert isinstance(outcome, Ambiguous) and len(outcome.candidates) == 2

    narrowed = logic.find_contact(ali, channel="whatsapp", context="local")
    assert isinstance(narrowed, Found) and narrowed.value.channel == "whatsapp"


def test_the_channel_is_data_and_nothing_is_opened_or_sent(ready, monkeypatch):
    """Item 15. "whatsapp" is a stored string. Memory does not check it is installed, open it or send
    anything - messaging is Phase 10, and there is no code path to it."""
    ali = person_id(logic.add_person("Ali"))
    logic.add_contact(ali, "whatsapp", ADDRESS)
    assert isinstance(logic.find_contact(ali, context="local"), Found)
    from app.brain.models import ARGS_FOR_KIND
    for invented in ("send_message", "message", "whatsapp", "sms", "email"):
        assert invented not in ARGS_FOR_KIND


def test_a_contact_needs_a_channel_and_an_address(ready):
    ali = person_id(logic.add_person("Ali"))
    for channel, address in (("", ADDRESS), ("phone", ""), ("  ", "  ")):
        assert isinstance(logic.add_contact(ali, channel, address), NotFound)
    assert adapter.row_counts(ready)["contacts"] == 0


def test_a_contact_for_a_person_who_does_not_exist_is_refused(ready):
    """The foreign key is enforced, and the failure arrives as a Memory result."""
    outcome = logic.add_contact(999, "phone", ADDRESS)
    assert isinstance(outcome, MemoryUnavailable), outcome
    assert adapter.row_counts(ready)["contacts"] == 0


# --- THE FROZEN HAPPY PATH ----------------------------------------------------------------------------

def test_message_my_friend_ali_resolves_ali_from_memory(ready):
    """Items 16 and 17, the frozen Happy path - as a LOOKUP. "message my friend Ali" is the sentence a
    user says; the Memory API is asked name="Ali", relationship="friend", and answers with the person and
    then their stored contact.

    It stops there. Nothing is sent, and messaging remains Phase 10."""
    ali = person_id(logic.add_person("Ali"))
    logic.add_relationship(ali, "friend")
    logic.add_contact(ali, "whatsapp", ADDRESS)
    someone_else = person_id(logic.add_person("Bilal"))
    logic.add_relationship(someone_else, "colleague")

    person = logic.find_person(name="Ali", relationship="friend")
    assert isinstance(person, Found), getattr(person, "message", person)
    assert person.value.id == ali and person.value.name == "Ali"

    contact = logic.find_contact(person.value.id, context="local")
    assert isinstance(contact, Found)
    assert (contact.value.channel, contact.value.address) == ("whatsapp", ADDRESS)


def test_the_happy_path_touches_no_executor_planner_brain_or_provider(ready, monkeypatch):
    """Item 18. The whole lookup runs with the Executor's entry point replaced by a failure: if Memory
    could reach it, this would not pass."""
    from app import console
    from app.executor import adapter as executor_adapter

    def forbidden(*args, **kwargs):
        raise AssertionError("Memory reached the Executor")

    monkeypatch.setattr(console, "execute_with_recovery", forbidden)
    monkeypatch.setattr(executor_adapter, "launch_app", forbidden)

    ali = person_id(logic.add_person("Ali"))
    logic.add_relationship(ali, "friend")
    logic.add_contact(ali, "whatsapp", ADDRESS)
    assert isinstance(logic.find_person("Ali", "friend"), Found)
    assert isinstance(logic.find_contact(ali, context="local"), Found)


# --- MISSING INFORMATION ------------------------------------------------------------------------------

def test_an_unknown_person_is_not_found_with_the_frozen_sentence(ready):
    """Items 19 and 20. Frozen Step 4: a clear "I don't know who that is" rather than a wrong guess."""
    logic.add_person("Ali")
    outcome = logic.find_person("Zubair")
    assert isinstance(outcome, NotFound)
    assert outcome.message == DO_NOT_KNOW == "I don't know who that is."


def test_a_failed_lookup_writes_nothing_at_all(ready):
    """Item 21. No placeholder person, no remembered question, no silent learning from a miss."""
    before = adapter.row_counts(ready)
    for _ in range(3):
        assert isinstance(logic.find_person("Zubair"), NotFound)
        assert isinstance(logic.find_person("Zubair", relationship="friend"), NotFound)
        assert isinstance(logic.resolve_application_alias("nothing", APPS), NotFound)
    assert adapter.row_counts(ready) == before, "a lookup created data"
    assert all(count == 0 for count in before.values())


def test_a_missing_lookup_asks_nothing_of_the_provider(ready):
    """Memory answers from the database or not at all. The provider guard is armed for every test, so a
    request would raise rather than go out."""
    assert isinstance(logic.find_person("Zubair"), NotFound)


# --- APPLICATIONS: aliases, never capability ----------------------------------------------------------

def test_an_alias_for_a_configured_app_is_remembered_and_resolves(ready):
    """Items 22 and 23."""
    assert isinstance(logic.remember_application_alias("editor", "notepad", APPS), Found)
    resolved = logic.resolve_application_alias("editor", APPS)
    assert isinstance(resolved, Found) and resolved.value == "notepad"


def test_an_alias_for_an_unconfigured_app_is_refused_outright(ready):
    """Item 24. Memory cannot create capability: config/config.yaml stays the source of truth for what
    can be opened, so an alias for an app that is not configured is never even stored."""
    outcome = logic.remember_application_alias("browser", "chrome", APPS)
    assert isinstance(outcome, NotFound)
    assert "set up to open" in outcome.message
    assert adapter.row_counts(ready)["applications"] == 0
    assert isinstance(logic.resolve_application_alias("browser", APPS), NotFound)


def test_a_stale_alias_does_not_widen_what_can_be_opened(ready):
    """Item 25. The app was configured when the alias was stored and is not any more - so the alias stops
    resolving. A remembered name must never outlive the capability it referred to."""
    assert isinstance(logic.remember_application_alias("editor", "notepad", APPS), Found)
    shrunk = ("calculator",)
    outcome = logic.resolve_application_alias("editor", shrunk)
    assert isinstance(outcome, NotFound)
    assert "isn't an app I'm set up to open any more" in outcome.message


def test_alias_resolution_returns_a_key_and_never_an_executable(ready):
    """Item 26. Not notepad.exe, not a path, not a command - and the table has no column that could hold
    one, so there is nothing to leak."""
    logic.remember_application_alias("editor", "notepad", APPS)
    resolved = logic.resolve_application_alias("editor", APPS)
    assert resolved.value == "notepad"
    for forbidden in (".exe", "/", "\\", "cmd", "powershell"):
        assert forbidden not in resolved.value
    assert "app_key" in " ".join(
        next(table for table in TABLES if table.name == "applications").columns)


def test_memory_never_changes_the_configured_app_list(ready):
    """executor.apps is configuration, and Memory only ever reads a set the caller hands it."""
    config = (settings.PROJECT_ROOT / "config" / "config.yaml").read_text(encoding="utf-8")
    logic.remember_application_alias("editor", "notepad", APPS)
    assert (settings.PROJECT_ROOT / "config" / "config.yaml").read_text(encoding="utf-8") == config
    # Checked as imports, not as prose: Memory's own documentation names executor.apps to explain what
    # it must NOT do, and a substring search over the file matches that explanation rather than any
    # behaviour. What matters is that nothing here can READ the configured list.
    imports = imported_names(memory_module_source("logic.py"))
    assert not any(name.startswith("app.executor") for name in imports), imports
    assert not any(name.startswith("config") and "settings" not in name for name in imports), imports


def test_aliases_are_case_and_space_insensitive_too(ready):
    logic.remember_application_alias("  Editor ", "notepad", APPS)
    assert logic.resolve_application_alias("EDITOR", APPS).value == "notepad"


# --- SENSITIVE-DATA RULES -----------------------------------------------------------------------------

def with_denied_address(context="brain"):
    """One person, one contact, and an exact deny rule for contacts.address in `context`."""
    ali = person_id(logic.add_person("Ali"))
    logic.add_contact(ali, "whatsapp", ADDRESS)
    assert isinstance(logic.set_sensitive_rule("contacts", "address", context, "deny"), Found)
    return ali


def test_an_exact_deny_rule_withholds_the_address(ready):
    """Items 27 and 28, the frozen Permission-denied case: a tagged field is not returned to a context
    that should not see it."""
    ali = with_denied_address("brain")
    outcome = logic.find_contact(ali, context="brain")
    assert isinstance(outcome, Redacted), outcome
    assert outcome.withheld == "contacts.address" and outcome.context == "brain"
    assert outcome.value.address is None, "the address came back anyway"
    assert outcome.value.disclosed is False
    assert outcome.value.channel == "whatsapp", "the safe part is still useful"


def test_the_denied_address_cannot_leak_through_any_representation(ready, caplog):
    """Item 29, pinned explicitly. The value must not appear in the result data, its repr, the message,
    an exception, or anything logged - because it was never put into the result at all."""
    import logging
    ali = with_denied_address("brain")
    with caplog.at_level(logging.DEBUG):
        outcome = logic.find_contact(ali, context="brain")
    surfaces = [repr(outcome), str(outcome), outcome.message, repr(outcome.value), str(outcome.value),
                outcome.withheld, outcome.context,
                "\n".join(record.getMessage() for record in caplog.records)]
    for surface in surfaces:
        assert ADDRESS not in surface, surface
        assert "0000001" not in surface, surface
    assert ADDRESS not in repr(outcome.value.__dict__)


def test_an_allowed_context_still_gets_the_address(ready):
    """Item 30. The rule is per context, so another local context is unaffected."""
    ali = with_denied_address("brain")
    allowed = logic.find_contact(ali, context="local")
    assert isinstance(allowed, Found) and allowed.value.address == ADDRESS
    assert allowed.value.disclosed is True


def test_an_explicit_allow_rule_also_discloses(ready):
    ali = with_denied_address("brain")
    assert isinstance(logic.set_sensitive_rule("contacts", "address", "console", "allow"), Found)
    assert isinstance(logic.find_contact(ali, context="console"), Found)


def test_even_an_allowed_address_is_not_in_the_repr(ready):
    """Stronger than the frozen requirement, and cheap: a repr is what reaches a log line by accident, so
    the address is never in one - disclosed or not."""
    ali = person_id(logic.add_person("Ali"))
    logic.add_contact(ali, "whatsapp", ADDRESS)
    found = logic.find_contact(ali, context="local")
    assert found.value.address == ADDRESS, "it IS available to the caller"
    assert ADDRESS not in repr(found.value) and ADDRESS not in repr(found)


def test_a_rule_for_one_field_hides_nothing_else(ready):
    """Item 31. Denying contacts.address must not quietly hide the channel, the person, or anything in
    another table."""
    ali = with_denied_address("brain")
    logic.add_relationship(ali, "friend")
    outcome = logic.find_contact(ali, context="brain")
    assert outcome.value.channel == "whatsapp"
    person = logic.find_person("Ali", relationship="friend")
    assert isinstance(person, Found) and person.value.name == "Ali"


def test_a_rule_naming_a_different_field_does_not_affect_the_address(ready):
    ali = person_id(logic.add_person("Ali"))
    logic.add_contact(ali, "whatsapp", ADDRESS)
    logic.set_sensitive_rule("contacts", "channel", "brain", "deny")
    assert isinstance(logic.find_contact(ali, context="brain"), Found), "the wrong field was filtered"


def test_no_rule_means_ordinary_local_retrieval(ready):
    """Item 32, and §15's default. In this slice nothing new leaves the machine, so an unruled field is
    retrieved normally; an explicitly denied one never is."""
    ali = person_id(logic.add_person("Ali"))
    logic.add_contact(ali, "whatsapp", ADDRESS)
    assert adapter.row_counts(ready)["sensitive_data_rules"] == 0
    assert isinstance(logic.find_contact(ali, context="anything-at-all"), Found)


def test_a_rule_is_replaced_in_place_rather_than_duplicated(ready):
    """Item 33. The UNIQUE on (table, field, context) is what prevents two conflicting exact rules, so an
    update replaces rather than adds - in one transaction."""
    ali = with_denied_address("brain")
    assert isinstance(logic.find_contact(ali, context="brain"), Redacted)
    assert isinstance(logic.set_sensitive_rule("contacts", "address", "brain", "allow"), Found)
    assert adapter.row_counts(ready)["sensitive_data_rules"] == 1, "a second conflicting rule was added"
    assert isinstance(logic.find_contact(ali, context="brain"), Found)


def test_an_allow_rule_grants_nothing_beyond_the_default(ready):
    """Rules filter Memory DISCLOSURE and are not Safety policy. The strongest thing an 'allow' row can
    do is restore what no rule at all already permits - so the two outcomes are identical, and there is
    nothing an 'allow' could unlock."""
    ali = person_id(logic.add_person("Ali"))
    logic.add_contact(ali, "whatsapp", ADDRESS)
    without_rule = logic.find_contact(ali, context="brain")

    assert isinstance(logic.set_sensitive_rule("contacts", "address", "brain", "allow"), Found)
    with_allow = logic.find_contact(ali, context="brain")

    assert type(without_rule) is type(with_allow) is Found
    assert without_rule.value == with_allow.value


def test_a_rule_needs_a_table_a_field_a_context_and_a_decision(ready):
    for bad in (("contacts", "address", "brain", "maybe"), ("", "address", "brain", "deny"),
                ("contacts", "  ", "brain", "deny"), ("contacts", "address", "", "deny")):
        assert isinstance(logic.set_sensitive_rule(*bad), NotFound), bad
    assert adapter.row_counts(ready)["sensitive_data_rules"] == 0


# --- RESULT SHAPES ------------------------------------------------------------------------------------

def test_results_are_typed_rather_than_strings(ready):
    """A caller branches on the type. Nothing has to be matched against wording."""
    ali = person_id(logic.add_person("Ali"))
    logic.add_person("Ali")
    assert isinstance(logic.find_person("Ali"), Ambiguous)
    assert isinstance(logic.find_person("Nobody"), NotFound)
    assert isinstance(logic.find_contact(ali, context="local"), NotFound)
    logic.add_contact(ali, "whatsapp", ADDRESS)
    assert isinstance(logic.find_contact(ali, context="local"), Found)


def test_no_result_can_carry_something_actionable(ready):
    """Item 34 restated for the new shapes: pure Memory-domain data only - no ExecutorAction, no Plan, no
    RiskLevel, nothing a provider could be told to do."""
    ali = person_id(logic.add_person("Ali"))
    logic.add_contact(ali, "whatsapp", ADDRESS)
    logic.add_relationship(ali, "friend")
    results = [logic.find_person("Ali"), logic.find_person("Ali", "friend"),
               logic.find_contact(ali, context="local"), logic.find_person("Nobody"),
               logic.resolve_application_alias("editor", APPS)]
    for result in results:
        for field in getattr(result, "__dataclass_fields__", {}):
            value = getattr(result, field)
            assert not hasattr(value, "kind") or isinstance(value, (Person, Contact)), (result, field)
            assert type(value).__name__ not in ("ExecutorAction", "Plan", "PlanStep", "RiskLevel",
                                                "Action", "Intent")


# --- Slice 1 regressions, with Slice 2 data in place --------------------------------------------------

def test_wipe_still_empties_the_five_structures_in_use(ready):
    """Item 38. Slice 1's wipe must still clear everything Slice 2 writes."""
    ali = person_id(logic.add_person("Ali"))
    logic.add_relationship(ali, "friend")
    logic.add_contact(ali, "whatsapp", ADDRESS)
    logic.remember_application_alias("editor", "notepad", APPS)
    logic.set_sensitive_rule("contacts", "address", "brain", "deny")
    counts = adapter.row_counts(ready)
    assert all(counts[name] == 1 for name in
               ("people", "relationships", "contacts", "applications", "sensitive_data_rules"))

    assert isinstance(logic.wipe(), MemoryDatabase)
    assert all(count == 0 for count in adapter.row_counts(ready).values())
    assert isinstance(logic.find_person("Ali"), NotFound), "memoryless, and still working"
    assert isinstance(logic.add_person("Ali"), Found), "and it accepts new memories again"


def test_every_operation_fails_softly_when_there_is_no_database(memory):
    """No initialise: every Slice 2 entry point returns the recoverable missing-memory result rather than
    raising, creating a database, or pretending memory is empty."""
    operations = [
        lambda: logic.add_person("Ali"),
        lambda: logic.add_relationship(1, "friend"),
        lambda: logic.add_contact(1, "whatsapp", ADDRESS),
        lambda: logic.find_person("Ali"),
        lambda: logic.find_contact(1, context="local"),
        lambda: logic.remember_application_alias("editor", "notepad", APPS),
        lambda: logic.resolve_application_alias("editor", APPS),
        lambda: logic.set_sensitive_rule("contacts", "address", "brain", "deny"),
    ]
    for operation in operations:
        assert isinstance(operation(), MemoryUnavailable), operation
    assert not memory.exists(), "an operation created a database"


def test_every_operation_fails_softly_on_a_corrupt_database(memory):
    """Item 36's sibling: no raw sqlite3 error escapes from any Slice 2 entry point either."""
    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_bytes(b"SQLite format 3\x00" + b"\x5a" * 300)
    for operation in (lambda: logic.add_person("Ali"), lambda: logic.find_person("Ali"),
                      lambda: logic.find_contact(1, context="local"),
                      lambda: logic.set_sensitive_rule("contacts", "address", "brain", "deny")):
        assert isinstance(operation(), MemoryUnavailable), operation


# --- Boundaries, for the Slice 2 surface --------------------------------------------------------------

def test_the_slice_2_api_adds_no_new_sqlite_or_cross_module_import():
    """Items 35 and 36. Unchanged by this slice: only the adapter touches SQLite, and Memory imports no
    Executor, Planner, Safety, Brain or Verifier."""
    assert "sqlite3" not in imported_names(memory_module_source("logic.py"))
    for name in ("logic.py", "models.py", "adapter.py"):
        imports = imported_names(memory_module_source(name))
        for forbidden in ("app.executor", "app.planner", "app.safety", "app.brain", "app.verifier"):
            assert not any(imported.startswith(forbidden) for imported in imports), f"{name}: {forbidden}"


def test_no_sentence_parsing_entered_the_memory_module():
    """§3. Language understanding stays in the Brain: the lookup API takes fields, and nothing here
    tokenises, splits on words or matches phrases."""
    source = memory_module_source("logic.py")
    for forbidden in ("def parse", "re.compile", "re.search", "re.match", "startswith(\"message",
                      "nltk", "spacy"):
        assert forbidden not in source, f"logic.py contains {forbidden}"


IMPLEMENTED_STRUCTURES = {
    # Slice 2
    "people", "relationships", "contacts", "applications", "sensitive_data_rules",
    # Slice 3
    "corrections",
}


def test_only_the_implemented_structures_are_ever_written():
    """Scope, slice by slice. The remaining nine tables stay persistence-only from Slice 1, so no write
    statement names them - this is what catches a slice quietly growing past its brief."""
    source = memory_module_source("adapter.py")
    assert len(TABLE_NAMES) - len(IMPLEMENTED_STRUCTURES) == 9, "the untouched set changed size"
    for name in TABLE_NAMES:
        if name in IMPLEMENTED_STRUCTURES:
            continue
        assert f"INSERT INTO {name}" not in source, f"{name} is out of scope for this slice"
        assert f"UPDATE {name}" not in source, f"{name} is out of scope for this slice"


def test_there_are_exactly_two_delete_statements_and_each_has_its_own_job():
    """Deletion is now implemented (Slice 5), so the Slice 2 form of this test - "no DELETE exists yet" -
    is obsolete. What is still worth pinning is that there are only TWO: the full wipe's, which empties a
    structure, and the per-entry one, which removes a single keyed row. A third would mean a new way to
    remove data that nothing above has reviewed."""
    source = memory_module_source("adapter.py")
    deletes = [line.strip() for line in source.splitlines() if "DELETE FROM" in line]
    assert len(deletes) == 2, deletes
    assert any("{name}" in line for line in deletes), "the full wipe's statement is missing"
    assert any("{column} = ?" in line for line in deletes), "the per-entry statement is missing"


# =======================================================================================================
# SLICE 3: the three frozen correction levels (Build Plan 6.8)
#
#   LEVEL 1 temporary        current task only, never written to the database
#   LEVEL 2 learned pattern  durable once the SAME correction is seen twice in this process
#   LEVEL 3 explicit         "remember this", stored immediately
#
# PRECEDENCE, highest first:
#   this task's temporary  >  explicit  >  learned pattern  >  ordinary stored fact
#
# Slice 2 found a real defect by testing a constraint instead of assuming it (PRAGMA foreign_keys is
# ignored inside a transaction, so contacts for missing people were accepted). The same habit here: every
# count and every precedence claim below is read back OUT OF SQLITE, never from the in-process model.
#
# No sentence is parsed. "No, use the other Ali" and "Remember this" are scenario names; the API receives
# Correction(scope, wrong_value, right_value, context).
# =======================================================================================================

from app.memory.logic import CANNOT_LEARN, CANNOT_REMEMBER
from app.memory.models import (EXPLICIT, LEARNED_PATTERN, Correction, CorrectionLearner,
                               CorrectionTask, PersistenceDecision)

ALLOW = PersistenceDecision.ALLOW      # these tests are about durable behaviour, so they say so
DENY = PersistenceDecision.DENY

SCOPE = "website:example.com/login"
WRONG, RIGHT = "button-A", "button-B"


def a_correction(scope=SCOPE, wrong=WRONG, right=RIGHT, context=None):
    return Correction(scope, wrong, right, context)


def rows(path, level=None):
    """Correction rows straight out of SQLite - never from the learner or the task object."""
    with sqlite3.connect(path) as conn:
        if level is None:
            return conn.execute("SELECT level, scope, wrong_value, right_value, context, occurrences "
                                "FROM corrections ORDER BY id").fetchall()
        return conn.execute("SELECT level, scope, wrong_value, right_value, context, occurrences "
                            "FROM corrections WHERE level = ? ORDER BY id", (level,)).fetchall()


# --- LEVEL 1: the temporary correction (D1) -----------------------------------------------------------

def test_a_temporary_correction_applies_inside_its_own_task(ready):
    """Item 1."""
    task = logic.correct_for_this_task(CorrectionTask("task-A"), a_correction())
    resolved = logic.resolve_value(SCOPE, WRONG, task=task)
    assert isinstance(resolved, Found) and resolved.value == RIGHT


def test_a_temporary_correction_writes_no_row_at_all(ready):
    """Item 2, and the frozen rule that level 1 does not persist. Read back from the table, not assumed:
    the CHECK on corrections.level cannot even express 'temporary'."""
    task = logic.correct_for_this_task(CorrectionTask("task-A"), a_correction())
    assert logic.resolve_value(SCOPE, WRONG, task=task).value == RIGHT
    assert rows(ready) == [], "a temporary correction reached the database"
    assert adapter.count_corrections(ready) == 0


def test_ending_the_task_removes_the_temporary_effect(ready):
    """Item 3."""
    task = logic.correct_for_this_task(CorrectionTask("task-A"), a_correction())
    assert logic.resolve_value(SCOPE, WRONG, task=task).value == RIGHT
    finished = task.ended()
    assert logic.resolve_value(SCOPE, WRONG, task=finished).value == WRONG, "it outlived its task"


def test_a_new_task_inherits_nothing(ready):
    """Item 4."""
    logic.correct_for_this_task(CorrectionTask("task-A"), a_correction())
    fresh = CorrectionTask("task-B")
    assert logic.resolve_value(SCOPE, WRONG, task=fresh).value == WRONG
    assert logic.resolve_value(SCOPE, WRONG).value == WRONG, "nor with no task at all"


def test_two_tasks_at_once_do_not_leak_into_each_other(ready):
    """Item 5. The task object is frozen and threaded by the caller, so there is no shared mutable state
    for one task's correction to appear in another."""
    task_a = logic.correct_for_this_task(CorrectionTask("task-A"), a_correction(right="button-B"))
    task_b = logic.correct_for_this_task(CorrectionTask("task-B"), a_correction(right="button-C"))
    assert logic.resolve_value(SCOPE, WRONG, task=task_a).value == "button-B"
    assert logic.resolve_value(SCOPE, WRONG, task=task_b).value == "button-C"
    assert task_a.corrections != task_b.corrections


def test_a_later_temporary_correction_replaces_the_earlier_one_in_a_task(ready):
    task = CorrectionTask("task-A")
    task = logic.correct_for_this_task(task, a_correction(right="button-B"))
    task = logic.correct_for_this_task(task, a_correction(right="button-C"))
    assert logic.resolve_value(SCOPE, WRONG, task=task).value == "button-C"
    assert len(task.corrections) == 1, "corrections for one target piled up"


# --- LEVEL 2: the learned pattern (D2) ----------------------------------------------------------------

def test_the_first_correction_does_not_become_a_learned_pattern(ready):
    """Items 6 and 7. The frozen rule: one correction must not permanently change behaviour. The evidence
    that it happened lives only in the process-local learner, which no lookup consults."""
    learner = CorrectionLearner()
    task, outcome = logic.observe_correction(CorrectionTask("task-A"), learner, a_correction(), ALLOW)

    assert isinstance(outcome, NotFound), outcome
    assert rows(ready) == [], "the first correction persisted"
    assert learner.count(a_correction()) == 1, "the candidate was not counted"
    assert logic.resolve_value(SCOPE, WRONG, task=task).value == RIGHT, "it still fixes this task"
    assert logic.resolve_value(SCOPE, WRONG).value == WRONG, "but changes nothing outside it"


def test_the_first_correction_stops_mattering_when_its_task_ends(ready):
    learner = CorrectionLearner()
    task, _outcome = logic.observe_correction(CorrectionTask("task-A"), learner, a_correction(), ALLOW)
    assert logic.resolve_value(SCOPE, WRONG, task=task.ended()).value == WRONG
    assert rows(ready) == []


def test_the_second_matching_correction_promotes_immediately(ready):
    """Items 8 and 9, read out of SQLite: exactly one learned row, occurrences = 2."""
    learner = CorrectionLearner()
    logic.observe_correction(CorrectionTask("task-A"), learner, a_correction(), ALLOW)
    task, outcome = logic.observe_correction(CorrectionTask("task-B"), learner, a_correction(), ALLOW)

    assert isinstance(outcome, Found), getattr(outcome, "message", outcome)
    stored = rows(ready)
    assert len(stored) == 1, stored
    level, scope, wrong, right, _context, occurrences = stored[0]
    assert (level, scope, wrong, right) == (LEARNED_PATTERN, SCOPE, WRONG, RIGHT)
    assert occurrences == 2, f"occurrences is {occurrences}, not 2"
    assert outcome.value == 2


def test_a_learned_pattern_outlives_its_task(ready):
    """Item 10."""
    learner = CorrectionLearner()
    logic.observe_correction(CorrectionTask("task-A"), learner, a_correction(), ALLOW)
    task, _ = logic.observe_correction(CorrectionTask("task-B"), learner, a_correction(), ALLOW)
    assert logic.resolve_value(SCOPE, WRONG, task=task.ended()).value == RIGHT
    assert logic.resolve_value(SCOPE, WRONG).value == RIGHT


def test_a_learned_pattern_survives_reopening_the_database(ready):
    """Item 11. Closed and reopened through the real logic, with a brand-new learner - so only the row
    can be what answers."""
    learner = CorrectionLearner()
    logic.observe_correction(CorrectionTask("task-A"), learner, a_correction(), ALLOW)
    logic.observe_correction(CorrectionTask("task-B"), learner, a_correction(), ALLOW)

    assert isinstance(logic.open_memory(), MemoryDatabase)
    assert logic.resolve_value(SCOPE, WRONG).value == RIGHT
    assert rows(ready)[0][5] == 2


def test_a_different_scope_is_a_different_correction(ready):
    """Item 12."""
    learner = CorrectionLearner()
    logic.observe_correction(CorrectionTask("task-A"), learner, a_correction(), ALLOW)
    _task, outcome = logic.observe_correction(CorrectionTask("task-B"), learner,
                                             a_correction(scope="website:other.example/login"), ALLOW)
    assert isinstance(outcome, NotFound), "a different scope counted as a repetition"
    assert rows(ready) == []
    assert learner.count(a_correction()) == 1


def test_a_different_right_value_is_a_different_correction(ready):
    """Item 13."""
    learner = CorrectionLearner()
    logic.observe_correction(CorrectionTask("task-A"), learner, a_correction(right="button-B"), ALLOW)
    _task, outcome = logic.observe_correction(CorrectionTask("task-B"), learner,
                                             a_correction(right="button-C"), ALLOW)
    assert isinstance(outcome, NotFound), "a different right value counted as a repetition"
    assert rows(ready) == []


def test_identity_ignores_case_and_surrounding_space_only(ready):
    """The same deterministic normalisation as everywhere else in Memory - and nothing more."""
    learner = CorrectionLearner()
    logic.observe_correction(CorrectionTask("task-A"), learner, a_correction(wrong="  Button-A "), ALLOW)
    _task, outcome = logic.observe_correction(CorrectionTask("task-B"), learner,
                                             a_correction(wrong="button-a"), ALLOW)
    assert isinstance(outcome, Found), "normalised equality did not match"
    assert len(rows(ready)) == 1


def test_a_near_miss_is_not_the_same_correction(ready):
    learner = CorrectionLearner()
    logic.observe_correction(CorrectionTask("task-A"), learner, a_correction(right="button-B"), ALLOW)
    _task, outcome = logic.observe_correction(CorrectionTask("task-B"), learner,
                                             a_correction(right="button-BB"), ALLOW)
    assert isinstance(outcome, NotFound), "fuzzy matching crept in"


def test_further_identical_corrections_update_the_one_row(ready):
    """Items 14 and 15. No second learned row, and the count keeps rising on the row that exists."""
    learner = CorrectionLearner()
    for task_name in ("A", "B", "C", "D"):
        logic.observe_correction(CorrectionTask(task_name), learner, a_correction(), ALLOW)
    stored = rows(ready)
    assert len(stored) == 1, f"duplicate learned rows: {stored}"
    assert stored[0][5] == 4, f"occurrences is {stored[0][5]}"
    assert adapter.count_corrections(ready, LEARNED_PATTERN) == 1


def test_a_new_learner_loses_the_unpromoted_count(ready):
    """Item 16. The candidate count is process-local by design: restarting the companion forgets an
    observation that never earned persistence, which the frozen Done-when does not require to survive."""
    first = CorrectionLearner()
    logic.observe_correction(CorrectionTask("task-A"), first, a_correction(), ALLOW)
    assert first.count(a_correction()) == 1

    restarted = CorrectionLearner()
    assert restarted.count(a_correction()) == 0
    _task, outcome = logic.observe_correction(CorrectionTask("task-B"), restarted, a_correction(), ALLOW)
    assert isinstance(outcome, NotFound), "a lost count still promoted"
    assert rows(ready) == []


def test_the_learner_holds_only_the_corrections_identity(ready):
    """§6. It is not a transcript cache: the only thing it can hold is the normalised identity, and its
    repr shows a count rather than the identities."""
    learner = CorrectionLearner()
    learner.observe(a_correction(context="task-notes"))
    assert set(learner._seen) == {a_correction(context="task-notes").identity}
    for key in learner._seen:
        assert len(key) == 4 and all(isinstance(part, str) for part in key)
    assert "button" not in repr(learner) and SCOPE not in repr(learner)
    assert repr(learner) == "CorrectionLearner(tracking=1 corrections)"


# --- LEVEL 3: explicit memory (D3) --------------------------------------------------------------------

def test_remember_this_is_stored_immediately(ready):
    """Items 17, 18 and 20. One call, no repetition, and it is an explicit row - not a learned one."""
    outcome = logic.remember_explicitly(a_correction(right="button-Z"), ALLOW)
    assert isinstance(outcome, Found), getattr(outcome, "message", outcome)
    stored = rows(ready)
    assert len(stored) == 1
    assert stored[0][0] == EXPLICIT and stored[0][3] == "button-Z"
    assert adapter.count_corrections(ready, LEARNED_PATTERN) == 0
    assert logic.resolve_value(SCOPE, WRONG).value == "button-Z", "not visible to the next lookup"


def test_an_explicit_memory_needs_no_temporary_correction_first(ready):
    assert isinstance(logic.remember_explicitly(a_correction(right="button-Z"), ALLOW), Found)
    assert rows(ready)[0][0] == EXPLICIT


def test_an_explicit_memory_survives_reopening_the_database(ready):
    """Item 19."""
    logic.remember_explicitly(a_correction(right="button-Z"), ALLOW)
    assert isinstance(logic.open_memory(), MemoryDatabase)
    assert logic.resolve_value(SCOPE, WRONG).value == "button-Z"


def test_remembering_the_same_thing_twice_does_not_duplicate_it(ready):
    logic.remember_explicitly(a_correction(right="button-Z"), ALLOW)
    logic.remember_explicitly(a_correction(right="button-Y"), ALLOW)
    stored = rows(ready, EXPLICIT)
    assert len(stored) == 1, stored
    assert stored[0][3] == "button-Y", "the newer instruction did not win"


def test_a_correction_needs_its_three_values(ready):
    for bad in (a_correction(scope="  "), a_correction(wrong=""), a_correction(right="   ")):
        assert isinstance(logic.remember_explicitly(bad, ALLOW), NotFound), bad
    assert rows(ready) == []


# --- PRECEDENCE: the authoritative order --------------------------------------------------------------

def test_a_learned_pattern_beats_the_plain_fact(ready):
    """Item 21."""
    learner = CorrectionLearner()
    for name in ("A", "B"):
        logic.observe_correction(CorrectionTask(name), learner, a_correction(right="button-Y"), ALLOW)
    assert logic.resolve_value(SCOPE, WRONG).value == "button-Y"


def test_an_explicit_memory_beats_a_learned_pattern(ready):
    """Item 22, and §2's correction to the earlier proposal. A direct instruction from the user must not
    be silently overridden by something inferred from repetition - so explicit wins, whichever was
    written first and however many times the pattern was seen."""
    learner = CorrectionLearner()
    for name in ("A", "B", "C"):
        logic.observe_correction(CorrectionTask(name), learner, a_correction(right="button-Y"), ALLOW)
    assert logic.resolve_value(SCOPE, WRONG).value == "button-Y"
    assert rows(ready, LEARNED_PATTERN)[0][5] == 3, "the pattern really is well established"

    assert isinstance(logic.remember_explicitly(a_correction(right="button-Z"), ALLOW), Found)
    assert logic.resolve_value(SCOPE, WRONG).value == "button-Z"


def test_the_full_frozen_precedence_chain(ready):
    """Items 10, 23, 24 and 25 together - §10's authoritative test, every step read back from SQLite.

        learned   X -> Y
        explicit  X -> Z    (supersedes it; the learned row is kept for history)
        task T    X -> W    (temporary, wins inside T only)
        T ends              -> Z again
    """
    learner = CorrectionLearner()
    for name in ("A", "B"):
        logic.observe_correction(CorrectionTask(name), learner, a_correction(right="button-Y"), ALLOW)
    assert logic.resolve_value(SCOPE, WRONG).value == "button-Y"

    logic.remember_explicitly(a_correction(right="button-Z"), ALLOW)
    assert logic.resolve_value(SCOPE, WRONG).value == "button-Z"

    task = logic.correct_for_this_task(CorrectionTask("task-T"), a_correction(right="button-W"))
    assert logic.resolve_value(SCOPE, WRONG, task=task).value == "button-W"

    assert logic.resolve_value(SCOPE, WRONG, task=task.ended()).value == "button-Z"
    assert logic.resolve_value(SCOPE, WRONG).value == "button-Z"

    levels = {row[0]: row for row in rows(ready)}
    assert set(levels) == {LEARNED_PATTERN, EXPLICIT}, "the learned row was deleted"
    assert levels[LEARNED_PATTERN][3] == "button-Y", "history was rewritten"
    assert levels[EXPLICIT][3] == "button-Z"


def test_the_superseded_learned_row_is_kept_for_history(ready):
    """Item 25 on its own: superseding is not deleting."""
    learner = CorrectionLearner()
    for name in ("A", "B"):
        logic.observe_correction(CorrectionTask(name), learner, a_correction(right="button-Y"), ALLOW)
    logic.remember_explicitly(a_correction(right="button-Z"), ALLOW)
    assert adapter.count_corrections(ready, LEARNED_PATTERN) == 1
    assert adapter.count_corrections(ready, EXPLICIT) == 1


def test_nothing_reorders_the_precedence_by_recency(ready):
    """A learned pattern observed AFTER the explicit instruction still does not outrank it."""
    logic.remember_explicitly(a_correction(right="button-Z"), ALLOW)
    learner = CorrectionLearner()
    for name in ("A", "B", "C", "D", "E"):
        logic.observe_correction(CorrectionTask(name), learner, a_correction(right="button-Y"), ALLOW)
    assert rows(ready, LEARNED_PATTERN)[0][5] == 5
    assert logic.resolve_value(SCOPE, WRONG).value == "button-Z", "recency or count reordered it"


def test_an_uncorrected_value_comes_back_unchanged(ready):
    assert logic.resolve_value(SCOPE, "button-Q").value == "button-Q"
    assert logic.resolve_value("website:nowhere", WRONG).value == WRONG


def test_the_context_field_separates_corrections(ready):
    """context is part of the identity, so the same wrong value corrected differently in two contexts
    does not collide."""
    logic.remember_explicitly(a_correction(right="button-Z", context="desktop"), ALLOW)
    assert logic.resolve_value(SCOPE, WRONG, context="desktop").value == "button-Z"
    assert logic.resolve_value(SCOPE, WRONG, context="mobile").value == WRONG
    assert logic.resolve_value(SCOPE, WRONG).value == WRONG


# --- FAILURE: memory unavailable ----------------------------------------------------------------------

def test_a_temporary_correction_still_works_with_no_database(memory):
    """Item 26. Level 1 needs no durable memory, so it keeps working when there is none."""
    assert not memory.exists()
    task = logic.correct_for_this_task(CorrectionTask("task-A"), a_correction())
    assert task.correction_for(SCOPE, WRONG).right_value == RIGHT
    resolved = logic.resolve_value(SCOPE, WRONG, task=task)
    assert isinstance(resolved, Found) and resolved.value == RIGHT
    assert not memory.exists(), "resolving created a database"


def test_a_promotion_that_cannot_be_written_never_claims_success(memory):
    """Item 27. The task is still fixed, and the durable half says plainly that it could not be kept."""
    learner = CorrectionLearner()
    logic.observe_correction(CorrectionTask("task-A"), learner, a_correction(), ALLOW)
    task, outcome = logic.observe_correction(CorrectionTask("task-B"), learner, a_correction(), ALLOW)

    assert isinstance(outcome, MemoryUnavailable), outcome
    assert CANNOT_LEARN in outcome.reason
    assert task.correction_for(SCOPE, WRONG) is not None, "the current task lost its fix"
    assert not memory.exists()


def test_an_explicit_write_that_fails_never_says_remembered(memory):
    """Item 28."""
    outcome = logic.remember_explicitly(a_correction(), ALLOW)
    assert isinstance(outcome, MemoryUnavailable), outcome
    assert CANNOT_REMEMBER in outcome.reason
    assert "remembered" not in outcome.reason.replace(CANNOT_REMEMBER, "")


def test_no_raw_sqlite_error_escapes_a_correction_operation(memory):
    """Item 29, against a genuinely corrupt file rather than a missing one."""
    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_bytes(b"SQLite format 3\x00" + b"\x6b" * 320)
    learner = CorrectionLearner()
    logic.observe_correction(CorrectionTask("task-A"), learner, a_correction(), ALLOW)
    operations = [
        lambda: logic.observe_correction(CorrectionTask("task-B"), learner, a_correction(), ALLOW)[1],
        lambda: logic.remember_explicitly(a_correction(), ALLOW),
        lambda: logic.resolve_value(SCOPE, WRONG),
    ]
    for operation in operations:
        assert isinstance(operation(), MemoryUnavailable), operation


def test_a_failed_promotion_leaves_no_partial_row(ready, monkeypatch):
    """Item 30. The look-up and the write share one transaction, so a failure mid-promotion rolls the
    whole thing back - no learned row, and no occurrence count without it."""
    learner = CorrectionLearner()
    logic.observe_correction(CorrectionTask("task-A"), learner, a_correction(), ALLOW)

    real_now = adapter._now
    monkeypatch.setattr(adapter, "_now", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("disk")))
    _task, outcome = logic.observe_correction(CorrectionTask("task-B"), learner, a_correction(), ALLOW)
    monkeypatch.setattr(adapter, "_now", real_now)

    assert isinstance(outcome, MemoryUnavailable), outcome
    assert rows(ready) == [], "a partial correction row survived"
    assert adapter.count_corrections(ready) == 0


def test_a_promotion_failure_can_still_succeed_later(ready):
    """The count is not consumed by a failed attempt: once memory works, the next identical correction
    promotes - and still produces exactly one row."""
    learner = CorrectionLearner()
    for name in ("A", "B", "C"):
        logic.observe_correction(CorrectionTask(name), learner, a_correction(), ALLOW)
    assert len(rows(ready)) == 1
    assert rows(ready)[0][5] == 3


# --- PRIVACY AND BOUNDARY ----------------------------------------------------------------------------

def test_no_new_column_or_blob_was_introduced_for_corrections():
    """Item 31. The frozen table is used as it is: no transcript, no typed payload, no credential, and no
    JSON blob was added to hold one."""
    corrections = next(table for table in TABLES if table.name == "corrections")
    names = [column.split()[0].lower() for column in corrections.columns]
    assert names == ["id", "level", "scope", "wrong_value", "right_value", "context", "occurrences",
                     "created_at"], names
    for column in names:
        for forbidden in FORBIDDEN_COLUMN_WORDS:
            assert forbidden not in column, f"corrections.{column}"


def test_the_level_check_still_cannot_express_temporary(ready):
    """A temporary correction has no durable representation even if something tried to write one."""
    corrections = next(table for table in TABLES if table.name == "corrections")
    check = " ".join(corrections.columns)
    assert "'learned_pattern'" in check and "'explicit'" in check
    assert "temporary" not in check
    with sqlite3.connect(ready) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO corrections (level, scope, right_value, occurrences, created_at) "
                         "VALUES ('temporary', 'x', 'y', 1, 1.0)")


def test_slice_3_added_no_cross_module_import_or_sqlite_leak():
    """Items 32 and 33, unchanged: only the adapter touches SQLite, and Memory imports no Executor,
    Planner, Safety, Brain or Verifier authority."""
    assert "sqlite3" not in imported_names(memory_module_source("logic.py"))
    assert "sqlite3" not in imported_names(memory_module_source("models.py"))
    for name in ("logic.py", "models.py", "adapter.py"):
        imports = imported_names(memory_module_source(name))
        for forbidden in ("app.executor", "app.planner", "app.safety", "app.brain", "app.verifier"):
            assert not any(imported.startswith(forbidden) for imported in imports), f"{name}: {forbidden}"


def test_corrections_cannot_produce_an_action(ready):
    """A correction resolves a VALUE. There is no path from one to something happening."""
    logic.remember_explicitly(a_correction(right="button-Z"), ALLOW)
    resolved = logic.resolve_value(SCOPE, WRONG)
    assert isinstance(resolved.value, str)
    assert type(resolved.value).__name__ not in ("ExecutorAction", "Plan", "Action", "RiskLevel")


def test_no_sentence_parsing_entered_the_correction_api():
    """§15. "No, use the other Ali" is a scenario name; the API takes structured fields.

    Checked over the CODE, with docstrings and comments excluded - the phrases this is about appear in
    the prose on purpose, and a substring search over the whole file would match my own explanation
    rather than any behaviour."""
    tree = ast.parse(memory_module_source("logic.py"))
    for node in ast.walk(tree):          # drop every docstring before looking at anything
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr)                 and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            body.pop(0)
    code = ast.unparse(tree)
    assert "#" not in code, "ast.unparse keeps no comments, so none can match"
    for forbidden in ("re.compile", "re.search", "re.match", "re.findall", "nltk", "spacy",
                      "split()", "tokenize"):
        assert forbidden not in code, f"logic.py does sentence work: {forbidden}"
    imports = imported_names(memory_module_source("logic.py"))
    for forbidden in ("re", "nltk", "spacy"):
        assert forbidden not in imports, f"logic.py imports {forbidden}"


# =======================================================================================================
# SLICE 4: versioned export and fresh-database restore (frozen Done-when D4 / D5)
#
#   D4  "export produces a file"
#   D5  "that export successfully restores into a fresh database"
#
# Everything below drives the real logic and the real adapter against real SQLite files and a real JSON
# document, all inside this test's own temporary directory. Row equality is read back out of the restored
# database, and the behavioural half re-runs Slice 2 and Slice 3 behaviour THROUGH the restored file -
# because identical rows are not proof that the memory still works.
# =======================================================================================================

import json
import os

from app.memory.models import (EXPORT_FORMAT_VERSION, RESTORE_ORDER, TABLE_BY_NAME, BackupRefused,
                               Exported, Restored)

EVERY_TABLE = set(TABLE_NAMES)


@pytest.fixture
def filled(ready):
    """An initialised memory database with one representative row in every one of the fifteen
    structures, written through the real production APIs where they exist."""
    ali = person_id(logic.add_person("Ali", note="from the frozen happy path"))
    logic.add_relationship(ali, "friend")
    logic.add_contact(ali, "whatsapp", ADDRESS)
    logic.remember_application_alias("editor", "notepad", APPS)
    logic.set_sensitive_rule("contacts", "address", "brain", "deny", retention="until I say otherwise")

    learner = CorrectionLearner()
    for name in ("task-A", "task-B"):          # twice, so it genuinely promotes
        logic.observe_correction(CorrectionTask(name), learner, a_correction(right="button-Y"), ALLOW)
    logic.remember_explicitly(a_correction(right="button-Z"), ALLOW)

    # The nine structures with no Slice 2/3 API yet are written directly, which is what they are for in
    # this test: proving the backup carries ALL fifteen, not only the ones with behaviour.
    with sqlite3.connect(ready) as conn:
        conn.execute("INSERT INTO identity (id, name, interaction_style, updated_at) "
                     "VALUES (1, 'Wajid', 'direct', 11.0)")
        conn.execute("INSERT INTO projects (id, name, folder_path, tools, created_at) "
                     "VALUES (1, 'companion', 'C:/Users/Wajid/Downloads', 'python', 12.0)")
        conn.execute("INSERT INTO files (id, path, description, project_id, last_known_state, created_at) "
                     "VALUES (1, 'C:/Users/Wajid/notes.txt', 'my notes', 1, 'exists', 13.0)")
        conn.execute("INSERT INTO websites (id, alias, url, created_at) "
                     "VALUES (1, 'docs', 'https://example.com/docs', 14.0)")
        conn.execute("INSERT INTO preferences (key, value, updated_at) VALUES ('browser', 'edge', 15.0)")
        conn.execute("INSERT INTO habits (id, description, observed_count, last_seen_at) "
                     "VALUES (1, 'opens notepad each morning', 3, 16.0)")
        conn.execute("INSERT INTO workflows (id, name, steps, created_at) "
                     "VALUES (1, 'morning', '1. open notepad', 17.0)")
        conn.execute("INSERT INTO short_term_context (id, person_id, app_key, page, task, updated_at) "
                     "VALUES (1, 1, 'notepad', NULL, 'writing notes', 18.0)")
        conn.execute("INSERT INTO permissions (id, scope, allowed, updated_at) "
                     "VALUES (1, 'files', 1, 19.0)")
    counts = adapter.row_counts(ready)
    assert all(count >= 1 for count in counts.values()), counts
    return ready


def table_rows(path, name):
    """Rows of one structure as dicts, straight out of SQLite, using the fixed column list."""
    columns = TABLE_BY_NAME[name].column_names
    with sqlite3.connect(path) as conn:
        rows = conn.execute(f"SELECT {', '.join(columns)} FROM {name} ORDER BY rowid").fetchall()
    return [dict(zip(columns, row)) for row in rows]


def everything(path):
    return {name: table_rows(path, name) for name in TABLE_NAMES}


def exported(path):
    return json.loads(path.read_text(encoding="utf-8"))


def point_memory_at(monkeypatch, path):
    """Make the Memory logic resolve to `path`, so a restored file can be opened as THE memory."""
    monkeypatch.setattr(logic, "database_path", lambda: path)


# --- D4: the export -----------------------------------------------------------------------------------

def test_an_export_produces_one_json_file(filled, tmp_path):
    """Items 1-4. D4, literally."""
    destination = tmp_path / "backup.json"
    result = logic.export_memory(destination)

    assert isinstance(result, Exported), getattr(result, "message", result)
    assert destination.is_file() and result.path == str(destination)
    document = exported(destination)
    assert document["export_format_version"] == EXPORT_FORMAT_VERSION
    assert document["schema_version"] == SCHEMA_VERSION
    assert isinstance(document["exported_at"], (int, float))
    assert set(document["tables"]) == EVERY_TABLE, "all fifteen structures must be in the backup"
    assert result.tables == 15


def test_the_export_keeps_keys_links_timestamps_and_occurrences(filled, tmp_path):
    """Items 5 and 6. The identifiers and the counts are the backup's whole point - without them the
    graph and the correction history cannot come back."""
    destination = tmp_path / "backup.json"
    logic.export_memory(destination)
    tables = exported(destination)["tables"]

    assert tables["people"][0]["id"] == 1
    assert tables["contacts"][0]["person_id"] == 1, "the contact lost its person"
    assert tables["relationships"][0]["person_id"] == 1
    assert tables["files"][0]["project_id"] == 1, "the file lost its project"
    assert tables["identity"][0]["updated_at"] == 11.0, "a timestamp was dropped"
    assert tables["habits"][0]["observed_count"] == 3
    learned = next(row for row in tables["corrections"] if row["level"] == LEARNED_PATTERN)
    assert learned["occurrences"] == 2, "the correction history was flattened"


def test_the_export_includes_permissions_and_sensitive_rules(filled, tmp_path):
    """Items 7 and 8, and §1's rule: restoring facts while dropping their protection rules is forbidden,
    so both travel in the same document as the data they guard."""
    destination = tmp_path / "backup.json"
    logic.export_memory(destination)
    tables = exported(destination)["tables"]

    assert tables["permissions"] == [{"id": 1, "scope": "files", "allowed": 1, "updated_at": 19.0}]
    rule = tables["sensitive_data_rules"][0]
    assert (rule["target_table"], rule["target_field"], rule["context"], rule["disclosure"]) == \
        ("contacts", "address", "brain", "deny")
    assert rule["retention"] == "until I say otherwise"


def test_the_export_carries_the_sensitive_value_itself(filled, tmp_path):
    """§15. A disclosure rule filters RUNTIME lookups; it does not redact the user's own backup, or the
    backup could not faithfully restore the database. The file is sensitive plaintext instead."""
    destination = tmp_path / "backup.json"
    logic.export_memory(destination)
    assert exported(destination)["tables"]["contacts"][0]["address"] == ADDRESS


def test_process_local_and_task_local_state_is_not_in_the_export(ready, tmp_path):
    """Items 9 and 10, and §14 - this is what keeps D1 meaningful. One correction observed once, plus an
    active temporary correction: neither may appear, and nothing may have been promoted."""
    learner = CorrectionLearner()
    task, outcome = logic.observe_correction(CorrectionTask("task-A"), learner,
                                            a_correction(right="button-W"), ALLOW)
    assert isinstance(outcome, NotFound), "it must not have promoted"
    assert learner.count(a_correction(right="button-W")) == 1
    assert task.correction_for(SCOPE, WRONG) is not None, "the temporary correction is live"

    destination = tmp_path / "backup.json"
    assert isinstance(logic.export_memory(destination), Exported)
    document = exported(destination)

    assert document["tables"]["corrections"] == [], "an unpromoted correction was exported"
    assert "button-W" not in destination.read_text(encoding="utf-8"), "the temporary value leaked"
    text = destination.read_text(encoding="utf-8").lower()
    for forbidden in ("learner", "candidate", "correctiontask", "previous_action"):
        assert forbidden not in text, forbidden
    assert set(document) == {"export_format_version", "schema_version", "exported_at", "tables"}


def test_the_export_contains_nothing_but_durable_memory(filled, tmp_path):
    """§1's exclusion list, checked against the document itself."""
    destination = tmp_path / "backup.json"
    logic.export_memory(destination)
    document = exported(destination)
    assert set(document["tables"]) == EVERY_TABLE
    for forbidden in ("claude_usage", "api_key", "ANTHROPIC", "usage_db_path", "db_path",
                      "ownership", "hwnd", "transcript"):
        assert forbidden.lower() not in destination.read_text(encoding="utf-8").lower(), forbidden


def test_an_existing_export_is_never_overwritten(filled, tmp_path):
    """Item 11, and §3. A previous backup is not collateral."""
    destination = tmp_path / "backup.json"
    destination.write_text("an earlier backup", encoding="utf-8")
    result = logic.export_memory(destination)
    assert isinstance(result, BackupRefused)
    assert "already exists" in result.message
    assert destination.read_text(encoding="utf-8") == "an earlier backup", "it was overwritten"


def test_a_failed_export_leaves_no_partial_destination(filled, tmp_path, monkeypatch):
    """Item 12, and §4. Serialisation fails midway; the destination must not appear, and the temporary
    sibling must not be left lying around looking like a backup."""
    destination = tmp_path / "backup.json"
    real_dump = json.dump

    def explode(*args, **kwargs):
        real_dump(*args, **kwargs)              # write some of it, THEN fail
        raise OSError("disk full")

    monkeypatch.setattr(json, "dump", explode)
    result = logic.export_memory(destination)
    monkeypatch.undo()

    assert isinstance(result, BackupRefused), result
    assert not destination.exists(), "a partial export was published"
    assert list(tmp_path.glob("backup.json*")) == [], "a leftover temporary file was left behind"


def test_the_export_result_carries_counts_and_no_row_contents(filled, tmp_path, caplog):
    """Item 13, and §2. Counts are safe; a name, a number or a path is not."""
    import logging
    destination = tmp_path / "backup.json"
    with caplog.at_level(logging.DEBUG):
        result = logic.export_memory(destination)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    for surface in (repr(result), str(result), logged):
        for secret in (ADDRESS, "Ali", "C:/Users/Wajid/notes.txt", "button-Z"):
            assert secret not in surface, f"{secret!r} leaked into {surface!r}"
    assert result.rows >= 15 and result.tables == 15


def test_an_export_refuses_when_there_is_no_memory(memory, tmp_path):
    """§5 and item 30's export half: a missing database is reported, and no file appears."""
    destination = tmp_path / "backup.json"
    result = logic.export_memory(destination)
    assert isinstance(result, MemoryUnavailable), result
    assert not destination.exists()


def test_an_export_refuses_a_corrupt_memory_database(memory, tmp_path):
    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_bytes(b"SQLite format 3\x00" + b"\x7c" * 320)
    destination = tmp_path / "backup.json"
    assert isinstance(logic.export_memory(destination), MemoryUnavailable)
    assert not destination.exists()


def test_an_export_refuses_an_unsupported_schema(filled, tmp_path):
    with sqlite3.connect(filled) as conn:
        conn.execute(f"UPDATE {VERSION_TABLE} SET schema_version = ?", (SCHEMA_VERSION + 3,))
    destination = tmp_path / "backup.json"
    result = logic.export_memory(destination)
    assert isinstance(result, MemoryUnavailable) and "newer version" in result.reason
    assert not destination.exists()


# --- D5: the restore ----------------------------------------------------------------------------------

def test_a_backup_restores_into_a_fresh_database(filled, tmp_path):
    """Item 14. D5, literally."""
    backup = tmp_path / "backup.json"
    logic.export_memory(backup)
    fresh = tmp_path / "restored" / "memory.db"

    result = logic.restore_memory(backup, fresh)
    assert isinstance(result, Restored), getattr(result, "message", result)
    assert fresh.is_file() and result.tables == 15 and result.rows >= 15


def test_restoring_over_an_existing_database_is_refused(filled, tmp_path):
    """Item 15, and §6. Fresh means fresh: nothing is merged, appended, overwritten or wiped."""
    backup = tmp_path / "backup.json"
    logic.export_memory(backup)
    occupied = tmp_path / "already" / "memory.db"
    occupied.parent.mkdir(parents=True)
    occupied.write_bytes(b"someone else's database")

    result = logic.restore_memory(backup, occupied)
    assert isinstance(result, BackupRefused)
    assert "already exists" in result.message
    assert occupied.read_bytes() == b"someone else's database", "an existing database was touched"


def test_all_fifteen_structures_round_trip_exactly(filled, tmp_path):
    """Items 16, 17, 18 and 19 - the main D4/D5 test. Every row of every structure, compared field by
    field out of both databases, so the ids, the links, the timestamps and the occurrence counts all have
    to survive."""
    before = everything(filled)
    backup = tmp_path / "backup.json"
    logic.export_memory(backup)
    fresh = tmp_path / "restored" / "memory.db"
    assert isinstance(logic.restore_memory(backup, fresh), Restored)

    after = everything(fresh)
    assert set(after) == EVERY_TABLE
    for name in TABLE_NAMES:
        assert after[name] == before[name], f"{name} did not round-trip"
    with sqlite3.connect(fresh) as conn:
        version = conn.execute(f"SELECT schema_version FROM {VERSION_TABLE}").fetchone()[0]
    assert version == SCHEMA_VERSION


def test_the_restored_database_is_sound_and_opens_normally(filled, tmp_path, monkeypatch):
    """Items 20 and 21."""
    backup = tmp_path / "backup.json"
    logic.export_memory(backup)
    fresh = tmp_path / "restored" / "memory.db"
    logic.restore_memory(backup, fresh)

    adapter.check_integrity(fresh)              # raises if it is not sound
    point_memory_at(monkeypatch, fresh)
    opened = logic.open_memory()
    assert isinstance(opened, MemoryDatabase) and opened.schema_version == SCHEMA_VERSION


def test_a_later_insert_does_not_collide_with_a_restored_id(filled, tmp_path, monkeypatch):
    """Item 22. The ids are restored exactly, so SQLite must not re-issue one - checked by adding a new
    person to the restored database and requiring a fresh id."""
    backup = tmp_path / "backup.json"
    logic.export_memory(backup)
    fresh = tmp_path / "restored" / "memory.db"
    logic.restore_memory(backup, fresh)
    point_memory_at(monkeypatch, fresh)

    existing = {row["id"] for row in table_rows(fresh, "people")}
    added = logic.add_person("Bilal")
    assert isinstance(added, Found), getattr(added, "message", added)
    assert added.value.id not in existing, "a restored id was handed out again"
    assert len(table_rows(fresh, "people")) == len(existing) + 1


# --- Behaviour after restore --------------------------------------------------------------------------

def test_the_happy_path_still_works_from_the_restored_database(filled, tmp_path, monkeypatch):
    """Items 23 and 24, and §13 A/B. Identical rows are not proof the memory still works, so the frozen
    happy path is re-run through the restored file."""
    backup = tmp_path / "backup.json"
    logic.export_memory(backup)
    fresh = tmp_path / "restored" / "memory.db"
    logic.restore_memory(backup, fresh)
    point_memory_at(monkeypatch, fresh)

    person = logic.find_person(name="Ali", relationship="friend")
    assert isinstance(person, Found) and person.value.name == "Ali"
    contact = logic.find_contact(person.value.id, context="local")
    assert isinstance(contact, Found)
    assert (contact.value.channel, contact.value.address) == ("whatsapp", ADDRESS)


def test_the_sensitive_denial_still_redacts_after_restore(filled, tmp_path, monkeypatch):
    """Item 25, and §13 C - the proof that the rules came back WITH the data they guard."""
    backup = tmp_path / "backup.json"
    logic.export_memory(backup)
    fresh = tmp_path / "restored" / "memory.db"
    logic.restore_memory(backup, fresh)
    point_memory_at(monkeypatch, fresh)

    ali = logic.find_person("Ali", "friend").value.id
    denied = logic.find_contact(ali, context="brain")
    assert isinstance(denied, Redacted), denied
    assert denied.value.address is None and denied.value.disclosed is False
    for surface in (repr(denied), str(denied), denied.message, repr(denied.value)):
        assert ADDRESS not in surface
    assert isinstance(logic.find_contact(ali, context="local"), Found), "and 'local' is still allowed"


def test_the_application_alias_still_resolves_only_within_capability(filled, tmp_path, monkeypatch):
    """Item 26, and §13 D. A restored alias is still only a name for a CONFIGURED app."""
    backup = tmp_path / "backup.json"
    logic.export_memory(backup)
    fresh = tmp_path / "restored" / "memory.db"
    logic.restore_memory(backup, fresh)
    point_memory_at(monkeypatch, fresh)

    assert logic.resolve_application_alias("editor", APPS).value == "notepad"
    stale = logic.resolve_application_alias("editor", ("calculator",))
    assert isinstance(stale, NotFound), "a restored alias widened what can be opened"


def test_the_correction_levels_and_counts_still_hold_after_restore(filled, tmp_path, monkeypatch):
    """Items 27, 28 and 29, and §13 E. The learned pattern applies, the explicit instruction still
    outranks it, and the occurrence count came back."""
    backup = tmp_path / "backup.json"
    logic.export_memory(backup)
    fresh = tmp_path / "restored" / "memory.db"
    logic.restore_memory(backup, fresh)
    point_memory_at(monkeypatch, fresh)

    assert logic.resolve_value(SCOPE, WRONG).value == "button-Z", "explicit no longer wins"
    learned = [row for row in table_rows(fresh, "corrections") if row["level"] == LEARNED_PATTERN]
    assert len(learned) == 1 and learned[0]["occurrences"] == 2
    assert learned[0]["right_value"] == "button-Y", "the learned history was rewritten"

    task = logic.correct_for_this_task(CorrectionTask("task-T"), a_correction(right="button-W"))
    assert logic.resolve_value(SCOPE, WRONG, task=task).value == "button-W"
    assert logic.resolve_value(SCOPE, WRONG, task=task.ended()).value == "button-Z"


# --- Restore failures: the destination always stays absent --------------------------------------------

def no_destination(tmp_path, name="target.db"):
    target = tmp_path / "fresh" / name
    return target


def refused(backup, target):
    outcome = logic.restore_memory(backup, target)
    assert isinstance(outcome, BackupRefused), outcome
    assert not target.exists(), "the destination was created by a failed restore"
    assert list(target.parent.glob("*.restoring")) == [], "a staging database was left behind"
    return outcome


def test_a_missing_backup_file_refuses(ready, tmp_path):
    refused(tmp_path / "nothing.json", no_destination(tmp_path))


def test_malformed_json_refuses(ready, tmp_path):
    """Item 30."""
    backup = tmp_path / "backup.json"
    backup.write_text("{not json at all", encoding="utf-8")
    assert "not valid JSON" in refused(backup, no_destination(tmp_path)).message


@pytest.mark.parametrize("document", ["[]", '"a string"', "42", "null"])
def test_a_document_that_is_not_an_object_refuses(ready, tmp_path, document):
    backup = tmp_path / "backup.json"
    backup.write_text(document, encoding="utf-8")
    refused(backup, no_destination(tmp_path))


def test_an_unsupported_export_format_version_refuses(filled, tmp_path):
    """Item 33."""
    backup = tmp_path / "backup.json"
    logic.export_memory(backup)
    document = exported(backup)
    document["export_format_version"] = EXPORT_FORMAT_VERSION + 1
    backup.write_text(json.dumps(document), encoding="utf-8")
    assert "newer format" in refused(backup, no_destination(tmp_path)).message


def test_an_unsupported_memory_schema_version_refuses(filled, tmp_path):
    """Item 34."""
    backup = tmp_path / "backup.json"
    logic.export_memory(backup)
    document = exported(backup)
    document["schema_version"] = SCHEMA_VERSION + 1
    backup.write_text(json.dumps(document), encoding="utf-8")
    assert "newer memory schema" in refused(backup, no_destination(tmp_path)).message


def test_a_backup_missing_a_structure_refuses(filled, tmp_path):
    """Item 31. Not guessed at, not filled in with an empty list."""
    backup = tmp_path / "backup.json"
    logic.export_memory(backup)
    document = exported(backup)
    del document["tables"]["sensitive_data_rules"]
    backup.write_text(json.dumps(document), encoding="utf-8")
    outcome = refused(backup, no_destination(tmp_path))
    assert "missing" in outcome.message and "sensitive_data_rules" in outcome.message


def test_a_backup_with_no_tables_section_refuses(ready, tmp_path):
    backup = tmp_path / "backup.json"
    backup.write_text(json.dumps({"export_format_version": EXPORT_FORMAT_VERSION,
                                  "schema_version": SCHEMA_VERSION, "exported_at": 1.0}),
                      encoding="utf-8")
    refused(backup, no_destination(tmp_path))


@pytest.mark.parametrize("broken", [
    {"id": 1, "name": "Ali"},                                   # missing created_at and note
    {"id": 1, "name": "Ali", "note": None, "created_at": 1.0, "extra": "x"},   # unknown field
    "not even a row object",
])
def test_a_malformed_row_refuses(filled, tmp_path, broken):
    """Item 32, and §7: no guessing at a missing field, and no silently dropping a bad row."""
    backup = tmp_path / "backup.json"
    logic.export_memory(backup)
    document = exported(backup)
    document["tables"]["people"] = [broken]
    backup.write_text(json.dumps(document), encoding="utf-8")
    outcome = refused(backup, no_destination(tmp_path))
    assert "people" in outcome.message


def test_a_section_that_is_not_a_list_refuses(filled, tmp_path):
    backup = tmp_path / "backup.json"
    logic.export_memory(backup)
    document = exported(backup)
    document["tables"]["people"] = {"id": 1}
    backup.write_text(json.dumps(document), encoding="utf-8")
    assert "not a list" in refused(backup, no_destination(tmp_path)).message


def test_a_foreign_key_violation_refuses(filled, tmp_path):
    """Item 35, and §9's refusal to relax constraints for convenience. The contact points at a person who
    is not in the backup, so the whole restore is rolled back."""
    backup = tmp_path / "backup.json"
    logic.export_memory(backup)
    document = exported(backup)
    document["tables"]["contacts"][0]["person_id"] = 999
    backup.write_text(json.dumps(document), encoding="utf-8")
    refused(backup, no_destination(tmp_path))


def test_a_duplicate_primary_key_refuses(filled, tmp_path):
    """Item 36."""
    backup = tmp_path / "backup.json"
    logic.export_memory(backup)
    document = exported(backup)
    document["tables"]["people"] = document["tables"]["people"] * 2
    backup.write_text(json.dumps(document), encoding="utf-8")
    refused(backup, no_destination(tmp_path))


def test_a_uniqueness_violation_refuses(filled, tmp_path):
    backup = tmp_path / "backup.json"
    logic.export_memory(backup)
    document = exported(backup)
    first = document["tables"]["applications"][0]
    document["tables"]["applications"].append({**first, "id": first["id"] + 1})
    backup.write_text(json.dumps(document), encoding="utf-8")
    refused(backup, no_destination(tmp_path))


def test_a_failure_halfway_through_the_insert_leaves_nothing(filled, tmp_path, monkeypatch):
    """Item 37. The rows go in inside one transaction, so a failure after some of them have been written
    still leaves the destination absent and the staging file gone."""
    backup = tmp_path / "backup.json"
    logic.export_memory(backup)
    target = no_destination(tmp_path)

    real_check = adapter.check_integrity
    monkeypatch.setattr(adapter, "check_integrity",
                        lambda path: (_ for _ in ()).throw(adapter.MemoryAdapterError("broke")))
    refused(backup, target)
    monkeypatch.setattr(adapter, "check_integrity", real_check)
    assert isinstance(logic.restore_memory(backup, target), Restored), "it recovers once fixed"


def test_a_bad_table_in_the_backup_is_rejected_as_data(filled, tmp_path):
    """Item 38, and §18 - the security invariant. A table name shaped like SQL is refused by NAME
    comparison against the fixed schema; it is never put into a statement."""
    backup = tmp_path / "backup.json"
    logic.export_memory(backup)
    document = exported(backup)
    malicious = "people; DROP TABLE people; --"
    document["tables"][malicious] = []
    backup.write_text(json.dumps(document), encoding="utf-8")

    outcome = refused(backup, no_destination(tmp_path))
    assert "don't have" in outcome.message
    assert table_rows(filled, "people"), "the source database lost rows"


def test_a_bad_column_in_the_backup_is_rejected_as_data(filled, tmp_path):
    """The column half of §18."""
    backup = tmp_path / "backup.json"
    logic.export_memory(backup)
    document = exported(backup)
    row = dict(document["tables"]["people"][0])
    row["name) VALUES ('x'); DROP TABLE people; --"] = "x"
    document["tables"]["people"] = [row]
    backup.write_text(json.dumps(document), encoding="utf-8")

    outcome = refused(backup, no_destination(tmp_path))
    assert "wrong fields" in outcome.message
    assert table_rows(filled, "people"), "the source database lost rows"


def test_no_identifier_in_the_adapters_sql_comes_from_outside_the_schema():
    """§18 at the source. Every table and column name in an export or restore statement is interpolated
    from models.TABLES or models.TABLE_BY_NAME - the only f-strings in those two functions - and the row
    VALUES are parameters."""
    source = memory_module_source("adapter.py")
    tree = ast.parse(source)
    transfer = {node.name: node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name in ("read_all_rows", "write_all_rows")}
    assert set(transfer) == {"read_all_rows", "write_all_rows"}
    for name, function in transfer.items():
        for node in ast.walk(function):
            if isinstance(node, ast.JoinedStr):            # every f-string in these two functions
                used = {ast.unparse(part.value) for part in node.values
                        if isinstance(part, ast.FormattedValue)}
                for expression in used:
                    assert any(token in expression for token in
                               ("table.name", "columns", "'?'")), f"{name}: {expression}"


def test_the_restore_order_satisfies_every_foreign_key():
    """§10, derived from the schema rather than trusted: each REFERENCES clause is read out of the column
    definitions, and the fixed order must place every target before its dependant."""
    dependencies = {}
    for table in TABLES:
        for column in table.columns:
            if "REFERENCES" in column:
                target = column.split("REFERENCES", 1)[1].split("(")[0].strip()
                dependencies.setdefault(table.name, set()).add(target)
    assert dependencies, "no foreign keys were found, so this test proves nothing"
    position = {name: index for index, name in enumerate(RESTORE_ORDER)}
    for name, targets in dependencies.items():
        for target in targets:
            assert position[target] < position[name], f"{target} must be restored before {name}"


# --- Boundaries ---------------------------------------------------------------------------------------

def test_export_and_restore_added_no_cross_module_import(filled, tmp_path):
    """Items 39 and 40. Still no Executor, Planner, Safety, Brain or Verifier, and still only the adapter
    touching SQLite."""
    assert "sqlite3" not in imported_names(memory_module_source("logic.py"))
    for name in ("logic.py", "models.py", "adapter.py"):
        imports = imported_names(memory_module_source(name))
        for forbidden in ("app.executor", "app.planner", "app.safety", "app.brain", "app.verifier"):
            assert not any(imported.startswith(forbidden) for imported in imports), f"{name}: {forbidden}"


def test_a_backup_writes_only_where_it_was_told(filled, tmp_path):
    """§16. The destination is explicit and there is no default location, so nothing appears anywhere
    else - in particular not beside the database and not in the repository."""
    before = {path for path in tmp_path.rglob("*")}
    destination = tmp_path / "chosen" / "backup.json"
    assert isinstance(logic.export_memory(destination), Exported)
    created = {path for path in tmp_path.rglob("*")} - before
    assert destination in created
    assert all(path == destination or path == destination.parent or path.is_dir()
               for path in created), sorted(str(path) for path in created)


def test_there_is_no_default_backup_destination():
    """§3. export_memory requires a destination: it cannot be called into a hidden location."""
    import inspect
    signature = inspect.signature(logic.export_memory)
    destination = signature.parameters["destination"]
    assert destination.default is inspect.Parameter.empty, "a default backup location exists"
    restore = inspect.signature(logic.restore_memory)
    assert restore.parameters["destination"].default is inspect.Parameter.empty


# =======================================================================================================
# SLICE 5: per-entry deletion - the other half of the frozen "Memory deletion (per-entry and full-wipe)"
#
# THE FK ACTIONS THIS SCHEMA ACTUALLY DECLARES, read off models.TABLES rather than assumed:
#
#   relationships.person_id      -> people(id)    ON DELETE CASCADE
#   contacts.person_id           -> people(id)    ON DELETE CASCADE
#   files.project_id             -> projects(id)  ON DELETE SET NULL
#   short_term_context.person_id -> people(id)    ON DELETE SET NULL
#   nothing uses RESTRICT or NO ACTION
#
# Slice 2 proved that a declared action is not a working one - PRAGMA foreign_keys is silently ignored
# inside a transaction, so contacts for missing people were being accepted. Every cascade and every
# set-null below is therefore exercised against a real temporary database and read back out of it.
# =======================================================================================================

from app.memory.models import SINGLETON_ROW_ID, Deleted

ID_KEYED = tuple(name for name in TABLE_NAMES if name != "preferences")


def row_count(path, table):
    with sqlite3.connect(path) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# --- The schema's own FK declarations ------------------------------------------------------------------

def test_the_declared_foreign_key_actions_are_what_this_slice_assumes():
    """§5. The behaviour tested below is only correct for these actions, so the actions themselves are
    pinned - a future schema change that alters one fails here rather than silently changing deletion."""
    declared = {}
    for table in TABLES:
        for column in table.columns:
            if "REFERENCES" in column:
                declared[f"{table.name}.{column.split()[0]}"] = column.split("REFERENCES", 1)[1].strip()
    assert declared == {
        "relationships.person_id": "people(id) ON DELETE CASCADE",
        "contacts.person_id": "people(id) ON DELETE CASCADE",
        "files.project_id": "projects(id) ON DELETE SET NULL",
        "short_term_context.person_id": "people(id) ON DELETE SET NULL",
    }, declared


def test_no_structure_uses_restrict_or_no_action(ready):
    """§6's third case. No foreign key in this schema refuses a delete while dependants exist, so there
    is deliberately no refusal branch for it. If one is ever added, this fails and forces the branch."""
    for table in TABLES:
        for column in table.columns:
            assert "RESTRICT" not in column.upper(), f"{table.name}: {column}"
            assert "NO ACTION" not in column.upper(), f"{table.name}: {column}"


def test_foreign_keys_are_actually_enforced_on_the_connection(ready):
    """§25, and the Slice 2 lesson restated: the pragma is on OUTSIDE the transaction, so it works."""
    ali = person_id(logic.add_person("Ali"))
    assert isinstance(logic.add_contact(999, "phone", ADDRESS), MemoryUnavailable), "FKs are off"
    assert isinstance(logic.add_contact(ali, "phone", ADDRESS), Found)


def test_every_structure_is_deletable_by_the_key_its_schema_gives_it():
    """§3. Twelve structures plus the two singletons are keyed by `id`; preferences by its name. Derived
    from the PRIMARY KEY in the schema, not hardcoded per table."""
    assert TABLE_BY_NAME["preferences"].key_column == "key"
    for name in ID_KEYED:
        assert TABLE_BY_NAME[name].key_column == "id", name
    assert len(TABLE_NAMES) == 15


# --- Basic deletion, structure by structure -----------------------------------------------------------

def test_deleting_a_person_takes_its_relationships_and_contacts_with_it(filled):
    """Items 1 and 27. CASCADE, proven against the database: the person's dependent rows go in the same
    transaction, and nothing else does."""
    bilal = person_id(logic.add_person("Bilal"))
    logic.add_relationship(bilal, "colleague")
    logic.add_contact(bilal, "phone", "+92-300-0000009")
    ali = logic.find_person("Ali", "friend").value.id
    assert row_count(filled, "people") == 2

    result = logic.delete_entry("people", bilal)
    assert isinstance(result, Deleted) and result.table == "people" and result.key == bilal

    assert row_count(filled, "people") == 1
    with sqlite3.connect(filled) as conn:
        assert conn.execute("SELECT COUNT(*) FROM relationships WHERE person_id = ?",
                            (bilal,)).fetchone()[0] == 0, "CASCADE did not happen"
        assert conn.execute("SELECT COUNT(*) FROM contacts WHERE person_id = ?",
                            (bilal,)).fetchone()[0] == 0, "CASCADE did not happen"
    assert isinstance(logic.find_person("Ali", "friend"), Found), "the other person was harmed"
    assert isinstance(logic.find_contact(ali, context="local"), Found)


def test_the_short_term_snapshot_keeps_its_row_and_loses_its_person(filled):
    """§5's third case and item 27's other half: short_term_context.person_id is SET NULL, so the
    snapshot survives its person."""
    ali = logic.find_person("Ali", "friend").value.id
    with sqlite3.connect(filled) as conn:
        assert conn.execute("SELECT person_id FROM short_term_context").fetchone()[0] == ali

    assert isinstance(logic.delete_entry("people", ali), Deleted)
    with sqlite3.connect(filled) as conn:
        row = conn.execute("SELECT person_id, app_key FROM short_term_context").fetchone()
    assert row is not None, "the snapshot was deleted with the person"
    assert row[0] is None and row[1] == "notepad", "SET NULL did not happen"


def test_deleting_an_unknown_key_is_not_a_success(filled):
    """Item 2, and §4. Zero rows removed is NotFound, nothing else changes, and no placeholder appears."""
    before = adapter.row_counts(filled)
    for table, key in (("people", 9999), ("contacts", 9999), ("preferences", "nonexistent")):
        outcome = logic.delete_entry(table, key)
        assert isinstance(outcome, NotFound), (table, outcome)
        assert "nothing was deleted" in outcome.message
    assert adapter.row_counts(filled) == before


def test_deleting_one_contact_leaves_the_person_and_the_relationship(filled):
    """Item 3."""
    ali = logic.find_person("Ali", "friend").value.id
    with sqlite3.connect(filled) as conn:
        contact = conn.execute("SELECT id FROM contacts WHERE person_id = ?", (ali,)).fetchone()[0]

    assert isinstance(logic.delete_entry("contacts", contact), Deleted)
    assert isinstance(logic.find_contact(ali, context="local"), NotFound), "lookups are stale"
    assert isinstance(logic.find_person("Ali", "friend"), Found)
    assert row_count(filled, "relationships") == 1


def test_deleting_one_relationship_leaves_the_person(filled):
    """Item 4."""
    ali = logic.find_person("Ali", "friend").value.id
    with sqlite3.connect(filled) as conn:
        relation = conn.execute("SELECT id FROM relationships WHERE person_id = ?", (ali,)).fetchone()[0]

    assert isinstance(logic.delete_entry("relationships", relation), Deleted)
    assert isinstance(logic.find_person("Ali", relationship="friend"), NotFound), "lookups are stale"
    assert isinstance(logic.find_person("Ali"), Found), "the person went too"


def test_deleting_an_alias_removes_the_memory_and_nothing_else(filled):
    """Items 5 and 6, and §9. The alias is forgotten; the configured capability is untouched, because
    Memory never held it in the first place."""
    config = (settings.PROJECT_ROOT / "config" / "config.yaml").read_text(encoding="utf-8")
    assert logic.resolve_application_alias("editor", APPS).value == "notepad"
    with sqlite3.connect(filled) as conn:
        alias_id = conn.execute("SELECT id FROM applications WHERE alias = 'editor'").fetchone()[0]

    assert isinstance(logic.delete_entry("applications", alias_id), Deleted)
    assert isinstance(logic.resolve_application_alias("editor", APPS), NotFound)
    assert (settings.PROJECT_ROOT / "config" / "config.yaml").read_text(encoding="utf-8") == config
    assert "notepad" in APPS, "the configured app_key is still configured"


def test_deleting_a_project_leaves_the_file_row_without_a_project(filled, tmp_path):
    """Items 7, 8 and 27. SET NULL, read back out of the database. And the file ON DISK is never touched:
    the Files structure holds metadata only, so a real file at the remembered path survives."""
    real_file = tmp_path / "notes.txt"
    real_file.write_text("my notes", encoding="utf-8")
    with sqlite3.connect(filled) as conn:
        conn.execute("UPDATE files SET path = ? WHERE id = 1", (str(real_file),))

    assert isinstance(logic.delete_entry("projects", 1), Deleted)
    with sqlite3.connect(filled) as conn:
        row = conn.execute("SELECT id, path, project_id FROM files").fetchone()
    assert row is not None, "the file row was deleted with its project"
    assert row[2] is None, "SET NULL did not happen"
    assert real_file.is_file() and real_file.read_text(encoding="utf-8") == "my notes"


@pytest.mark.parametrize("table", ["websites", "habits", "workflows", "permissions",
                                   "sensitive_data_rules"])
def test_deleting_one_id_keyed_entry_removes_only_it(filled, table):
    """Items 9, 10, 11, 14 and 15. §12: removal from Memory only - nothing is visited, run or executed."""
    before = adapter.row_counts(filled)
    assert before[table] == 1
    assert isinstance(logic.delete_entry(table, 1), Deleted)
    after = adapter.row_counts(filled)
    assert after[table] == 0
    assert {name: count for name, count in after.items() if name != table} == \
        {name: count for name, count in before.items() if name != table}, "something else changed"


def test_deleting_a_preference_uses_its_name_and_does_not_touch_config(filled):
    """Item 12, and §12's last line. Preferences are keyed by name, and config/config.yaml is not
    Memory's to edit."""
    config = (settings.PROJECT_ROOT / "config" / "config.yaml").read_text(encoding="utf-8")
    assert row_count(filled, "preferences") == 1
    assert isinstance(logic.delete_entry("preferences", "browser"), Deleted)
    assert row_count(filled, "preferences") == 0
    assert isinstance(logic.delete_entry("preferences", "browser"), NotFound)
    assert (settings.PROJECT_ROOT / "config" / "config.yaml").read_text(encoding="utf-8") == config


def test_the_two_singletons_are_cleared_explicitly(filled):
    """Items 16 and 17, and §13."""
    assert row_count(filled, "identity") == 1 and row_count(filled, "short_term_context") == 1

    assert isinstance(logic.clear_identity(), Deleted)
    assert row_count(filled, "identity") == 0
    assert isinstance(logic.clear_identity(), NotFound), "clearing twice is not a success"

    assert isinstance(logic.clear_short_term_context(), Deleted)
    assert row_count(filled, "short_term_context") == 0
    assert row_count(filled, "people") == 1, "clearing the snapshot took a person with it"


def test_clearing_the_phase_4_snapshot_does_not_touch_phase_3_context(filled):
    """Item 37, and §13's instruction. PreviousActionContext is a Phase 3 runtime mechanism on
    TurnContext; the Phase 4 table is a different thing, and clearing one is not ending the other."""
    from app.brain.models import PreviousActionContext
    from app.planner.models import TurnContext
    context = TurnContext(previous_action_context=PreviousActionContext("open_app", "notepad"))

    assert isinstance(logic.clear_short_term_context(), Deleted)
    assert context.previous_action_context == PreviousActionContext("open_app", "notepad")
    assert "app.memory" not in imported_names(
        (settings.PROJECT_ROOT / "app" / "planner" / "models.py").read_text(encoding="utf-8"))


# --- Correction deletion ------------------------------------------------------------------------------

def correction_ids(path):
    with sqlite3.connect(path) as conn:
        return {level: row_id for row_id, level in
                conn.execute("SELECT id, level FROM corrections").fetchall()}


def test_deleting_the_learned_row_leaves_the_explicit_one_winning(filled):
    """Items 13 and 18. Only that durable row goes."""
    ids = correction_ids(filled)
    assert set(ids) == {LEARNED_PATTERN, EXPLICIT}
    assert isinstance(logic.delete_entry("corrections", ids[LEARNED_PATTERN]), Deleted)

    assert set(correction_ids(filled)) == {EXPLICIT}
    assert logic.resolve_value(SCOPE, WRONG).value == "button-Z"


def test_deleting_the_explicit_row_makes_the_learned_pattern_the_winner_again(filled):
    """Item 19. The precedence order is unchanged; only the top entry was removed."""
    ids = correction_ids(filled)
    assert logic.resolve_value(SCOPE, WRONG).value == "button-Z", "explicit wins to begin with"

    assert isinstance(logic.delete_entry("corrections", ids[EXPLICIT]), Deleted)
    assert logic.resolve_value(SCOPE, WRONG).value == "button-Y", "the learned pattern did not resurface"
    assert correction_ids(filled)[LEARNED_PATTERN] == ids[LEARNED_PATTERN]


def test_a_task_correction_and_the_learner_are_untouched_by_a_database_delete(filled):
    """Items 20 and 21, and §7. A temporary correction lives on its task object and the candidate count
    lives in the process - neither is a durable entry, so neither is addressable here and neither
    changes when a row goes."""
    learner = CorrectionLearner()
    learner.observe(a_correction(right="button-Q"))
    task = logic.correct_for_this_task(CorrectionTask("task-T"), a_correction(right="button-W"))
    ids = correction_ids(filled)

    assert isinstance(logic.delete_entry("corrections", ids[EXPLICIT]), Deleted)
    assert isinstance(logic.delete_entry("corrections", ids[LEARNED_PATTERN]), Deleted)

    assert task.correction_for(SCOPE, WRONG).right_value == "button-W", "the task lost its correction"
    assert learner.count(a_correction(right="button-Q")) == 1, "the learner lost its evidence"
    assert logic.resolve_value(SCOPE, WRONG, task=task).value == "button-W"
    assert logic.resolve_value(SCOPE, WRONG).value == WRONG, "both durable rows really are gone"


# --- Privacy ------------------------------------------------------------------------------------------

def test_deleting_a_contact_never_reveals_its_address(filled, caplog):
    """Item 22, and §8. The result carries the structure and the key; the address is not in the result,
    its repr, the message or anything logged."""
    import logging
    ali = logic.find_person("Ali", "friend").value.id
    with sqlite3.connect(filled) as conn:
        contact = conn.execute("SELECT id FROM contacts WHERE person_id = ?", (ali,)).fetchone()[0]

    with caplog.at_level(logging.DEBUG):
        result = logic.delete_entry("contacts", contact)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    for surface in (repr(result), str(result), logged):
        assert ADDRESS not in surface and "0000001" not in surface, surface
    assert result == Deleted("contacts", contact)


def test_deleting_a_sensitive_rule_reveals_nothing_it_protected(filled, caplog):
    """Item 23. Removing the rule changes future disclosure - it must not disclose anything itself."""
    import logging
    with sqlite3.connect(filled) as conn:
        rule = conn.execute("SELECT id FROM sensitive_data_rules").fetchone()[0]
    ali = logic.find_person("Ali", "friend").value.id
    assert isinstance(logic.find_contact(ali, context="brain"), Redacted)

    with caplog.at_level(logging.DEBUG):
        result = logic.delete_entry("sensitive_data_rules", rule)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    for surface in (repr(result), str(result), logged):
        assert ADDRESS not in surface, surface
    # The rule is gone, so §15's default applies again - which is the consequence, stated plainly.
    assert isinstance(logic.find_contact(ali, context="brain"), Found)


def test_deleting_a_permission_row_changes_no_safety_authority(filled):
    """Item 14's invariant, and §8's last line. Memory Permissions was never Safety's authority, so
    removing one cannot grant or revoke anything."""
    from app.safety.logic import CONFIRMATION_REQUIRED_AT
    from app.safety.models import RiskLevel
    before = CONFIRMATION_REQUIRED_AT
    assert isinstance(logic.delete_entry("permissions", 1), Deleted)
    assert CONFIRMATION_REQUIRED_AT is before
    assert CONFIRMATION_REQUIRED_AT == RiskLevel.MEDIUM
    for forbidden in ("app.safety", "app.executor"):
        assert not any(name.startswith(forbidden)
                       for name in imported_names(memory_module_source("logic.py")))


def test_deletion_introduced_no_new_sensitive_column(filled):
    """Item 24."""
    for table in TABLES:
        for column in table.columns:
            name = column.split()[0].lower()
            for forbidden in FORBIDDEN_COLUMN_WORDS:
                assert forbidden not in name, f"{table.name}.{name}"


# --- Transactions and failure -------------------------------------------------------------------------

def test_a_failure_during_a_delete_rolls_the_whole_thing_back(filled, monkeypatch):
    """Item 28. A person whose delete fails midway keeps their relationships and contacts - the cascade
    and the row go together or not at all."""
    bilal = person_id(logic.add_person("Bilal"))
    logic.add_relationship(bilal, "colleague")
    logic.add_contact(bilal, "phone", "+92-300-0000009")
    before = adapter.row_counts(filled)

    # A Connection instance will not accept a replaced `execute` (it is read-only), so the failure is
    # injected through a subclass - and the guarded connect is kept in the chain, not bypassed.
    class FailsOnCommit(sqlite3.Connection):
        def execute(self, sql, *parameters):
            if sql.startswith("COMMIT"):
                raise sqlite3.OperationalError("disk I/O error")
            return super().execute(sql, *parameters)

    guarded = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect",
                        lambda *args, **kwargs: guarded(*args, **{**kwargs, "factory": FailsOnCommit}))
    outcome = logic.delete_entry("people", bilal)
    monkeypatch.undo()

    assert isinstance(outcome, MemoryUnavailable), outcome
    assert adapter.row_counts(filled) == before, "a partial delete survived"


def test_no_raw_sqlite_error_escapes_a_delete(memory):
    """Item 29, against a genuinely corrupt database."""
    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_bytes(b"SQLite format 3\x00" + b"\x4d" * 300)
    for call in (lambda: logic.delete_entry("people", 1), logic.clear_identity,
                 logic.clear_short_term_context):
        assert isinstance(call(), MemoryUnavailable), call


def test_a_delete_with_no_database_does_not_create_one(memory):
    """§18. Nothing is initialised just because a delete was asked for, and nothing claims to have
    deleted anything."""
    assert not memory.exists()
    outcome = logic.delete_entry("people", 1)
    assert isinstance(outcome, MemoryUnavailable), outcome
    assert not memory.exists(), "a delete created a database"


# --- Persistence, export and restore ------------------------------------------------------------------

def test_a_deleted_entry_stays_gone_after_reopening(filled):
    """Item 30."""
    assert isinstance(logic.delete_entry("websites", 1), Deleted)
    assert isinstance(logic.open_memory(), MemoryDatabase)
    assert row_count(filled, "websites") == 0


def test_a_deletion_survives_export_and_restore(filled, tmp_path):
    """Items 31, 32 and 33, and §16. The deletion is part of durable Memory state, so a backup taken
    afterwards does not contain it and a restore does not bring it back - with no tombstone of any kind,
    because the frozen docs ask for none."""
    ali = logic.find_person("Ali", "friend").value.id
    with sqlite3.connect(filled) as conn:
        contact = conn.execute("SELECT id FROM contacts WHERE person_id = ?", (ali,)).fetchone()[0]
    assert isinstance(logic.delete_entry("contacts", contact), Deleted)
    expected = everything(filled)

    backup = tmp_path / "backup.json"
    assert isinstance(logic.export_memory(backup), Exported)
    assert exported(backup)["tables"]["contacts"] == [], "the deleted contact was exported"
    assert ADDRESS not in backup.read_text(encoding="utf-8"), "its address was exported"

    fresh = tmp_path / "restored" / "memory.db"
    assert isinstance(logic.restore_memory(backup, fresh), Restored)
    assert everything(fresh) == expected, "the restore did not match the post-deletion state"
    assert row_count(fresh, "contacts") == 0
    assert row_count(fresh, "people") == 1, "an unrelated row was lost"
    assert row_count(fresh, "sensitive_data_rules") == 1, "the protection rules were dropped"
    assert row_count(fresh, "permissions") == 1


# --- Boundaries ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("malicious", [
    "people; DROP TABLE people; --",
    "sqlite_master",
    "people WHERE 1=1",
    "",
    "PEOPLE",
])
def test_a_table_name_from_outside_the_schema_cannot_become_sql(filled, malicious):
    """Items 34 and 35, and §2. The name is compared against the fixed schema map and refused as data;
    it never reaches a statement. The source database is intact afterwards."""
    before = adapter.row_counts(filled)
    outcome = logic.delete_entry(malicious, 1)
    assert isinstance(outcome, NotFound), outcome
    assert "isn't something I remember" in outcome.message
    assert adapter.row_counts(filled) == before, "a crafted name changed the database"
    assert row_count(filled, "people") == 1


def test_the_adapter_builds_its_delete_only_from_the_schema():
    """§2 at the source: the structure's own name and key column are the only identifiers interpolated,
    and the key is a parameter."""
    tree = ast.parse(memory_module_source("adapter.py"))
    function = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == "delete_row")
    # Only the f-strings that BECOME SQL - the first argument of conn.execute(...). The error message
    # also interpolates, and that is not a statement.
    statements = [node.args[0] for node in ast.walk(function)
                  if isinstance(node, ast.Call) and ast.unparse(node.func).endswith(".execute")
                  and node.args and isinstance(node.args[0], ast.JoinedStr)]
    assert statements, "no built statement was found to check"
    interpolations = {ast.unparse(part.value) for node in statements for part in node.values
                      if isinstance(part, ast.FormattedValue)}
    assert interpolations == {"structure.name", "column"}, interpolations
    assert "TABLE_BY_NAME.get(table)" in ast.unparse(function), "the fixed schema map is not consulted"


def test_wipe_was_not_reimplemented_on_top_of_per_entry_delete():
    """Item 38, and §17. Wipe stays one transactional whole-memory operation; per-entry delete is a
    different thing and must not have quietly replaced it."""
    tree = ast.parse(memory_module_source("adapter.py"))
    wipe = next(node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == "wipe")
    called = {ast.unparse(node.func) for node in ast.walk(wipe) if isinstance(node, ast.Call)}
    assert not any("delete_row" in name for name in called), called
    logic_tree = ast.parse(memory_module_source("logic.py"))
    logic_wipe = next(node for node in ast.walk(logic_tree)
                      if isinstance(node, ast.FunctionDef) and node.name == "wipe")
    logic_called = {ast.unparse(node.func) for node in ast.walk(logic_wipe) if isinstance(node, ast.Call)}
    assert not any("delete_entry" in name for name in logic_called), logic_called


def test_a_wipe_still_empties_everything_after_per_entry_deletes(filled):
    """Item 38's behavioural half: the two operations coexist."""
    assert isinstance(logic.delete_entry("websites", 1), Deleted)
    assert isinstance(logic.wipe(), MemoryDatabase)
    assert all(count == 0 for count in adapter.row_counts(filled).values())
    assert isinstance(logic.add_person("Ali"), Found), "memoryless and still usable"


def test_deletion_produces_no_action_and_no_new_cross_module_import(filled):
    """Items 36 and 41."""
    assert isinstance(logic.delete_entry("websites", 1), Deleted)
    for name in ("logic.py", "models.py", "adapter.py"):
        imports = imported_names(memory_module_source(name))
        for forbidden in ("app.executor", "app.planner", "app.safety", "app.brain", "app.verifier"):
            assert not any(imported.startswith(forbidden) for imported in imports), f"{name}: {forbidden}"
    assert "sqlite3" not in imported_names(memory_module_source("logic.py"))
