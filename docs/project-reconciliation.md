# Project Reconciliation — planned vs. actually true

**Written 2026-10-10. Analysis only; no application code was changed to produce it.**

## What this document is

One place that reconciles what the frozen plan assumed with what is true in the code today, and gives a
defensible order for what remains.

**The frozen documents remain authoritative for what was agreed:** `build-plan.md`,
`step1-feasibility_risk.md`, `step2-dev-environment.md`, `step3-workspace-architecture.md`,
`step4-production-development.md`. The phase closeouts (`phase2-closeout.md`, `phase3-closeout.md`,
`phase4-closeout.md`) remain authoritative for what each phase delivered and what it carried out.
`owner-testing-improvement-backlog.md` remains the record of real-machine findings, and it still
authorizes nothing.

**This document is not a second plan.** Where it recommends something, the recommendation is marked
`[MY RECOMMENDATION]` and is separate from anything the owner confirmed. Where it cannot establish
something without an experiment, it says `UNKNOWN` and names the experiment.

Two corrections to the brief that produced this document, since both affect what can be relied on:

* there are **three** closeout documents (Phases 2, 3, 4), not five. Phases 0 and 1 closed against
  checklists recorded in `CLAUDE.md` and `step4`, with no closeout record of their own.
* `docs/owner-testing-improvement-backlog.md` holds **15 numbered findings (BL-1 … BL-15)**, of which
  BL-15 is already resolved and BL-1 was reclassified to a known limitation on 2026-10-09.

---

## 1. Status at a glance

| | |
|---|---|
Phases closed | 0, 1, 2, 3, 4 (evidence in `CLAUDE.md` and the three closeouts) |
Phase 5 | **IN PROGRESS, NOT COMPLETE.** 2 of 5 frozen observation layers exist (UIA, DOM) |
Phases 6–10 | not started |
Usability plan (post-2026-10-06 audit) | Slices 1, 2, 3, 5 complete; Slice 4 **parked** (resumes as `type_text` only) |
Production Python | ~15,500 lines across 10 module folders + `main.py` |
Tests | **4,559 collected** across 96 files (~46,600 lines) |
Git | 36 commits, one tag (`v0.1`, Phase 0). **No `v1.0`** — Build Plan Tier 1 is not declared |
Entry points | `python main.py` (health check), `--console` (typed), `--voice` |
Apps controllable | **5**: notepad, calculator, chrome, vscode, explorer |
Action kinds | **12** |

### The working tree is not clean, and that matters to this inventory

As of writing there is **uncommitted work** in the tree — the Phase 4 wiring slice (local
`remember`/`recall`) and the explicit `start fresh memory` command:

```
 M CLAUDE.md  app/console.py  app/executor/commands.py
 M app/memory/logic.py  app/memory/queries.py
 M tests/test_console_prompts.py  tests/test_memory_integration.py
 ?? app/brain/personal_memory.py
 ?? tests/test_memory_brain_wiring.py  tests/test_memory_initialisation.py
```

Its validation is **incomplete**: the 5-mutant proof for the initialisation command was never run, and
the full offline suite has not been run since it was added. The focused suites pass (648 passed on the
eight affected files, 1 pre-existing machine-state failure). **Everything below that depends on this
work is marked `UNCOMMITTED`** so it is not mistaken for shipped, proven behaviour.

---

## 2. Honest inventory

Four classifications, with evidence for every line.

### REAL-MACHINE VERIFIED — the owner ran it and it worked

| Capability | Evidence |
|---|---|
Typed console: open/close app, click by coordinate, type, shortcut, scroll, refresh, window controls | Phase 1 Done-when, two runs of `scripts/phase1_checklist.py` (`--real-desktop`, `--real-elevated`), 2026-09-20 |
Emergency stop `Ctrl+Alt+Backspace` | Phase 2 closeout: ~22 ms median, registered before the speech model loads |
Voice in and out, 5 loose spoken commands | Phase 2 Done-when, 2026-09-29 |
Brain understanding of loosely-worded commands, multi-step plans, the plan prompt | Phase 3 Done-when, 2026-10-02; backlog W-2, W-3, W-4 |
Memory: alias resolution, person/contact lookup, three correction levels, export/restore, wipe, degradation | Phase 4 Done-when, 2026-10-03 — **but see §3, all of it via tests, not through the product** |
Named UIA click in a native app | Phase 5 Slice 2 owner smoke: Calculator "Seven" resolved, confirmed, re-identified, clicked, 7 displayed |
Named click from the typed console | Phase 5 Slice 3 owner smoke: `open calculator` then `click seven` |
**The three-command browser workflow**: `open assistant browser` → `open this site <url>` → `click X in assistant browser` → `close assistant browser` | backlog **W-1**, all four succeeded |
Duplicate-control refusal clicking nothing | backlog **W-5** |
Navigation of a fast site | backlog **W-6** (`example.com`, no timeout) |

### TESTED ONLY — green tests, never run against real hardware in this state

| Capability | Why it is only this |
|---|---|
Local `remember` / `recall` for people and contacts | `UNCOMMITTED`. The owner's smoke reached the **refusal** path only (no database existed), so the stored-and-recalled path has never run on the real machine |
`start fresh memory` | `UNCOMMITTED`, written after that smoke; never run by the owner |
Memory export / restore / wipe / per-entry deletion | Phase 4 proved these in tests; **no production caller exists** (see §3) |
The three correction levels | Same — proven in tests, unreachable from the product |
Sensitive-data disclosure filtering, and the new `BRAIN`/`USER` context split | `UNCOMMITTED` for the split. The rules it reads **can never be written** (see §3) |
DOM click re-resolution after confirmation, frame refusal, readiness wait | Exercised against fakes; the real-browser path is proven only to the extent W-1 covers it |
Everything about voice beyond the Phase 2 Done-when | e.g. Roman Urdu handling across three scripts is tested, not measured in use |

### BUILT BUT UNREACHABLE — code exists, no production caller

**This is the category the owner asked to be hunted, and it is larger than the one known instance.**
Method: a call-graph reachability pass from `main.py::main` over all of `app/` plus `main.py`, treating
a bare attribute reference as a reference (so a callback passed by name counts), then each finding
confirmed individually by grep. Caveat stated honestly: the Executor dispatches action kinds through a
`_PREPARERS` dict, so names reached only by string dispatch were checked by hand and excluded.

| Unreachable | Consequence |
|---|---|
**`memory.remember_application_alias`** | The **write** half of the alias feature. The read half *is* wired into the console. So **the owner cannot teach an alias through the product at all** — Phase 4's Done-when ("it remembers an app alias between runs") is demonstrable only in tests |
**`memory.export_memory` / `memory.restore_memory`** | The owner **cannot back up or restore their memory**. The missing-database message's offer to "restore a memory export" is honest only because it says *once that is available* |
**`memory.wipe`** | No way to clear memory from the product |
**`memory.observe_correction`, `remember_explicitly`, `resolve_value`** | **All three correction levels are unreachable.** `correct_for_this_task` is called only by `observe_correction`, which nothing calls. Phase 4's headline Done-when is a test-only capability |
**`memory.set_sensitive_rule`** | Disclosure rules can never be written, so the `BRAIN`/`USER` split added in the uncommitted slice **has no way to be configured by the owner**. It is correct and inert |
**`memory.add_relationship`** | "my friend Ali" cannot be taught; the relationship column exists and nothing can fill it |
**`memory.queries.contact`** (BRAIN context) | Documented as Phase 4 Limitation B: no messaging caller, because messaging is Phase 10 |
**`memory.clear_identity`, `clear_short_term_context`** | No production caller |
**`app/dashboard/`** | 24 lines of skeleton placeholders across four files (*"no implementation yet"*). The Dashboard is **one of the eight components in Build Plan Section 2** and is the only one with no implementation |
`executor.click_target` (the standalone primitive) | Minor and not a capability gap: the named-click *kind* is live via `_prepare_named_click`. Only the bare primitive has no caller |
`listener.voice_stop_enabled` (setting) | Nothing reads it, deliberately — Phase 2 Limitation A |

Nine of the fifteen memory structures also still have **no write API at all** (Identity, Projects,
Files, Websites, Preferences, Habits, Workflows, Short-term context, Permissions) — Phase 4
Limitation C. That is a separate and larger gap than the unreachable functions above: for those nine
there is nothing to make reachable yet.

### PLANNED, NOT BUILT

OCR, Claude vision, coordinate fallback and cross-layer recovery (3 of 5 Phase 5 layers);
conversational and pronoun context (Phase 6); workflows (Phase 7); the full Low/Medium/High/Critical
per-action-type policy (Phase 8); packaging, tray icon and uninstall flow (Phase 9); all communication
integrations (Phase 10 — `app/integrations/` holds only an empty `__init__.py`).

### Stated plainly: what it can and cannot do

**It can control five applications** — notepad, calculator, chrome, vscode, explorer — plus its own
ephemeral Chrome session. Inside them it can: open and close them; bring one to the front; click a
control **by name** (native apps via UIA, web pages via the DOM); click by coordinate; type text;
send a shortcut; scroll; refresh; minimise/maximise/restore/close; and navigate its own browser to an
address the owner typed. Every action passes the safety gate first, and Medium-or-above asks first.

**It cannot**: type into a named window without the owner switching focus by hand (Slice 4, parked);
tell whether a click actually achieved anything (**every click is `UNVERIFIED`** — BL-5); proceed when
two controls share a name (BL-3, a dead end); understand a plain-language report that something failed
(BL-6); see anything that is neither in a UIA tree nor in its own browser's DOM (no OCR, no vision);
remember anything the owner asks it to remember **in committed code**; send a message to anyone; or run
as an installed application with a tray icon.

A console application cannot be configured as an app at all — `powershell` was removed from
`config.yaml` after four real attempts left no surviving process, because `launch_app` runs
`Popen([path])` with handles redirected to NUL and no `CREATE_NEW_CONSOLE`.

---

## 3. Where reality disagreed with the plan

The core of this document. Each entry: what the frozen plan assumed, what turned out to be true, how it
was discovered, and what it means for what is not yet built.

### D-1. The frozen plan's "fifteen structures" became a schema, not a feature set

**Assumed** (Build Plan 6.8, Step 4 Phase 4): building memory as a real table structure rather than a
freeform notes blob is what makes *"message him"* a lookup instead of a guess.
**True:** the schema was built exactly as specified and is genuinely good — and then **almost none of
its write side was connected to anything the owner can type.** Aliases, relationships, corrections,
export, restore, wipe and the disclosure rules are all reachable only from tests.
**How discovered:** the owner's own smoke of the uncommitted `remember` slice hit *"I have no memory
database yet"* — because `initialize()` had never had a production caller either. The rest was found by
the reachability pass in §2 of this document.
**Means for what is unbuilt:** this is the project's single most repeated failure mode, and it is a
*wiring* failure, not a design failure. Phase 6 (context), Phase 7 (workflows) and Phase 10 (messaging)
all plan to build *more* subsystems on top of this one. Any of them can be built to the same standard
and still be unusable. **Every slice from here should end with a line the owner can type.**

### D-2. Chrome is single-instance, so a launched window can never be owned

**Assumed** (Phase 1/3 design): the assistant launches an app, waits for a new window, and tags that
window with an ownership token, which is what later authorises closing it.
**True:** Chrome hands the launch to the already-running process and exits, so no new window is
attributable to the assistant. The frozen rule was then changed (Slice 2): if exactly one usable window
already exists, **activate it instead of launching**, and it stays **unowned**.
**Consequence recorded deliberately:** `close chrome` refuses for a window the owner already had open,
and that is correct, not a bug.
**Means for what is unbuilt:** ownership can never be the gate for a browser-based capability. Phase 10
over WhatsApp Web is browser-based, so it must be authorised per-capability (D-3), never by ownership.

### D-3. Ownership was the right gate for closing and the wrong gate for clicking

**Assumed:** one authorisation concept — "did the assistant open this?" — suffices for everything it
does to a window.
**True** (Slice 3): using it for clicking blocked every useful thing in the owner's own Chrome, for no
safety gain. Clicking now accepts a named-or-found window behind the existing Medium confirmation,
which *is* the authorisation and therefore always says whether the assistant opened the window or only
found it. Closing still requires the token.
**Means for what is unbuilt:** the project already has the right pattern for "powerful action in a
window we do not own" — a confirmation that names the window, plus structural re-checks after the
confirmation and before acting. A WhatsApp send should reuse that shape rather than invent one.

### D-4. Whisper does not reliably return Roman script for Roman Urdu

**Assumed** (Step 4, Build Plan 6.6): Roman Urdu is *"English-script text the Brain interprets
contextually"*.
**True** (Phase 2, measured): `type mera naam Wajid hai` came back as `type मेरा नेम वाजिजली` —
Devanagari, reported as Hindi at 0.62 confidence, and the content wrong as well.
**Means for what is unbuilt:** the Brain must handle **three scripts**, not one, and the script the
owner intended is not always the one that arrives. Anything voice-driven inherits this. It is also why
the uncommitted `remember` slice refuses to store a spoken contact: a mis-heard digit stored silently is
worse than a retype.

### D-5. Roman Urdu word order broke a privacy guard before it shipped

**Assumed** (implicitly, by every English-first parser in the project): a command's marker word comes
first.
**True:** Urdu is SOV, so `Ali ka whatsapp +92-300-0000001 yaad rakho` puts the marker **last**. A
leading-marker-only guard would have sent that phone number to the provider — the exact thing the slice
existed to prevent.
**How discovered:** while auditing the guard's coverage before writing it (uncommitted slice).
**Means for what is unbuilt:** every future deterministic recogniser that handles Roman Urdu must test
both ends of the line. This is a *new* divergence, not in any closeout.

### D-6. Spoken emergency stop was measured and rejected

**Assumed:** a spoken "stop" is a natural safety trigger.
**True** (Phase 2): three detectors measured, none good enough — Whisper-small 11.5–11.9 s and 0/6
detections; sherpa-onnx KWS 2/5 and 3/5 recall with 6/10 and 5/10 false positives; Vosk never installed
(sdist-only transitive dependency). `Ctrl+Alt+Backspace` stays authoritative.
**Means for what is unbuilt:** the closeout's own advice is to restart from the **contract** question
rather than a fourth detector — *"please stop"* and *"stop it"* are stop intents, while *stopped*,
*stopping* and *full stop* are the negatives that matter.

### D-7. The assistant's browser is deliberately profile-less, which blocks every logged-in site

**Assumed** (Build Plan Section 4): Playwright "reliably clicks real page elements", with no statement
either way about sessions.
**True:** the session is `browser.new_context()` — non-persistent, no `user_data_dir`, no
`storage_state`, no cookie import — specifically so the owner's own Chrome profile stays untouchable.
**Means for what is unbuilt:** **anything requiring a login is unreachable today**, including WhatsApp
Web, Slack and email in a browser. This is a *decision*, not a tool limit — Playwright does support
`launch_persistent_context` — so it can be reversed deliberately, for a **dedicated assistant profile**,
without ever touching the owner's Chrome data. See §4.

### D-8. A real website can exceed the navigation bound

**Assumed:** navigation completes, and completion means ready.
**True** (BL-1, measured warm): DOMContentLoaded at **30.9 s** for the Herokuapp demo and **15.4 s** for
the owner's own site, against a 20 s bound. The page is still usable — the owner clicked in both — so the
cost is a wait, not a failure, and the result stays `UNVERIFIED` rather than claiming a load.
**Reclassified by the owner to a KNOWN LIMITATION on 2026-10-09; not to be reopened.**
**Means for what is unbuilt:** "navigate then immediately act" cannot be assumed by any later workflow
(BL-2 is the same root). A workflow replay (Phase 7) that chains navigation into a click needs an
explicit readiness step, not an assumption.

### D-9. Every click is unverified, and nothing distinguishes "worked" from "did nothing"

**Assumed** (Build Plan Section 2, the Action → Result → Recovery loop): *"after 'click Login', the
system needs to actively ask 'did Login actually happen?'"* — the Verifier is described as the single
thing that stops silent failure.
**True** (BL-5): a click is *dispatched*; its effect was never observable. The message is honest and the
limitation is by construction.
**Means for what is unbuilt:** this is the **largest honest gap against the frozen vision**, and it
gates more than it looks. It gates BL-6 (the assistant cannot evaluate a failure report it has no
ground truth for), it gates workflow replay (Phase 7 verifies each step during replay), and — most
importantly — **it gates sending a message to a real person**, where "I pressed send and cannot tell
whether it sent" is a materially worse answer than it is for a demo page.

### D-10. Four tests depend on what is open on the owner's machine — now five

**Assumed:** the offline suite is hermetic.
**True:** three `test_desktop_isolation.py` tests assert the guard names `launch_app`, but with a
Notepad window open the Executor correctly chooses *activation* instead (Slice 2's frozen rule), so the
guard names `activate_window`. Proven by forcing an empty window list, and by observing one real
window (`handle=328718, '*Untitled - Notepad'`). `list_windows` is **deliberately not** redirected
offline (`safety_guards.py:436`).
**New, found 2026-10-09:** a **fifth** such test —
`test_console_brain.py::test_a_low_brain_floor_cannot_soften_a_real_executor_rule` — fails when the
**clipboard is empty**, because the real Executor reads the real clipboard and refuses the `ctrl+v`
before the assertion's data exists. The laptop rebooting emptied it. Confirmed pre-existing with all
current work stashed.
**Means for what is unbuilt:** the suite cannot be used as a release gate while its result depends on
desktop state. Phase 5's own closeout already names this family (Limitation G: seven observation reads
"NOT centrally isolated yet").

### D-11. The project's stated finish line has moved out of reach by its own terms

**Assumed** (Build Plan Section 9): Tier 1 — Phases 0–4 complete, **used daily for a week**, and
**packaged into an installer** — is the recommended real finish line, after which the project can
legitimately stop at v1.0.
**True:** Phases 0–4 are closed, but the project is run as `python main.py --console` from a terminal,
packaging is Phase 9 (not started), and the one tag is `v0.1`. The daily-use condition is also the one
that produced the 15-item backlog, which is itself evidence that daily use had not happened before.
**Means for what is unbuilt:** Tier 1 cannot be declared without Phase 9, which sits *behind* five
unbuilt phases in the frozen order. This is the sharpest frozen-order-versus-value conflict in the
project, and §5 takes a position on it.

### D-12. Documentation has drifted in two places

Not divergences from the plan, but drift that would mislead a future session:

* `CLAUDE.md` still says DOM Slices 1 and 2 are *"NOT yet validated against a real browser"*. Backlog
  **W-1** supersedes that: the full browser workflow was confirmed on the real machine. The DOM layer
  is real-machine verified to the extent W-1 covers it.
* `CLAUDE.md` describes the uncommitted Phase 4 wiring slice as **DONE (2026-10-09)**. It is written and
  focused-tested, but uncommitted, not mutation-proved, and not yet run in a full suite.

---

## 4. The remaining vision — honest feasibility

The goal, from the Master Plan and from the owner directly: *a desktop companion that feels like a
helpful friend. Natural language in English, Urdu and Roman Urdu. It remembers. It can message people
on WhatsApp, Slack and Email. It understands what is on screen.*

Sizes are: **hours** · **one slice** (a day or so, one testable change) · **several slices** ·
**a phase** (weeks, its own Done-when).

### 4.1 "It remembers"

| | |
|---|---|
**Exists to build on** | The entire Phase 4 schema — 15 tables, typed results, `Found`/`Ambiguous`/`NotFound`/`Redacted`, disclosure contexts, export/restore/wipe/delete, the three correction levels, and the privacy rule that nothing stored reaches Claude. Plus (uncommitted) a proven local-recogniser seam ahead of every provider call |
**Genuinely missing** | Production callers for most of the write side (§2), write APIs for nine of the fifteen structures, and a decision about what `remember that my work folder is …` should mean |
**Size** | Making the existing features reachable: **one slice each**, several in total. Giving the nine structures write APIs: **several slices**. The full "human-like memory" of BL-7: **a phase** |
**UNKNOWN** | None material. This is wiring, and the architecture is already decided |

### 4.2 "Understands what is on screen"

| | |
|---|---|
**Exists** | UIA layer (exact accessible-name matching, re-identification, refusal on duplicates) and DOM layer (role allowlist, bounded readiness wait, re-resolution after confirmation) |
**Missing** | OCR, Claude vision, coordinate fallback, cross-layer recovery — 3 of 5 frozen layers — plus BL-3/BL-4 disambiguation and BL-5 verification |
**Size** | OCR: **several slices**. Vision: **several slices** (and it costs money per call). Cross-layer recovery: **a phase**, realistically, because it needs all layers first. Disambiguation (BL-3/BL-4): **one slice per layer**, after the DOM ordering audit |
**The finding that should weigh here** | The 2026-10-06 weakness audit found that **none** of the owner's fifteen observed session failures was caused by the assistant being unable to *see* the screen. BL-14 records this as a finding to weigh, not permission to skip |

### 4.3 "Natural language in English, Urdu and Roman Urdu"

| | |
|---|---|
**Exists** | The Brain accepts all three scripts; five loose commands verified; clarification; multi-step planning; a deterministic parser for the common verbs; Roman Urdu *keep* markers at both ends of a line (uncommitted) |
**Missing** | Feedback understanding (BL-6: `add element click nahi huwa` was not connected to the preceding action); the Urdu question form (`Ali ka number kya hai` falls through); pronouns and multi-turn subjects (Phase 6) |
**Size** | BL-6 properly: **several slices**, and it depends on D-9. Phase 6 as frozen: **a phase**. The Urdu question form alone: **hours** |

### 4.4 Messaging — and the WhatsApp verdict

**The honest verdict: WhatsApp is feasible, it is not blocked by anything unworkable, and it is not
small. Two of its four prerequisites do not exist yet, and one of those is the project's largest open
gap.**

What already exists that it builds on:

* a real browser automation layer that can **find and click** a control on a page by name, with
  re-resolution after the confirmation (DOM Slice 2, real-machine verified via W-1)
* navigation to an address the owner typed, with a provenance check that refuses a model-invented one
* the safety gate, with Medium-or-above confirmation, and the Slice 3 pattern for *"a powerful action in
  a window we do not own"* — a confirmation that names the target plus structural re-checks afterwards
* People/Relationships/Contacts in Memory, and a local `remember`/`recall` for contacts (uncommitted)
* the frozen risk analysis already done: Step 1 Section 5 states the ToS risk plainly, and Step 4
  Phase 10 requires it to be **explicitly re-confirmed as accepted** before any use beyond testing

What is genuinely missing, in dependency order:

1. **A persistent browser profile.** The session is non-persistent by deliberate design (D-7), so
   WhatsApp Web cannot stay logged in. A **dedicated assistant profile** — its own `user_data_dir`,
   logged in once by the owner scanning the QR code, never the owner's Chrome profile — is supported by
   Playwright and would not touch the owner's data. It is a reversal of a documented decision, and it
   creates a new persistent artifact holding a live session for a messaging account, so it needs its own
   privacy review. **Size: one slice**, plus the review.
2. **DOM typing.** The DOM layer can `dom_query`, `dom_click`, `browser_navigate` and
   `dom_page_has_frames`. There is **no `dom_type` / `dom_fill`**. A message cannot be composed.
   **Size: one slice**, and it is the same prepare→run seam surgery Slice 4 measured (the focused
   control is half of `_focus_identity` and is unknown until after activation).
3. **Send-with-confirmation.** Frozen: sending is *always at least Medium risk*. The gate exists; this is
   a new kind plus a confirmation that shows the recipient and the message. **Size: one slice.**
4. **Knowing whether it sent.** This is D-9/BL-5, and for messaging it is not optional polish. A click
   that reports `UNVERIFIED` on a demo page costs the owner a glance; a *send* that reports
   `UNVERIFIED` leaves them not knowing whether a real person received a real message — and the backlog's
   own standing constraint forbids repeating the action to find out (*"never repeat a potentially
   destructive action to test whether it worked"*). **Size: several slices**, DOM-first, because a page's
   own state is readable in a way UIA's is not.

**What is NOT blocked:** nothing here hits a wall. The ToS risk is real, inherent and already accepted
in principle by the frozen plan, and it is not eliminated by good code.

**UNKNOWN, and the experiment that would settle it:** whether WhatsApp Web's own DOM is tractable for
this layer at all — whether a recipient's chat and the message box expose stable accessible roles and
names that the existing role allowlist can match, and whether the send control is distinguishable from
everything else on the page. **The experiment:** with a persistent profile logged in, run the existing
*read-only* `dom_query` against WhatsApp Web and record what the allowlist actually finds for the search
box, a chat in the list, the message box and the send button. **Read-only, no click, no send** — the
real_browser gate already exists for exactly this. One session's work, and it would de-risk the whole of
Phase 10 before a line of send code is written.

**Slack would be substantially less work** and the frozen plan already prefers it (*"prefer the official
API"*): an HTTP API call needs no browser, no profile, no DOM typing, and returns a real send
confirmation — which means it does not depend on D-9 at all. If the goal is *"it can message people"*,
Slack is the cheap proof and WhatsApp is the expensive want. `[MY RECOMMENDATION]` below takes a
position on this.

### 4.5 The rest

| Capability | Exists | Missing | Size |
|---|---|---|---|
Workflows (Phase 7) | verified steps, plans, repetition in planning (W-3) | record/save/replay, per-step verification during replay, workflow corrections | **a phase**, and it inherits D-9 |
Full safety policy (Phase 8) | risk levels, the gate on every action, advisory raise-only floors, Medium+ confirmation | per-action-type Low/Medium/High/Critical rules; BL-13 (is the Brain's floor-raising sensible? reason strings say nothing) | BL-13 audit: **one slice**. Phase 8 proper: **several slices** |
Packaging (Phase 9) | a working CLI with three modes | PyInstaller + Inno Setup, tray icon, uninstall-with-memory flow, Windows Credential Manager for the API key | **several slices**; the tray icon and credential move are each **one slice** |
Dashboard | 24 lines of placeholders | all of it | **several slices** — and it is 1 of the 8 frozen components |

---

## 5. A defensible order for what remains

Ranked by the owner's own criterion: **does this materially move the whole companion toward something
they would open every morning, or is it optimising an edge case?** Not by architectural completeness,
and not by frozen phase numbers.

The ordering principle that falls out of §3 is one sentence: **finish the things that are built before
building new things, because the project's most repeated failure is building well and wiring nothing.**

### Tier A — finish what exists (highest value per hour, lowest risk)

| # | Work | Why it ranks here | Depends on |
|---|---|---|---|
A1 | **Land the uncommitted slice properly**: run the 5-mutant proof, run the full suite, commit | It is the only thing standing between the owner and a companion that remembers anything at all. Leaving validated-but-uncommitted work in the tree is also how mutant residue has twice survived a session | — |
A2 | **Make the Phase 4 write side reachable**: teach an alias, set a relationship, export, restore, wipe | Four of these are **frozen Phase 4 Done-when capabilities that the product cannot perform**. Export/restore first: it is the only one whose absence is *irreversible* — there is no backup of the memory the owner is now starting to build | A1 |
A3 | **Fix the five machine-state-dependent tests** (D-10) | While the suite's result depends on an open Notepad window and a non-empty clipboard, it cannot gate anything. Cheap, and it unblocks every later "is the tree green?" judgement | — |
A4 | **BL-13: audit what the Brain actually sends as `risk_floor`**, and decide whether the confirmation should carry the Brain's own `why` | Confirmations are the project's main safety surface. An escalation the owner cannot judge erodes the value of every confirmation, and this is an audit plus a wording change, not a redesign | — |
A5 | **A desktop/Start shortcut that launches the typed console** (DEC-4) | The cheapest item in this document — hours, a `.lnk` and possibly a one-line launcher. It is what the owner asked for instead of Phase 9, and it removes the daily friction of typing a command | — |

### Tier B — the one large gap that gates several wants

| # | Work | Why | Depends on |
|---|---|---|---|
B1 | **BL-5: click/send outcome verification, DOM first** | This is D-9. It gates BL-6, workflow replay, and any honest message send. DOM first because a page's own state is readable. Its retry policy is part of the design, not an afterthought — "Delete" is the reason | A3 (needs a trustworthy suite) |
B2 | **BL-3/BL-4: duplicate-control disambiguation**, two separate audits (UIA, DOM) | The most direct dead end the owner actually hit. The UIA conclusion must not be re-applied to DOM without its own evidence | B1 is not strictly required, but they share the DOM resolution path |

### Tier C — the owner's stated want

| # | Work | Why in this order | Depends on |
|---|---|---|---|
C1 | **The WhatsApp Web read-only DOM experiment** (§4.4) | One session, read-only, and it either de-risks or kills the biggest want before any send code exists. It can be done *at any time* — it needs nothing from Tier A or B | the existing `real_browser` gate |
C2 | **Persistent dedicated browser profile** + its privacy review | Prerequisite for every logged-in site, not just WhatsApp | C1's result |
C3 | **DOM typing** | Prerequisite for composing a message | C2 |
C4 | **One message send, behind a Medium+ confirmation that shows recipient and text — to the owner's OWN number only** | The frozen Phase 10 Done-when needs 10 real sends in a row; this is the first. Per **DEC-3** it may ship before B1, with a fail-closed recipient allowlist of exactly one number instead of verification. Lifting that allowlist is its own later decision, taken when B1 lands | C3 (**not** B1, per DEC-3) |

### Tier D — deferred, with reasons

Phase 6 (pronouns/context) — valuable for the "friend" feeling, but BL-6 is the part the owner actually
hit, and BL-6 needs B1 first. Phase 7 (workflows) — inherits D-9 wholesale. Phase 9 (packaging) — see
the trade below. OCR/vision/coordinate fallback — BL-14, and the 2026-10-06 finding that no observed
failure was caused by not seeing the screen. Dashboard — real value for trust, no observed failure
attributable to its absence.

**Tier D changes under DEC-2:** Phase 6 and the voice limitations are no longer "deferred work" in the
same sense — voice typing and clicking are **out of scope permanently**, so Phase 2 Limitation B and
Phase 3 Limitation C are settled rather than pending.

### Where this order disagrees with the frozen phases

| Frozen order | This order | Why |
|---|---|---|
Phase 5 (all five observation layers) comes before Phase 6–10 | **Phase 5's remaining three layers are deferred below messaging** | No observed session failure was caused by the assistant being unable to see the screen. The owner's actual dead ends were authorisation, prompts, duplicate controls and click verification |
Phase 9 (packaging) comes before Phase 10 | **Packaging stays deferred**, with one exception noted below | A tray icon does not make the companion do anything new. But see Q4: if "open every morning" *means* not opening a terminal, this inverts |
Phase 10 is last | **Messaging is brought forward to Tier C**, and the read-only experiment to *now* | It is the owner's stated priority, and the experiment that would settle its feasibility is cheap, safe and independent of everything else |
Phase 4 is complete | **Phase 4 needs a Tier A slice** | Its Done-when capabilities exist but are unreachable from the product (§2). This is not reopening a frozen decision; it is finishing it |

### Frozen requirements this order chooses NOT to meet, and what that costs

1. **Phase 5's Done-when** — "the same target found via at least three different layers in three
   different apps/pages", and a vision call only firing when UIA, DOM and OCR all genuinely fail.
   **Not met, deliberately deferred.** Cost: the assistant cannot act in any application that exposes
   neither a UIA tree nor a DOM — Store apps, custom-drawn UIs, canvas-based web apps, anything in an
   image. The owner will hit this as a hard "I can't see that" wall, not a degraded experience.
2. **Phase 8's full per-action-type policy.** Cost: risk classification stays keyword-and-kind based
   with an advisory Brain floor. It fails safe, but it will keep over-confirming harmless things
   (BL-13's `open app powershell` reporting MEDIUM) and the owner keeps paying attention tax.
3. **Phase 9 entirely — the installer, the tray icon and the uninstall-with-memory flow — and therefore
   Build Plan Tier 1 / v1.0.** Replaced by a shortcut per **DEC-4**. Cost: the project cannot declare its
   own recommended finish line, and there is no uninstall path that offers to remove the memory file.
   **Chosen by the owner, not deferred.**
4. **Phase 10's Done-when** — 10 real sends in a row — would be met for *one* integration at best, and
   per **DEC-3** the first version sends **unverified, and only to the owner's own number**. So the
   frozen Done-when is not met by C4: ten sends to oneself is a smoke test, not the frozen criterion.
   Cost accepted knowingly: messaging arrives much sooner, and nobody else can receive a message the
   assistant cannot confirm it sent.
5. **The Dashboard**, one of the eight frozen components. Cost: no single place to see status, logs and
   settings, and no way to inspect or clear learned corrections — which Build Plan 6.6 specifically asks
   for so a wrong learned fix cannot quietly become permanent.

---

## 6. My recommendations

Everything in this section is **mine**, not something the owner confirmed.

**[MY RECOMMENDATION 1] — Do A1 and A2 before anything else.** The companion currently cannot remember
anything in committed code, and there is no way to back up what it does remember. Both are small. This
is also the single cheapest way to stop the §3 D-1 pattern from repeating.

**[MY RECOMMENDATION 2] — Run the WhatsApp read-only DOM experiment (C1) now, in parallel.** It is
read-only, it is gated by machinery that already exists, it costs one session, and it either de-risks
the owner's biggest want or tells them it is harder than hoped — before any effort is spent on a
persistent profile or DOM typing. This is the highest information-per-hour item in the whole project.

**[MY RECOMMENDATION 3] — Do Slack before WhatsApp if the goal is "it can message people".** An
official API gives a real send confirmation, needs no browser, no persistent profile, no DOM typing, and
does not depend on BL-5 at all. It would turn "messaging" from several slices into roughly one, and it
would prove the whole Planner→Safety→send→confirm path end to end, which WhatsApp can then reuse. If the
goal is specifically *WhatsApp*, this does not apply — the owner said WhatsApp is what they want, and
that is their call to make, not mine.

**[MY RECOMMENDATION 4] — Treat BL-5 as a prerequisite for sending, not for clicking.** Shipping
unverified clicks was the right trade; shipping an unverified *send* to a real person is not the same
decision, and I would not ship C4 without B1.

**[MY RECOMMENDATION 5] — Fix the five machine-state tests (A3) before the next full-suite gate.** Two
separate reboots/desktop states have now produced "failures" that cost a session's worth of
investigation each to prove innocent.

**[MY PARAGRAPH ON A PAST DECISION I THINK IS NOW WRONG]** — as §6 of the brief allows, one paragraph,
evidence, decision left to the owner. **The nine persistence-only memory structures should not have
been created as tables before anything could write to them.** Phase 4 built fifteen tables because
Build Plan 6.8 lists fifteen categories, and six got real APIs while nine got schema, export, restore,
delete and nothing else. The evidence that this was the wrong order is the §2 inventory: the project now
carries a schema, migration surface, export format and test burden for `Habits`, `Workflows`,
`Permissions` and six more, none of which any code path can fill, while the *features the owner actually
asked for* — teach an alias, remember a contact — were the ones left unwired. I would not propose
deleting them now; the cost is sunk and they are harmless. I raise it because the same instinct
("implement the full frozen list first, wire it later") is what Phase 6, 7 and 10 will invite, and
resisting it is worth more than any table.

---

## 7. The four decisions — ANSWERED BY THE OWNER, 2026-10-10

Four questions were asked where the answer changes the architecture or the order. All four are answered,
and the answers are recorded here because §5's order depends on them.

### DEC-1 — WhatsApp: **run the read-only experiment first.** Profile and ToS deferred.

No dedicated profile is created and no ToS decision is taken yet. **C1 (the read-only `dom_query`
against WhatsApp Web) happens before C2**, and its result informs both. D-7's documented privacy
decision therefore stands untouched for now.

*Consequence:* C1 needs a logged-in WhatsApp Web session to read, which a profile-less context cannot
hold — so the experiment needs a **one-off, throwaway** logged-in session that is not kept afterwards,
or it needs the profile it was meant to precede. **This is a real wrinkle to settle at the top of C1,
not a blocker:** the honest shape is a temporary `user_data_dir` under a scratch path, scanned once,
read once, deleted — never installed as the assistant's standing profile. It is still read-only and
still sends nothing.

### DEC-2 — Voice: **simple commands only.** Permanent scope, not deferred work.

`open` and `close` by voice stay exactly as they work today. **No investment in typing or clicking from
voice.** This converts two open limitations into settled scope:

* Phase 2 Limitation B (voice `TYPE_TEXT` targets the console window) — **closed as out of scope**, not
  pending. The focus hand-over redesign it asked for is not happening.
* Phase 3 Limitation C (voice may plan only `open_app`/`close_app`) — **this is now the intended design**,
  not a restriction awaiting removal.
* D-4 (three scripts) still matters for what voice *does* handle, but it no longer has to carry typing
  or clicking.

### DEC-3 — First message send: **unverified is acceptable, but only to the owner's own number.**

C4 may ship **before** B1, with the blast radius bounded instead of the verification gap closed.

*Consequence, and it is a new design requirement that did not exist before this answer:* the send path
must enforce a **recipient allowlist containing only the owner's own number**, and it must do so
**fail-closed and structurally** — refusing any recipient it cannot positively match, rather than
checking a flag. Until B1 lands, a send to anyone else is refused, not confirmed. When BL-5 lands, the
allowlist is what gets lifted, deliberately and as its own decision.

### DEC-4 — Packaging: **a shortcut is enough.** No installer, no tray icon.

A desktop or Start-menu shortcut that launches the typed console without typing a command. **Hours, not
slices** — it is a `.lnk` plus possibly a one-line launcher script, and it needs no PyInstaller, no Inno
Setup and no uninstall flow.

*Consequence:* Phase 9 stays deferred and **Build Plan Tier 1 / v1.0 remains out of reach by the owner's
explicit choice**, which is now a recorded decision rather than an unmet obligation. The shortcut is
added to Tier A as **A5** because it is the cheapest item in the whole document.

---

## 8. Appendix — the A1 owner smoke, for an empty machine

Added 2026-10-10 as part of A1. **These commands have NOT been run by the assistant:** running them
would create the real `data/memory.db` and act outside the repository's protected pytest root, which the
test-safety rule forbids. The console lines are the same ones the offline tests drive, and the ledger
query was verified read-only against the real ledger (103 rows at the time of writing). Everything else
below is an expectation to check, not an observation.

**Precondition — an empty machine.** `data/memory.db` must not exist. It does not today; confirm with:

```powershell
Test-Path .\data\memory.db
```

### Step 1 — ledger row count BEFORE

Zero provider calls is the claim, and this is the proof. The ledger is `data/claude_usage.db`, the table
is **`claude_requests`**, and a row is written when a request is *authorized* — so a provider call
cannot happen without the count moving.

```powershell
.\venv\Scripts\python.exe -c "import sqlite3; print('ledger rows BEFORE:', sqlite3.connect('data/claude_usage.db').execute('SELECT COUNT(*) FROM claude_requests').fetchone()[0])"
```

### Step 2 — the console session

```powershell
.\venv\Scripts\python.exe main.py --console
```

Then type these eight lines, one at a time, at the `> ` prompt:

| # | Type this | Expect |
|---|---|---|
1 | `start fresh memory` | `Memory is ready: a new, empty database with all 15 structures, at …` — and it says it lives on this machine |
2 | `remember Ali's whatsapp is +92-300-0000001` | `Remembered: Ali's whatsapp. It stays on this machine - I didn't send it anywhere.` |
3 | `what is Ali's whatsapp` | `Ali's whatsapp: +92-300-0000001` |
4 | `remember Ali's whatsapp is +92-300-0000001` | **The duplicate.** The *same* `Remembered:` line again — deliberately, because the intended state is stored. The proof that nothing was duplicated is the row count in step 3 below, not the message |
5 | `remember Ali's whatsapp is +92-300-0000002` | **The conflict.** `I already have a different whatsapp for Ali, so I haven't stored this one. I won't overwrite what you told me before, and I won't keep two.` Note it names the **channel** and not the stored number — the write path applies no disclosure rule, so it may not disclose |
6 | `Sara ka whatsapp +92-300-0000003 yaad rakho` | **Roman Urdu, marker at the END.** `Remembered: Sara's whatsapp. …` This is the SOV case (D-5); a leading-only guard would have sent this number to the provider |
7 | `what is Sara's whatsapp` | `Sara's whatsapp: +92-300-0000003` |
8 | `remember that my work folder is C:\Users\Wajid\work` | **The work-folder refusal.** `I keep anything you ask me to remember on this machine, so I did NOT send that to the reasoning service - and I couldn't work out what to store from it, so I've stored nothing either.` The path is refused locally and not transmitted |

Then `exit`.

### Step 3 — what is actually on disk, and the ledger count AFTER

```powershell
.\venv\Scripts\python.exe -c "import sqlite3; c = sqlite3.connect('data/memory.db'); print('people:', c.execute('SELECT id, name FROM people ORDER BY id').fetchall()); print('contacts:', c.execute('SELECT person_id, channel, address FROM contacts ORDER BY id').fetchall())"
```

```powershell
.\venv\Scripts\python.exe -c "import sqlite3; print('ledger rows AFTER:', sqlite3.connect('data/claude_usage.db').execute('SELECT COUNT(*) FROM claude_requests').fetchone()[0])"
```

**Pass conditions:**

* `people` holds **exactly two** rows — Ali and Sara. Three would mean the duplicate created a second
  person, which is the failure `add_person`'s never-merge behaviour makes possible.
* `contacts` holds **exactly two** rows — `…0001` and `…0003`. `…0002` must be absent (the conflict was
  refused) and `…0001` must appear once (the duplicate stored nothing).
* **`ledger rows AFTER` equals `ledger rows BEFORE`.** Every one of the eight lines was answered locally;
  if this number moved, something reached the provider and the slice's central claim is wrong.

### Optional — prove the refusals do not destroy anything

```powershell
.\venv\Scripts\python.exe main.py --console
```

Type `start fresh memory` once more. Expect `I already have a memory database, so I've left it exactly as
it is. Nothing was created, changed or deleted.` Then re-run the step 3 queries: both counts must be
unchanged.

---

## 9. Standing constraints this document does not touch

Everything in `owner-testing-improvement-backlog.md` Section 9 remains in force, in particular: this
document **authorizes nothing**; parked Slice 4 resumes as named `type_text` **only**; Phase 5 is **not**
complete; no competing memory subsystem; the ambiguity rule is not weakened and the first matching
element is not silently chosen; the prompt-state and Safety fixes are not undone; the UIA ordering
conclusion is not re-applied to DOM; a usable page is not a verified navigation; and a potentially
destructive action is never repeated to find out whether it worked.
