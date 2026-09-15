# AI Desktop Companion

A personal Windows desktop AI assistant (Windows 11 primary, Windows 10 22H2 where
practical). You speak or type a command; it understands, plans, checks safety, acts,
verifies the result, and remembers what matters.

**Status:** Step 3 project skeleton only — no features implemented yet.
Current phase: 0 — Foundation (see `docs/step4-production-development.md` Section 18).

## Architecture

Eight components, one folder each under `app/`:

```
Listener → Brain → Planner → Permission & Safety → Executor → Verifier → Memory
                         (Dashboard observes all of it)
```

| Folder | Component |
|---|---|
| `app/listener/` | Speech-to-text (local Whisper) |
| `app/brain/` | Claude API adapter + intent understanding |
| `app/planner/` | Intent → numbered step list |
| `app/safety/` | Permission & Safety gate |
| `app/executor/` | Mouse / keyboard / browser / app control |
| `app/verifier/` | Checks each step actually worked |
| `app/memory/` | SQLite-backed structured memory |
| `app/dashboard/` | Tray icon + status/settings window |
| `app/integrations/` | Placeholder until Phase 10 |

Each module contains `adapter.py` (the only file that talks to an external system),
`logic.py` (decision-making), and `models.py` (data shapes).

## Project layout

```
app/        application modules (above)
tests/      one test file per module
docs/       project specification — the source of truth
config/     config.yaml (non-secret settings) + settings.py (get_setting)
data/       memory.db and local files (git-ignored)
logs/       rotating log files (git-ignored)
scripts/    one-off setup/dev scripts
main.py     thin entry point
```

Secrets go in a local `.env` (copy `.env.example`); never commit `.env`.

## Documentation

- `docs/build-plan.md` — build & production plan
- `docs/step1-feasibility_risk.md` — feasibility, risks, requirements
- `docs/step2-dev-environment.md` — development environment
- `docs/step3-workspace-architecture.md` — workspace & architecture
- `docs/step4-production-development.md` — phase-by-phase development plan

## Non-Goals

For the initial personal version, this system is **not** intended to be: an unrestricted
autonomous agent that acts without limits, a multi-user platform, a SaaS product, a
remote-computer controller, or a guaranteed-compatible automation system for every
third-party application. (docs/build-plan.md Section 6.9)
