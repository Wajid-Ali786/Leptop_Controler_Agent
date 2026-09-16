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

Current phase: 1 — Basic Computer Control. IN PROGRESS (started 2026-09-16) - one task at a time.
Phase 0 — Foundation: COMPLETE (Done-when checklist passed 2026-09-16; tagged v0.1).
Model selection is still PENDING - deferred to Phase 3 (see brain.model in config/config.yaml).
