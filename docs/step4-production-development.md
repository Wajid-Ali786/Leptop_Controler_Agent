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
- Typed commands: `app/executor/commands.py` (grammar) + `app/console.py` (`python main.py --console`)
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

**PHASE 1 — COMPLETE (verified 20 September 2026).** Every Build bullet, every test-case row and every Done-when clause above is satisfied, by the two checklist runs recorded below. The deferred items at the end of this section were never Phase 1 requirements and stay deferred; closing Phase 1 does not complete them.

- **How Phase 1 is verified — `scripts/phase1_checklist.py`.** It maps each Step 4 Phase 1 requirement to the pytest tests that are its evidence, runs pytest once per selected group, and reads results from JUnit XML. It parses no prose, counts no totals and contains no Executor, action or Verifier logic of its own. Statuses mean exactly:
  - **PASS** — the mapped tests executed **in that invocation** and passed.
  - **FAIL** — they executed and failed.
  - **NOT RUN** — that group wasn't selected in this mode. It is *not* a historical failure, and it is never promoted to PASS from an earlier run.
  - **DEFERRED** — a Step 4 line explicitly defers it (non-blocking).
  - **DOCUMENTED** — a human observed it and it is recorded here with a date and a way to reproduce. Visually distinct from PASS, because nothing executed it.

  A row's status comes **only from its own mapped tests**. A failing test that is no requirement's evidence is listed separately under `RELATED / UNMAPPED TEST FAILURES` and still forces a non-zero exit, without mislabelling any requirement. A mapped test that has been renamed or is no longer collected is a `MAPPING ERROR`, never a PASS. Exit codes: **0** every blocking row satisfied, **1** something executed failed (including a regression row, an unmapped failure or a broken mapping), **2** nothing failed but blocking evidence was NOT RUN.
- **Phase 1 is verified by TWO explicit runs, and that is by design.**

      python scripts/phase1_checklist.py --real-desktop     # the elevated Notepad MINIMIZED
      python scripts/phase1_checklist.py --real-elevated    # the elevated Notepad IN FRONT

  The two real groups need opposite foreground conditions: the ordinary real-desktop tests need their own Medium-integrity windows to take the foreground, while the permission-denied test needs a High-integrity Notepad in front — and a High-integrity foreground window prevents a Medium-integrity process from taking the foreground back. Measured here: with the elevated Notepad minimized, every real-desktop row passed; in a run where it ended up restored on screen, the one test that could not acquire the foreground failed. The checklist deliberately carries no evidence between invocations, so **neither run prints `FULL PHASE 1: PASS` and both exit 2 — that is not a failure.** The Phase 1 evidence set is the two runs together. A `--real-desktop --real-elevated` invocation is *not* a valid substitute: it was tried and was procedurally invalid.
- **Final evidence (20 September 2026).**

  | Group | Result |
  |---|---|
  | Offline suite | **1096 passed, 18 skipped**; 19/19 checklist rows PASS in both runs |
  | Real desktop (`--real-desktop`) | **4/4 PASS**, no unmapped failures, 21/22 blocking satisfied in that invocation, exit 2 |
  | Real elevated (`--real-elevated`) | **1/1 PASS**, no unmapped failures, exit 2 |
  | Physical global-hotkey reachability | **DOCUMENTED** (18 September 2026 — see the emergency-stop notes) |
  | Failures | **none** |

- **The real acceptance evidence in brief.**
  - **Emergency stop:** the global hotkey `Ctrl+Alt+Backspace` (RegisterHotKey → WM_HOTKEY, no keyboard hook), physically verified with PowerShell, Chrome and an elevated Notepad in front, and its 65-trial threshold acceptance passes against the approved limits.
  - **Deliberately-broken action:** a test-owned window that receives `SC_MAXIMIZE` and deliberately swallows it is reported `failed` — never a false `done` — and the same window with the break removed reports `done`, proving the fixture was genuinely targetable.
  - **Permission denied:** a Medium-integrity (RID 0x2000, not elevated) test process acting on a High-integrity (RID 0x3000, elevated) Notepad — both read directly from their own tokens — got `PostMessageW` **FALSE with ERROR_ACCESS_DENIED (5)**, which the adapter turned into "it runs with administrator rights, and the assistant doesn't run elevated": outcome `failed`, `ok=False`, `retryable=False`, no retry offered, and the target stayed **not maximized**.
- **Deferred — recorded as deferred, not as failures, and NOT completed by Phase 1 closing.**
  - Real browser refresh test — it would open the user's real browser profile.
  - Click-effect verification — Phase 5 (screen understanding); a click stays honestly `unverified` in Phase 1.
  - Refresh-effect verification — Phase 5; a refresh is never `done` in Phase 1.
  - The optional real clipboard test — it replaces the machine's clipboard, so it stays behind `RUN_REAL_CLIPBOARD_TEST=1` and no Step 4 Phase 1 line requires it.
- **Known limitations carried out of Phase 1** (each already recorded in the notes below; collected here so they aren't lost):
  - The console's focus hand-over is clunky for a Medium command — switch, answer, switch back.
  - Restoring a minimized window needs targeting beyond the active window (later phase).
  - Windows' secure desktop (the UAC prompt itself, Ctrl+Alt+Delete) is out of scope for the hotkey: no ordinary program receives input there.
  - The global hotkey reserves `Ctrl+Alt+Backspace` system-wide while the assistant runs, so no other program can use that combination then.
  - Real-desktop tests need an idle mouse and keyboard; using the desk during a run makes them fail for environmental reasons, and the checklist labels such failures as interference rather than as broken requirements.
  - `close_app`'s `done` means the target is no longer a **visible titled top-level window** in the Verifier's enumeration — not that its `HWND` has already been destroyed.
  - A **minimized** Notepad's `HWND` destruction trails that `done` by a few milliseconds (measured 2.3–6.5 ms, median 3.3 ms); a normal window's was already destroyed at return in every measured close.
  - Theoretical limit of that visibility-based oracle: an app that hid its window without destroying it would be reported closed while still alive. Not observed; recorded so the oracle isn't mistaken for proof of process exit.
  - Real-desktop and real-elevated verification are intentionally separate, for the integrity/foreground reason above.

**Implementation notes — open/close apps (recorded 16 September 2026).** These record how Phase 1 behaves on a real Windows desktop and what was measured. They don't reopen any Step 1–3 decision.

- **close_app closes only windows this session opened.** `open_app` remembers the windows it verified (in memory only, so nothing carries over a restart). `close_app` closes the most recently opened of those that is still open. A window the user opened themselves is never closed; the assistant says so and leaves it alone. Closing is always a polite request (`WM_CLOSE`, like clicking the window's X), never ending a process. Outcomes are distinct: `done`, `already_closed`, `needs_user` (e.g. a "Save changes?" dialog), `still_open`, `failed`. `needs_user` and `still_open` are never retryable, so the recovery loop can't retry into an app that is waiting for the user. Closing is always at least MEDIUM risk, so it is always confirmed. Since the Window Controls task (17 September 2026) this is a code constant, not the configurable `close` keyword, so configuration can't lower it; other safety rules may still raise it. close_app and window control close share one close mechanism (see the Window Controls notes).
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

  It shows **none of the text itself**, for maximum privacy in Phase 1 (changed on 17 September 2026 from an earlier 40-character preview). The prompt is built without access to the text, so two texts of the same length and Enter count produce the identical prompt. Example: `type 49 characters into window "Untitled - Notepad" (field: Edit) AND PRESS ENTER 1 TIME - Enter can submit a form, send a message or run a command`. The logged safety rule is the typing/Enter rule, never words from the text. If the active window, its title or the focused field changed after approval, nothing is typed.
- **Adapter.** Windows `SendInput` with Unicode characters (ctypes), one character per call. Each call carries the key-down and key-up together (both halves of an emoji go in one call), so a stop never leaves a key held down. A line break is a real Enter key press. The pause between characters is `executor.typing_interval_seconds` (0.01 s) and is interruptible.
- **Outcomes.**
  - `done` (verified): only when the focused field's text, read before and after (`WM_GETTEXT`, line endings normalised, waiting up to `verifier.text_settle_seconds` = 1 s), contains the exact typed text **one more time than before**.
  - `unverified` (`ok=True`, `verified=False`): the field can't be read (browsers, terminals, password fields, more than `verifier.max_read_characters` = 100000), or the text wasn't found afterwards (apps auto-correct or replace selections).
  - `failed`: nothing was typed, e.g. validation failed, the window changed after approval, or input was refused before the first character.
  - **`partial`** (`ok=False`, `verified=False`, `progress=(sent, total)`): typing stopped part-way because the active window/field changed or Windows refused input. The message says how many characters were typed and that they may already be in the window.
  - An **emergency stop after something was typed** raises `TypingInterruptedError` (a kind of `EmergencyStopError`), carrying that `partial` result. A stop before the first character is the plain `EmergencyStopError`.
  - **Nothing about typing is ever retryable**, including `failed`, `partial` and `unverified`: a retry could type the text twice.
- **Text never shown, persisted or logged.** The text exists only transiently in memory, to validate it, type it (`SendInput`) and check it was typed. The confirmation prompt, logs, result messages, `repr()` of actions, results and safety objects, error messages and safety rules describe it only by its length. The field's text read back for checking is compared in memory and discarded. There is no password or secret detection in Phase 1.
- **Tested.** 72 offline tests in `tests/test_executor_type_text.py` cover:
  - validation, prompt wording (identical prompts for different texts of the same length and Enter count), HIGH risk for Enter
  - the window, field or title changing after approval, and a title changing *during* typing (Notepad's "*") not stopping it
  - `partial` from a focus change and from refused input, with correct progress
  - emergency stop before, during confirmation, before the first character, part-way, during the pause between characters, and during the read-back check
  - `done` versus `unverified` read-back cases, and never retried
  - the result rules, the adapter's `SendInput` events, and a rule that only the Executor adapter sends input
  - a unique secret-like string not appearing in the confirmation prompt, logs, `repr()`, results, errors or safety actions, assessments and rules in 8 scenarios (the test also checks the secret really was typed and read back), plus 3 planted leaks (in the prompt, in logs and `repr()`, in a result message) that the check must catch, so it can't pass trivially

  Removing the per-character focus check, the HIGH risk for Enter, or the length-only logging makes tests fail. Opt-in real-desktop test (17 September 2026, re-run after removing the preview): the test opened Notepad (`Edit` field active) and typed "Hello from the AI Desktop Companion test", a line break, then "line two". The prompt read exactly as in the example above (HIGH), with none of the text. Result: `done`, verified, `progress=(49, 49)`, 1.05 s including confirmation and the check. The test then emptied that Notepad as test code and closed it with `close_app` (`done`; no save dialog appeared); cleanup had to close 0 windows.
- **Limitations.**
  - If the user types at the same time, the characters interleave; only a change of active window or field is detected.
  - Windows may silently drop input to an app running as administrator; the read-back then gives `unverified`, never `done`.
  - A future command window (Phase 2/9) will itself be the active window, so it must hand focus back before typing; until then the "window changed after approval" check stops typing safely.
  - Some apps (e.g. browsers) have no readable fields for this check, so typing into them is always `unverified`.
- **Open decision for Phase 3.** With no preview at all, approving text the Brain generates from a spoken command, rather than text the user typed, means approving it blind. Revisit this before the Brain generates text to type, together with whether to mask suspected secrets.

**Implementation notes — keyboard shortcuts (recorded 17 September 2026).**

- **Action.** `ExecutorAction(SHORTCUT, "ctrl+c")` presses one shortcut from a **fixed allow-list** in `app/executor/shortcuts.py`. Key names are joined with `+`; case, spaces and modifier order don't matter; aliases `control`, `windows`, `escape`, `del` are accepted. Each shortcut has one normalized name (e.g. `Ctrl+C`), used in prompts and logs. The risk levels are code, not settings.
- **Risk levels** (the gate asks for confirmation at MEDIUM and above):

  | Shortcut | Level | Why |
  |---|---|---|
  | Ctrl+A | LOW | only changes the selection; changing the selected content is itself confirmed |
  | Win+D | LOW | shows the desktop; changes no data and pressing it again brings the windows back |
  | Ctrl+C | MEDIUM | replaces the clipboard, and in a terminal or console stops the running program; Phase 1 can't reliably recognise every terminal, so it is always confirmed (no terminal detection) |
  | Ctrl+S | MEDIUM | saving can overwrite an existing file, and the document may hold changes by the user or another program, not only the assistant |
  | Ctrl+Z | MEDIUM | undoes the most recent change, which may be the user's own; some apps can't redo |
  | Ctrl+X | MEDIUM | removes the selection from the document and replaces the clipboard |
  | Alt+Tab | MEDIUM | changes no data, but the window it switches to can't be known in advance, and following actions land there |
  | Ctrl+V | HIGH | pastes clipboard content the assistant can't see; pasted lines can run in a terminal, pasted files are copied in File Explorer |
  | ~~Alt+F4~~ | — | *removed on 17 September 2026 (Window Controls task)*: it was a separate way to close a window. It is now refused, pointing to close_app / window control close, which share the one close mechanism |

  **Not supported:** **F5, Ctrl+R, Ctrl+F5, Shift+F5 and Ctrl+Shift+R** (refused with a message pointing to the Refresh action, which is the only way to send a refresh key — see the Refresh notes); **Alt+F4** (refused with a message pointing to close_app / window control close); every other combination ("isn't a supported shortcut yet"); **Ctrl+Alt+Delete** and **Win+L**, which are reserved Windows shortcuts this assistant will not send or test. Win+L is intentionally unsupported in Phase 1 because it locks the Windows session.
- **Prompts** (MEDIUM and HIGH; on screen only, never logged) name the shortcut, the window title and field, and what it does. Examples: `press Ctrl+Z in window "Untitled - Notepad" (field: Edit) - undoes the last change; some apps can't redo it`, `press Ctrl+V in window "…" (field: Edit) - pastes the clipboard (it holds: text). I can't see what's on it; pasting into a terminal or chat can run or send it`. LOW shortcuts run without a prompt and the gate sees only `press Ctrl+A`, never a title.
- **Validation (before anything is asked or sent).** The shortcut must be supported. Shortcuts that act on a window need an active window. No Ctrl, Alt, Shift or Win may be held down on the keyboard (checked again right before sending). Ctrl+V needs something on the clipboard. After confirmation, if the active window, its title or its field changed, nothing is pressed.
- **Sending.** The whole shortcut is **one `SendInput` call** (virtual keys plus scan codes): modifiers down, key down, key up, modifiers up in reverse. It is sent immediately after the last emergency-stop check, so a stop can never leave keys down. If Windows accepts **0 events** the result is `failed`. If it accepts **some but not all**, every key and modifier involved is released at once (an unassigned key is tapped first when Alt or Win is involved, so a lone release opens no menu) and the result is `unverified`. If it accepts all, the shortcut's own check runs. In every case the modifiers must then read as released; if not they are released again, and if still down the message says to press and release them.
- **Outcomes: `done` only on evidence.**
  - Ctrl+C: the clipboard's change counter moved.
  - Ctrl+X: the counter moved and the field's text got shorter.
  - Ctrl+A: a standard edit field reports all its text selected.
  - Alt+Tab: a different window is active; if visibly not, `failed`.
  - Win+D: the desktop is active.
  - Ctrl+Z, Ctrl+S, Ctrl+V: always `unverified`, and anything that can't be confirmed is `unverified`.
  - **Never retryable.** An emergency stop while checking the effect raises `EmergencyStopError`; the keys were already sent and released.
- **Clipboard.** Its **contents are never read** — nothing opens the clipboard or fetches its data (a rule test enforces it). Only its change counter and which kinds of data it holds (text, image, files) are read. The kinds appear only in the Ctrl+V prompt; nothing clipboard-related is logged.
- **Tested.** 98 offline tests in `tests/test_executor_shortcut.py` cover:
  - parsing, aliases, refusals and reserved shortcuts; the full risk table; Ctrl+C MEDIUM in every window
  - LOW running without a prompt but through the gate; exact MEDIUM/HIGH prompts; decline and no confirmation method
  - no active window, held modifiers (before and after approval), empty clipboard, target changed after approval
  - (originally) Alt+F4 session-only, desktop refusal, `done` / `needs_user` / `still_open` — replaced by an Alt+F4 refusal test when Alt+F4 was removed
  - 0 and partly accepted input with the defensive release; modifiers still down released again, or an honest note
  - emergency stop before, during confirmation, right before sending, and while checking
  - every verification case; never retried
  - titles and clipboard kinds never reaching logs, results or errors, plus a planted leak the check must catch
  - the adapter's single batch, extended Win key, scan codes and release sequence
  - the clipboard-contents rule

  Breaking Ctrl+V's HIGH level, the partial-input release, the held-modifier check, the Alt+F4 session restriction (now: the Alt+F4 refusal), or keeping titles out of results makes tests fail.

  Opt-in real-desktop test (17 September 2026): in a Notepad the test opened, it typed "abc". **Ctrl+A** ran with no prompt → `done` (everything selected), 0.20 s. **Ctrl+Z** prompted at MEDIUM → `unverified`. **Alt+F4** prompted at HIGH on that session window → `done` (since the Window Controls task this test closes its Notepad with window control close instead: MEDIUM → `done`, re-run 17 September 2026). Cleanup had to close 0 windows, no modifier read as held afterwards, and the clipboard's change counter was unchanged. The clipboard test (Ctrl+C, then Ctrl+V into its own Notepad) exists behind a second opt-in, `RUN_REAL_CLIPBOARD_TEST=1`, because it replaces the clipboard's contents; it has **not** been run.
- **Limitations.**
  - A held modifier can't be told apart from one stuck after sending, so the "may still be held down" note can also appear while the user is holding a key.
  - Ctrl+A is verifiable only in standard edit fields.
  - Alt+Tab and Win+D are tested offline only, because on a real desktop they would move or hide the user's windows.
  - Windows may silently drop input to an app running as administrator; the check then gives `unverified` or `failed`, never a false `done`.
- **Open decision for Phase 3.** Confirming every Ctrl+C will likely feel heavy in daily use. Revisit lowering Ctrl+C to LOW in contexts known to be safe, once the assistant can reliably tell terminals and consoles apart.

**Implementation notes — scroll (recorded 17 September 2026).**

- **Action and input.** `ExecutorAction(SCROLL, "down 3")`. The format is `up N` or `down N`: an explicit direction word (signs like `+3` or `-3` are refused, because Windows' wheel sign is the opposite of what many people expect) and N **wheel notches**, from 1 to `executor.max_scroll_notches` (20). A missing count is refused, not guessed. **Vertical only:** `left`/`right` are refused ("Horizontal scrolling isn't supported yet").
- **What a notch is (and isn't).** One notch is one wheel click (a wheel delta of 120). **It does not guarantee a number of lines or pixels:** Windows defaults to 3 lines per notch, users can change that (even to a whole screen), and every app decides for itself (browsers scroll by pixels, smoothly; some apps zoom or switch items). "down 3" means three wheel clicks. **Recorded for Phase 3:** when the Brain says "scroll down a bit" or "a page", it must map that to notches, or to a different mechanism such as Page Down.
- **Target.** The wheel goes to the window under the pointer or to the focused window, depending on a Windows setting. So scrolling is only allowed when the **window under the mouse pointer is the active window**, so both settings deliver it to the same window. The assistant **never moves the pointer** for a scroll; if the pointer is elsewhere, it asks the user to move it. The scroll lands in the pane under the pointer.
- **Risk.**
  - **LOW, no prompt, only where a standard scroll area is positively identified:** a readable standard vertical scroll bar on the control under the pointer or one of its parents. Scrolling there changes no data and is undone by scrolling back.
  - **MEDIUM over standard controls whose value the wheel changes:** drop-down lists (`ComboBox`, `ComboBoxEx32`), sliders (`msctls_trackbar32`), spin boxes (`msctls_updown32`) and date pickers (`SysDateTimePick32`). These are found on the control under the pointer **or any of its parents** (at most 16 steps), so e.g. the Edit inside a drop-down list still counts. Prompt (on screen only): `scroll down 2 notches over a drop-down list in window "…" - scrolling over it changes its value`.
  - **MEDIUM, and capped, for surfaces that can't be classified** (changed on 17 September 2026 from LOW with the cap alone). This covers browser and custom web content, WPF, Store/UWP-style content, Electron and custom-drawn surfaces, and any target where no standard vertical scroll bar can be found on the control under the pointer or its parents. The user is always asked, **and** at most `executor.max_unclassified_notches` (3) notches are sent per command; the cap is an extra bound, not a substitute for confirmation. Prompt (on screen only), e.g. for "down 10": `scroll down 3 notches in window "…" - I couldn't confidently identify this window's scroll area, so for safety I'll send at most 3 notches (you asked for 10)`. The result says so again, and is normally `unverified`, since there's no readable scroll position. A value-changing control is still named when one is found. **Revisit classification in Phase 5**, when screen understanding can recognise these surfaces.
  - Held Ctrl/Alt/Shift/Win is refused (Ctrl+wheel zooms), before scrolling and before every notch.
- **Sending and emergency stop.** One notch per `SendInput` call, at the pointer's position, with `executor.scroll_interval_seconds` (0.05 s, interruptible) between notches. **Before every notch** the active window, the control under the pointer and held modifiers are checked, then the emergency stop immediately before sending. A stop before the first notch raises `EmergencyStopError`; a stop after at least one raises `ActionInterruptedError` carrying a `partial` result. (`ActionInterruptedError` was added as the general form of this error; `TypingInterruptedError` now inherits from it, with no change in typing behavior.)
- **Outcomes.**
  - `done` only when a standard scroll bar's position (read before and, for up to `verifier.scroll_settle_seconds` = 1 s, after) **moved in the requested direction**.
  - `unverified`: no readable standard scroll bar; already at the top/bottom ("…already at the top, so nothing moved"); didn't move, or moved the other way; unreadable afterwards.
  - `failed`: nothing scrolled (refused input or a change before the first notch).
  - `partial` with `progress=(sent, total)`: the pointer moved off what it was over, the active window changed, a modifier was pressed, or Windows refused input part-way.
  - **Never retryable.** Logs contain the direction, count, level, rule and outcome, never window titles or control class names.
- **Tested.** 66 offline tests in `tests/test_executor_scroll.py` cover:
  - input refusals; LOW with no prompt only where a standard scroll bar is identified (the gate sees no title)
  - MEDIUM for each value-changing control, including an Edit inside a drop-down list and one nested in a group, and decline
  - unclassified surfaces: MEDIUM with the exact prompt; the safety gate receives MEDIUM with the unclassified rule; declining or having no confirmation method sends 0 notches; approving "down 10" sends at most 3; the result's cap message; within the cap it still asks
  - no active window, pointer not over the active window, held modifiers, unreadable desktop
  - `partial` for each per-notch change, and `failed` before the first notch
  - emergency stop before, before the first notch, part-way, during the pause and during the check
  - every verification case; never retried
  - titles and classes never leaking, plus a planted leak the check must catch
  - the adapter's wheel events

  Removing the cap, the parent walk, the stop check before each notch, the pointer-over-active-window rule, MEDIUM for value-changing controls, or MEDIUM for unclassified surfaces (6 tests fail) makes tests fail.

  Opt-in real-desktop test (17 September 2026, re-run after the change to MEDIUM for unclassified surfaces): it recorded the pointer position, opened its own Notepad, filled it with 200 lines and put the pointer over it (test code). Notepad has a standard scroll bar, so it stays LOW. **down 5** → `done` with no prompt; **up 5** → `done`. It then put the Notepad explicitly at the top (test code); **up 1** → `unverified`, "already at the top". **down 999** and **left 3** were refused. In cleanup (which runs even if an assertion fails) it restored the pointer to its original position, emptied and closed only its own Notepad (`done`), and had to close 0 windows.
- **Limitations.**
  - Value-changing controls inside browsers, WPF, Store and Electron apps can't be detected; those surfaces are asked about and capped at 3 notches instead.
  - A spin box's paired text field isn't one of its parents, so only the arrows themselves are recognised.
  - Apps without standard scroll bars can only ever give `unverified`.
  - Because the pointer is never moved, the user must place it over the window to scroll.
- **Open decision for Phase 3 (Planner): a safety cap looks the same as an interruption.** When an unclassified `down 10` is deliberately capped to 3 notches, the result carries `progress=(3, 10)`, meaning "3 sent of 10 requested because of a safety cap". A `partial` result stopped mid-action carries the same kind of pair (e.g. `(3, 5)`). A Planner reading `progress` alone can't tell "safety cap, working as intended" from "interrupted, needs attention", and those call for opposite responses. The outcome differs today (`unverified` versus `partial`), but that's an indirect signal. When the Planner is built, add a separate field (e.g. a reason the action sent less than requested: safety cap versus interruption) rather than overloading `progress`. Scroll behavior is deliberately unchanged for now.
- **Open decision for Phase 8.** Browsers and Electron apps can't be classified, so nearly every real-world scroll will ask for confirmation. If that becomes tiring in daily use, revisit — most likely by remembering the approval per window for the session, rather than by lowering the risk level.

**Implementation notes — refresh (recorded 17 September 2026).** Kept in Phase 1 deliberately narrow. Refresh needs to know *which application* the active window belongs to, and that is readable from standard Windows APIs (the owning executable and the top-level window class). Scroll's classification problem was about content *inside* a window, which isn't readable.

- **Action.** `ExecutorAction(REFRESH)` refreshes the **active window**. It takes no target; any target text is refused ("Refresh doesn't take a target; it refreshes the active window.").
- **Supported apps** — a code table (not a setting), matching BOTH the executable file name and the top-level window class:

  | App | Executable | Window class | Level | Why |
  |---|---|---|---|---|
  | Chrome | `chrome.exe` | `Chrome_WidgetWin_1` | MEDIUM | reloading can lose unsaved form input or page state |
  | Edge | `msedge.exe` | `Chrome_WidgetWin_1` | MEDIUM | same |
  | Firefox | `firefox.exe` | `MozillaWindowClass` | MEDIUM | same |
  | File Explorer folder window | `explorer.exe` | `CabinetWClass` | LOW | re-reads the folder listing; changes no data |
  | File Explorer with a focused `Edit` control | `explorer.exe` | `CabinetWClass` | MEDIUM | a rename or typed address may be committed or discarded |

  **Everything else is refused before the safety gate**, with no prompt: "I can refresh only Chrome, Edge, Firefox and File Explorer windows in Phase 1, so I didn't press anything." That includes Electron apps sharing Chrome's window class (VS Code, where F5 starts debugging; Slack), WPF apps, the desktop and taskbar, and unreadable executables. Browser risk is MEDIUM always in Phase 1; there is no attempt to lower it from page contents.
- **Prompts** (on screen only): `refresh window "…" (Chrome) - reloads the page; anything typed into it that isn't saved may be lost`, and for File Explorer while editing: `refresh window "…" (File Explorer) - a text box is being edited (renaming a file or typing an address); pressing F5 now may commit or discard it`. A File Explorer folder view refreshes with no prompt.
- **Checks.** An active window must exist and match the table. No Ctrl, Alt, Shift or Win may be held (Ctrl+F5 or Shift+F5 would hard-reload). After confirmation and **immediately before sending**, the active window is re-read and must have the same window handle, executable, top-level class and focused control. The title is deliberately not part of this, because browser titles change by themselves. Then the emergency stop is checked last.
- **Mechanism.** F5, sent only through Refresh, reusing the keyboard-shortcut `SendInput` batch. 0 events accepted → `failed`. Partly accepted → F5 released at once → `unverified`. All accepted → **`unverified`**: "Pressed F5 to refresh Chrome. I can't confirm the refresh happened." A refresh is **never `done`** in Phase 1, because nothing reliable proves it happened (a fast reload can finish before anything could observe it, and a page can intercept F5). Refusals and a changed target are `failed`. **Never retryable.** Logs name the app kind, level, rule and outcome, never window titles.
- **One path only.** F5, Ctrl+R, Ctrl+F5, Shift+F5 and Ctrl+Shift+R are refused by the Keyboard Shortcuts action with a message pointing to Refresh. No generic shortcut can send a refresh key without Refresh's app identification.
- **Tested.** 58 offline tests in `tests/test_executor_refresh.py` cover:
  - every supported app and its level, with `unverified` outcomes
  - 10 refused combinations before the safety gate: `code.exe`, `slack.exe` and `claude.exe` with `Chrome_WidgetWin_1`; Chrome's executable with the wrong class; Firefox's executable with Chrome's class; the desktop; the taskbar; Notepad; a WPF app; and an unreadable executable
  - exact prompts; decline and no confirmation method
  - a target refused; no active window; held modifiers before and after approval; unreadable desktop
  - window, executable, class, focused-control or no-window changes after approval, and a title change alone NOT blocking
  - emergency stop before, during confirmation and right before sending
  - 0 and partly accepted input; never `done`; never retried
  - the refresh keys refused as shortcuts
  - titles never leaking, plus a planted leak the check must catch
  - a read-only real executable lookup

  Removing the class check, browser MEDIUM, Explorer-editing MEDIUM, the pre-send re-check, the title-free identity, or the Ctrl+R shortcut refusal makes tests fail. Opt-in real-desktop test (17 September 2026): the test created a temporary folder and opened it in a new File Explorer window (focused control `DirectUIHWND`). Refresh ran with **no prompt** → `unverified`, 0.25 s. In `finally`, it closed only that window (1 closed; a File Explorer window that was already open stayed open) and deleted only its folder. No real browser test is run: it would open the user's real profile.
- **Limitations.**
  - A web page can intercept or ignore F5, so a browser refresh may not happen even though F5 was sent; this is why it's `unverified`.
  - A program renamed to one of these executable names would be treated as that app.
  - Installed web apps (PWAs) running under `chrome.exe` or `msedge.exe` count as browsers.
- **What would make refresh verifiable (Phase 5).** UI Automation access to an app's own controls (e.g. a browser's Reload/Stop button state), recognition of in-page contexts that capture F5 (web IDEs), and detection of unsaved form input, so a browser refresh could be `done` and possibly lower risk.

**Implementation notes — window controls (recorded 17 September 2026).**

- **Action.** `ExecutorAction(WINDOW_CONTROL, "minimize" | "maximize" | "restore" | "close")` acts on the **active window** only. Case and spaces are ignored; anything else is refused ("Window controls: minimize, maximize, restore or close").
- **Refused before the safety gate** (nothing asked or sent): no active window; the desktop or taskbar; a tool window; a window that isn't responding. The capability checks are operation-specific: minimize is refused only if the window has no minimize box, and maximize only if it has no maximize box. They don't apply to restore or close.
- **Identity** = window handle + executable + top-level class, captured at validation and re-read **immediately before sending**. The title is deliberately not part of it, because titles change by themselves (Notepad adds "*"). If the identity changed: `failed`, nothing sent.
- **Risk levels:**

  | Operation | Level | Why |
  |---|---|---|
  | minimize | LOW | changes no data; one click brings the window back. Windows then activates another window, but every action that could do harm re-checks its own target and asks with the window title |
  | maximize | LOW | only changes the window's size; undone by restore |
  | restore | LOW | only changes size or position |
  | close | MEDIUM (code-constant minimum) | can lose unsaved work; effective risk = max(MEDIUM, any other safety rule) |

- **Minimize / maximize / restore — the first Phase 1 actions that return `done` on direct evidence.** The mechanism is a posted `WM_SYSCOMMAND` (`SC_MINIMIZE` / `SC_MAXIMIZE` / `SC_RESTORE`), exactly what the title-bar buttons send, so the app handles it its own way. No keyboard shortcuts; no forced `ShowWindow`. The emergency stop is checked immediately before posting. The window's state is then **read back** (`IsIconic` / `IsZoomed`) for up to `verifier.window_state_settle_seconds` (2 s):
  - `done` when the requested state is read back, or when the window is **already** in that state (nothing is sent)
  - `unverified` when the request was accepted but the state can't be read afterwards
  - `failed` when the state is readable but didn't change in time, the window disappeared, or Windows refused the request (e.g. an app running as administrator)

  "restore" means a normal window (neither minimized nor maximized). Restoring a *minimized* window is outside this task: a minimized window normally isn't the active window.
- **Close — one mechanism, no bypass.** close_app ("close notepad") and window control close ("close the active window") both first **resolve a window group from this session's own records**: `_open_session_group` by app name, `_session_group_containing` by the active window's handle. A group is never built from a raw window handle.
  - If the active window isn't one the assistant opened, window control close is refused before the safety gate ("I only close windows I opened in this session…"). Option A stands.
  - Both actions then go through `execute()` with **exactly one confirmation** (MEDIUM). Prompt for window control close: `close window "…" (notepad, opened by the assistant this session) - closing can lose unsaved work`.
  - Both call the **one internal close mechanism**, `_close_session_group()`. It does the graceful `WM_CLOSE` (to the Store-app frame if there is one), handles hosted windows, checks the emergency stop immediately before the request, waits for the close, detects save dialogs, and returns `done` / `already_closed` / `needs_user` / `still_open` / `failed`. It asks nothing itself.
  - As a **defensive check**, the mechanism refuses any group that isn't (still) recorded as opened by this session, so a future caller can't hand it an unrelated window, and a session forgotten during confirmation closes nothing.
  - A rule test checks the structure: only `_request_close` sends a close request, only `_close_session_group` calls it, and only `_prepare_close_app` and `_prepare_window_control` reach `_close_session_group`.
  - **Alt+F4 was removed from the keyboard-shortcut allow-list** and is refused, pointing to close_app / window control close. There is no other way to close a window.
- **Emergency stop.** Checked before validation, after confirmation, and immediately before the request. A request is one posted message, so there's nothing to interrupt mid-way. A stop while checking the result raises `EmergencyStopError`, logging that the request was already sent. **Never retryable.**
- **Privacy.** Logs contain the operation, level, rule and outcome, never window titles. Only the close prompt shows a title, on screen.
- **Tested.** 70 offline tests in `tests/test_executor_window_control.py` cover:
  - unknown operations; LOW with no prompt and `done` on read-back for all three state changes
  - already in the requested state (nothing sent); a slow app; a state that doesn't change → `failed`; unreadable afterwards → `unverified`; the window disappearing; requests refused by Windows
  - every refusal before the gate; operation-specific capability checks (a missing maximize box doesn't block minimize, and neither blocks close)
  - identity changes (window, executable, class, none) and a title-only change not blocking
  - emergency stop before, right before sending, and while checking; never retried
  - close: session-only, MEDIUM with one confirmation, capabilities ignored, decline and no confirmation method; MEDIUM without the `close` keyword for both close actions; the MEDIUM floor raised by a stricter rule; identity change after approval; `needs_user` / `still_open`; stop right before the close request
  - both close actions using the one mechanism; the mechanism refusing a forged group and a forgotten session; Alt+F4 refused; the one-mechanism rule test
  - titles never leaking, plus a planted leak the check must catch
  - the adapter's `WM_SYSCOMMAND` codes and errors; a read-only real window-state read

  Also: close_app's existing tests pass unchanged apart from the rule text ("closing a window can lose unsaved work"), plus a test that close_app stays MEDIUM without the `close` keyword; the shortcut tests replace the Alt+F4 tests with a refusal. Mutation checks: removing window control close's session restriction (1 test fails), the mechanism's ownership check (2), the MEDIUM constant (6), close_app's minimum (3), the identity re-check (5), the minimize capability check (1), "done only on read-back" (1), or the Alt+F4 refusal (3) makes tests fail.

  Opt-in real-desktop test (17 September 2026), using only Notepads the test started:
  - **A** (opened by the assistant): maximize → `done`, restore → `done`, minimize → `done` (each with no prompt, each state read back; 0.58 s for all three); then close_app closed the minimized window → `done`.
  - **B** (opened by the assistant): window control close prompted once at MEDIUM → `done`.
  - **C** (started by the test, not the assistant): window control close was refused with no prompt and C stayed open; Alt+F4 as a shortcut was refused and C stayed open.
  - Cleanup closed exactly C. 0 Notepad windows and 0 Notepad processes before and after. The shortcut real test, now closing with window control close, was re-run and passed.
- **Limitations and open decisions.**
  - Restoring a minimized window needs targeting beyond the active window (later).
  - **For Phase 3 (Planner):** "already in the requested state" is `done` just like a real change; only the message tells them apart. Consider a separate field, together with the Scroll `progress` note.
  - Closing the user's own windows remains refused (Option A); changing that is an explicit decision for the project owner.
  - Observation, not changed: Alt+Tab is MEDIUM because silent LOW actions could land in an unexpected window. Since Ctrl+S moved to MEDIUM, only harmless LOW actions remain, so that reason is weaker than when it was set.

**Implementation notes — typed commands (recorded 18 September 2026).** This is where a command enters the assistant in Phase 1: `python main.py --console`.

- **Two files, one path.** `app/executor/commands.py` is a deterministic grammar that turns one line into an `ExecutorAction` (or a refusal) and does nothing else: it imports only `app/executor/models.py`, reads no window and sends no input. `app/console.py` is the entry point: it parses, asks, and hands the action to `execute_with_recovery()` - the only way anything runs from here. No Claude, no fuzzy matching, no closest-command guessing; understanding loosely worded commands is Phase 3.
- **The grammar** (the command word is case-insensitive; runs of whitespace count as one):

  | Line | Action |
  |---|---|
  | `open <app>` | `OPEN_APP` |
  | `close <app>` | `CLOSE_APP` |
  | `close window` | `WINDOW_CONTROL close` |
  | `click <x>, <y>` | `CLICK` |
  | `type <text>` / `type "<text>"` | `TYPE_TEXT` |
  | `shortcut <keys>` | `SHORTCUT` |
  | `scroll up\|down <n>` | `SCROLL` |
  | `refresh` | `REFRESH` |
  | `minimize` / `maximize` / `restore`, each optionally followed by `window` | `WINDOW_CONTROL` |
  | `help`, `exit` | the console itself; never actions |

  The only alias is that optional `window`. `window` is therefore reserved after `close`, so an app can never be called "window".
- **Parsing versus validation.** The parser picks the action and passes the raw target on; the Executor's own preparers still decide whether it is usable, so `click abc`, `shortcut ctrl+q`, `scroll 3`, `refresh now` and a bare `open` ("Which app should I open?") are refused there, with no side effects and nothing asked. The parser refuses only what the grammar itself decides: a bare `close` (**ambiguous** - close an app, or the active window? - never guessed), words after a window control, an unclosed quote, and anything unknown. Every unknown line gets the **same** message, so it can't hint at a nearest match. Nothing is executed in any of these cases.
- **Typed text.** Everything after `type ` is text and is never re-read as a command (`type close window` types those words). Spaces inside it are kept exactly, spaces around it are not; `type "  hello  "` keeps them, and only the outer pair of quotes is the quoting (`type ""quoted""` types `"quoted"`). There are no escape characters, so a backslash is a backslash and **a line break can't be typed from a command in Phase 1**. Text is never logged, never repeated in a refusal and never put in a repr, and no refusal message quotes the line it refused, so a mistyped `type` can't leak either. The console logs nothing of the line; the Executor still logs its own targets (e.g. an app name) exactly as before.
- **Focus hand-over - the console is itself the active window.** While you type, the window in front is the console, so a command that lands wherever the desktop's focus or pointer is would land on the console (`minimize` would minimize it; `type` would type into it). Those commands - **click, type, shortcut, scroll, refresh and window controls** - therefore wait for **you** to put the window you mean in front. `open` and `close <app>` don't, because they name the app. Every action kind is classified explicitly, an unclassified one fails safe by asking, and a rule test checks that a new action can't silently default to either.
  - The console **only watches**: it reads which window is in front and whether a modifier key is held, and never activates a window or sends input. Focus counts as handed over once a window other than the console has been in front, with no modifier held (so the Alt+Tab switcher isn't mistaken for a window), for `console.focus_settle_seconds` (0.5 s), within `console.focus_handover_seconds` (15 s).
  - A confirmation is answered in the console, which takes focus back, so after a "yes" it waits the same way again. If that hand-back doesn't happen, nothing is sent. The Executor's own "the window changed after you approved it" check stays the final word - the hand-over is a convenience, not a safety mechanism.
  - The Phase 2/9 command window can hand focus back itself; this is the Phase 1 stand-in for that (see the Type Text limitation above).
- **Confirmation by a real person - the first time this isn't a test fixture.** Medium risk and above prints the level, the rule and what will happen, and only the exact answer `yes` (any capitals, surrounding spaces ignored) allows it. `y`, `ok`, `yes please`, Enter, `no`, end of input and Ctrl+C all deny, matching the safety gate, which allows only a literal `True`. The retry offer uses the same answer.
- **Emergency stop.** Every wait is interruptible, and **the console never resets the flag**: once it is set, each command reports it and nothing runs until the assistant is restarted. A stop while confirming is reported as a stop, not a denial, and an interrupted action comes back with how much had already happened. Ctrl+C in the console is *not* the emergency stop: it says so and leaves. Whether to wire it to the stop is part of the emergency-stop measurement task, which this entry point is built for: `handle_command()` is a plain function on the main thread, a trigger from any thread lands at the next checkpoint, and a stop comes back as its own status with the partial result rather than as a failure.
- **Outcomes** (`CommandReply.status`): `refused` (the parser), `ran` (see the result), `denied` (the safety gate), `stopped`, `not_handed_over`.
- **How this reaches Phase 3.** `handle_command()` is where later input arrives: the Phase 2 transcript, and a Phase 9 tray window with its own confirm and hand-over. The deterministic parser stays as the direct-command path Phase 3 needs when Claude is unreachable, and a line the parser calls unknown is what would go to Brain -> Planner, each planned action running through the same step.
- **Tested.** 88 offline tests in `tests/test_executor_commands.py` (every supported form, case and whitespace, ambiguous/malformed/unknown lines, raw targets left to the Executor, all the typed-text rules, privacy with a planted leak, and rule tests that the parser imports nothing that can act and calls no adapter) and 81 in `tests/test_console.py` (a parsed command reaching `execute_with_recovery` and a refused one reaching nothing, LOW without a prompt and MEDIUM with one, the full answer table, the retry offer, hand-over classification and behaviour before and after a confirmation, the real hand-over watching and never switching, the emergency stop in four places, the loop with help/exit/blank lines/Ctrl+C/an unexpected failure, privacy, and rule tests that the console imports no adapter, runs only through `execute_with_recovery`, only reads from the Verifier and never triggers or resets the stop). Mutation checks: guessing what a bare `close` means (3 tests fail), turning an unknown line into the closest command (19), typing an unclosed quote as-is (5), logging typed text (8), accepting any answer starting with "y" (6), dropping the click hand-over (1), skipping the hand-back after a confirmation (3), ignoring a failed hand-over (1), resetting the stop (3), or importing an adapter (1).
- **The Phase 1 acceptance run** (opt-in real-desktop test, 18 September 2026): ten different commands typed into the real console, back to back, 4.9 s in total, three confirmations. Test setup started its own Notepad as a scroll fixture (200 lines, put at the top) and opened a temporary folder in a new File Explorer window; the test, never the console, switched windows.

  | # | Typed | Result |
  |---|---|---|
  | 1 | `refresh` (Explorer) | no prompt, `unverified`: "Pressed F5 to refresh File Explorer..." |
  | 2 | `open calculator` | `done` after 1.3 s |
  | 3 | `close calculator` | MEDIUM, `yes` -> `done` after 0.3 s |
  | 4 | `open notepad` | `done` after 0.3 s |
  | 5 | `maximize` | no prompt, `done`, read back as maximized |
  | 6 | `type Hello from the typed-command test` | MEDIUM (`type 33 characters into window "Untitled - Notepad" (field: Edit)`), `yes` -> `done`, verified, `progress=(33, 33)` |
  | 7 | `shortcut ctrl+a` | no prompt, `done` (everything selected) |
  | 8 | `click 716, 264` (the fixture's text area) | MEDIUM, `yes` -> `unverified`, pointer at (716, 264) |
  | 9 | `scroll down 3` (the same fixture) | no prompt, `done`, scroll position 0 -> 9 |
  | 10 | `minimize` | no prompt, `done`, read back as minimized |

  None of the prompts contained the typed text. Cleanup closed exactly the two Notepads the test started and its one Explorer window, deleted only its folder, put the pointer back, and left no Calculator window, no modifier held and the clipboard's change counter unchanged.
- **Limitations and open decisions.**
  - The interactive hand-over is clunky for a Medium command: switch to the window, switch back to answer, switch again. It is covered offline; the acceptance run scripts it as test code, so the waiting itself has only been tried by hand.
  - A typed command can't press Enter or type a line break (no escape syntax, and Enter isn't a supported shortcut).
  - A person **can** now trigger the emergency stop while an action runs: the global hotkey `Ctrl+Alt+Backspace`, registered with `RegisterHotKey` and delivered as `WM_HOTKEY` — no low-level keyboard hook, so no other keystroke is ever seen. It has been verified by physically pressing it with PowerShell, with Chrome and with an elevated Notepad in front. Windows' secure desktop (the UAC prompt itself) stays out of scope: no ordinary program receives input there. Its approved response-time thresholds are in the emergency-stop notes below. Whether Ctrl+C in the console should also stop an action is still open.
  - Unchanged, as decided: the Executor logs an app name it was given (so a sentence typed after `open` is logged as an unknown app name). Only typed text is private.
  - A console window that is itself a target can't be used: the hand-over always waits for a *different* window.

**Implementation notes — emergency-stop global hotkey (recorded 18 September 2026).** The hotkey is `Ctrl+Alt+Backspace` and **stays as it is**: it was tested physically and meets the requirement. Its response-time thresholds were approved on 18 September 2026 and are now encoded in the acceptance path.

- **What it is.** `app/executor/hotkey.py` runs one daemon thread that calls `RegisterHotKey(NULL, 1, 0x4003, 0x08)` — `MOD_NOREPEAT|MOD_CONTROL|MOD_ALT` with `VK_BACK` — and blocks in `GetMessageW`. A press does exactly one thing: `emergency_stop.trigger("global-hotkey")`. There is no second stop mechanism and nothing here ever resets the stop. Deliberately **not** a keyboard hook: Windows delivers only the one registered combination, so no other keystroke is ever seen, and this module sends no input and performs no desktop action. The combination is `executor.emergency_stop_hotkey`; the console prints it at startup and once more if it stops being active.
- **Established observations — physical keyboard, this machine, 18 September 2026.** These are measurements, not inferences:
  1. Registration succeeds. No other process owns the combination (no `ERROR_HOTKEY_ALREADY_REGISTERED`).
  2. A physical `Ctrl+Alt+Backspace` produces `WM_HOTKEY` with **PowerShell in front** (`powershell.exe` / `ConsoleWindowClass`).
  3. A physical `Ctrl+Alt+Backspace` produces `WM_HOTKEY` with **Chrome in front** (`chrome.exe` / `Chrome_WidgetWin_1`), foreground confirmed both at the start of the run and at the moment of the press. This is the requirement the hotkey had to meet: Chrome is where the owner spends most of their time, so a stop that failed there would not be a stop.
  4. A physical `Ctrl+Alt+Backspace` produces `WM_HOTKEY` with an **elevated Notepad in front** (`notepad.exe` / class `Notepad`, started with "Run as administrator"), and `emergency_stop.trigger("global-hotkey")` fired. No errors. This is one observation of one case on this machine; it is not a general claim about every elevated application.
  5. Synthetic presses fired `WM_HOTKEY` **65 times out of 65** across the idle, typing, scrolling and one-shot measurement trials.
  6. **The earlier "NOT RECEIVED" runs with Chrome or the Claude app in front are void, and are *not* evidence about Chrome.** Claude Code opened the 60-second window at the same instant it told the owner to press the key, so the owner was not at the keyboard while it was open. Nothing about the hotkey can be read from them, and they must not be recorded as a Chrome effect.
- **Not a cause — do not carry forward.** While the earlier runs looked like a foreground-app effect, two explanations were floated: that Ctrl+Alt collides with AltGr in Chromium, and that a low-level keyboard hook was swallowing the combination. Both were **hypotheses only, and the evidence above does not support either**. Neither is a documented cause of anything in this project.
- **Elevated windows: measured once, and it worked (observation 4).** The earlier UNKNOWN is resolved for this machine and this case only — one elevated Notepad, in front, one physical press. Nothing in the code depends on the answer, and it is not generalised to every elevated application. Windows' secure desktop (the UAC prompt itself, Ctrl+Alt+Delete) remains out of scope by design: no ordinary program receives input there.
- **Alternative combinations (tested only as far as noted).** `Ctrl+Shift+Backspace`, `Ctrl+Alt+Shift+Esc`, `Ctrl+Shift+Insert` and `Ctrl+Alt+Shift+Insert` all register successfully. `Ctrl+Shift+Backspace` was also received with Chrome in front; the other three are **untested, not failures** — the probe run reached its timeout first. If one is ever adopted it is a config change, not a code change, since all their keys are already in the allowed key table. Two things to weigh first: `Ctrl+Shift+Insert` is paste in many terminals and editors, and a registered hotkey takes that binding away system-wide; and **F12 must not be used in any combination**, because Microsoft reserves it for the debugger at all times and says not to register it.
- **What the thresholds measure.** Latency **C** is: synthetic `SendInput` press → `WM_HOTKEY` → the existing emergency stop → the Executor actually stops. It is **synthetic end to end** and makes **no claim about physical keyboard or driver latency**. That physical presses reach the listener at all is separate *reachability* evidence (observations 2, 3 and 4) and is deliberately never timed — a physical press can't be timestamped from Python. Timings use `time.perf_counter()`, because `time.monotonic()` only moves in ~15.6 ms steps on Windows, coarser than the thing being measured.
- **Approved acceptance thresholds (18 September 2026).** Approved by the project owner after reading the first measurement:
  1. **Hard maximum:** C ≤ **100 ms** for *every* timed trial of *every* scenario.
  2. **Typical:** for the 20-trial Type Text and 20-trial Scroll scenarios, **median C ≤ 25 ms** and **p95 C ≤ 50 ms**.
  3. **Idle and one-shot:** the hard maximum only. No p95 requirement is invented from 5 one-shot trials.
  4. **Non-timing, and not negotiable by any number:** no input may continue after the stop takes effect; no stopped action may be automatically retried; a one-shot action stopped before its final send checkpoint must send nothing.

  If a run misses a threshold it is reported as it is. The thresholds are not adjusted to make a run pass, and the code is not tuned to chase a number.
- **Where they are encoded.** In the existing measurement harness, not a second system: `scripts/measure_emergency_stop.py` holds the constants (`HARD_MAX_C_MS`, `TYPICAL_MEDIAN_C_MS`, `TYPICAL_P95_C_MS`, `TYPICAL_SCENARIOS`) and the judging (`Scenario.checks()`, `Verdict`, `print_acceptance`), and `acceptance_run()` is the one acceptance path. The script exits 1 when a run fails. `tests/test_emergency_stop_acceptance.py` pins the approved numbers offline (23 tests, including that only Type Text and Scroll carry median/p95) and calls the same `acceptance_run()` for the opt-in real run:

      $env:RUN_REAL_DESKTOP_TEST='1'; pytest tests/test_emergency_stop_acceptance.py -m real_desktop -v -s

  The report prints, per scenario: trial count, timed count, min, median, p95, max, each threshold with PASS/FAIL, and the two non-timing rules.
- **Acceptance run — PASS (18 September 2026, 65 trials).** Every threshold met, every trial timed:

  | Scenario | Trials | min | median | p95 | max | Thresholds |
  |---|---|---|---|---|---|---|
  | Idle press | 20 | 0.3 ms | 0.5 ms | 1.5 ms | 1.5 ms | max ≤ 100 ms — PASS |
  | Type Text (810 chars) | 20 | 1.4 ms | 2.0 ms | 5.1 ms | 5.1 ms | max/median/p95 — PASS |
  | Scroll (down 20, 200 lines) | 20 | 1.0 ms | 3.6 ms | 22.0 ms | 22.0 ms | max/median/p95 — PASS |
  | One-shot click | 5 | 0.9 ms | 1.0 ms | 2.0 ms | 2.0 ms | max ≤ 100 ms — PASS |

  No input continued after a stop in any trial, no stopped action was retried, and every one-shot trial sent nothing (the pointer never moved). Worst C overall: **22.0 ms**, against a 100 ms hard maximum.
- **The attempt before it, recorded because it is real.** An earlier run of the same acceptance path failed its validity check: in 3 of 20 Scroll trials the Executor **refused to scroll at all**, because Ctrl/Shift/Alt were physically held down on the keyboard at that moment ("Shift and Ctrl are held down, which would change what the wheel does"). That is the scroll safety rule working correctly, but it means no stop was exercised in those trials, so the run could not be called a pass. Every timing threshold passed in that run too. The re-run above, with the keyboard left alone, timed all 65 trials. **Nothing was changed to make it pass.**
- **Harness fixes made while running this (measurement code only, not the assistant).** `open_notepad()` now waits for the Notepad it opened to actually reach the front, brings *only that window* forward if Windows kept the front window it had, checks the handle is one this run opened, and closes it again if it can't get there — an earlier failure of that check left a Notepad behind.

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
Phase 1 — Basic Computer Control: COMPLETE / CLOSED (Done-when verified 20 September 2026)
  Evidence: the two checklist runs in Section 4 - phase1_checklist.py --real-desktop (4/4)
  and --real-elevated (1/1), offline 1096 passed / 18 skipped, no failures.
Current phase: 2 — Voice (next; see Section 5)
Last updated: 16 September 2026
```

Update this block — and the matching "Current phase" line in `CLAUDE.md` (Step 3, Section 4) — every time a phase's Done-when checklist is actually confirmed passing, not when it merely feels finished. This is the single source of truth both you and every future Claude Code session should check before starting new work.
