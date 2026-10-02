"""
Memory - SQLite-backed structured memory (docs/build-plan Section 6.8, docs/step4 Section 7).

Phase 4 Slice 1 (persistence foundation) is built: the fifteen frozen structures as real tables, an
explicit schema version, explicit initialisation, fail-soft opening, and a full wipe. Retrieval, the
three correction levels, export/import and per-entry deletion are later slices, and nothing here is
wired into the Brain, the Planner or the Executor yet.

models.py defines the tables; adapter.py is the only file that talks to SQLite; logic.py decides.
"""
