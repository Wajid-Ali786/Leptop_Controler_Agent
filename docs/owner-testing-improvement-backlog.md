# Owner Testing — Improvement Backlog

**Status: BACKLOG / DEFERRED** — opened 2026-10-08, after owner real-machine testing of usability
Slices 1–5.

**THIS FILE AUTHORIZES NOTHING.** It is a record of what the owner observed, what it means, and what
could be done about it. Nothing here is a work instruction, and nothing here may trigger
implementation. Work resumes only when the owner explicitly authorizes improvement work, and naming
an item here is not that authorization.

**The frozen planning documents are unchanged and remain the source of truth:** Master Plan v0.1,
`step1-feasibility_risk.md`, `step2-dev-environment.md`, `step3-workspace-architecture.md`,
`step4-production-development.md`, `build-plan.md`. Nothing in this file overrides them. The phase
closeout records (`phase2-closeout.md`, `phase3-closeout.md`, `phase4-closeout.md`) are likewise
unchanged.

---

## How to read this file

Every finding carries the same block:

| Field | Meaning |
|---|---|
**Evidence** | `OWNER-OBSERVED` (the owner saw it on the real machine) · `CODE` (read from the source) · `PRIOR REPORT` (established in an earlier audit or slice report) · `INFERENCE` (reasoning, not observation) |
**Status** | `NOT STARTED` · `UNDER INVESTIGATION` · `RESOLVED` |
**Priority** | a suggestion only; the owner sets the order |

**An inference is never written as an observed defect.** Where a cause is a hypothesis it is marked
`UNCONFIRMED` in the finding itself. Nothing is marked `RESOLVED` without stated supporting evidence.

---

## Section 0 — Development checkpoint (as of 2026-10-08)

| Slice | What it was | State |
|---|---|---|
Slice 1 | console prompt states | **COMPLETE** |
Slice 2 | available-and-in-front app opening | **COMPLETE** |
Slice 3 | per-capability click authorization | **COMPLETE** |
Slice 3 follow-up | correction-order fix | **COMPLETE** |
Slice 4 | named targets for type/refresh/scroll/shortcut | **PARKED, not cancelled** — resumes as `type_text` **only** |
Slice 5 | assistant-browser navigation | **COMPLETE** |

**Exact tree state, so a later session does not re-derive it:**

```
4427 passed, 48 skipped, 0 failed          (4475 collected)
Slice 5 mutation proof: 17/17 caught
Restoration verified by content: ten mutant markers grep to 0,
                                 five load-bearing lines intact
```

**PHASE 5 IS NOT COMPLETE.** Two of the five frozen observation layers exist (UIA and DOM). The
frozen Done-when criteria are unmet. See Section 6.

---

## Section 1 — WHAT NOW WORKS (confirmed on the real machine)

**Recorded first and deliberately.** A future session must not spend turns re-proving any of these.
All six are `OWNER-OBSERVED` on the real machine, not inferred from tests.

| # | Confirmed working | Note |
|---|---|---|
**W-1** | `open assistant browser` → `open this site <url>` → `click X in assistant browser` → `close assistant browser`, all four succeeded | **Slice 5's whole purpose. The three-command browser workflow WORKS.** |
**W-2** | `open assistant browser and open <url>` produced a correct **two-step** plan and ran both steps | multi-step planning from one sentence works |
**W-3** | `click Delete in assistant browser 2 times` produced a correct **two-step** plan | repetition is understood and planned, not guessed |
**W-4** | a new command typed at the plan prompt cancelled the plan and ran instead | Slice 1's rule, confirmed on real hardware |
**W-5** | the duplicate-control refusal fired correctly and **clicked nothing** | the refusal half of BL-5 works; only the recovery is missing |
**W-6** | `https://example.com` navigated with **no timeout** | navigation itself is sound; BL-1 is about readiness criteria, not about navigation being broken |

**W-2 vs BL-10 is a distinction worth preserving:** explicit multi-step planning works (W-2);
*remembered-site reference resolution* has never been demonstrated (BL-10). They are not the same
capability and a future session must not treat W-2 as evidence for BL-10.

---

## Section 2 — OBSERVED DEFECTS

### BL-1 — A slow website may exceed the navigation bound (KNOWN LIMITATION, closed 2026-10-09)

**RECLASSIFIED BY THE OWNER from a defect to a KNOWN LIMITATION. No further rounds.**

- **The limitation:** a slow website may exceed the navigation bound. The assistant then says
  honestly that it cannot confirm the page loaded, and **the page is still usable** — the owner
  verified this by clicking successfully on both failing sites. **The cost is a wait, not a
  failure.**
- **Evidence:** `OWNER-OBSERVED`. Measured DOMContentLoaded, in a **warm** browser:
  **herokuapp 30.9 s, smebluepages 15.4 s.** Main document only, via PowerShell: herokuapp 1.23 s,
  smebluepages 12.27 s, Wikipedia 1.15 s. In the assistant browser, Wikipedia and example.com
  navigated normally while herokuapp and smebluepages reported the 20-second bound.
- **User impact:** a wait on slow sites, followed by an honest "I can't confirm it loaded" and a
  page that can still be clicked in.
- **Status:** `KNOWN LIMITATION` — not a defect, not under investigation, and **not** to be reopened
  as another milestone, bound or measurement round.
- **What was changed while it was investigated:** the navigation milestone is `commit` in
  `app/executor/adapter.browser_navigate` (it was `load`, briefly `domcontentloaded`), bounded by
  `browser.navigate_timeout_seconds`, which is unchanged at 20.0. The honest timeout message is
  unchanged, and `networkidle` remains excluded — Playwright's own docstring marks it DISCOURAGED.
- **The one rule that must survive:** **do not conflate a usable page with a verified successful
  navigation.** The result stays `Outcome.UNVERIFIED` and tells the owner to look at the page.

### BL-2 — A page is sometimes unusable immediately and usable later

- **Evidence:** `OWNER-OBSERVED` — the page was sometimes not usable immediately but became usable
  later, including after a refresh.
- **Expected:** once navigation reports completion, the page is ready to be acted on.
- **Actual:** readiness and reported completion do not coincide.
- **User impact:** a click after a navigation can fail for reasons that have nothing to do with the
  click.
- **Suspected cause, UNCONFIRMED:** the same root as BL-1 — the completion signal is measuring the
  wrong thing. May also interact with the DOM readiness wait (`browser.query_timeout_seconds`), which
  the 2026-10-05 fix bounded but which is a *query* wait, not a *page* wait.
- **Proposed direction:** treat alongside BL-1; these are probably one investigation, not two.
- **Priority:** HIGH (with BL-1)
- **Status:** `UNDER INVESTIGATION`
- **Future real-machine acceptance test:** navigate, then immediately issue a named click with no
  manual wait and no refresh, on three different third-party pages.

### BL-3 — `click first Delete` is read as a literal control name

- **Evidence:** `OWNER-OBSERVED` — after several Add Element operations two Delete buttons were
  present; `click Delete` correctly refused (W-5); `click first Delete` then failed because
  *"first Delete"* was taken as the control's name.
- **Expected:** having been told two controls match, the user has some way to proceed.
- **Actual:** there is no way to proceed. The refusal is correct and the recovery does not exist.
- **User impact:** a dead end. The user must act by hand, which is the exact failure mode the
  2026-10-06 weakness audit identified as moving risk somewhere that cannot be confirmed or logged.
- **Suspected cause:** `CODE` — matching is whole-string against the user's words, so an ordinal
  prefix becomes part of the name. No selection vocabulary exists.
- **Proposed direction:** see BL-4 for the decision that has to come first. **Do not silently choose
  the first matching element.**
- **Priority:** HIGH — it is the most direct dead end the owner hit.
- **Status:** `NOT STARTED`
- **Future real-machine acceptance test:** with two identically-named controls present, a stated
  disambiguation command acts on the intended one, and a wrong or ambiguous one still refuses and
  clicks nothing.

---

## Section 3 — MISSING CAPABILITIES

### BL-4 — Duplicate-control disambiguation: UIA and DOM must be audited SEPARATELY

- **Evidence:** `PRIOR REPORT` + `OWNER-OBSERVED` (BL-3).
- **The point of this entry:** the 2026-10-07 audit concluded that candidate ordering is **not stable
  enough to select by index**, and it reached that conclusion **about UIA** — from pywinauto's own
  documentation that `runtime_id` "may be different from run to run" and from UIA `FindAll` having no
  documented stable order. **That conclusion does not automatically transfer to DOM.** Playwright has
  its own ordering semantics (document order, `nth()`, locator resolution) which were **not** examined
  and must be re-audited on their own evidence.
- **Expected:** a disambiguation design appropriate to each layer.
- **Actual:** one audit exists, it covers one layer, and its conclusion risks being over-applied.
- **User impact:** if DOM is assumed to share UIA's instability, a safe and simple DOM solution may be
  rejected for no reason; if UIA is assumed to share DOM's stability, an unsafe index selection may be
  built.
- **Proposed direction:** two audits, reported separately. For UIA, the 2026-10-07 conclusion stands
  unless new evidence appears: offer a choice only where candidates differ on the existing identity
  fields, and keep refusing when they are structurally identical. For DOM, start from Playwright's
  documented ordering and re-derive. **Do not turn candidate position or bounds into a
  coordinate-click mechanism.**
- **Priority:** HIGH (gates BL-3)
- **Status:** `NOT STARTED`
- **Future real-machine acceptance test:** the BL-3 test, run once against a DOM page and once against
  a native UIA app with two identically-named controls.

### BL-5 — Click outcome verification and safe recovery

- **Evidence:** `CODE` (DOM clicks return `Outcome.UNVERIFIED` by construction) +
  `OWNER-OBSERVED` (an Add Element click sometimes did not produce the expected visible result).
- **Expected:** the assistant can tell whether a click achieved anything, and can recover safely when
  it did not.
- **Actual:** every click is `UNVERIFIED`. The honest message says so, but nothing distinguishes
  "dispatched and worked" from "dispatched and did nothing".
- **User impact:** the owner cannot trust a reported click, and the assistant cannot help when one
  silently fails.
- **Suspected cause:** `CODE` — by design. A click is *sent*; its effect was never observable. The
  2026-10-06 audit recorded this as a genuine limit (register item 32), possibly a phase of work.
- **Proposed direction:** investigate what a *verifiable* post-click observation would be for the DOM
  layer specifically (where a page's own state is readable, unlike UIA). **Never blindly repeat a
  potentially destructive action** — a retry policy is part of this design, not an afterthought, and
  "Delete" is the obvious reason why.
- **Priority:** MEDIUM–HIGH — high value, genuinely large, and it interacts with BL-6.
- **Status:** `NOT STARTED`
- **Future real-machine acceptance test:** a click that demonstrably works reports a verified outcome;
  a click that demonstrably does nothing reports that distinctly, and nothing is retried automatically.

### BL-6 — Conversation and feedback understanding

- **Evidence:** `OWNER-OBSERVED` — `add element click nahi huwa` ("the add element click didn't
  happen") was not understood as feedback about the preceding action.
- **Expected:** a follow-up that reports a failure is connected to the action it refers to.
- **Actual:** it is treated as an unrelated new request.
- **User impact:** the owner cannot tell the assistant that something went wrong in the way a person
  naturally would, in any of the three languages they use.
- **Suspected cause:** `CODE` — short-term context carries only a narrow `PreviousActionContext`
  (kind and a safe target for a *verified* action). Broad conversational context is Phase 6 and was
  never built; `_remember()` deliberately keeps nothing for `UNVERIFIED` results, which is exactly the
  case the owner was reporting on.
- **Proposed direction:** the assistant must connect corrections, failure reports and retries to
  recent actions, and must **distinguish successfully executed, failed and UNVERIFIED actions** when
  doing so. **A natural-language follow-up must never blindly replay a previous action.** Note the
  dependency: without BL-5 the assistant cannot know whether the owner's report is correct, so this
  and BL-5 are related but not the same work.
- **Priority:** MEDIUM–HIGH
- **Status:** `NOT STARTED`
- **Future real-machine acceptance test:** after an action that did nothing, a plain-language failure
  report in English and in Roman Urdu is recognised as being about that action, and produces a
  question or a corrected plan rather than a blind repeat.

### BL-7 — Human-like memory and the companion experience

- **Evidence:** owner-stated goal (`OWNER-OBSERVED` intent, not a defect) + one observed failure
  (BL-10).
- **The goal, in the owner's framing:** a desktop AI companion that feels like a helpful friend, not
  an isolated command executor.
- **Capabilities in scope when this resumes:** working memory for recent commands, targets and
  results; short-term conversational context across turns; episodic memory for relevant past
  activity; remembering previously used websites where appropriate; long-term memory for preferences,
  people, projects, routines and corrections; learning from explicit corrections; natural English,
  Urdu and Roman Urdu interaction.
- **MANDATORY CONSTRAINT:** the **Phase 4 Memory foundation already exists** — fifteen structures, of
  which nine are persistence-oriented only. **Audit and reuse its architecture. Do NOT create a
  competing memory subsystem.** See `phase4-closeout.md`.
- **PRIVACY CONSTRAINT:** browsing history and personal information may be persisted **only** under
  the existing privacy and retention rules. Phase 4's standing rule is that Memory data is never sent
  to Claude and that any future egress needs its own privacy review. Remembered websites are exactly
  the kind of data that rule exists for.
- **Priority:** MEDIUM — this is the owner's long-term goal and is a phase of work, not a slice.
- **Status:** `NOT STARTED`
- **Future real-machine acceptance test:** to be defined with the owner when this resumes; a single
  acceptance test cannot cover a phase.

### BL-8 — Named `refresh` of the assistant browser still needs manual window switching

- **Evidence:** `OWNER-OBSERVED` — refreshing the assistant browser through the generic `refresh`
  command still required manual window switching.
- **Expected:** a named target, as `click_target` has since Slice 3.
- **Actual:** `refresh` is in `HANDS_OVER`, so the console asks the owner to switch windows.
- **User impact:** the Alt+Tab the usability plan set out to remove is still present for this action.
- **Suspected cause:** `CODE` — Slice 4 is parked.
- **Proposed direction:** **Slice 4 is PARKED, not cancelled, and MUST NOT be silently resumed.** Its
  three measured findings are already recorded in `CLAUDE.md` so a later session resumes from them
  rather than re-deriving: (a) the prompt-budget restructuring that fits, (b) the wire cost is zero,
  (c) the real cost — `_focus_identity` includes the focused **control**, which cannot be known before
  activation on a named path, making each kind surgery on its own prepare→run seam. **The agreed scope
  when it resumes is named `type_text` ONLY**, followed by a *fresh* decision on refresh, scroll and
  shortcut.
- **Priority:** MEDIUM (owner-set: `type_text` is the one actually hit; refresh/scroll/shortcut are a
  smaller want than originally implied)
- **Status:** `NOT STARTED` (parked)
- **Future real-machine acceptance test:** `type <text> in <app>` acts in the named window with no
  manual switch, refuses honestly when no safe typing destination can be established, and every other
  kind's hand-over is unchanged.

---

## Section 4 — KNOWN LIMITS (working as designed, recorded so they are not mistaken for defects)

### BL-9 — An unqualified `click X` needs qualifying in a mixed session

- **Evidence:** `PRIOR REPORT` (Slice 5 context-chooser audit) + `CODE`.
- **The limit:** after navigating, an unqualified `click X` reaches the assistant browser **only when
  nothing else was opened in that session**. If `open chrome` happened in the same session, the click
  **refuses** and `in assistant browser` is required.
- **This is the existing ambiguity rule working as designed, not a defect.** It was audited during
  Slice 5 and deliberately **not** weakened to make the workflow read nicely.
- **User impact, stated plainly:** the three-command workflow does **not** read as advertised in a
  mixed session. W-1 was observed in a session where the assistant browser was the only context.
- **Proposed direction:** none required. If it later becomes annoying enough to change, the change is
  a *selection* design (which context the user means), not a loosening of the ambiguity rule.
- **Priority:** LOW
- **Status:** `NOT STARTED` (no action proposed)
- **Future real-machine acceptance test:** in one session, `open chrome` then the three-command
  browser workflow; the unqualified click refuses and names `in assistant browser`, and the qualified
  form succeeds.

### BL-10 — Remembered-site reference resolution has never been demonstrated

- **Evidence:** `OWNER-OBSERVED` —
  `open assistant browser and open the internet herokuapp site that i was open sometime ago`
  was **not understood**. The same request **with the explicit URL** produced a correct two-step plan
  (W-2).
- **Expected (eventually):** a reference to a previously used site resolves to that site.
- **Actual:** not understood. There is no site memory to resolve against, and the Brain is instructed
  never to invent or complete an address — correctly, since Slice 5's URL-provenance check would
  refuse an address the owner did not supply.
- **User impact:** the owner must keep URLs to hand.
- **Proposed direction:** part of BL-7. Note the interaction that must be designed deliberately, not
  stumbled into: Slice 5 requires a navigation address to appear in **what the user typed**. A
  remembered site would be an address the user did *not* type in that command, so site memory and the
  provenance rule meet head-on. Whatever resolves that must keep provenance meaningful — a remembered
  URL is still not a model-invented one, but the current check cannot tell them apart.
- **Priority:** MEDIUM (with BL-7)
- **Status:** `NOT STARTED`
- **Future real-machine acceptance test:** a site used earlier in the session is reachable by
  reference; a site never used is not guessed; and a model-invented address is still refused.

### BL-11 — Generic foreground actions cannot distinguish the two Chromes

- **Evidence:** `PRIOR REPORT` — already recorded in `CLAUDE.md` as a known limitation.
- **The limit:** `refresh`, `scroll`, `shortcut` and `window_control` cannot tell the owner's personal
  Chrome from the assistant's Chrome: both are `chrome.exe` + `Chrome_WidgetWin_1`, and refresh
  identity deliberately excludes the title.
- **Mitigation in force:** the owner's own foreground selection plus the existing Safety confirmation
  remains authoritative. `window_control close` still refuses the assistant browser, which has no
  ownership token.
- **Priority:** LOW
- **Status:** `NOT STARTED` (recorded, deliberately not fixed)
- **Future real-machine acceptance test:** with both browsers open, a generic foreground action acts
  on whichever the owner put in front, and the confirmation names a window they can recognise.

---

## Section 5 — DESIRED IMPROVEMENTS

### BL-12 — Correction-prompt UX: "Left it there." reads as confusing

- **Evidence:** `OWNER-OBSERVED`.
- **What works and must not regress:** a new command supplied at a correction prompt **does** reach
  the outer loop and runs once (Slice 1's handoff, confirmed in W-4 and in the Slice 3 follow-up).
- **Actual:** the intermediate `Left it there.` can still read as confusing.
- **User impact:** cosmetic but trust-affecting — the owner is unsure what just happened.
- **Proposed direction:** future work should **distinguish a correction to the previous intent from an
  entirely new command**, and word the closing line accordingly. **Do NOT undo the existing
  prompt-state or Safety fixes** — specifically: the reason is reported before the correction prompt;
  `NO_CORRECTION_AFTER` stays `{DENIED, STOPPED, NOT_HANDED_OVER}`; the Medium-and-above confirmation
  keeps its yes-only contract; and the closing line must not re-print the failure reason.
- **Priority:** LOW–MEDIUM
- **Status:** `NOT STARTED`
- **Future real-machine acceptance test:** at a correction prompt, prose produces a corrected plan and
  a command produces that command, and in each case the closing line says which happened.

---

## Section 6 — DEFERRED ARCHITECTURAL DECISIONS

### BL-13 — Does the Brain raise risk floors sensibly? (second sighting)

- **Evidence:** `OWNER-OBSERVED`, twice.
  ```
  > click Delete in assistant browser
  Needs your OK - HIGH risk (the reasoning step asked for extra care)
  ```
  The first sighting was `open app powershell` reporting MEDIUM for a plain app launch (which led to
  the prompt guidance added in the pre-Phase-5 usability work).
- **Expected:** either the floor is justified and the reason says why, or it is not raised.
- **Actual:** the Brain raised the floor to HIGH and the reason string says nothing about why.
- **Honest framing, which matters here:** for a control named "Delete" a raised floor **may well be
  correct**. The problem is **not** that this instance was wrong. The problem is that **nobody has
  established whether the Brain raises floors sensibly or arbitrarily**, and the reason string gives
  the owner nothing to judge by. The mechanism is sound by construction — `risk_floor` is advisory and
  can only ever *raise*, never lower, and Safety remains authoritative.
- **User impact:** the owner cannot tell a well-judged escalation from a random one, which over time
  erodes the value of the confirmation itself.
- **Proposed direction:** audit what the Brain actually sends as `risk_floor` across a sample of
  commands, and decide whether the reason string should carry the Brain's **own** justification
  (it already produces a `why` per intent). Any change must keep the raise-only contract intact.
- **Priority:** MEDIUM
- **Status:** `UNDER INVESTIGATION` — **explicitly not classified as a defect.**
- **Future real-machine acceptance test:** across a stated sample of commands, every raised floor is
  either explained in the confirmation or not raised.

### BL-14 — Frozen Phase 5 gaps are still outstanding obligations

- **Evidence:** `PRIOR REPORT` + frozen `build-plan.md` / `step4-production-development.md`.
- **Still outstanding:** OCR, Vision, coordinate fallback, cross-layer recovery, and the original
  Phase 5 Done-when criteria. Two of five observation layers exist (UIA, DOM).
- **These are deferred obligations, not completed features.** Phase 5 must not be described as
  complete.
- **Proposed direction:** **do NOT reprioritize them merely because they are frozen requirements.**
  When work resumes, assess them against daily-use value, as the 2026-10-06 weakness audit did — that
  audit found that **none** of the owner's fifteen observed session failures was caused by the
  assistant being unable to *see* the screen, and ranked these below the authorization and prompt
  work that has since been done. That ranking is a finding to weigh, not a decision to skip them.
- **Priority:** DEFERRED — owner's call, weighed against daily-use value
- **Status:** `NOT STARTED`
- **Future real-machine acceptance test:** the frozen Phase 5 Done-when criteria, unchanged.

---

## Section 7 — RESOLVED, with the evidence

### BL-15 — Slice 5 regression debt: three failing tests (RESOLVED)

The owner previously observed **4424 passed / 3 failed / 48 skipped**. All three are fixed, and the
tree is now **4427 passed / 48 skipped / 0 failed**. The way the third was fixed matters and is
recorded in full so nobody later mistakes it for a loosened test.

| | Test | Nature | What was done |
|---|---|---|---|
1 | `test_console.py::test_every_action_kind_is_classified_for_hand_over` | **bookkeeping** | `navigate` added to the `NO_HANDOVER` set. It acts on the assistant's own browser session, addressed by an opaque session id, not by whichever window is in front |
2 | `test_executor_risk_floor.py::test_the_advisory_floor_is_not_part_of_the_action_contract` | **bookkeeping** | `ExecutorAction` gained `url`, so the expected field list grew. An explicit `risk_floor not in fields` assertion was **added**, so the test's real point is now tested directly rather than implied by the exact field list |
3 | `test_dom_observation.py::test_the_ordinary_suite_reaches_no_browser_and_no_network` | **NOT bookkeeping — REWRITTEN** | see below |

**#3 in full, because the distinction is the point.** Its **name** is a boundary guarantee — the
ordinary suite must reach no browser and no network. Its **mechanism** was a substring scan asserting
that navigation code did not exist anywhere in the Executor adapter (`"page.goto" not in code`), with
the comment *"Slice 1 needs no navigation"*. That was true while the DOM layer only **read** pages.
Slice 5 adds navigation **deliberately**, so the mechanism's premise was superseded while the
guarantee in its name was not.

It was **rewritten to the enduring invariant**:

- navigation exists in **exactly one** function (`code.count("page.goto") == 1`);
- that one occurrence is inside `browser_navigate`;
- `browser_navigate` is in `safety_guards.BROWSER_BOUNDARIES`, so the central guard refuses it offline;
- **no other kind of outbound request** appeared alongside it (`request.get`, `api_request`,
  `urlopen`, `requests.`, `httpx` all absent).

**This is a strengthening, not a loosening.** Supporting evidence, independent of the rewrite:
Slice 5's mutation proof includes *"navigation escapes the central test guard"*, which is caught by
`test_an_unguarded_navigation_attempt_is_refused` — a test that deliberately does **not** install the
fake adapter, calls the real `browser_navigate`, and requires `PhysicalBrowserEscaped` to be raised.
The guard was proved by **firing it**, not by assuming it. Separately, adding `browser_navigate` to
the central boundary list caused an **existing** parametrized test to gain a case by itself
(`test_an_ordinary_test_cannot_reach_a_browser_boundary[browser_navigate]`), which is evidence the new
primitive went into the shared guard rather than into a special case.

- **Status:** `RESOLVED` — evidence: full suite 4427 passed / 48 skipped / 0 failed; 17/17 mutants
  caught; restoration verified by content.

---

## Section 8 — Future real-machine acceptance tests, collected

Gathered from the findings above so a later session can plan a single testing session rather than
re-reading the whole file. None of these exists yet.

| For | Test |
|---|---|
BL-1 / BL-2 | navigate to the Herokuapp Add/Remove Elements demo and two other third-party pages; each reports confirmed completion within the bound, and an immediate named click succeeds with no manual refresh |
BL-3 / BL-4 | with two identically-named controls, a stated disambiguation acts on the intended one; an ambiguous one still refuses and clicks nothing — run once against DOM, once against a native UIA app |
BL-5 | a click that works reports a verified outcome; a click that does nothing reports that distinctly; nothing is retried automatically |
BL-6 | a plain-language failure report, in English and in Roman Urdu, is recognised as being about the preceding action and never triggers a blind repeat |
BL-8 | `type <text> in <app>` acts in the named window with no manual switch, refuses when no safe typing destination can be established, and leaves every other kind's hand-over unchanged |
BL-9 | in one session, `open chrome` then the browser workflow: the unqualified click refuses and names `in assistant browser`; the qualified form succeeds |
BL-10 | a site used earlier in the session is reachable by reference; a site never used is not guessed; a model-invented address is still refused |
BL-11 | with both browsers open, a generic foreground action acts on whichever the owner put in front |
BL-12 | at a correction prompt, prose yields a corrected plan and a command yields that command, and the closing line says which happened |
BL-13 | across a stated sample of commands, every raised floor is either explained in the confirmation or not raised |
BL-14 | the frozen Phase 5 Done-when criteria, unchanged |

---

## Section 9 — Standing constraints for whoever resumes this

1. **This file authorizes nothing.** Improvement work resumes only on the owner's explicit
   instruction.
2. **Do not silently resume parked Slice 4.** Its scope on resumption is named `type_text` **only**.
3. **Do not declare Phase 5 complete.** Two of five observation layers exist; the frozen Done-when is
   unmet.
4. **Do not create a competing memory subsystem.** Phase 4's foundation exists; audit and reuse it.
5. **Do not weaken the ambiguity rule** to make a workflow read nicely (BL-9), and **do not silently
   choose the first matching element** (BL-3).
6. **Do not undo the prompt-state or Safety fixes** from Slices 1 and 3 and the correction-order
   follow-up (BL-12).
7. **Do not re-apply the UIA ordering conclusion to DOM** without a separate audit (BL-4).
8. **Do not treat a usable page as a verified navigation** (BL-1).
9. **Never repeat a potentially destructive action to test whether it worked** (BL-5).
10. The frozen planning documents and the phase closeout records are **unchanged** and remain the
    source of truth.
