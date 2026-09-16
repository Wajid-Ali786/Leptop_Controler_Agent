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
4. Every module change stays inside its own app/<module>/ folder unless the task
   explicitly requires wiring two modules together in main.py.

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

## Current phase
See /docs/build-plan.md Section 6 for the phase table. Check which phase is active
before starting new work — don't build ahead of the current phase.

Current phase: 0 — Foundation. Code complete; close-out in progress - run scripts/phase0_checklist.py.
Phase 0 is NOT signed off until the real-key and no-internet checks it lists as PENDING pass.
