# AI Desktop Companion

A personal Windows desktop AI assistant (Windows 11 primary, Windows 10 22H2 where
practical). You speak or type a command; it understands, plans, checks safety, acts,
verifies the result, and remembers what matters.

**Status:** Phase 0 (Foundation) complete — tagged `v0.1`. **Phase 1 (Basic Computer Control)
complete**, verified 20 September 2026: typed commands (`python main.py --console`) drive opening
and closing apps, clicking by screen coordinate, typing text, keyboard shortcuts, scrolling,
refresh (browsers and File Explorer) and window controls — each through the safety gate and checked
by the Verifier — with a global emergency-stop hotkey (`Ctrl+Alt+Backspace`) that halts an action
mid-way. Next phase: **Phase 2 — Voice**.
See `docs/step4-production-development.md` Section 4 (implementation notes and the Phase 1
close-out evidence) and Section 18.

## Setup (fresh clone)

Needs **Python 3.12** and **Git** on PATH (see `docs/step2-dev-environment.md`).

```powershell
git clone https://github.com/Wajid-Ali786/Leptop_Controler_Agent.git
cd Leptop_Controler_Agent
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Then create your local `.env` from the template and paste your Anthropic API key
(billed separately from any Claude subscription; never commit this file):

```powershell
copy .env.example .env
notepad .env
```

`requirements.txt` is the full pinned set. `requirements.in` is the shorter list of
packages this project actually chose; regenerate the pins after changing it:

```powershell
pip install -r requirements.in
pip freeze > requirements.txt
```

## Running

```powershell
python main.py                 # startup health check, offline only
python main.py --check-claude  # also makes one real, minimal Claude request (costs a few tokens)
python main.py --console       # type commands ("open notepad"); help lists them, exit leaves
```

`main.py` exits 0 when every check passes and 1 otherwise, naming what is wrong.

In the console, one line is one command: `open notepad`, `close notepad`, `close window`,
`click 500, 300`, `type hello`, `shortcut ctrl+a`, `scroll down 3`, `refresh`, `minimize`,
`maximize`, `restore`. Anything Medium risk or above asks first, and only the exact answer `yes`
runs it. Because the console is itself the active window while you type, a command that acts on
another window waits for you to switch to it first — the console only watches which window is in
front, and never switches windows itself.

## Tests

```powershell
pytest                                                  # whole suite, offline, no API calls
python scripts/phase0_checklist.py                      # Phase 0 pass/fail checklist
python scripts/phase1_checklist.py                      # Phase 1 checklist (offline only; safe default)
python scripts/phase1_checklist.py --real-desktop       # + real-desktop acceptance (elevated Notepad MINIMIZED)
python scripts/phase1_checklist.py --real-elevated      # + permission denied (elevated Notepad IN FRONT)
python scripts/fresh_clone_check.py                     # verify a fresh clone installs and passes
$env:RUN_REAL_CLAUDE_TEST='1'; pytest -m real_api       # opt-in real API tests (cost a few tokens)
$env:RUN_REAL_DESKTOP_TEST='1'; pytest -m real_desktop -s  # opt-in: really opens/closes Notepad and Calculator
python scripts/trace_app_windows.py notepad calculator  # diagnostic: an app's windows over time while opened/closed
```

Phase 1's full evidence needs **two** checklist runs, because the real-desktop tests need their own
windows to reach the foreground while the permission-denied test needs an elevated Notepad in front,
and Windows won't let a normal process take the foreground back from an elevated window. Each run
therefore exits **2** — "nothing failed, but the other real group wasn't selected" — which is expected,
not a failure. Real-desktop tests also need an idle mouse and keyboard.

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
app/        application modules (above), plus health.py and logging_setup.py
tests/      one test file per module
docs/       project specification — the source of truth
config/     config.yaml (non-secret settings) + settings.py (get_setting)
data/       memory.db, claude_usage.db and local files (git-ignored)
logs/       rotating log files (git-ignored)
scripts/    repeatable checks (Phase 0 and Phase 1 checklists, fresh-clone verification) and dev diagnostics
main.py     thin entry point
```

Secrets live in a local `.env` (copy `.env.example`); never commit `.env`.
`config/` is the bottom layer: `app/` imports from it, never the other way round.

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
