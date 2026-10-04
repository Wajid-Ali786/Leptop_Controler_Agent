# Phase 4 — Memory: CLOSEOUT RECORD

**Status: COMPLETE / CLOSED** — closed 2026-10-03.

This document records what Phase 4 delivered, the evidence for it, and what it deliberately did not
deliver. It is a record, not a plan.

**The frozen planning documents are unchanged and remain the source of truth for what Phase 4
required:** Master Plan v0.1, `step1-feasibility_risk.md`, `step2-dev-environment.md`,
`step3-workspace-architecture.md`, `step4-production-development.md`. Nothing in this file overrides
them; Section 7 of frozen Step 4 is still the definition of done for this phase.

---

## 1. Frozen Build bullets — all delivered

> **Frozen wording (Step 4 Section 7):** *Build the structured model (Build Plan Section 6.8), as real
> database tables, not a notes blob:* Identity, People, Relationships, Contacts, Applications, Projects,
> Files, Websites, Preferences, Habits, Workflows, Corrections, Short-term context, Permissions,
> Sensitive-data rules. *Also build:* SQLite schema and `app/memory/adapter.py`; Memory retrieval
> (lookups the Brain/Planner can query); Memory update; Three correction levels (temporary correction,
> learned pattern, explicit memory); Export/import; Memory deletion (per-entry and full-wipe).

| Frozen bullet | Where |
|---|---|
Fifteen structures as **real tables**, not a notes blob | `app/memory/models.py` — `TABLES`, one `Table` per structure with explicit columns. No generic notes table, no key/value bag, no JSON document. A test requires every structure to carry at least two real columns |
SQLite schema and `app/memory/adapter.py` | the **only** file in the module importing `sqlite3`; `models.py` and `logic.py` hold no connection |
Memory retrieval — *lookups the Brain/Planner can query* | `app/memory/queries.py` — `person()`, `contact()`, `application()`. Typed results; no SQL, no adapter and no `sqlite3` reachable from Brain or Planner |
Memory update | `add_person`, `add_relationship`, `add_contact`, `remember_application_alias`, `set_sensitive_rule` |
Three correction levels | `correct_for_this_task` (temporary), `observe_correction` (learned pattern at 2), `remember_explicitly` (explicit) |
Export / import | `export_memory` (versioned JSON), `restore_memory` (into a fresh database) |
Memory deletion — per-entry **and** full-wipe | `delete_entry` for all fifteen structures by each one's own key, plus `clear_identity` / `clear_short_term_context`; `wipe` unchanged from Slice 1 |

---

## 2. Frozen Done-when — evidence

> **Frozen wording (Step 4 Section 7):** *the three correction levels are each demonstrated at least once
> (a one-off fix that doesn't persist, a repeated correction that becomes a learned pattern, and an
> explicit "remember this" that's stored immediately), export produces a file that successfully restores
> into a fresh database, and a full memory wipe leaves the app functional (just memoryless), not broken.*

| Clause | Verdict | Evidence |
|---|---|---|
**1. A one-off fix that doesn't persist** | **PASS** | A temporary correction applies inside its task, is gone once the task ends, and writes **zero** rows — read back from SQLite. `corrections.level` is CHECK-constrained to `learned_pattern`/`explicit`, so "temporary" has no durable representation at all; a direct insert of it raises `IntegrityError` |
**2. A repeated correction becomes a learned pattern** | **PASS** | One correction stores nothing; the second writes exactly **one** `learned_pattern` row with `occurrences = 2`. Further identical corrections update that same row (3, 4…) rather than adding more. Survives reopening the database with a fresh learner, so only the row can be answering |
**3. An explicit "remember this" stored immediately** | **PASS** | One call, no repetition, visible to the very next lookup, and it creates **no** learned row. Survives reopening |
**4. Export restores into a fresh database** | **PASS** | All fifteen structures round-trip field by field out of both databases — primary keys, foreign-key links, timestamps and occurrence counts preserved; restored database passes `PRAGMA quick_check` and opens through normal Memory logic; a later insert gets a non-conflicting id |
**5. A full wipe leaves the app functional, just memoryless** | **PASS** | Wipe empties all fifteen, keeps the schema and its version, and the database accepts new memories again. Proven at **application level** across four states — valid, wiped, missing, corrupt — in each of which a deterministic Phase 0/1 command *and* a typed Brain command both still work |

No completion criteria were added.

### Final owner-validated offline suite

**3513 passed, 45 skipped, 0 failed** — independently validated by the project owner, with no physical
desktop, audio, provider or database side effect.

---

## 3. What Phase 4 implemented

- **Fifteen real structured SQLite tables**, with the frozen fifteen as the operative list (Step 4
  refines Build Plan 6.8's thirteen by unpacking its People line into People, Relationships, Contacts)
- **`app/memory/adapter.py` as the SQLite boundary** — the only file in the module that connects
- **Local Memory update and retrieval** for six structures; typed results throughout
- **People / Relationships / Contacts resolution** — `Found` / `Ambiguous` / `NotFound`, never a guess
- **Applications alias resolution that cannot widen Executor capability** — an alias resolves to an
  `app_key` configuration already has, never to an executable, a path or a command
- **Sensitive-data field filtering** on exact `(table, field, context)`, applied at the result boundary
- **Temporary correction** (task-scoped, never written), **learned pattern** (durable at two matching
  corrections), **explicit memory** (durable immediately)
- **Correction precedence: temporary > explicit > learned > ordinary stored fact** — explicit outranks
  learned on purpose, so a direct instruction is never silently overridden by something inferred
- **Export to versioned, self-describing JSON** and **restore into a fresh database**, all-or-nothing
- **Per-entry deletion** across all fifteen structures, and **full wipe** unchanged
- **A local Brain/Planner query boundary** (`app/memory/queries.py`) that always supplies a disclosure
  context and can return nothing actionable
- **Application alias integration** in the real console path — one branch, reached only after
  configuration has already refused a name for being an unknown app
- **A persistence-decision gate** on both durable correction writes: a required argument with no
  default, so a caller that has not decided cannot persist anything
- **Application-level degradation proof**: memoryless, wiped, missing and corrupt all leave the
  companion usable

### Schema identifiers never come from data

Every table and column name used by export, restore and deletion is taken from `models.TABLES`; a JSON
file or a caller supplies only values, as SQLite parameters. A table named `people; DROP TABLE people; --`
and a column named `name) VALUES ('x'); DROP TABLE people; --` are both refused as data, with the source
database intact — and an AST test requires every interpolation in those statements to come from the
schema.

---

## 4. Frozen test cases

| Category | Frozen test | Result |
|---|---|---|
**Happy path** | *"message my friend Ali" correctly resolves Ali from the People table* | **PASS** as a lookup: `name="Ali", relationship="friend"` → the intended Person, then their stored contact. **Messaging itself remains Phase 10** — `send_message`, `message`, `whatsapp`, `sms` and `email` appear in no kind table and in no front end's capability set |
**Wrong input** | *a correction to a wrong resolution ("no, the other Ali") fixes the current command only* | **PASS** — applies in its task, gone after it, zero durable rows |
**Ambiguous input** | *two contacts with similar names → the assistant asks rather than guessing* | **PASS** — `Ambiguous` with both candidates; not the first row, not the newest, not alphabetical, and the model is never asked to choose |
**Missing info** | *no matching contact → clear "I don't know who that is" rather than a wrong guess* | **PASS** — exact string, and a failed lookup writes nothing: no placeholder, no silent learning |
**Permission denied** | *a Sensitive-data-rules-tagged field isn't returned to a context that shouldn't see it* | **PASS** — `Redacted` with the address absent from the result, its repr, the message and anything logged; it is left out where the record is built, not removed afterwards |
**Recovery** | *a corrupted/missing `memory.db` produces a clear error and a path to restore from export, not a silent crash* | **PASS** — `MemoryUnavailable` naming the problem and a way forward; no raw `sqlite3` error escapes; a corrupt file is never overwritten; the core application still works |
**Emergency stop** | *N/A for this phase — no live action is running during a pure memory lookup* | **N/A, and not invented.** Memory imports no Executor module, mentions no `emergency_stop`, and cannot produce an action. The stop itself is unchanged and still covered by its Phase 1 and Phase 3 tests |

---

## 5. Test-safety note

The Phase 4 final offline baseline is **3513 passed, 45 skipped, 0 failed**.

One source of non-determinism was removed before closeout. `app/verifier/adapter.py::modifier_keys_down()`
reads live global keyboard state through `GetAsyncKeyState`, and nothing centrally replaced it — so
whether a shortcut test passed depended on whether a modifier key happened to be held while the suite
ran. It made `test_a_low_brain_floor_cannot_soften_a_real_executor_rule` fail once in six identical runs.

**Fixed in the TEST HARNESS only:**

- **production verifier code is unchanged** — `app/verifier/adapter.py` and `app/verifier/logic.py` are
  byte-identical, and the real implementation still calls `GetAsyncKeyState`
- normal offline tests redirect `modifier_keys_down()` to `[]` through one autouse fixture in the
  repository-root `conftest.py`
- the existing **DESKTOP marker + `RUN_REAL` gate** pair is reused; no new marker category exists
- real-desktop tests that satisfy that pair keep access to the real read

It **redirects rather than refuses**, because this boundary is a read: it sends nothing and changes
nothing, so there was no escape to prevent — only non-determinism to remove. A test that needs a
modifier to look held still patches the function in its own body, which runs after the fixture.
Verified by ten consecutive runs of the previously flaky test: **10/10 pass**.

Phase 4 also added a central SQLite isolation guard, closing a gap that predated it: the Claude usage
ledger had been protected only by whichever fixture a test happened to request. Both `memory.db` and
`claude_usage.db` are now redirected into each test's own temporary directory, and a database inside the
repository is refused outright.

---

## 6. Accepted limitations carried out of Phase 4 — all NON-BLOCKING

None of these is Phase 4 incompleteness. Each is either a deliberate later phase or an accepted trade.

**A. Memory data is not automatically injected into Claude prompts.** Nothing stored — a person, a
contact, a path, a preference, a correction — reaches a provider request. `PreviousActionContext`
(kind + safe target) remains the only Phase 3 payload that leaves the machine. **Any future
Memory → provider egress requires a separate privacy review**, and is not something the query boundary
can do on its own.

**B. The People/Relationship query boundary has no messaging production caller.** It is built, reachable
and tested, but nothing in Brain or Planner has a person to resolve yet, because **messaging remains
Phase 10**. Resolving Ali is where Phase 4 stops.

**C. Nine of the fifteen structures remain persistence-oriented** rather than having rich feature APIs.
People, Relationships, Contacts, Applications, Sensitive-data rules and Corrections have update and
retrieval; Identity, Projects, Files, Websites, Preferences, Habits, Workflows, Short-term context and
Permissions are created, exported, restored, deleted and wiped, but have no feature API of their own.

**D. An imprecise message after a restart.** The first repeated observation of an **already-learned**
correction reports *"I'll remember that if it comes up again (1 of 2)"*, because the process-local
repetition count was lost. **Resolution remains correct** — the learned row still wins every lookup — so
this is wording, not behaviour.

**E. Unpromoted repetition evidence is process-local.** `CorrectionLearner` counts live in the running
process and are lost on restart. An observation that never earned persistence does not survive, which the
frozen Done-when does not require it to.

**F. Phase 3's `PreviousActionContext` remains separate from Phase 4's persisted short-term context.**
They are different mechanisms: one is the current turn's runtime view, the other a single persisted
snapshot of the active person/app/page/task. Clearing the table does not end a turn or touch a
`TurnContext`.

**G. The wider verifier observation boundaries are not centrally isolated yet.** Not covered by the
keyboard guard above, and per-test-faked only:

```
active_target        cursor_position      window_at        list_windows
clipboard_sequence_number    clipboard_kinds    read_text
```

**Carried explicitly into Phase 5 — Screen Understanding DESIGN/AUDIT**, with special privacy attention
to **`read_text`** (it can read the contents of whatever window is in front) and **the clipboard reads**
(they report what is on the user's clipboard). Phase 5 is the phase that materially expands screen
observation, so it is the right place to define **one** coherent observation-boundary policy rather than
seven ad-hoc guards. Deliberately not fixed here.

---

## 7. Not Phase 4 requirements — recorded so they are not reintroduced

None of the following appears in frozen Step 4 for Phase 4, and none was adopted:

- vector databases, embeddings, RAG or semantic search
- cloud or remote memory
- LLM summarisation of memory, or autonomous learning
- knowledge graphs
- file-content indexing, OCR or web page capture (Files holds *"known file locations"*, Websites
  *"frequently used sites and their aliases"*)
- raw conversation storage or pronoun resolution (Phase 6)
- workflow execution (Phase 7)
- messaging (Phase 10)
- a secret-content classifier — the persistence decision is the caller's to make
- a tombstone format for deletions
- encryption or key management for the export

---

## 8. Configuration at closeout

| Setting | Value | Why |
|---|---|---|
`memory.db_path` | `data/memory.db` | frozen by Step 3 Section 1; `data/*.db` is git-ignored. The **path** only — remembered facts never live in configuration, which is in source control |

The database is created only when memory is initialised **explicitly**: a missing `memory.db` is reported
with a recovery direction, never silently replaced by an empty one.

---

## 9. Phase status

- **Phase 0 — Foundation: COMPLETE** (Done-when checklist passed 2026-09-16; tagged v0.1)
- **Phase 1 — Basic Computer Control: COMPLETE** (Done-when verified 2026-09-20)
- **Phase 2 — Voice: COMPLETE** (Done-when verified 2026-09-29; `docs/phase2-closeout.md`)
- **Phase 3 — Brain + Planner: COMPLETE** (Done-when verified 2026-10-02; `docs/phase3-closeout.md`)
- **Phase 4 — Memory: COMPLETE** (Done-when verified 2026-10-03; this document)
- **Next: Phase 5 — Screen Understanding — DESIGN/AUDIT only, not started**

Per the Build Plan's own framing, the project is *usable and real* after Phase 4; Phases 5–10 are
genuinely optional.

---

## 10. Deferred items, carried forward deliberately

Recorded so they are not lost, and so none is mistaken for Phase 4 incompleteness:

- the imprecise *"1 of 2"* wording after a restart (Limitation D)
- one-round clarification wording — a choice question should ask the user to **name or select** A or B
  rather than inviting a yes/no that cannot be used
- `exit` / `cancel` / `no` at the semantic-correction prompt, which currently consumes the next line
- the honest lost-ownership message, replacing *"I didn't open it"* when a window group was opened but
  can no longer be identified
- naming the window in the `close_app` confirmation — informed consent, **not** ownership proof
- more applications in `executor.apps`: the companion still controls `notepad` and `calculator`, and the
  whole stack already generalises over that setting
- the wider verifier observation isolation (Limitation G) — Phase 5
