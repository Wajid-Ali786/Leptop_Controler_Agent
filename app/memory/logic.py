"""
Memory decision-making. Talks only to memory.adapter (docs/step4 Section 7).

Slice 1 is the persistence foundation and nothing else: where the database lives, creating one
explicitly, opening one that should already exist, refusing one this build does not understand,
reporting a broken one clearly, and emptying one. Retrieval, the three correction levels, export/import
and per-entry deletion are later slices.

TWO DIFFERENT SITUATIONS, DELIBERATELY NOT MERGED. Frozen Step 4 requires a missing or corrupt
memory.db to produce "a clear error and a path to restore from export, not a silent crash", and also
requires a full wipe to leave the assistant working. So:

  initialize()  - the user (or first-run setup) asks for a memory database. Creates it.
  open_memory() - everything else. Does NOT create anything. A missing database is reported as
                  MemoryUnavailable with a way forward, never silently replaced by an empty one, because
                  "there is no memory" and "your memory is gone" are not the same sentence.
  wipe()        - keeps a valid, current, empty database. This is not a missing database.

Memory never produces an action. This module imports no Executor, no Planner and no Safety module, and
nothing here can start, stop, confirm or authorise anything - app/safety stays the only authority on
what may run, and config/config.yaml stays the only source of what may be launched.
"""
import json
import logging
import os
import time
from pathlib import Path

from app.memory import adapter
from app.memory.models import (EXPLICIT, EXPORT_FORMAT_VERSION, LEARNED_PATTERN, RESTORE_ORDER,
                               SCHEMA_VERSION, TABLE_BY_NAME, TABLE_NAMES, Ambiguous, BackupRefused,
                               Contact, Correction, CorrectionLearner, CorrectionTask, Exported, Found,
                               Deleted, MemoryDatabase, MemoryUnavailable, NotFound, Person,
                               Redacted, Restored, SINGLETON_ROW_ID, normalize)
from config.settings import PROJECT_ROOT, SettingsError, get_setting

log = logging.getLogger(__name__)
_clock = time.time  # replaced in tests to control the exported timestamp

# Said whenever memory can't be used. It always names a way forward (frozen Recovery case). Restore is
# a later slice, so this points at it as the intended route without claiming it exists yet.
RECOVERY_HINT = ("You can start a fresh memory, or restore a memory export once that is available. "
                 "Nothing was changed or deleted.")

_MISSING_REASON = "I have no memory database yet, so I don't remember anything."
_NEWER_REASON = ("This memory database was written by a newer version of the assistant "
                 "(schema {found}; this build understands {understood}), so I won't read it.")


def database_path() -> Path:
    """Where the memory database lives, from config. Relative paths resolve under the project root, the
    same way the usage ledger does. Raises SettingsError if the setting is missing or unusable."""
    value = get_setting("memory.db_path")
    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f"Setting 'memory.db_path' must be a file path, got {value!r}.")
    path = Path(value.strip())
    return path if path.is_absolute() else PROJECT_ROOT / path


def initialize() -> MemoryDatabase | MemoryUnavailable:
    """Create the memory database, or bring an existing one up to this build's tables. EXPLICIT: nothing
    else in this module creates a database.

    Idempotent, and never destructive - a database whose schema is newer than this build is refused
    untouched rather than recreated."""
    try:
        path = database_path()
    except SettingsError as exc:
        return _unavailable(str(exc), "")
    if path.is_file():
        existing = open_memory()
        if isinstance(existing, MemoryUnavailable):
            return existing            # broken, or newer than this build: do not create over it
    try:
        version = adapter.initialize(path)
    except adapter.MemoryAdapterError as exc:
        return _unavailable(str(exc), path)
    if version != SCHEMA_VERSION:
        return _newer_schema(version, path)
    log.info("Memory: database ready with %d structures at schema %d", len(TABLE_NAMES), version)
    return MemoryDatabase(path=str(path), schema_version=version)


def open_memory() -> MemoryDatabase | MemoryUnavailable:
    """Open the memory database that should already exist.

    Returns MemoryUnavailable - a result, not an exception - when there is nothing to open, when the file
    is damaged, or when its schema is newer than this build understands. A caller can carry on without
    memory; it must not be told that memory is empty when it is actually unreadable."""
    try:
        path = database_path()
    except SettingsError as exc:
        return _unavailable(str(exc), "")
    try:
        version = adapter.open_existing(path)
    except FileNotFoundError:
        log.info("Memory: no database at the configured path; nothing is remembered yet")
        return _unavailable(_MISSING_REASON, path)
    except adapter.MemoryAdapterError as exc:
        log.warning("Memory: the database could not be opened (%s)", type(exc).__name__)
        return _unavailable(str(exc), path)
    if version > SCHEMA_VERSION:
        log.warning("Memory: database schema %d is newer than this build's %d", version, SCHEMA_VERSION)
        return _newer_schema(version, path)
    if version < SCHEMA_VERSION:
        # Older than this build. There is no migration framework in this slice, so it is reported rather
        # than upgraded in place - and never deleted.
        return _unavailable(
            f"This memory database uses an older schema ({version}; this build uses {SCHEMA_VERSION}).",
            path)
    return MemoryDatabase(path=str(path), schema_version=version)


def wipe() -> MemoryDatabase | MemoryUnavailable:
    """Delete everything remembered, keeping a usable, current, empty database (frozen Done-when: a full
    wipe leaves the assistant working, just memoryless).

    Refuses to touch a database it could not open cleanly, so a corrupt file is never "fixed" by
    emptying it."""
    opened = open_memory()
    if isinstance(opened, MemoryUnavailable):
        return opened
    path = Path(opened.path)
    try:
        adapter.wipe(path)
    except FileNotFoundError:
        return _unavailable(_MISSING_REASON, path)
    except adapter.MemoryAdapterError as exc:
        return _unavailable(str(exc), path)
    log.info("Memory: wiped; %d structures remain, all empty", len(TABLE_NAMES))
    return MemoryDatabase(path=str(path), schema_version=opened.schema_version)


def _newer_schema(found: int, path) -> MemoryUnavailable:
    return _unavailable(_NEWER_REASON.format(found=found, understood=SCHEMA_VERSION), path)


def _unavailable(reason: str, path) -> MemoryUnavailable:
    return MemoryUnavailable(reason=reason, recovery=RECOVERY_HINT, path=str(path))


# --- Slice 2: local update and retrieval -------------------------------------------------------------
# Five structures only: people, relationships, contacts, applications, sensitive_data_rules. The other
# ten stay persistence-only from Slice 1.
#
# NO LANGUAGE UNDERSTANDING LIVES HERE. "message my friend Ali" is a sentence for the Brain to read; what
# arrives here is name="Ali", relationship="friend". Memory answers structured questions.
#
# NOTHING HERE ACTS. There is no path from a remembered fact to an Executor action: no capability is
# created, no risk level is produced, no confirmation is skipped.

DO_NOT_KNOW = "I don't know who that is."


def add_person(name: str, note: str | None = None):
    """Remember a person by the name the user wrote.

    Two people may genuinely share a name, so this never merges on a match - it adds an identity, and the
    ambiguity that creates is reported at lookup time rather than guessed away here."""
    if not isinstance(name, str) or not name.strip():
        return NotFound("I need a name to remember someone.")
    clean = name.strip()
    return _write(lambda path: Person(adapter.insert_person(path, clean, note), clean, note))


def add_relationship(person_id: int, relation: str):
    """Record how the USER is related to this person - "friend", "brother", "manager".

    Narrow by design: the user's own relationship to someone, not a person-to-person graph, and nothing
    is ever inferred from it."""
    if not isinstance(relation, str) or not relation.strip():
        return NotFound("I need a relationship to remember.")
    return _write(lambda path: adapter.insert_relationship(path, person_id, relation.strip()))


def add_contact(person_id: int, channel: str, address: str):
    """Remember a way to reach someone. DATA ONLY: nothing is checked for being installed, nothing is
    opened and nothing is sent - messaging is Phase 10."""
    if not _given(channel) or not _given(address):
        return NotFound("I need both a channel and an address to remember a contact.")
    return _write(lambda path: Contact(
        adapter.insert_contact(path, person_id, channel.strip(), address.strip()),
        person_id, channel.strip(), address.strip()))


def find_person(name: str, relationship: str | None = None):
    """The one person matching `name`, and `relationship` when given.

    Found when exactly one matches. NotFound when none does - and nothing is written, learned or guessed.
    Ambiguous when more than one does: the first row is never chosen."""
    opened = open_memory()
    if isinstance(opened, MemoryUnavailable):
        return opened
    try:
        rows = adapter.select_people(Path(opened.path), normalize(name), relationship)
    except adapter.MemoryAdapterError as exc:
        return _unavailable(str(exc), opened.path)
    people = tuple(Person(row[0], row[1], row[2]) for row in rows)
    if not people:
        return NotFound(DO_NOT_KNOW)
    if len(people) > 1:
        return Ambiguous(people, f"I know {len(people)} people by that name, so I'm not going to guess.")
    return Found(people[0])


def find_contact(person_id: int, channel: str | None = None, *, context: str):
    """The one contact for a person, filtered by `channel` when given and by the sensitive-data rules for
    `context` always.

    `context` is required and has no default: a caller must say who is asking, because that is what the
    rules are written against."""
    opened = open_memory()
    if isinstance(opened, MemoryUnavailable):
        return opened
    path = Path(opened.path)
    try:
        rows = adapter.select_contacts(path, person_id, channel)
        disclosure = adapter.select_disclosure(path, "contacts", "address", context)
    except adapter.MemoryAdapterError as exc:
        return _unavailable(str(exc), opened.path)
    if not rows:
        return NotFound("I don't have a contact like that.")
    denied = disclosure == "deny"
    # The address is left OUT here, at the boundary - not blanked later, and never put into the result in
    # the first place, so there is nothing for a caller, a repr or a log line to leak.
    contacts = tuple(Contact(row[0], row[1], row[2], None if denied else row[3], not denied)
                     for row in rows)
    if len(contacts) > 1:
        return Ambiguous(contacts, f"I have {len(contacts)} contacts for them, so I'm not going to guess.")
    if denied:
        return Redacted(contacts[0], "contacts.address", context,
                        "I'm not allowed to share that contact's address here.")
    return Found(contacts[0])


def remember_application_alias(alias: str, app_key: str, configured_app_keys):
    """Remember that the user calls a CONFIGURED app by another name.

    `configured_app_keys` is supplied by the caller, because config/config.yaml is the source of truth for
    what can be opened and Memory must not be able to discover or extend it. An alias for an app that is
    not configured is refused rather than stored, so memory can never become a way in."""
    if not _given(alias):
        return NotFound("I need an alias to remember.")
    if app_key not in set(configured_app_keys or ()):
        return NotFound(f"'{app_key}' isn't one of the apps I'm set up to open, so I won't remember it.")
    return _write(lambda path: _alias_stored(path, alias.strip(), app_key))


def resolve_application_alias(alias: str, configured_app_keys):
    """The configured app_key an alias means, or NotFound.

    Returns a KEY and never an executable, a command or a path - the applications table holds no column
    that could carry one. A remembered alias whose app is no longer configured resolves to NotFound: a
    stale memory must not widen what the Executor can open."""
    opened = open_memory()
    if isinstance(opened, MemoryUnavailable):
        return opened
    try:
        app_key = adapter.select_application_alias(Path(opened.path), normalize(alias))
    except adapter.MemoryAdapterError as exc:
        return _unavailable(str(exc), opened.path)
    if app_key is None:
        return NotFound(f"I don't know an app called '{alias}'.")
    if app_key not in set(configured_app_keys or ()):
        return NotFound(f"I remember '{alias}', but '{app_key}' isn't an app I'm set up to open any more.")
    return Found(app_key)


def set_sensitive_rule(target_table: str, target_field: str, context: str, disclosure: str,
                       retention: str | None = None):
    """Add or replace the exact rule for one table, field and context.

    No wildcards and no policy language: the frozen requirement is that a tagged field is not returned to
    a context that should not see it, and an exact tuple answers exactly that."""
    if disclosure not in ("allow", "deny"):
        return NotFound("A rule either allows or denies.")
    if not all(_given(value) for value in (target_table, target_field, context)):
        return NotFound("A rule needs a table, a field and a context.")
    return _write(lambda path: adapter.upsert_sensitive_rule(
        path, target_table.strip(), target_field.strip(), context.strip(), disclosure, retention))


def _given(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _alias_stored(path, alias: str, app_key: str) -> str:
    adapter.upsert_application_alias(path, alias, app_key, None)
    return app_key


def _write(operation):
    """Run one adapter write against an open database, turning every failure into a result."""
    opened = open_memory()
    if isinstance(opened, MemoryUnavailable):
        return opened
    try:
        return Found(operation(Path(opened.path)))
    except adapter.MemoryAdapterError as exc:
        return _unavailable(str(exc), opened.path)


# --- Slice 3: the three correction levels -------------------------------------------------------------
# Frozen Build Plan 6.8: a temporary correction for this task only, a learned pattern once the same
# correction repeats, and an explicit memory stored the moment the user asks for it.
#
# NO SENTENCE IS PARSED HERE EITHER. "No, use the other Ali" and "Remember this" are things a user says;
# what arrives is a Correction(scope, wrong_value, right_value, context). The Brain will do the
# translating, in a later approved slice.

CANNOT_LEARN = "I can't remember that for next time right now."
CANNOT_REMEMBER = "I can't store that right now, so I won't say I've remembered it."


def correct_for_this_task(task: CorrectionTask, correction: Correction) -> CorrectionTask:
    """LEVEL 1. Apply a temporary correction to one task and nothing else.

    Pure: it touches no database, writes no row, counts towards nothing, and has no effect once the task
    is over. It works whether or not memory is available, because it needs no memory."""
    return task.with_correction(correction)


def observe_correction(task: CorrectionTask, learner: CorrectionLearner, correction: Correction):
    """A user correction: always LEVEL 1 for this task, and LEVEL 2 once it has been seen twice.

    Returns (task, outcome). The task always comes back with the temporary correction applied, so the
    current task is fixed even when nothing durable can be written. The outcome says what became of the
    durable half:

        Found(occurrences)  promoted - a learned_pattern row now exists
        NotFound            seen once so far; nothing durable yet, which is the frozen behaviour
        MemoryUnavailable   it should have been promoted but the database could not take it

    The learner's count is process-local evidence only. It is never consulted by a lookup: until a row
    exists, a correction affects nothing beyond its own task."""
    corrected = correct_for_this_task(task, correction)
    seen = learner.observe(correction)
    if seen < CorrectionLearner.PROMOTE_AT:
        return corrected, NotFound(f"I'll remember that if it comes up again "
                                   f"({seen} of {CorrectionLearner.PROMOTE_AT}).")
    promoted = _record(LEARNED_PATTERN, correction, seen, CANNOT_LEARN)
    return corrected, promoted


def remember_explicitly(correction: Correction):
    """LEVEL 3. The user said to remember this, so it is stored immediately.

    No threshold, no repetition, and no need for a temporary correction first. Visible to the very next
    lookup. If it cannot be written, that is reported - it is never silently called remembered."""
    return _record(EXPLICIT, correction, 1, CANNOT_REMEMBER)


def resolve_value(scope: str, ordinary_value: str, *, task: CorrectionTask | None = None,
                  context: str | None = None):
    """What `ordinary_value` should actually be in `scope`, applying the frozen precedence:

        this task's temporary correction  >  explicit memory  >  learned pattern  >  ordinary_value

    Deliberately layered on top of a value the caller already has, rather than being a resolution engine
    of its own, so the same correction logic can sit over any structured lookup later.

    A temporary correction is answered without touching the database at all, which is why level 1 keeps
    working when memory is unavailable."""
    if task is not None:
        temporary = task.correction_for(scope, ordinary_value, context)
        if temporary is not None:
            return Found(temporary.right_value)
    opened = open_memory()
    if isinstance(opened, MemoryUnavailable):
        return opened
    path = Path(opened.path)
    try:
        # Explicit is asked FIRST: a direct instruction outranks anything inferred from repetition.
        for level in (EXPLICIT, LEARNED_PATTERN):
            row = adapter.select_correction(path, level, scope, ordinary_value, context)
            if row is not None:
                return Found(row[1])
    except adapter.MemoryAdapterError as exc:
        return _unavailable(str(exc), opened.path)
    return Found(ordinary_value)


def _record(level: str, correction: Correction, occurrences: int, failure: str):
    """Write one durable correction, reporting rather than pretending when it cannot be written."""
    if not all(_given(value) for value in (correction.scope, correction.wrong_value,
                                           correction.right_value)):
        return NotFound("A correction needs a scope, the wrong value and the right one.")
    opened = open_memory()
    if isinstance(opened, MemoryUnavailable):
        return _unavailable(f"{failure} {opened.reason}", opened.path)
    try:
        _row_id, total = adapter.record_correction(
            Path(opened.path), level, correction.scope.strip(), correction.wrong_value.strip(),
            correction.right_value.strip(), correction.context, occurrences)
    except adapter.MemoryAdapterError as exc:
        return _unavailable(f"{failure} {exc}", opened.path)
    log.info("Memory: stored a %s correction (now %d occurrence(s))", level, total)
    return Found(total)


# --- Slice 4: export and restore (frozen D4 / D5) -----------------------------------------------------
# D4  "export produces a file"
# D5  "that export successfully restores into a fresh database"
#
# The destination is always given explicitly - there is no default backup location, because a hidden one
# is a file the user does not know exists. Nothing is ever overwritten: an export refuses a destination
# that is already there, and a restore refuses a database that is already there.
#
# Both operations are all-or-nothing, and both are built around the same rule: the fixed schema in
# app/memory/models.py decides every table and column name, and a JSON file supplies only values.

_NEWER_EXPORT = ("This backup was written in a newer format (version {found}; this build understands "
                 "{understood}), so I won't read it.")
_NEWER_MEMORY = ("This backup holds a newer memory schema (version {found}; this build uses "
                 "{understood}), so I won't restore it.")


def export_memory(destination) -> Exported | BackupRefused | MemoryUnavailable:
    """Write all fifteen structures to `destination` as one versioned JSON document.

    The file is the user's own backup, so it carries the real values - including sensitive ones - and the
    sensitive-data rules alongside them. A backup that dropped the rules would restore facts with their
    protections stripped. It is therefore sensitive plaintext, and no row value is logged or returned.

    Refuses rather than overwriting an existing file, and writes through a temporary sibling so a failure
    can never leave something that looks like a complete backup."""
    target = Path(destination)
    if target.exists():
        return BackupRefused(f"{target.name} already exists, so I won't overwrite it.", str(target))
    opened = open_memory()                      # exists, integrity-checked, schema understood
    if isinstance(opened, MemoryUnavailable):
        return opened
    source = Path(opened.path)
    try:
        adapter.check_integrity(source)
        missing = set(TABLE_NAMES) - set(adapter.table_names(source))
        if missing:
            return _unavailable(f"The memory database is missing {len(missing)} structure(s): "
                                f"{', '.join(sorted(missing))}.", source)
        snapshot = adapter.read_all_rows(source)
    except adapter.MemoryAdapterError as exc:
        return _unavailable(str(exc), source)

    document = {
        "export_format_version": EXPORT_FORMAT_VERSION,
        "schema_version": opened.schema_version,
        "exported_at": _clock(),
        "tables": snapshot,
    }
    partial = target.with_name(target.name + ".partial")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with partial.open("w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial, target)             # only now does the destination appear
    except (OSError, TypeError, ValueError) as exc:
        partial.unlink(missing_ok=True)
        return BackupRefused(f"The backup could not be written ({type(exc).__name__}).", str(target))
    rows = sum(len(table) for table in snapshot.values())
    log.info("Memory: exported %d structures, %d row(s)", len(snapshot), rows)
    return Exported(str(target), len(snapshot), rows)


def restore_memory(export_path, destination) -> Restored | BackupRefused | MemoryUnavailable:
    """Restore a backup into a FRESH memory database at `destination`.

    Fresh means the destination does not exist. Nothing is merged, appended, overwritten or wiped: this
    is the path out of the frozen Recovery case, where memory.db has gone missing and a backup is what
    puts it back.

    The whole file is validated before the destination is created, and the rows are written into a
    staging database that only becomes the destination once it is complete and passes its integrity
    check. Every failure leaves the destination absent."""
    target = Path(destination)
    if target.exists():
        return BackupRefused(f"{target.name} already exists. Restoring only ever creates a new memory "
                             f"database, so nothing was changed.", str(target))
    document = _read_export(Path(export_path))
    if isinstance(document, BackupRefused):
        return document
    snapshot = _validated_tables(document)
    if isinstance(snapshot, BackupRefused):
        return snapshot

    staging = target.with_name(target.name + ".restoring")
    staging.unlink(missing_ok=True)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        written = adapter.write_all_rows(staging, snapshot)
        adapter.check_integrity(staging)
        os.replace(staging, target)
    except adapter.MemoryAdapterError as exc:
        staging.unlink(missing_ok=True)
        return BackupRefused(f"The backup could not be restored ({exc}).", str(target))
    except OSError as exc:
        staging.unlink(missing_ok=True)
        return BackupRefused(f"The backup could not be restored ({type(exc).__name__}).", str(target))
    log.info("Memory: restored %d structures, %d row(s)", len(TABLE_NAMES), written)
    return Restored(str(target), len(TABLE_NAMES), written)


def _read_export(path: Path):
    """The parsed document, or a refusal. Nothing out of the file reaches the message."""
    if not path.is_file():
        return BackupRefused(f"There is no backup at {path.name}.", str(path))
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        return BackupRefused(f"{path.name} could not be read ({type(exc).__name__}).", str(path))
    except json.JSONDecodeError:
        return BackupRefused(f"{path.name} is not valid JSON, so it isn't a backup I can read.", str(path))
    if not isinstance(document, dict):
        return BackupRefused(f"{path.name} is not a backup document.", str(path))
    found = document.get("export_format_version")
    if found != EXPORT_FORMAT_VERSION:
        return BackupRefused(_NEWER_EXPORT.format(found=found, understood=EXPORT_FORMAT_VERSION),
                             str(path))
    schema = document.get("schema_version")
    if schema != SCHEMA_VERSION:
        return BackupRefused(_NEWER_MEMORY.format(found=schema, understood=SCHEMA_VERSION), str(path))
    return document


def _validated_tables(document: dict):
    """Check the document against the FIXED schema, and return only rows keyed by known columns.

    Strict on purpose, for format version 1: every one of the fifteen structures must be present, each as
    a list of objects whose keys are exactly that table's columns. An unknown table or an unknown column
    is a refusal, not something to ignore and not something to pass to SQL - which is what keeps a
    malicious identifier in a file from ever becoming an identifier in a statement."""
    tables = document.get("tables")
    if not isinstance(tables, dict):
        return BackupRefused("The backup has no tables section.")
    unknown = sorted(set(tables) - set(TABLE_NAMES))
    if unknown:
        return BackupRefused(f"The backup names {len(unknown)} structure(s) I don't have: "
                             f"{', '.join(repr(name) for name in unknown)}.")
    missing = sorted(set(TABLE_NAMES) - set(tables))
    if missing:
        return BackupRefused(f"The backup is missing {len(missing)} structure(s): "
                             f"{', '.join(missing)}.")
    snapshot: dict[str, list[dict]] = {}
    for name in RESTORE_ORDER:
        rows = tables[name]
        if not isinstance(rows, list):
            return BackupRefused(f"The backup's '{name}' section is not a list of rows.")
        expected = set(TABLE_BY_NAME[name].column_names)
        checked = []
        for number, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                return BackupRefused(f"Row {number} of '{name}' is not a row object.")
            present = set(row)
            if present != expected:
                # Field NAMES only - a malformed backup never gets its values quoted back.
                surplus = sorted(present - expected)
                absent = sorted(expected - present)
                detail = f"unexpected {surplus}" if surplus else f"missing {absent}"
                return BackupRefused(f"Row {number} of '{name}' has the wrong fields ({detail}).")
            checked.append({column: row[column] for column in TABLE_BY_NAME[name].column_names})
        snapshot[name] = checked
    return snapshot


# --- Slice 5: per-entry deletion ----------------------------------------------------------------------
# Removal from MEMORY, and nothing else. Deleting a remembered file does not touch the file on disk;
# deleting a website visits nothing; deleting a workflow runs nothing; deleting a preference does not
# edit config/config.yaml; deleting an application alias changes neither executor.apps nor what can be
# opened. There is no code path from here to any of those.
#
# Full wipe stays exactly as it was - a single transactional whole-memory operation. It is not
# reimplemented on top of this, because one statement per structure is the right way to empty everything
# and fifteen round trips is not.


def delete_entry(table: str, key) -> Deleted | NotFound | MemoryUnavailable:
    """Delete one durable entry of `table`, identified by its own primary key.

    `table` must be one of the fifteen frozen structures. `key` is the integer id for every structure
    except preferences, which is keyed by its name - the schema decides, not this function.

    An entry that is not there returns NotFound: zero rows removed is never reported as a success, and
    nothing else is touched. Dependent rows follow the schema's own ON DELETE actions, which for this
    schema means relationships and contacts go with their person, while a file keeps its row and loses
    its project, and the short-term snapshot keeps its row and loses its person."""
    if table not in TABLE_BY_NAME:
        return NotFound(f"'{table}' isn't something I remember.")
    opened = open_memory()
    if isinstance(opened, MemoryUnavailable):
        return opened
    try:
        affected = adapter.delete_row(Path(opened.path), table, key)
    except adapter.MemoryAdapterError as exc:
        return _unavailable(str(exc), opened.path)
    if not affected:
        return NotFound(f"There's no {table} entry with that key, so nothing was deleted.")
    log.info("Memory: deleted 1 entry from %s", table)
    return Deleted(table, key)


def clear_identity() -> Deleted | NotFound | MemoryUnavailable:
    """Remove the stored Identity row. Singleton: there is only ever one."""
    return delete_entry("identity", SINGLETON_ROW_ID)


def clear_short_term_context() -> Deleted | NotFound | MemoryUnavailable:
    """Remove the persisted "active person/app/page/task" snapshot. Singleton.

    This is the Phase 4 TABLE and nothing else. Phase 3's PreviousActionContext is a separate in-memory
    mechanism on TurnContext; clearing this does not end a turn, reset a TurnContext or touch it in any
    way."""
    return delete_entry("short_term_context", SINGLETON_ROW_ID)
