"""
The only file in this module that talks to the SQLite database (docs/step4 Section 7).

Everything here is mechanics: connecting, creating the fifteen tables, reading and writing the schema
version, checking integrity, and emptying the user tables. No decisions - app/memory/logic.py decides
what any of it means and turns a failure into something the user can act on.

Every function raises MemoryAdapterError and never a raw sqlite3 exception, so no database detail
escapes this boundary. The pattern follows app/brain/cost_controls.py, which is the project's other
SQLite user, with one deliberate difference: that ledger fails CLOSED because untracked spend is
unacceptable, while Memory degrades to "no memory" because frozen Step 4 requires a wiped database to
leave the assistant working.

Creating a database is EXPLICIT (initialize). Opening one does not create it: frozen Step 4 requires a
missing memory.db to be visible and recoverable, not silently replaced by an empty one.
"""
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from app.memory.models import (RESTORE_ORDER, SCHEMA_VERSION, TABLE_BY_NAME, TABLE_NAMES,
                               TABLES, VERSION_TABLE, normalize)

_now = time.time  # replaced in tests to control the clock

_VERSION_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {VERSION_TABLE} (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version INTEGER NOT NULL,
    created_at REAL NOT NULL
)
"""


class MemoryAdapterError(Exception):
    """The memory database could not be used. Messages are safe to show the user: they name the file
    and the problem, never a row of remembered data."""


def initialize(path: Path) -> int:
    """Create the memory database at `path` if it is not there, and make sure it holds every table this
    build needs. Returns the schema version now recorded.

    Idempotent: running it on an existing, current database changes nothing and deletes nothing. It does
    NOT touch a database whose schema version is newer than this build - logic.py checks that first.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _failure(path, exc) from None
    with _connect(path) as conn:
        _create_everything(conn, path)
        return _read_version(conn, path)


def open_existing(path: Path) -> int:
    """The schema version of the database at `path`, which must already exist.

    Raises FileNotFoundError if it does not - deliberately, so logic.py can tell "no memory yet" apart
    from "memory is broken" and say something different about each."""
    if not path.is_file():
        raise FileNotFoundError(path)
    with _connect(path) as conn:
        _integrity_check(conn, path)
        return _read_version(conn, path)


def wipe(path: Path) -> None:
    """Delete every row of remembered data, keeping the schema and its version (frozen Done-when: a full
    wipe leaves the assistant working, just memoryless).

    One transaction: a failure part-way through rolls back, so there is no half-wiped database. Only this
    file's tables are touched - no other database, and never the usage ledger."""
    if not path.is_file():
        raise FileNotFoundError(path)
    with _connect(path) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            for name in TABLE_NAMES:
                conn.execute(f"DELETE FROM {name}")
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            _rollback(conn)
            raise _failure(path, exc) from None
        except BaseException:
            _rollback(conn)
            raise


def table_names(path: Path) -> list[str]:
    """The user tables present in the database, for diagnostics and tests."""
    with _connect(path) as conn:
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        except sqlite3.Error as exc:
            raise _failure(path, exc) from None
    return sorted(name for (name,) in rows if name != VERSION_TABLE)


def row_counts(path: Path) -> dict[str, int]:
    """How many rows each required table holds. Used to prove a fresh or wiped database is empty."""
    counts = {}
    with _connect(path) as conn:
        for name in TABLE_NAMES:
            try:
                counts[name] = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            except sqlite3.Error as exc:
                raise _failure(path, exc) from None
    return counts


# --- mechanics ---------------------------------------------------------------------------------------

@contextmanager
def _connect(path: Path):
    try:
        conn = sqlite3.connect(path, timeout=5, isolation_level=None)
    except (OSError, sqlite3.Error) as exc:
        raise _failure(path, exc) from None
    try:
        # MUST be outside a transaction: SQLite silently ignores this pragma inside one, which is how a
        # contact for a person who does not exist was accepted before this moved here.
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
    except sqlite3.Error as exc:
        conn.close()
        raise _failure(path, exc) from None
    finally:
        if conn:
            conn.close()


def _create_everything(conn: sqlite3.Connection, path: Path) -> None:
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(_VERSION_SCHEMA)
        for table in TABLES:
            conn.execute(table.create_statement)
        conn.execute(
            f"INSERT OR IGNORE INTO {VERSION_TABLE} (id, schema_version, created_at) VALUES (1, ?, ?)",
            (SCHEMA_VERSION, _now()),
        )
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        _rollback(conn)
        raise _failure(path, exc) from None
    except BaseException:
        _rollback(conn)
        raise


def _read_version(conn: sqlite3.Connection, path: Path) -> int:
    try:
        row = conn.execute(f"SELECT schema_version FROM {VERSION_TABLE} WHERE id = 1").fetchone()
    except sqlite3.Error as exc:
        raise _failure(path, exc) from None
    if row is None:
        raise MemoryAdapterError(
            f"Memory database {path} has no schema version recorded, so it can't be trusted."
        )
    return int(row[0])


def _integrity_check(conn: sqlite3.Connection, path: Path) -> None:
    """PRAGMA quick_check, as the usage ledger does. A corrupt file is reported, never overwritten."""
    try:
        result = conn.execute("PRAGMA quick_check").fetchone()[0]
    except sqlite3.Error as exc:
        raise _failure(path, exc) from None
    if result != "ok":
        raise MemoryAdapterError(f"Memory database {path} failed its integrity check ({result}).")


def _rollback(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass


def _failure(path: Path, exc: Exception) -> MemoryAdapterError:
    return MemoryAdapterError(f"Memory database {path} could not be used ({type(exc).__name__}).")


# --- Slice 2: the five structures local lookups use --------------------------------------------------
# Mechanics only. Matching is by models.normalize() applied on BOTH sides in SQL, so the comparison form
# is the same whether a row was written before or after a query. Each write is one transaction.

def insert_person(path: Path, name: str, note: str | None) -> int:
    """A new person. Deliberately no "merge if the name matches": two people really can both be Ali, and
    the frozen ambiguity case depends on that staying representable."""
    return _write(path, "INSERT INTO people (name, note, created_at) VALUES (?, ?, ?)",
                  (name, note, _now()))


def insert_relationship(path: Path, person_id: int, relation: str) -> int:
    return _write(path, "INSERT INTO relationships (person_id, relation, created_at) VALUES (?, ?, ?)",
                  (person_id, relation, _now()))


def insert_contact(path: Path, person_id: int, channel: str, address: str) -> int:
    return _write(path, "INSERT INTO contacts (person_id, channel, address, created_at) "
                        "VALUES (?, ?, ?, ?)", (person_id, channel, address, _now()))


def upsert_application_alias(path: Path, alias: str, app_key: str, note: str | None) -> int:
    """An alias is unique, so remembering it again points it at the current app_key."""
    return _write(path, "INSERT INTO applications (alias, app_key, note, created_at) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT (alias) DO UPDATE SET app_key = excluded.app_key, "
                        "note = excluded.note", (alias, app_key, note, _now()))


def upsert_sensitive_rule(path: Path, target_table: str, target_field: str, context: str,
                          disclosure: str, retention: str | None) -> int:
    return _write(path, "INSERT INTO sensitive_data_rules (target_table, target_field, context, "
                        "disclosure, retention, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT (target_table, target_field, context) DO UPDATE SET "
                        "disclosure = excluded.disclosure, retention = excluded.retention, "
                        "updated_at = excluded.updated_at",
                  (target_table, target_field, context, disclosure, retention, _now()))


def select_people(path: Path, normalized_name: str, relation: str | None = None) -> list[tuple]:
    """(id, name, note) for everyone whose name matches, optionally only those carrying `relation`.

    The relationship filter is an INNER JOIN on stored rows: it narrows by what the user actually said,
    and can never add a person who has no such relationship recorded."""
    sql = ("SELECT DISTINCT p.id, p.name, p.note FROM people p "
           "{join}WHERE lower(trim(p.name)) = ? {extra}ORDER BY p.id")
    if relation is None:
        return _read(path, sql.format(join="", extra=""), (normalized_name,))
    return _read(path, sql.format(join="JOIN relationships r ON r.person_id = p.id ",
                                  extra="AND lower(trim(r.relation)) = ? "),
                 (normalized_name, normalize(relation)))


def select_relations(path: Path, person_id: int) -> list[str]:
    rows = _read(path, "SELECT relation FROM relationships WHERE person_id = ? ORDER BY id", (person_id,))
    return [relation for (relation,) in rows]


def select_contacts(path: Path, person_id: int, channel: str | None = None) -> list[tuple]:
    """(id, person_id, channel, address) for a person, optionally one channel."""
    if channel is None:
        return _read(path, "SELECT id, person_id, channel, address FROM contacts WHERE person_id = ? "
                           "ORDER BY id", (person_id,))
    return _read(path, "SELECT id, person_id, channel, address FROM contacts WHERE person_id = ? "
                       "AND lower(trim(channel)) = ? ORDER BY id", (person_id, normalize(channel)))


def select_application_alias(path: Path, normalized_alias: str) -> str | None:
    """The app_key an alias points at, or None. Never an executable and never a path - the applications
    table has no column that could hold one."""
    rows = _read(path, "SELECT app_key FROM applications WHERE lower(trim(alias)) = ?", (normalized_alias,))
    return rows[0][0] if rows else None


def select_disclosure(path: Path, target_table: str, target_field: str, context: str) -> str | None:
    """'allow', 'deny', or None when no exact rule covers this table/field/context."""
    rows = _read(path, "SELECT disclosure FROM sensitive_data_rules WHERE target_table = ? "
                       "AND target_field = ? AND context = ?", (target_table, target_field, context))
    return rows[0][0] if rows else None


def _write(path: Path, sql: str, parameters: tuple) -> int:
    """One statement, one transaction. A failure rolls back, so no half-written record survives."""
    with _connect(path) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(sql, parameters)
            conn.execute("COMMIT")
            return int(cursor.lastrowid)
        except sqlite3.Error as exc:
            _rollback(conn)
            raise _failure(path, exc) from None
        except BaseException:
            _rollback(conn)
            raise


def _read(path: Path, sql: str, parameters: tuple) -> list[tuple]:
    with _connect(path) as conn:
        try:
            return conn.execute(sql, parameters).fetchall()
        except sqlite3.Error as exc:
            raise _failure(path, exc) from None


# --- Slice 3: durable corrections ---------------------------------------------------------------------
# Only levels 'learned_pattern' and 'explicit' ever reach the table - a temporary correction has no code
# path to here at all. Matching is on the NORMALISED values of scope, wrong_value and context, while the
# stored text keeps the form the user wrote, exactly as people.name does.
#
# COALESCE on context so a row written with NULL and a query with "" are the same correction.

_CORRECTION_MATCH = ("lower(trim(scope)) = ? AND lower(trim(COALESCE(wrong_value, ''))) = ? "
                     "AND lower(trim(COALESCE(context, ''))) = ?")


def select_correction(path: Path, level: str, scope: str, wrong_value: str,
                      context: str | None = None) -> tuple | None:
    """(id, right_value, occurrences) for the correction of `level` covering this target, or None.

    `right_value` is NOT part of the match: an explicit "X should be Z" has to be findable as the rule
    covering X even when a learned row says "X should be Y"."""
    rows = _read(path, f"SELECT id, right_value, occurrences FROM corrections "
                       f"WHERE level = ? AND {_CORRECTION_MATCH} ORDER BY id",
                 (level, normalize(scope), normalize(wrong_value), normalize(context or "")))
    return rows[-1] if rows else None


def record_correction(path: Path, level: str, scope: str, wrong_value: str, right_value: str,
                      context: str | None, occurrences: int) -> tuple[int, int]:
    """Write the durable correction, and return (id, occurrences) as the table now holds them.

    ONE transaction containing the look-up AND the write, so a promotion can never produce two learned
    rows for the same correction, nor an occurrence count without the value it counts. BEGIN IMMEDIATE
    takes the write lock before the SELECT, so no second writer can slip between them - which is what
    stands in for the UNIQUE constraint this table deliberately does not have (the same correction may
    legitimately exist once as 'learned_pattern' and once as 'explicit')."""
    with _connect(path) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            found = conn.execute(
                f"SELECT id, occurrences FROM corrections WHERE level = ? AND {_CORRECTION_MATCH} "
                f"ORDER BY id",
                (level, normalize(scope), normalize(wrong_value), normalize(context or ""))).fetchall()
            if found:
                row_id, current = found[-1]
                total = max(int(current), occurrences)
                conn.execute("UPDATE corrections SET right_value = ?, occurrences = ? WHERE id = ?",
                             (right_value, total, row_id))
            else:
                cursor = conn.execute(
                    "INSERT INTO corrections (level, scope, wrong_value, right_value, context, "
                    "occurrences, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (level, scope, wrong_value, right_value, context, occurrences, _now()))
                row_id, total = int(cursor.lastrowid), occurrences
            conn.execute("COMMIT")
            return row_id, total
        except sqlite3.Error as exc:
            _rollback(conn)
            raise _failure(path, exc) from None
        except BaseException:
            _rollback(conn)
            raise


def count_corrections(path: Path, level: str | None = None) -> int:
    if level is None:
        return _read(path, "SELECT COUNT(*) FROM corrections", ())[0][0]
    return _read(path, "SELECT COUNT(*) FROM corrections WHERE level = ?", (level,))[0][0]


# --- Slice 4: reading a snapshot out, and writing one into a fresh database ---------------------------
# SECURITY INVARIANT. Every table and column identifier below comes from app/memory/models.TABLES. A
# name out of a JSON file is NEVER concatenated into SQL - it is validated against the fixed schema in
# logic.py and then discarded; only VALUES cross this boundary, as SQLite parameters. That is why
# restore takes rows keyed by a known column name and builds its statement from models, not from the
# file.


def read_all_rows(path: Path) -> dict[str, list[dict]]:
    """Every row of all fifteen structures, as one consistent snapshot.

    The whole read happens inside a single transaction, so a write landing midway cannot produce a
    backup that is half one state and half another."""
    snapshot: dict[str, list[dict]] = {}
    with _connect(path) as conn:
        try:
            conn.execute("BEGIN")
            for table in TABLES:
                columns = table.column_names
                rows = conn.execute(
                    f"SELECT {', '.join(columns)} FROM {table.name} ORDER BY rowid").fetchall()
                snapshot[table.name] = [dict(zip(columns, row)) for row in rows]
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            _rollback(conn)
            raise _failure(path, exc) from None
        except BaseException:
            _rollback(conn)
            raise
    return snapshot


def write_all_rows(path: Path, snapshot: dict[str, list[dict]]) -> int:
    """Create the schema at `path` and insert every row, in foreign-key order. Returns the row count.

    All of it in ONE transaction with foreign keys enforced, so a violated key, a duplicate primary key
    or any other constraint failure rolls the whole restore back and leaves nothing behind. Constraints
    are deliberately NOT relaxed: an export that cannot satisfy them is not a faithful backup."""
    initialize(path)
    written = 0
    with _connect(path) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            for name in RESTORE_ORDER:
                table = TABLE_BY_NAME[name]
                columns = table.column_names
                statement = (f"INSERT INTO {table.name} ({', '.join(columns)}) "
                             f"VALUES ({', '.join('?' * len(columns))})")
                for row in snapshot.get(name, ()):
                    conn.execute(statement, tuple(row[column] for column in columns))
                    written += 1
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            _rollback(conn)
            raise _failure(path, exc) from None
        except BaseException:
            _rollback(conn)
            raise
    return written


def check_integrity(path: Path) -> None:
    """Raise MemoryAdapterError unless the database at `path` passes PRAGMA quick_check."""
    with _connect(path) as conn:
        _integrity_check(conn, path)


def next_rowid(path: Path, table: str) -> int:
    """The id SQLite would hand the next insert into `table`. Used to prove a restored database does not
    re-issue an id that an exported row already holds. `table` must be a known structure."""
    if table not in TABLE_BY_NAME:
        raise MemoryAdapterError(f"{table!r} is not a memory structure.")
    rows = _read(path, f"SELECT COALESCE(MAX(id), 0) + 1 FROM {TABLE_BY_NAME[table].name}", ())
    return int(rows[0][0])


# --- Slice 5: deleting one entry ----------------------------------------------------------------------

def delete_row(path: Path, table: str, key) -> int:
    """Delete the one entry of `table` with primary key `key`, and return how many rows went.

    0 means there was nothing there - the caller reports that rather than claiming a success.

    SECURITY: `table` is looked up in the fixed schema map and the structure's own name and key column
    are what reach the statement; a name that is not a known structure raises before any SQL is built.
    The key itself is a parameter. Nothing from outside the schema is ever concatenated.

    Foreign keys are enforced on this connection (_connect turns them on OUTSIDE the transaction, which
    is what makes ON DELETE CASCADE and ON DELETE SET NULL actually happen), so the dependent rows the
    schema describes are dealt with inside this same transaction or not at all."""
    structure = TABLE_BY_NAME.get(table)
    if structure is None:
        raise MemoryAdapterError(f"{table!r} is not a memory structure.")
    column = structure.key_column
    with _connect(path) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(f"DELETE FROM {structure.name} WHERE {column} = ?", (key,))
            affected = cursor.rowcount
            conn.execute("COMMIT")
            return int(affected)
        except sqlite3.Error as exc:
            _rollback(conn)
            raise _failure(path, exc) from None
        except BaseException:
            _rollback(conn)
            raise
