"""
Data shapes and the schema the Memory module stores (docs/build-plan Section 6.8,
docs/step4 Section 7).

The fifteen structures frozen Step 4 requires are REAL TABLES, listed in TABLES below. They are
deliberately NOT a notes blob, a key/value bag or one JSON document: "message him" resolving to a
specific person has to be a lookup, not a guess, and that needs columns.

Nothing here imports sqlite3. These are the definitions; app/memory/adapter.py is the only file that
talks to a database.

WHAT THESE TABLES DO NOT HOLD, by construction rather than by convention (asserted in
tests/test_memory.py): API keys, passwords, auth or session tokens, raw type_text payloads,
conversation transcripts, arbitrary window titles, file contents, and the Executor's window-ownership
tokens. There is also no general-purpose payload column, because one would quietly permit all of them.
"""
from dataclasses import dataclass
from enum import Enum

# The schema this build understands. A database recording a HIGHER number was written by a newer build,
# so it is refused rather than guessed at (app/memory/logic.py). There is no migration framework in this
# slice: while Phase 4 is being built, a schema change is handled by explicitly re-initialising a
# development database, never by production logic deleting anything.
SCHEMA_VERSION = 1

# Set by the adapter when it creates a database; read back to decide whether this build can use it.
VERSION_TABLE = "schema_meta"


@dataclass(frozen=True)
class Table:
    """One frozen Phase 4 structure, and the columns that satisfy its frozen purpose."""
    name: str
    purpose: str            # quoted from the frozen docs where they state one
    columns: tuple[str, ...]

    @property
    def create_statement(self) -> str:
        body = ",\n    ".join(self.columns)
        return f"CREATE TABLE IF NOT EXISTS {self.name} (\n    {body}\n)"

    @property
    def column_names(self) -> tuple[str, ...]:
        """Just the column names, in order - the table-level constraint lines are not columns.

        This is the ONLY source of column identifiers for export and restore: no name out of a JSON
        file is ever put into SQL (app/memory/adapter.py)."""
        return tuple(column.split()[0] for column in self.columns
                     if not column.upper().startswith(("UNIQUE", "CHECK", "FOREIGN", "PRIMARY")))

    @property
    def key_column(self) -> str:
        """The column a single entry is addressed by - "id" for every structure except preferences,
        which is keyed by its name. Read off the PRIMARY KEY in the schema rather than assumed, so a
        structure keyed differently cannot be deleted by the wrong column."""
        for column in self.columns:
            if "PRIMARY KEY" in column.upper():
                return column.split()[0]
        raise ValueError(f"{self.name} has no primary key")


# --- The fifteen frozen structures -------------------------------------------------------------------
# Field sets are the MINIMUM that satisfies the frozen purpose and the frozen Phase 4 test cases.
# Nothing is added because it might be useful later.

TABLES: tuple[Table, ...] = (
    Table(
        name="identity",
        purpose='"your name, preferred interaction style"',
        columns=(
            "id INTEGER PRIMARY KEY CHECK (id = 1)",   # one row: this is the user
            "name TEXT NOT NULL",
            "interaction_style TEXT",
            "updated_at REAL NOT NULL",
        ),
    ),
    Table(
        name="people",
        purpose='"names, relationships, which phone number/contact belongs to whom" - the names part',
        columns=(
            "id INTEGER PRIMARY KEY AUTOINCREMENT",
            "name TEXT NOT NULL",
            "note TEXT",                               # a short user-written description, never a transcript
            "created_at REAL NOT NULL",
        ),
    ),
    Table(
        name="relationships",
        purpose='the relationships part of the same line - narrowly: the user\'s relationship to a person',
        columns=(
            "id INTEGER PRIMARY KEY AUTOINCREMENT",
            "person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE",
            "relation TEXT NOT NULL",                  # "friend", "brother" - as the user said it
            "created_at REAL NOT NULL",
            "UNIQUE (person_id, relation)",
        ),
    ),
    Table(
        name="contacts",
        purpose='"which phone number/contact belongs to whom"',
        columns=(
            "id INTEGER PRIMARY KEY AUTOINCREMENT",
            "person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE",
            "channel TEXT NOT NULL",                   # "phone", "email" - the kind of address
            "address TEXT NOT NULL",                   # the number or handle itself: SENSITIVE
            "created_at REAL NOT NULL",
            "UNIQUE (person_id, channel, address)",
        ),
    ),
    Table(
        name="applications",
        purpose='"installed apps and their aliases"',
        columns=(
            "id INTEGER PRIMARY KEY AUTOINCREMENT",
            # app_key REFERS to a key in config executor.apps. It is deliberately not a path and not an
            # executable: memory remembers what the user calls an app, and can never make a new one
            # launchable. An alias whose app_key is not configured resolves to the existing refusal.
            "app_key TEXT NOT NULL",
            "alias TEXT NOT NULL",
            "note TEXT",
            "created_at REAL NOT NULL",
            "UNIQUE (alias)",
        ),
    ),
    Table(
        name="projects",
        purpose='"project names, folder paths, related tools"',
        columns=(
            "id INTEGER PRIMARY KEY AUTOINCREMENT",
            "name TEXT NOT NULL",
            "folder_path TEXT",                        # a location, not its contents: SENSITIVE
            "tools TEXT",                              # a short user-written list
            "created_at REAL NOT NULL",
            "UNIQUE (name)",
        ),
    ),
    Table(
        name="files",
        purpose='"known file locations" - metadata and location ONLY: no contents, no OCR, no embeddings',
        columns=(
            "id INTEGER PRIMARY KEY AUTOINCREMENT",
            "path TEXT NOT NULL",                      # SENSITIVE (Feasibility Section 9 names file paths)
            "description TEXT",                        # what the user calls it
            "project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL",
            "last_known_state TEXT",                   # e.g. "exists", "moved" - never file content
            "created_at REAL NOT NULL",
            "UNIQUE (path)",
        ),
    ),
    Table(
        name="websites",
        purpose='"frequently used sites and their aliases" - reference only: no page capture, no cookies',
        columns=(
            "id INTEGER PRIMARY KEY AUTOINCREMENT",
            "alias TEXT NOT NULL",
            "url TEXT NOT NULL",
            "created_at REAL NOT NULL",
            "UNIQUE (alias)",
        ),
    ),
    Table(
        name="preferences",
        purpose='"browser, folders, voice behavior, confirmation style"',
        columns=(
            "key TEXT PRIMARY KEY",
            "value TEXT NOT NULL",
            "updated_at REAL NOT NULL",
        ),
    ),
    Table(
        name="habits",
        purpose='"recurring patterns worth remembering"',
        columns=(
            "id INTEGER PRIMARY KEY AUTOINCREMENT",
            "description TEXT NOT NULL",
            "observed_count INTEGER NOT NULL DEFAULT 1",
            "last_seen_at REAL NOT NULL",
            "UNIQUE (description)",
        ),
    ),
    Table(
        name="workflows",
        purpose='"saved multi-step routines" - STORAGE only; replay is Phase 7',
        columns=(
            "id INTEGER PRIMARY KEY AUTOINCREMENT",
            "name TEXT NOT NULL",
            "steps TEXT NOT NULL",                     # the saved routine, as the user named its steps
            "created_at REAL NOT NULL",
            "UNIQUE (name)",
        ),
    ),
    Table(
        name="corrections",
        purpose='"past mistakes and the context they occurred in"',
        columns=(
            "id INTEGER PRIMARY KEY AUTOINCREMENT",
            # Build Plan 6.8's three levels. Level 1 (temporary) is TASK-SCOPED and is never written
            # here - a row exists only once a correction has earned persistence by repeating
            # (learned_pattern) or because the user said "remember this" (explicit).
            f"level TEXT NOT NULL CHECK (level IN ('learned_pattern', 'explicit'))",
            "scope TEXT NOT NULL",                     # where it applies, e.g. an app_key or a website alias
            "wrong_value TEXT",                        # what was resolved before; kept so a fix is auditable
            "right_value TEXT NOT NULL",
            "context TEXT",                            # short, user-facing; never a transcript
            "occurrences INTEGER NOT NULL DEFAULT 1",  # what promotes a repeat to a learned pattern
            "created_at REAL NOT NULL",
        ),
    ),
    Table(
        name="short_term_context",
        purpose='"active person/app/page/task right now" - ONE current snapshot, never a history',
        columns=(
            "id INTEGER PRIMARY KEY CHECK (id = 1)",   # one row, so it cannot grow into a conversation log
            "person_id INTEGER REFERENCES people(id) ON DELETE SET NULL",
            "app_key TEXT",
            "page TEXT",
            "task TEXT",
            "updated_at REAL NOT NULL",
        ),
    ),
    Table(
        name="permissions",
        purpose='"what this assistant is and isn\'t allowed to touch"',
        columns=(
            "id INTEGER PRIMARY KEY AUTOINCREMENT",
            # USER-DECLARED SCOPE ONLY. app/safety remains the only authority on what may run: a row
            # here can contribute a refusal, and can never authorise an action, lower a risk level or
            # skip a confirmation. There is deliberately no risk_level column.
            "scope TEXT NOT NULL",
            "allowed INTEGER NOT NULL CHECK (allowed IN (0, 1))",
            "updated_at REAL NOT NULL",
            "UNIQUE (scope)",
        ),
    ),
    Table(
        name="sensitive_data_rules",
        purpose='"what needs stricter handling/retention"',
        columns=(
            "id INTEGER PRIMARY KEY AUTOINCREMENT",
            # Explicit enough to answer the frozen Phase 4 test "a Sensitive-data-rules-tagged field
            # isn't returned to a context that shouldn't see it": the rule names the table, the field
            # and the context, so retrieval can filter on it. Not an uninterpretable blob.
            "target_table TEXT NOT NULL",
            "target_field TEXT NOT NULL",
            "context TEXT NOT NULL",                   # which caller/scope the rule is about
            "disclosure TEXT NOT NULL CHECK (disclosure IN ('allow', 'deny'))",
            "retention TEXT",                          # how long it may be kept, in the user's terms
            "updated_at REAL NOT NULL",
            "UNIQUE (target_table, target_field, context)",
        ),
    ),
)

TABLE_NAMES: tuple[str, ...] = tuple(table.name for table in TABLES)
TABLE_BY_NAME: dict[str, "Table"] = {table.name: table for table in TABLES}

# Column names that must never appear in any Memory table. The point is not the word but what it would
# invite: a place for a secret, a transcript, the user's typed words, a window title, a file's contents
# or an Executor ownership token. Enforced as a test over TABLES.
FORBIDDEN_COLUMN_WORDS: tuple[str, ...] = (
    "api_key", "apikey", "password", "passwd", "secret", "token", "credential", "auth",
    "transcript", "conversation", "utterance", "typed_text", "type_text", "payload",
    "window_title", "title", "content", "contents", "body", "blob", "raw",
)


@dataclass(frozen=True)
class MemoryDatabase:
    """An open, usable memory database: where it is and which schema it holds."""
    path: str
    schema_version: int


@dataclass(frozen=True)
class MemoryUnavailable:
    """Memory cannot be used, and why - a RESULT, not an exception, so a caller can carry on without it.

    `recovery` is the sentence shown to the user. It always names a way forward, because frozen Step 4
    requires a corrupt or missing database to produce "a clear error and a path to restore from export,
    not a silent crash"."""
    reason: str
    recovery: str
    path: str = ""

    @property
    def message(self) -> str:
        return f"{self.reason} {self.recovery}".strip()


# --- Slice 2: the shapes local lookups return --------------------------------------------------------
# Callers branch on the RESULT TYPE, never on the wording of a message. Every one of these is pure
# Memory-domain data: no ExecutorAction, no Plan, no RiskLevel, nothing a provider could act on.

def normalize(text: str) -> str:
    """The one comparison form for a name or an alias: surrounding whitespace ignored, case ignored.

    Deliberately nothing else - no fuzzy matching, no edit distance, no phonetics, no nickname
    inference. Two records "match" only when a person would call them the same word.

    This is the ONLY normaliser, and it uses lower() rather than casefold() on purpose: the queries in
    app/memory/adapter.py apply SQLite's lower() to the column, and a second, subtly different form on
    the parameter side would make a row findable or unfindable depending on which side was folded.
    SQLite's lower() is ASCII-only, which loses nothing for Urdu or Devanagari - both are caseless."""
    return " ".join(text.split()).lower() if isinstance(text, str) else ""


@dataclass(frozen=True)
class Person:
    """Someone the user told the assistant about. `name` is kept exactly as they wrote it - normalisation
    is for comparison only and never rewrites what is shown back."""
    id: int
    name: str
    note: str | None = None


@dataclass(frozen=True)
class Contact:
    """One way to reach a person. `address` is the phone number or handle, and is the field frozen
    Step 4's Permission-denied case is about, so it is None whenever a rule withheld it.

    The repr NEVER contains the address, disclosed or not: a repr is what ends up in a log line or an
    assertion failure by accident."""
    id: int
    person_id: int
    channel: str
    address: str | None = None
    disclosed: bool = True

    def __repr__(self) -> str:
        length = len(self.address) if isinstance(self.address, str) else 0
        return (f"Contact(id={self.id!r}, person_id={self.person_id!r}, channel={self.channel!r}, "
                f"address=<{length} characters>, disclosed={self.disclosed!r})")


@dataclass(frozen=True)
class Found:
    """Exactly one record matched."""
    value: object


@dataclass(frozen=True)
class NotFound:
    """Nothing matched. `message` is safe to show the user and names no stored data."""
    message: str


@dataclass(frozen=True)
class Ambiguous:
    """More than one record matched, so nothing is chosen - not the first row, not the newest, not the
    first alphabetically. `candidates` is what a future clarification question would be built from."""
    candidates: tuple
    message: str


@dataclass(frozen=True)
class Redacted:
    """A record matched, and a sensitive-data rule withheld one of its fields for this context.

    `value` is the record with that field already removed - the field is excluded AT THIS BOUNDARY, not
    hidden from display after being handed over."""
    value: object
    withheld: str          # "<table>.<field>"
    context: str
    message: str


# --- Slice 3: the three correction levels (docs/build-plan Section 6.8) ------------------------------
# Frozen levels, and the only ones:
#
#   TEMPORARY       "No, the other button." Current task only; never written to the database.
#   LEARNED PATTERN stored once the SAME correction has been seen twice in this process.
#   EXPLICIT        "Remember this." Stored at once, with no repetition required.
#
# PRECEDENCE, highest first:
#
#   this task's temporary correction  >  explicit memory  >  learned pattern  >  ordinary stored fact
#
# Explicit beats learned on purpose: an explicit instruction from the user must not be silently
# overridden by something the assistant worked out from repetition. Nothing reorders this - not recency,
# not how many times a pattern was seen.

LEARNED_PATTERN = "learned_pattern"   # the two values corrections.level accepts
EXPLICIT = "explicit"


@dataclass(frozen=True)
class Correction:
    """One correction the USER made: in `scope`, `wrong_value` should have been `right_value`.

    Only these four fields exist, and that is deliberate - see CorrectionLearner for why a correction
    must never carry anything else."""
    scope: str
    wrong_value: str
    right_value: str
    context: str | None = None

    @property
    def identity(self) -> tuple[str, str, str, str]:
        """What makes two corrections "the same": exact equality of every field after normalisation.

        No fuzzy matching, no similarity score, no model judgement. Changing the right value, or the
        scope, makes it a DIFFERENT correction rather than another observation of this one."""
        return (normalize(self.scope), normalize(self.wrong_value), normalize(self.right_value),
                normalize(self.context or ""))

    @property
    def target(self) -> tuple[str, str, str]:
        """What a correction is ABOUT, ignoring the value it proposes - so an explicit "X should be Z"
        can be found as the correction covering X even though a learned row says "X should be Y"."""
        return (normalize(self.scope), normalize(self.wrong_value), normalize(self.context or ""))


@dataclass(frozen=True)
class CorrectionTask:
    """The temporary corrections belonging to ONE task (frozen level 1).

    Nothing here is written to the database, and nothing survives the task: a task "ends" when its object
    is discarded, and ended() makes that explicit. Frozen and threaded by the caller, like the Planner's
    TurnContext, so two tasks running at once cannot leak into one another."""
    name: str
    corrections: tuple[Correction, ...] = ()

    def with_correction(self, correction: Correction) -> "CorrectionTask":
        """This task, plus one temporary correction. A later correction of the same target replaces the
        earlier one rather than piling up behind it."""
        kept = tuple(existing for existing in self.corrections
                     if existing.target != correction.target)
        return CorrectionTask(self.name, kept + (correction,))

    def ended(self) -> "CorrectionTask":
        """The same task with its temporary corrections gone - what "the task ended" means."""
        return CorrectionTask(self.name)

    def correction_for(self, scope: str, value: str, context: str | None = None) -> Correction | None:
        target = (normalize(scope), normalize(value), normalize(context or ""))
        for correction in reversed(self.corrections):
            if correction.target == target:
                return correction
        return None


class CorrectionLearner:
    """How many times the same correction has been seen IN THIS PROCESS. Frozen level 2's evidence.

    This is NOT a Memory record and NOT a table:
      * it never changes what a lookup returns - only a promoted learned_pattern row does that
      * it is lost when the companion restarts, which the frozen Done-when does not require to survive
      * it holds ONLY the normalised identity of a correction: scope, wrong value, right value, context

    That last point is the privacy boundary. It is not a transcript cache: there is nowhere here for a
    conversation, a typed payload, a window title, a credential or any other task detail to be kept, and
    the repr deliberately shows a count rather than the identities it is counting."""

    PROMOTE_AT = 2    # the approved threshold: the same correction, twice

    def __init__(self):
        self._seen: dict[tuple[str, str, str, str], int] = {}

    def observe(self, correction: Correction) -> int:
        """Record one occurrence and return how many times this exact correction has now been seen."""
        count = self._seen.get(correction.identity, 0) + 1
        self._seen[correction.identity] = count
        return count

    def count(self, correction: Correction) -> int:
        return self._seen.get(correction.identity, 0)

    def __repr__(self) -> str:
        return f"CorrectionLearner(tracking={len(self._seen)} corrections)"


# --- Slice 4: export and restore (frozen Done-when D4 / D5) -------------------------------------------
# One versioned, self-describing JSON document holding all fifteen structures. It is a BACKUP of the
# user's own durable memory, so it carries the real values - including the sensitive ones - and the
# sensitive-data rules alongside them. Restoring facts while dropping the rules that guard them would
# reinstate data with its protections stripped, which is why both travel together.
#
# The file is therefore SENSITIVE PLAINTEXT. No encryption is required by the frozen requirement and
# none is invented here; what is enforced is that no row value is ever logged, shown in a repr, or
# echoed back in a failure message.

EXPORT_FORMAT_VERSION = 1

# The order rows must be inserted in, so a foreign key always has its target already present. Fixed
# here rather than taken from the export, and a test derives the real dependencies from the REFERENCES
# clauses above and checks this order satisfies every one of them.
RESTORE_ORDER: tuple[str, ...] = (
    "identity",
    "people",              # before relationships, contacts and short_term_context
    "relationships",
    "contacts",
    "projects",            # before files
    "files",
    "applications",
    "websites",
    "preferences",
    "habits",
    "workflows",
    "corrections",
    "short_term_context",
    "permissions",
    "sensitive_data_rules",
)


@dataclass(frozen=True)
class Exported:
    """A backup was written. Counts only - never a row of remembered data."""
    path: str
    tables: int
    rows: int


@dataclass(frozen=True)
class Restored:
    """A backup was restored into a fresh database. Counts only."""
    path: str
    tables: int
    rows: int


@dataclass(frozen=True)
class BackupRefused:
    """An export or a restore did not happen, and why.

    `reason` names tables, fields, versions and paths - never a value out of the file. A malformed export
    must not get its contents quoted back through an error message."""
    reason: str
    path: str = ""

    @property
    def message(self) -> str:
        return self.reason


# --- Slice 5: per-entry deletion ----------------------------------------------------------------------
# The frozen Build bullet is "Memory deletion (per-entry and full-wipe)". Full wipe exists; this is the
# other half. It covers all fifteen structures, by the key each one actually has - and it is deletion
# only: no table gains create or update operations it did not already have.

SINGLETON_ROW_ID = 1       # identity and short_term_context each hold exactly one row, CHECK (id = 1)


@dataclass(frozen=True)
class Deleted:
    """One durable entry was removed.

    Carries the structure's name and the entry's KEY - never any of its contents, so deleting a contact
    cannot put an address into a result, a log line or an assertion failure."""
    table: str
    key: object


# --- Final integration: the persistence decision -----------------------------------------------------

class PersistenceDecision(Enum):
    """Whether a caller has decided this correction is safe to keep.

    Memory has no way to recognise a password or an API key inside an arbitrary string, and it must not
    guess. So a durable write requires the CALLER to say so: the decision is a required argument with no
    default, which means a caller that has not thought about it cannot accidentally persist anything.

    It carries no value of its own - only "allow" or "deny" - so the decision itself can never become a
    place where a secret is passed along."""
    ALLOW = "allow"
    DENY = "deny"

