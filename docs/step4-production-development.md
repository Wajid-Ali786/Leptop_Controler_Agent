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

**Implementation notes — open/close apps (recorded 16 September 2026).** These record how Phase 1 behaves on a real Windows desktop and what was measured. They don't reopen any Step 1–3 decision.

- **close_app closes only windows this session opened.** `open_app` remembers the windows it verified (in memory only, so nothing carries over a restart). `close_app` closes the most recently opened of those that is still open. A window the user opened themselves is never closed; the assistant says so and leaves it alone. Closing is always a polite request (`WM_CLOSE`, like clicking the window's X), never ending a process. Outcomes are distinct: `done`, `already_closed`, `needs_user` (e.g. a "Save changes?" dialog), `still_open`, `failed`. `needs_user` and `still_open` are never retryable, so the recovery loop can't retry into an app that is waiting for the user. Closing is Medium risk (`close`, like Roman Urdu `band`), so it is always confirmed.
- **Store-app window groups (e.g. Calculator).** A Windows Store app has two windows with the same title: an outer frame (`ApplicationFrameWindow`, owned by `ApplicationFrameHost.exe`) and a content window (`Windows.UI.Core.CoreWindow`, owned by the app's own process). The observed sequence, the same in every run, was:
  1. The frame is created *cloaked* (it exists but isn't drawn on screen).
  2. 0.2–0.3 s later, the content window appears as a separate top-level window.
  3. 0.5–0.65 s after the frame was created, the frame is shown.
  4. 0.4–0.5 s after that, the content window moves inside the frame.
  5. On close, the frame is cloaked and hidden, the content window becomes a separate top-level window again, and it is destroyed up to about 0.2 s after the frame is hidden.

  So `open_app` only counts a window as appeared once it is **not cloaked**, and records every new matching window present at that moment as one group (frame + content window). `close_app` asks the frame to close, then reports `done` only when every window in the group, **and every matching window hosted inside it**, is gone. Nothing is special-cased by app name, so any Store app added to `executor.apps` gets the same handling. `scripts/trace_app_windows.py` is a development diagnostic that prints an app's windows over time; use it when a new app behaves unexpectedly.
- **Measured latency cost.** Waiting for the window to be on screen adds **+0.3–0.6 s to `open_app` for Store apps**: Calculator went from 0.5–0.9 s to 1.1–1.4 s, now reported when the window is actually visible. **Notepad is unaffected** (0.2–0.3 s): an ordinary app's window is never cloaked, so it is verified on the first check after launch.
- **Known open decision — Windows' invisible pre-launched Calculator.** Windows may start Calculator hidden at sign-in (observed: a cloaked frame plus `CalculatorApp.exe`, started about 90 s after boot). The Verifier counts cloaked windows as open, so with nothing on screen, "close calculator" currently answers that 2 calculator windows are open but weren't opened by the assistant, and leaves them alone. That is expected current behavior, not a bug. It is deliberately left unchanged because a window on another virtual desktop is also cloaked but is a real, open window; ignoring cloaked windows would misreport those. `open_app` never reused the pre-launched copy in testing; if Windows ever did reuse it, `open_app` would time out and offer a retry. Open for the project owner to decide.

**Implementation notes — mouse click by screen coordinate (recorded 17 September 2026).** This is the Phase 1 last-resort fallback only; there is no screen understanding, OCR or element detection yet.

- **Action and pipeline.** `ExecutorAction(CLICK, "x, y")` (whole numbers; `(x, y)` also accepted) sends one left click through the usual pipeline: emergency stop → validation → safety gate → emergency stop → action → check.
- **Validation (before anything is asked or sent).** Malformed coordinates are refused with an example of the right form. The point must lie on an actual monitor, read from Windows; gaps between monitors of different sizes don't count, and negative coordinates on a left or top monitor are fine. So `(99999, 99999)` fails with a message listing the screens. The four corner pixels of the main screen are refused too, because they are pyautogui's fail-safe corners. Coordinates are real screen pixels on every monitor (the process is made per-monitor DPI aware before reading or clicking).
- **Safety.** Every click is **always Medium risk**, whatever the words say (a code constant, not a setting), so it is always confirmed. The confirmation names the exact coordinates and the title of the window at that point, e.g. `click at (724, 284) on window "Untitled - Notepad"`. Window titles appear only in the prompt, never in the logs. Right after confirmation, the window at the point is read again; if a different window (or the same window with a different title) is there now, nothing is clicked.
- **Emergency stop.** It is checked before validation, after confirmation, and again immediately before the click. pyautogui's fail-safe stays on: if the pointer is in a main-screen corner, pyautogui refuses to act, and that triggers the emergency stop with source `mouse-corner` (it stays stopped until reset). The click itself is a single instant input event, so there is nothing to interrupt partway.
- **Outcome: honestly unverified.** A click's effect can't be observed in Phase 1, so a sent click returns outcome **`unverified`** with `ok=True` and `verified=False`: "Clicked at (x, y). I can't check what the click did." It is never reported as `done`. The only check afterwards is that the mouse pointer really is at (x, y); if not, the result is `failed` ("the click may have landed somewhere else"). Nothing about a click is ever retryable, so the recovery loop never offers a retry.
- **Tested.** 58 offline tests in `tests/test_executor_click.py` (parsing, off-screen and multi-monitor gaps, fail-safe corners, confirmation always required, prompt wording, window changed after approval, emergency stop before/during/right before the click, fail-safe → emergency stop, pointer elsewhere, never retried), plus safety tests for the minimum risk level and read-only Verifier tests. Removing the always-Medium rule or the window re-check makes tests fail. Opt-in real-desktop test (17 September 2026, one 1366×768 screen): `(99999, 99999)` was refused without moving the pointer. A Notepad window the test opened was clicked at the centre of its empty text area `(724, 284)`: the prompt read `click at (724, 284) on window "Untitled - Notepad"`, the result was `unverified`, the pointer was at `(724, 284)` and the window title was unchanged. The test then closed that Notepad with `close_app` (`done`), and cleanup had to close 0 windows. The first click in a session took 0.86 s in total, mostly loading pyautogui (0.38 s import, measured separately).
- **Limitations.** Windows silently drops input sent to a window running as administrator, with no error to detect; the `unverified` outcome covers this, but the message can't say it happened. Multi-monitor and display-scaling handling is covered by offline tests only; the real test ran on a single monitor at this machine's scaling. A click moves the user's real mouse pointer and leaves it at the clicked point.

**Implementation notes — type text (recorded 17 September 2026).** Typing into the active window's focused field; keyboard shortcuts, Tab and other control keys are not part of this.

- **Action and pipeline.** `ExecutorAction(TYPE_TEXT, text)` types the exact text into the **active window's focused field**, through the usual pipeline. The steps are: emergency stop → validation → safety gate → emergency stop → re-check the active window/field → typing (with a stop check and a focus check before every character) → read-back check.
- **Validation (before anything is asked or typed).** Text must be non-empty and at most `executor.max_type_characters` (1000). Line breaks (`\n`, and `\r\n` counted as one) are allowed. Tab, other control characters and broken Unicode are refused. Any language and emoji are fine. There must be an active window.
- **Safety.** Typing is **always at least Medium risk**, and **any line break makes it HIGH**, because each one is a real Enter key press, which can submit a form, send a message or run a command. Both are code constants, not settings. The confirmation shows:
  - the character count, the window title and the field type
  - `AND PRESS ENTER n TIME(S) - Enter can submit a form, send a message or run a command`, plus `(ends with Enter: it will submit as soon as typing finishes)` when it does
  - a preview of **at most the first 40 characters**, with `⏎` for each Enter and `…` when longer

  Example: `type 49 characters into window "Untitled - Notepad" (field: Edit) AND PRESS ENTER 1 TIME - Enter can submit a form, send a message or run a command: "Hello from the AI Desktop Companion test…"`. The logged safety rule is the typing/Enter rule, never words from the text. If the active window, its title or the focused field changed after approval, nothing is typed.
- **Adapter.** Windows `SendInput` with Unicode characters (ctypes), one character per call. Each call carries the key-down and key-up together (both halves of an emoji go in one call), so a stop never leaves a key held down. A line break is a real Enter key press. The pause between characters is `executor.typing_interval_seconds` (0.01 s) and is interruptible.
- **Outcomes.**
  - `done` (verified): only when the focused field's text, read before and after (`WM_GETTEXT`, line endings normalised, waiting up to `verifier.text_settle_seconds` = 1 s), contains the exact typed text **one more time than before**.
  - `unverified` (`ok=True`, `verified=False`): the field can't be read (browsers, terminals, password fields, more than `verifier.max_read_characters` = 100000), or the text wasn't found afterwards (apps auto-correct or replace selections).
  - `failed`: nothing was typed, e.g. validation failed, the window changed after approval, or input was refused before the first character.
  - **`partial`** (`ok=False`, `verified=False`, `progress=(sent, total)`): typing stopped part-way because the active window/field changed or Windows refused input. The message says how many characters were typed and that they may already be in the window.
  - An **emergency stop after something was typed** raises `TypingInterruptedError` (a kind of `EmergencyStopError`), carrying that `partial` result. A stop before the first character is the plain `EmergencyStopError`.
  - **Nothing about typing is ever retryable**, including `failed`, `partial` and `unverified`: a retry could type the text twice.
- **Text never persisted or logged.** The text appears only transiently in memory (to type and check it) and, at most its first 40 characters, in the on-screen prompt, which is never logged. Logs, result messages, `repr()` of actions, results and safety objects, error messages and safety rules describe it only by its length. The field's text read back for checking is compared in memory and discarded. There is no password or secret detection in Phase 1.
- **Tested.** 69 offline tests in `tests/test_executor_type_text.py` cover:
  - validation, prompt wording and truncation, HIGH risk for Enter
  - the window, field or title changing after approval, and a title changing *during* typing (Notepad's "*") not stopping it
  - `partial` from a focus change and from refused input, with correct progress
  - emergency stop before, during confirmation, before the first character, part-way, during the pause between characters, and during the read-back check
  - `done` versus `unverified` read-back cases, and never retried
  - the result rules, the adapter's `SendInput` events, and a rule that only the Executor adapter sends input
  - a unique secret-like string not appearing in logs, `repr()`, results, errors or safety diagnostics in 8 scenarios

  Removing the per-character focus check, the HIGH risk for Enter, or the length-only logging makes tests fail. Opt-in real-desktop test (17 September 2026): the test opened Notepad (`Edit` field active) and typed `Hello from the AI Desktop Companion test⏎line two`. The prompt read exactly as in the example above (HIGH). Result: `done`, verified, `progress=(49, 49)`, 1.06 s including confirmation and the check. The test then emptied that Notepad as test code and closed it with `close_app` (`done`; no save dialog appeared); cleanup had to close 0 windows.
- **Limitations.**
  - If the user types at the same time, the characters interleave; only a change of active window or field is detected.
  - Windows may silently drop input to an app running as administrator; the read-back then gives `unverified`, never `done`.
  - A future command window (Phase 2/9) will itself be the active window, so it must hand focus back before typing; until then the "window changed after approval" check stops typing safely.
  - Some apps (e.g. browsers) have no readable fields for this check, so typing into them is always `unverified`.
- **Open decision for Phase 3.** When the Brain generates the text from a spoken command, rather than the user typing it, the 40-character preview limit and whether to mask suspected secrets in the prompt need revisiting.

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
Current phase: 1 — Basic Computer Control (in progress)
Last updated: 16 September 2026
```

Update this block — and the matching "Current phase" line in `CLAUDE.md` (Step 3, Section 4) — every time a phase's Done-when checklist is actually confirmed passing, not when it merely feels finished. This is the single source of truth both you and every future Claude Code session should check before starting new work.
