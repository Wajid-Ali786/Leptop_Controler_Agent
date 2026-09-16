# Step 4 — Production Development Plan
*AI Desktop Companion project — what to build, in what order, and what "done" means. No decisions are reopened from Steps 1–3.*
*Prepared: 14 September 2026*

---

## 1. Scope

This document covers **feature implementation only**. It starts from the Step 3 project structure (already committed to Git) and tells Claude Code exactly what to build, phase by phase, with a measurable pass/fail test for each one. It does not re-decide architecture, tooling, or folder structure — those are frozen in Steps 1–3 and are referenced here, not repeated.

If a task in this document seems to require reopening a Step 1–3 decision, that's a signal to stop and flag it explicitly, not to quietly decide it inline.

---

## 2. Development Rules

These apply to every phase below, without exception:

1. **Vibe coding with Claude Code** — Claude Code writes the code; you direct, test, and approve.
2. **One small, testable feature at a time.** Never ask for "the whole phase" in one request — break every phase below into its individual bullet points and build one at a time.
3. **Test before moving on.** A feature isn't finished when it compiles — it's finished when its test passes (see Section 14).
4. **Git commit after every working change**, using the naming convention in Section 16.
5. **Never bypass Permission & Safety**, even temporarily "just to test something." If a test requires bypassing it, that's a sign the safety module itself needs a test mode — build that, don't route around the gate.
6. **Don't build future-phase features early.** If Phase 3 work naturally suggests a Phase 6 feature, note it and stay in Phase 3. Building ahead of the current phase is exactly the failure mode the Build Plan's phase-gate structure (Section 8) exists to prevent.

---

## 3. Phase 0 — Foundation

**Build:**
- Project skeleton confirmed working (the Step 3 structure, actually populated with empty `__init__.py`/`adapter.py`/`logic.py`/`models.py` files per module)
- `config/settings.py` — reads `config/config.yaml` and `.env`, exposes `get_setting(name)`
- Logging — a simple rotating log to `logs/`, used by every module from the start
- Environment loading — confirms `.env` loads correctly and fails loudly (not silently) if `ANTHROPIC_API_KEY` is missing
- `app/brain/adapter.py` — minimal Claude API adapter (Section 2 of Step 3), tested with **both** a real API call and a mocked/local response — so Phase 0 isn't blocked by internet or API issues on a given day
- Rate limit, token limit, and money budget (Build Plan Section 5.5) — implemented as real enforced checks, not comments
- A minimal Permission & Safety gate (`app/safety/logic.py`) — even a single hardcoded rule ("does this action contain delete/shutdown/send? → confirm") counts, as long as it's real and enforced
- Basic `pytest` setup — one trivial passing test, confirming the test runner itself works
- A startup/health check — running `main.py` prints a clear "all systems OK" or names exactly what's missing

**Test cases:**
| Category | Test |
|---|---|
| Happy path | `main.py` launches cleanly, logs a startup message, exits cleanly |
| Happy path (mocked) | A mocked/local fake Claude response flows through `app/brain/adapter.py` correctly, with no internet or real API key required — this is what makes Phase 0 testable offline |
| AI failure | Invalid/missing API key → clear error message naming the problem, not a stack trace |
| Network unavailable | No internet → Claude adapter fails gracefully with a specific "can't reach Claude" message, rest of the app still starts |
| Permission denied | Simulate a blocked rate/budget check → the gate actually blocks the call, doesn't just log a warning |
| Recovery | Restarting after a crash doesn't corrupt config or logs |
| Emergency stop | The stop mechanism (even a placeholder function) exists and is callable — full behavior arrives in Phase 1 |

**Done when:** `main.py` runs cleanly on a fresh clone of the repo, the health check reports all systems OK with a valid API key and a clear specific failure with an invalid one, the mocked/local test path passes with no internet connection at all, `pytest` runs and passes, and a deliberately-oversized fake request is blocked by the token/budget limit rather than sent.

---

## 4. Phase 1 — Basic Computer Control

**Build (typed commands only — no voice yet):**
- Open/close applications
- Mouse click (by coordinate, as the deliberate last-resort fallback per the observation hierarchy — full hierarchy arrives in Phase 5)
- Type text
- Keyboard shortcuts
- Scroll
- Refresh (browser/window)
- Basic Windows window controls (minimize/maximize/close)
- `app/executor/adapter.py` wrapping `pyautogui`/`pywinauto`
- `app/verifier/logic.py` — confirms each action's expected result actually happened
- Emergency stop — a real interrupt that halts the Executor mid-action, tested explicitly against its own defined test rather than assumed to be instant (Feasibility doc Section 8 treats any *unmeasured, unacceptable* lag here as a bug — measure first, then set the bar)

**Test cases:**
| Category | Test |
|---|---|
| Happy path | "open notepad" opens Notepad; "type hello" types "hello" |
| Wrong input | "open nonexistentapp123" fails cleanly with a clear message |
| Missing info | "open" with nothing specified → asks which app, doesn't guess |
| App unavailable | Targeting an app that isn't installed → clear failure, no silent hang |
| Permission denied | An action requiring admin rights → confirmation/refusal per Section 3's Windows permissions note, not a crash |
| Recovery | An app that fails to open → Verifier catches it, Action→Result→Recovery loop offers retry |
| Emergency stop | Interrupt halts the Executor; measure actual response time on your machine first, then confirm it meets whatever threshold you define as acceptable — don't assume a number before measuring |

**Done when:** 10 different typed commands, covering every bullet above, run correctly back-to-back with no code changes between them, the emergency stop halts as quickly as technically possible on your machine and passes the emergency-stop test you define for it (measured, not assumed), and the Verifier correctly detects at least one deliberately-broken action (e.g. targeting a renamed button) instead of reporting false success.

---

## 5. Phase 2 — Voice

**Build:**
- `app/listener/adapter.py` wrapping local `faster-whisper`
- English recognition
- Urdu recognition
- Hindi recognition
- Roman Urdu handling (Feasibility doc Section 6.6/6.7 note: this is English-script text the Brain interprets contextually, not a distinct Whisper language code)
- Mixed-language correction loop ("No, I said Ali, not Alif" — Build Plan Section 6.6/6.8)
- TTS output via `edge-tts`
- Offline TTS fallback (Windows built-in/`pyttsx3`)

**Test cases:**
| Category | Test |
|---|---|
| Happy path | A clear English command is transcribed and spoken back correctly |
| Wrong input | A misheard word is corrected via the correction loop without repeating the whole command |
| Ambiguous input | An accented or fast Roman Urdu phrase — confirm it either resolves correctly or asks for clarification, doesn't silently misfire an action |
| Missing info | Silence/background noise doesn't trigger a false command |
| Network unavailable | No internet → TTS falls back to the offline engine automatically, Listener (local) is unaffected |
| Recovery | A failed transcription re-prompts rather than acting on a guess |
| Emergency stop | A spoken "stop"/interrupt phrase halts an in-progress action |

**Done when:** 5 loosely-worded commands (including one deliberately mixed-language) are understood correctly or correctly clarified, at least one misheard word is fixed via the correction loop without restarting the command, and TTS output is audible in both the online and offline paths.

---

## 6. Phase 3 — Brain + Planner

**Build:**
- Natural command understanding via Claude
- Structured intent extraction
- Plan generation (the numbered step list from the Build Plan, Section 2)
- Clarification questions when intent is genuinely ambiguous — e.g. "Usko message kar do" → "Ali ko?" rather than guessing
- Risk classification feeding into Permission & Safety (reasoning-aware, building on Phase 1's basic keyword version)
- Action selection (which Executor capability a plan step maps to)
- Context handling (short-term — full conversational context arrives in Phase 6)

**Test cases:**
| Category | Test |
|---|---|
| Happy path | A loosely-worded command produces a correct, inspectable plan |
| Wrong input | A nonsensical command is rejected with a clear message, not silently "planned" |
| Ambiguous input | "Usko message kar do" with no prior context triggers a clarification question, never a guess |
| Missing info | A command missing a required detail (e.g. no message content) is flagged before planning proceeds |
| Network unavailable | Claude unreachable → the assistant states this plainly (Feasibility doc Section 8), Phase 1-level direct commands still work |
| AI failure | A malformed/unexpected API response is caught and reported, not passed silently to the Executor |
| Permission denied | A plan step classified High/Critical risk stops for confirmation before Planner hands it to the Executor |
| Recovery | A rejected/failed plan can be corrected and re-planned without restarting the whole command |
| Emergency stop | Works identically to Phase 1 — confirm it isn't accidentally bypassed by the new Planner layer |

**Done when:** 5 loosely-worded commands (including the mixed-language one from Phase 2) produce correct plans or correct clarification questions, the "usko message kar do" ambiguity case is handled exactly as specified above at least 5 times in a row, and a simulated Claude-unavailable test correctly falls back to the Phase 0/1 error path instead of crashing.

---

## 7. Phase 4 — Memory

**Build the structured model (Build Plan Section 6.8), as real database tables, not a notes blob:**
Identity, People, Relationships, Contacts, Applications, Projects, Files, Websites, Preferences, Habits, Workflows, Corrections, Short-term context, Permissions, Sensitive-data rules.

**Also build:**
- SQLite schema and `app/memory/adapter.py`
- Memory retrieval (lookups the Brain/Planner can query)
- Memory update
- Three correction levels (Build Plan Section 6.8): temporary correction, learned pattern, explicit memory
- Export/import (Feasibility doc Section 9 — protecting your data means being able to back it up)
- Memory deletion (per-entry and full-wipe)

**Test cases:**
| Category | Test |
|---|---|
| Happy path | "message my friend Ali" correctly resolves Ali from the People table |
| Wrong input | A correction to a wrong resolution ("no, the other Ali") fixes the current command only |
| Ambiguous input | Two contacts with similar names → the assistant asks rather than guessing |
| Missing info | No matching contact → clear "I don't know who that is" rather than a wrong guess |
| Permission denied | A Sensitive-data-rules-tagged field isn't returned to a context that shouldn't see it |
| Recovery | A corrupted/missing `memory.db` produces a clear error and a path to restore from export, not a silent crash |
| Emergency stop | N/A for this phase — no live action is running during a pure memory lookup |

**Done when:** the three correction levels are each demonstrated at least once (a one-off fix that doesn't persist, a repeated correction that becomes a learned pattern, and an explicit "remember this" that's stored immediately), export produces a file that successfully restores into a fresh database, and a full memory wipe leaves the app functional (just memoryless), not broken.

---

## 8. Phase 5 — Screen Understanding

**Build the full observation hierarchy (Build Plan Section 6.5), in this order of preference:**
```
1. Windows UI Automation tree
2. Browser DOM (via Playwright)
3. OCR / text recognition
4. Screenshot + Claude vision
5. Coordinate fallback
```
Each layer needs its own verification and recovery: if a layer fails to find the target, the system retries a higher layer before falling back, rather than jumping straight to coordinates.

**Test cases:**
| Category | Test |
|---|---|
| Happy path | A native app button is found and clicked via the UI Automation tree, no vision call needed |
| App unavailable | An app with no accessibility tree exposed → falls through cleanly to OCR/vision |
| UI changed | A button moves/resizes between runs → higher layers still find it; only pure coordinate fallback breaks (expected, per Feasibility doc Section 1) |
| Permission denied | An app blocking simulated input (Feasibility doc Section 3) → clear failure naming the restriction, not a silent hang |
| Recovery | A failed click at one layer triggers a retry at a different layer before asking the user |
| Emergency stop | Works identically through every layer of the hierarchy |

**Done when:** the same target ("click Login") is correctly found via at least three different layers of the hierarchy in three different test apps/pages, a UI change test confirms the system doesn't fall back to raw coordinates when a higher layer still works, and a vision-fallback call only fires when the UI tree, DOM, and OCR all genuinely fail — confirmed by checking logs, not assumed.

---

## 9. Phase 6 — Conversation & Context

**Build:**
- Full short-term context tracking across a multi-turn exchange — e.g. "Message Ali" → "Tell him I'm busy" correctly resolves "him" to Ali without repeating the name
- Context expiry rules (when does "him" stop meaning Ali? — e.g. a new subject introduced, or a time-out)

**Test cases:**
| Category | Test |
|---|---|
| Happy path | The "message Ali" / "tell him I'm busy" example works correctly, 5 times in a row |
| Ambiguous input | A new person is mentioned mid-conversation → old context is correctly replaced, not blended |
| Missing info | Context expired/cleared → "tell him" with no recent subject asks who, doesn't guess |
| AI failure | A context-tracking error is caught and surfaced, not silently misattributed |

**Done when:** the core "him = Ali" example passes 5/5, and a context-switch test (mentioning a second person, then testing that pronouns correctly point to the new one) also passes.

---

## 10. Phase 7 — Workflow Learning

**Build:**
- Record/save a workflow (a sequence of already-verified steps)
- Replay a saved workflow
- Repeat N times
- Corrections during a workflow (using the same three-level system from Phase 4)
- Learned patterns specific to workflows
- Workflow-level verification (each step still individually verified during replay)
- Workflow-level recovery (a failed step during replay pauses and asks, rather than continuing blind)

**Test cases:**
| Category | Test |
|---|---|
| Happy path | A recorded 3-step workflow replays correctly |
| Wrong input | Replaying against a changed environment (e.g. a moved button) is caught by Phase 5's Verifier, not blindly executed |
| Missing info | Replaying a workflow that references a since-deleted app/contact fails clearly, not silently |
| Recovery | A failed step mid-replay pauses the whole workflow and asks, rather than continuing to step N+1 |
| Emergency stop | Halts a workflow mid-replay exactly as it would a single action |

**Done when:** a saved workflow replays correctly 3 times in a row, a deliberately-broken replay (moved button) is caught and paused rather than executed blindly, and "repeat N times" correctly stops after N with no drift.

---

## 11. Phase 8 — Full Safety

**Important — this is an expansion, not the introduction of safety.** A minimal Permission & Safety gate has been live and enforced since Phase 0, and became reasoning-aware in Phase 3. Nothing in this project runs without *some* safety check at any point before this phase — Phase 8 makes that check sophisticated, it does not switch it on.

**Expand Phase 0/3's basic and reasoning-aware safety into the complete system:**
- Low / Medium / High / Critical risk tiers, as defined in the Build Plan (Section 2) and vision doc Section 10
- Detailed per-action-type permission rules (not just keyword matching)
- A visible audit trail — what was classified at what level and why, viewable in the Dashboard

**Test cases:**
| Category | Test |
|---|---|
| Permission denied | Every risk tier is tested with at least one real action, confirming the correct confirmation behavior at each tier |
| Wrong input | An action that's ambiguous between two risk tiers defaults to the higher one |
| Recovery | A denied/cancelled action leaves the system in a clean state, not a half-completed one |
| Emergency stop | Works at every risk tier, including mid-confirmation-prompt |

**Done when:** all four risk tiers have been triggered and correctly handled at least once each, the audit trail correctly logs every classification decision, and a full week of real use (per the Build Plan's Tier 2 closing criteria) produces zero unsafe actions without confirmation.

---

## 12. Phase 9 — Packaging

**Build:**
- Windows installer (PyInstaller + Inno Setup, per the Build Plan)
- Tray application (via `pystray`)
- Startup option (launch with Windows, per Step 1's permissions note)
- Production config — switching from `.env` to Windows Credential Manager for secrets (Feasibility doc Section 6, Step 3 Section 3's dev/production split)
- Log rotation confirmed working in the packaged build, not just dev
- Backup/export (already built in Phase 4 — confirm it still works from the packaged app)
- Uninstall flow with an explicit choice to keep or remove the memory file

**Test cases:**
| Category | Test |
|---|---|
| Happy path | Fresh install → tray icon appears → assistant responds to a basic command |
| Recovery | Upgrading an existing install preserves memory and settings |
| Permission denied | Install/uninstall doesn't require admin rights unless a specific step genuinely needs it |

**Done when:** installing on a clean Windows 11 machine (or VM) results in a working tray-based assistant with no manual setup steps beyond entering the API key once, and uninstalling correctly offers and honors the memory keep/remove choice.

---

## 13. Phase 10 — Communication Integrations

**Only after the policy/technical check from Step 1's Feasibility doc (Section 5) — read each target app's automation policy first:**
- Slack (prefer the official API)
- WhatsApp (simulated interaction, with the ToS risk explicitly acknowledged, not built around)
- Email
- Contacts sync

**Test cases:**
| Category | Test |
|---|---|
| Happy path | A message sends successfully via the chosen integration |
| App unavailable | The target app/service being logged out or unreachable is handled gracefully |
| Network unavailable | Same as above, network-specific case |
| Permission denied | Sending a message is always at least Medium risk (Feasibility doc Section 9) and requires confirmation |
| Recovery | A failed send is reported clearly, not silently retried indefinitely |

**Done when:** at least one integration (ideally Slack, given its official API) works reliably for 10 real sends in a row, and the WhatsApp-specific ToS risk has been explicitly re-confirmed as accepted before that integration is used for anything beyond testing.

---

## 14. Testing Strategy

Every phase above already has its own table — this section is the shared template they're drawn from, so new tests you add later stay consistent:

```
Happy path
Wrong input
Ambiguous input
Missing information
App unavailable
Network unavailable
UI changed
AI failure
Permission denied
Recovery
Emergency stop
```

Not every category applies to every phase — each phase's table above only includes the categories that are actually meaningful for it. Forcing an irrelevant category into a phase's tests (e.g. "UI changed" for a pure memory-lookup feature) produces a test that doesn't test anything real.

---

## 15. Definition of Done

The meta-rule behind every phase's "Done when" line above: **a phase is done when its specific test cases pass, not when it "seems to work."** Write the phase's Done-when checklist as an explicit, repeatable test before starting the phase's first feature — this is what turns "I think this works" into "I confirmed this works," and it's what makes the closing tiers in the Build Plan (Section 9) honest rather than aspirational.

---

## 16. Git / Versioning

- **Branch strategy:** work directly on `main` for solo personal-project pace; only branch if a feature genuinely risks breaking a working state you want to keep (e.g. a risky Phase 5 experiment) — merge back and delete the branch once it's stable.
- **Commit naming:** `<phase>: <what changed>` — e.g. `phase1: add mouse click and verifier check`, `phase4: add export/import for memory`.
- **Rollback:** since every working feature gets its own commit (Development Rule 4), rolling back a broken change is `git revert <commit>` or checking out the last known-good commit — this is the entire reason the Build Plan insisted on a real Git habit from commit #1.
- **Tags:** tag each closing milestone from the Build Plan's Section 9 — `v0.1` at end of Phase 0, continuing through the phases, `v1.0` at Tier 1 (Personal MVP), `v2.0` at Tier 2, `v3.0` at Tier 3 if you go that far.

---

## 17. Claude Code Workflow

For every task in this document, the loop is:

```
Read docs (this file + CLAUDE.md)
   ↓
Inspect current code (what already exists before adding to it)
   ↓
Explain the plan (in plain terms, before writing code)
   ↓
Implement
   ↓
Run tests
   ↓
Report changes (what changed, what was tested, what passed/failed)
   ↓
Wait for your approval/feedback before starting the next task
```

This loop applies to every single bullet point in every phase above — not once per phase. Skipping straight from "explain the plan" to the next task without your feedback is exactly the runaway-scope pattern the Build Plan's vibe-coding method (Section 5) exists to prevent.

---

## 18. Current Phase Tracker

```
Phase 0 — Foundation: COMPLETE (Done-when checklist passed; tagged v0.1)
Current phase: 1 — Basic Computer Control (not started)
Last updated: 16 September 2026
```

Update this block — and the matching "Current phase" line in `CLAUDE.md` (Step 3, Section 4) — every time a phase's Done-when checklist is actually confirmed passing, not when it merely feels finished. This is the single source of truth both you and every future Claude Code session should check before starting new work.
