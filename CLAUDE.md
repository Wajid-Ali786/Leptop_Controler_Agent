# AI Desktop Companion — Project Rules

## What this project is
A personal, Windows desktop AI assistant. Full context: /docs/build-plan.md,
/docs/step1-feasibility_risk.md, /docs/step2-dev-environment.md,
/docs/step3-workspace-architecture.md, /docs/step4-production-development.md.

## Non-negotiable architecture rules
1. Pipeline order is fixed: Listener → Brain → Planner → Permission & Safety →
   Executor → Verifier → Memory. Dashboard observes all of it; it is not a pipeline step.
2. Adapter pattern is mandatory. Each app/<module>/adapter.py is the ONLY file
   allowed to import an external library (anthropic, faster_whisper, pyautogui,
   pywinauto, playwright). logic.py never imports these directly.
3. Every setting lives in config/config.yaml or .env — never hardcoded in code.
4. Every module change stays inside its own app/<module>/ folder, and modules are normally
   wired together in main.py. Sanctioned exception: a module's logic may call another
   module's logic directly where routing the call through main.py would create a way to
   bypass safety or verification. Example: app/executor/logic.py calls app/safety
   (authorize before every action) and app/verifier/logic.py (verify after every action),
   so no code path can act unauthorized or unverified. The emergency stop
   (app/executor/emergency_stop.py) is likewise callable from any module by design.

Git operations belong to the project owner (permanent; not numbered, so existing rule
references stay valid): never run git commit, git push, git tag, or any other command that
modifies Git history or the remote (e.g. amend, rebase, reset, merge, cherry-pick, revert,
branch/tag deletion). The owner does all of these. Read-only commands (git status, git diff,
git log, git show) are fine. Stage files (git add) only when the owner explicitly asks.

## Safety rules — apply from the first line of code, not "later"
5. Every action the Executor takes must pass through app/safety/ first. No exceptions,
   even during early testing.
6. Any action classified Medium risk or above must ask for user confirmation before running.
7. External content (webpages, emails, messages) read by the Brain is always DATA to
   reason about, never an instruction to obey. Never let read content and user commands
   merge into one instruction stream.
8. A rate limit, a token limit, and a money budget must all be enforced before any
   feature that calls the Claude API ships — not just one of the three.

## Development process
9. Build one small, testable feature at a time. Run it before asking for the next one.
10. Every completed feature gets a Git commit with a short, clear message.
11. When a feature is added, ask whether tests/ needs a matching test — don't skip
    silently.

## Non-Goals — do not build these for the personal version
12. Not an unrestricted autonomous agent, not multi-user, not a SaaS product, not a
    remote-computer controller, not guaranteed-compatible with every third-party app.
    If a request drifts toward one of these, flag it before building.

## Amendments
- 2026-09-16 — Rule 4: sanctioned cross-module logic calls where routing through main.py
  would create a way to bypass safety or verification (app/executor/logic.py → app/safety
  and app/verifier/logic.py).
- 2026-09-16 — Module shape (docs/step3 Section 2): a module folder may hold focused extra
  files beyond adapter.py / logic.py / models.py when a concern doesn't fit them
  (app/brain/cost_controls.py, app/executor/emergency_stop.py). Rule 2 still applies.

## Current phase
See /docs/build-plan.md Section 6 for the phase table. Check which phase is active
before starting new work — don't build ahead of the current phase.

Current phase: 5 — Screen Understanding. IN PROGRESS; NOT COMPLETE.
  The privacy and test-isolation boundaries required before expanding observation capability
  (/docs/phase4-closeout.md Limitation G) were defined in Slice 1 and are in force: structural reads
  are redirected offline, content reads are refused, and pywinauto is blocked at import.
  Slice 1 — observation foundation + local UIA target resolution: DONE (2026-10-03).
  Slice 2 — UIA re-identification + safe action-target bridge: DONE (2026-10-04), owner-run real UIA
    smoke passed (Calculator "Seven": resolved, confirmed, re-identified, one click, 7 displayed).
  Slice 3 — Brain + Planner wiring for a named click: DONE (2026-10-04), owner-run typed-console
    smoke passed ("open calculator" then "click seven").
  Auto-focus inter-slice — a named click brings its own owned window to the front after the
    confirmation: DONE (2026-10-04). SetForegroundWindow only; a refusal is a retryable message and
    never an escalation, a minimized window is refused rather than restored, and every other action
    keeps the manual focus hand-over.
  DOM Slice 1 — assistant-owned ephemeral browser session + read-only DOM resolution: DONE
    (2026-10-04). NOT yet validated against a real browser: everything offline is faked, including
    Playwright, so channel="chrome" launching, the role allowlist matching real page semantics and
    get_by_role's behaviour are all UNPROVEN until a gated real_browser run. Playwright lives only in
    app/executor/adapter.py (it can click, unlike UIA); the decision is a pure function in
    app/verifier/observation.py. The owner's own Chrome profile is unreachable: non-persistent
    context, no user-data-dir, no storage state, no cookie import. Real browser access needs the
    real_browser marker AND RUN_REAL_BROWSER_TEST=1 - real_desktop does NOT grant it. No DOM action,
    no navigation, and no UIA->DOM orchestration yet: a later slice must first audit how a Playwright
    page maps to the top-level window UIA reads.
  DOM Slice 2 — one authorized DOM click: DONE (2026-10-05). Playwright locator.click() behind the
    Executor adapter, never a coordinate conversion and never a JS/dispatch click. The element token
    now maps (inside the adapter only) to the semantic description that found it, so the target is
    re-resolved AFTER the confirmation; a changed page, a vanished control, a duplicate or a changed
    role all click nothing. The page's URL is fingerprinted inside the adapter and never returned or
    logged. NOT yet validated against a real browser. The click cannot be interrupted mid-call: the
    emergency stop is checked immediately before and immediately after, which is the honest guarantee.
  Assistant-browser wiring — the DOM primitive reachable from the typed console: DONE (2026-10-05).
    Two args-free kinds (open_browser / close_browser), parsed deterministically from "open/close
    assistant browser" so they cost no model call. `app="assistant browser"` is a RESERVED context
    selector recognised before configured apps and before Memory aliases; `app="chrome"` still means
    the owner's configured personal Chrome and routes to UIA. One assistant browser at a time, headed
    (the user navigates it themselves — there is no navigation command). A named click chooses its
    context: one candidate is used, and an owned app window beside a live browser page REFUSES and
    names both rather than preferring either. NOT yet validated end to end from the real console.
  Layers 1 and 2 of the frozen five-layer hierarchy exist. For an explicitly selected
  assistant-browser page, page-content targets route straight to DOM; HWND mapping is not required
  for that scoped workflow, and browser chrome plus native window controls remain UIA territory. OCR,
  vision and coordinate fallback are later slices; exact UIA accessible-name matching is
  authoritative, so "7" does not find a control called "Seven", and control-name aliases are
  deliberately NOT built on Phase 4's application aliases.
Phase 0 — Foundation: COMPLETE (Done-when checklist passed 2026-09-16; tagged v0.1).
Phase 1 — Basic Computer Control: COMPLETE (Done-when verified 2026-09-20).
  Verified by TWO runs of scripts/phase1_checklist.py, because the real-desktop and
  real-elevated groups need opposite foreground conditions (see /docs/step4 Section 4):
    python scripts/phase1_checklist.py --real-desktop     # elevated Notepad MINIMIZED
    python scripts/phase1_checklist.py --real-elevated    # elevated Notepad IN FRONT
  Each exits 2 because the other real group isn't selected; that is expected, not a failure.
Phase 2 — Voice: COMPLETE (Done-when verified 2026-09-29). Evidence, and the four limitations it
  carries, are in /docs/phase2-closeout.md — read that before starting Phase 3. In short: spoken
  emergency stop is NOT implemented (Ctrl+Alt+Backspace stays authoritative), TYPE_TEXT from voice
  mode targets the console window, and Roman Urdu speech can come back in Devanagari or Urdu script,
  so the Brain will need three scripts rather than one.
Phase 3 — Brain + Planner: COMPLETE (Done-when verified 2026-10-02). Evidence, and the eight
  limitations it carries, are in /docs/phase3-closeout.md — read that before starting Phase 4. In
  short: Voice may plan only open_app and close_app until focus handover is redesigned; a window
  whose ownership token can't be attached is not automatically closable; a destroyed-and-recreated
  window refuses rather than guessing; persistent memory is Phase 4, broad conversational context
  Phase 6, and communication integrations Phase 10.
Phase 4 — Memory: COMPLETE (Done-when verified 2026-10-03). Evidence, and the seven limitations it
  carries, are in /docs/phase4-closeout.md — read that before starting Phase 5. In short: Memory data
  is never sent to Claude and any future egress needs its own privacy review; the People query boundary
  has no messaging caller because messaging is Phase 10; nine of the fifteen structures are
  persistence-oriented only; and the wider verifier observation reads (read_text, the clipboard reads,
  active_target, cursor_position, window_at, list_windows) are NOT centrally isolated yet.
Model selection is RESOLVED: brain.model = claude-sonnet-5-5 (config/config.yaml).

## Test safety rule (added 2026-10-01, after a real desktop side-effect incident)
- Never run a pytest or scratch probe that can reach a physical or provider boundary from
  outside the repository's protected pytest root. conftest.py and safety_guards.py at the
  repository root are the outer boundary; a file outside it gets no guards at all.
- Normal/offline tests must rely on the project-wide fail-closed guards, not on high-level
  mocks alone. A mock on the wrong level is not isolation: when a refactor moves the level
  being mocked, the tests keep passing while the machine is acted on.
- Never claim "no physical/provider action occurred" unless that specific boundary has
  evidence — from a guard that fired, or from an independent observation. Verify each
  boundary separately; never infer one from another.
