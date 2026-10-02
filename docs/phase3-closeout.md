# Phase 3 — Brain + Planner: CLOSEOUT RECORD

**Status: COMPLETE / CLOSED** — closed 2026-10-02.

This document records what Phase 3 delivered, the evidence for it, and what it deliberately did not
deliver. It is a record, not a plan.

**The frozen planning documents are unchanged and remain the source of truth for what Phase 3
required:** Master Plan v0.1, `step1-feasibility_risk.md`, `step2-dev-environment.md`,
`step3-workspace-architecture.md`, `step4-production-development.md`. Nothing in this file overrides
them; Section 6 of frozen Step 4 is still the definition of done for this phase.

---

## 1. Frozen Build bullets — all delivered

| Frozen bullet (Step 4 Section 6) | Where |
|---|---|
Natural command understanding via Claude | `app/brain/` — `logic.route()` decides local vs. Brain from **resolvability**, not parseability; `adapter.py` is the only file importing `anthropic`; `interpreter.py` is the one place joining adapter, logic and settings, so `app/console.py` imports no adapter |
Structured intent extraction | `Understood` / `Intent` with typed `*Args` per kind (never a dict), `interpretation_schema()`, `validate_interpretation()`. Structured Outputs via `output_config={"format": {"type": "json_schema", …}}` |
Plan generation (numbered step list) | `app/planner/` — `Plan(steps)` of `PlanStep(number, action, why, risk_floor)`, `MAX_PLAN_STEPS = 5`. Semantic data only: a `Plan` carries no identity and no state |
Clarification questions when genuinely ambiguous | `NeedsClarification` → `PendingClarification`; **exactly one round** per root command, spent by asking |
Risk classification feeding Permission & Safety | `Intent.risk_floor` → `PlanStep.risk_floor` → `execute(…, risk_floor=…)` → `_with_advisory_floor()`. Advisory and one-directional: it may raise the level, never lower it |
Action selection into existing Executor capability | `ARGS_FOR_KIND` is a subset of the Executor's own `_PREPARERS`; `FrontEnd.may_plan` bounds each front end; anything else is `NotSupported` |
Context handling (short-term) | `PreviousActionContext` + `previous_action_context()`; exactly one entry, updated only by a **verified** action |

Full conversational context was **not** built: it is Phase 6, and its absence is deliberate.

---

## 2. Frozen Done-when — evidence

> **Frozen wording (Step 4 Section 6):** *5 loosely-worded commands (including the mixed-language one
> from Phase 2) produce correct plans or correct clarification questions, the "usko message kar do"
> ambiguity case is handled exactly as specified above at least 5 times in a row, and a simulated
> Claude-unavailable test correctly falls back to the Phase 0/1 error path instead of crashing.*

| Clause | Verdict | Evidence |
|---|---|---|
**Five loosely-worded commands** | DONE | All five confirmed `BrainEligible` (no deterministic parse exists for any of them), then carried end to end to a plan or a question. Table in Section 3 |
**The mixed-language one from Phase 2** | DONE, in **both** permitted branches | `notepad kholo` → a one-step `open_app notepad` plan when understood, **and** a correct clarification (*"Notepad kholun?"*) when not. Phase 2's own evidence went through the "correctly clarified" branch (phase2-closeout Limitation D) |
**"usko message kar do" × 5 in a row** | DONE | Five consecutive iterations, each a fresh `TurnContext`: no recipient guessed, exactly **one** clarification question (counted, not merely present), the answer never echoed to the screen, messaging still `NotSupported`, and nothing executed at any point. Also run as five separate processes: 8 passed × 5 |
**Simulated Claude-unavailable falls back without crashing** | DONE | Whole-string equality on the frozen sentence for **all five** lines; `Status.UNAVAILABLE`; zero Executor calls; no exception escapes the console. Then, in the same session, `open notepad` ran locally with `interpret` wired to fail if called — so the Phase 0/1 path provably needs no provider |

### The frozen unavailable sentence

Verified identical to the frozen text by direct comparison:

> `I can't reach my reasoning service right now, so I can only do direct commands until it's back.`

It is the **whole** message — no prefix, no suffix, no second line, and the deterministic parser's own
explanation is never appended to it.

### Final offline suite

**3207 passed, 45 skipped, 0 failed** — no `ANTHROPIC_API_KEY`, no `RUN_REAL_*` flags, root guards
active.

---

## 3. The five accepted loose commands

| # | Input | Route | Interpretation | Outcome | Executor kind |
|---|---|---|---|---|---|
1 | `could you open notepad for me please` | BrainEligible `unknown_command` | `Understood(open_app notepad)` | one-step plan; `yes` runs it | `open_app` |
2 | `notepad kholo` — **the Phase 2 mixed-language case** | BrainEligible `unknown_command` | `Understood(open_app notepad)` / `NeedsClarification` | plan, or correct clarification | `open_app` |
3 | `usko message kar do` | BrainEligible `unknown_command` | `NeedsClarification` → `NotSupported` | one question, then an honest refusal | none, correctly |
4 | `open the editor` | BrainEligible `unresolved_target` | `NeedsClarification` → `Understood` | question, answer, plan | `open_app` |
5 | `ab isko band kar do` | BrainEligible `unknown_command` | `Understood(close_app notepad)` | plan carrying `RiskLevel.MEDIUM` | `close_app` |

No transliteration anywhere: the bytes the user gives are the bytes that reach the prompt, and the
restatement comes back unchanged. Nothing was added to the deterministic parser's vocabulary to make
any of these pass.

**What this proves and what it does not.** The provider is scripted in these tests, so they prove the
*pipeline* turns an interpretation into the right plan, the right question or the right refusal, and
that nothing reaches the computer when it should not. They say nothing about how well Claude
understands any particular sentence — a mocked reply cannot show that. The real-provider evidence is in
Section 5.

---

## 4. Behaviour recorded at closeout

**Malformed provider output fails closed.** `validate_interpretation()` is pure and retries nothing.
Garbage, a structurally invalid reply, and a reply with no intents all become `InterpretationError` →
`Status.NO_PLAN` → no Executor call. There is no code path from an unreadable reply to an action, and
no unsafe default plan exists.

**risk_floor → Safety.** The higher of the two floors wins; an advisory floor that does not raise
changes nothing at all; a lower advisory floor cannot soften a real Executor rule or the shortcut
table; an unreadable floor fails closed to CRITICAL; MEDIUM, HIGH and CRITICAL all take the one
existing confirmation path; every retry is asked for with the same floor. `app/console.py` never calls
`authorize()` itself, and Voice adds no second gate.

**Bounded clarification and re-plan.** One clarification round and one semantic correction per root
command. The old plan is terminal *before* the replacement request is produced; the replacement carries
a new `plan_id`; a spent id is never live again, including across a new root command. A running plan
cannot be corrected underneath itself. No autonomous retry, and no `PlanStep` editor. The hard bound is
**≤ 3 Brain calls per root command**, and no outcome of one line buys a fourth. ASR correction is a
separate mechanism and costs nothing.

**PreviousActionContext.** Exactly one entry, ever. Only a **verified** action updates it — failures,
safety denials, unverified results, emergency-stopped actions, local refusals, rejected proposals, and
clarification or correction answers all leave it untouched. `TYPE_TEXT` payloads never enter it. It
survives a new root command while the Brain-call allowance resets. There is no persistent history.

**Voice Brain restrictions.** Deterministic Phase 2 commands stay local and free. From voice, only
`open_app` and `close_app` may be planned; `type_text`, `click`, `shortcut`, `scroll`, `refresh` and
`window_control` are refused, and a mixed plan is refused entirely. A typed `yes` is required and only
the exact word accepts. Plan acceptance and the Safety question remain separate. `_speak` has exactly
one call site and no path from it can open the microphone (asserted at AST level), so TTS and the
listener stay sequential. The Voice console imports no stop path, so the physical hotkey remains
authoritative.

**HWND ownership-token safety fix.** A confirmed false positive was found during Phase 3 real
validation: ownership was a set of integers, so when an owned window was destroyed and Windows later
gave its handle number to a different window, `close_app` closed that stranger. Fixed by attaching an
unpredictable ownership token to each verified window as a Windows window property
(`SetPropW`/`GetPropW`/`RemovePropW` in `app/executor/adapter.py`) and requiring **both** a recorded
handle **and** a matching token. A property belongs to the window object and is removed when the object
is destroyed, so a recycled handle number carries no token. Six mutations of the mechanism were planted
and all six were caught by named tests.

---

## 5. Real evidence retained

**Real provider health and cost accounting (Slice 2).** A controlled real Brain call succeeded with
Structured Outputs. The pre-flight estimate exceeded the provider's reported input tokens (estimated
3333 ≥ actual 2329), so the reservation is conservative in the safe direction. Cost recorded:
$0.005878. An earlier attempt failed *safely* with a 400 on an unsupported schema keyword — the failure
path worked as designed and no partial interpretation reached the Planner.

**Real typed Brain session (Slice 3A).** Four interactions in one real typed-console session: a loose
request was interpreted, a numbered plan was shown, acceptance ran it through the existing Executor
path, and the Verifier confirmed the result.

**Real Voice Brain session (Slice 3B).** A spoken loose request reached the Brain, the plan was shown
on screen (never spoken), typed acceptance was required, and a Brain-planned `close_app` still asked
the separate MEDIUM Safety question.

**Real Windows ownership-token smoke (2026-10-02).** On a real Notepad window:

```
open notepad    -> Opened notepad; its window appeared after 0.3s.
close notepad   -> separate MEDIUM Safety confirmation -> yes
                -> Closed notepad after 0.0s.
```

The ownership token therefore works against a real window: attached at a verified open, read back at
close, and the close verified.

**Provider ledger at closeout:** 17 rows in `data/claude_usage.db`, newest 2026-10-02 10:14:08,
`claude-sonnet-5-5`. All three cost controls — rate limit, token limit and money budget — were enforced
before any Claude-calling feature shipped.

---

## 6. Known limitations carried out of Phase 3 — all NON-BLOCKING

None of these is Phase 3 incompleteness. Each is either a deliberate later phase or an accepted
fail-safe.

**A. Spoken emergency stop — not implemented.** `Ctrl+Alt+Backspace` remains the authoritative
emergency stop. Inherited unchanged from Phase 2 Limitation A.

**B. The Listener may transcribe Roman Urdu into another script.** Measured in Phase 2: Roman Urdu
speech can return as Devanagari or Urdu script. The Brain accepts all three scripts unchanged, so this
does not block Phase 3 — but the script the user intended is not always the script that arrives.
Inherited from Phase 2 Limitation C.

**C. Voice Brain planning is restricted to `open_app` and `close_app`** until focus handover is
redesigned. Voice mode passes no focus object, so the active window at preparation time is the console
being driven; typing and clicking from voice are therefore not useful yet. Its architectural home is
already in frozen Step 4 Section 4. Related to Phase 2 Limitation B.

**D. If an owned window object is destroyed and recreated under a new HWND, automatic close may refuse
rather than guess ownership.** The replacement carries no token, so the close declines. This fails
safe, and the information needed to do better is not available from endpoint snapshots. No recovery
from matching title, app alias, process identity or conversational context.

**E. A window that cannot receive the ownership property token is not automatically closable.**
`SetPropW` is restricted by User Interface Privilege Isolation and fails with `ERROR_ACCESS_DENIED` on
a window belonging to a process of higher integrity level. The open still succeeds and says so:
*"…but I won't be able to close it automatically."* The window is never recorded on its handle number
alone.

**F. Full persistent memory is Phase 4.** Short-term context is one verified action, held in memory,
and nothing survives a restart.

**G. Broad conversational and pronoun context is Phase 6.** "Him", "it" and multi-turn subject tracking
beyond the single previous action are explicitly out of scope here.

**H. Communication integrations are Phase 10.** This is why `usko message kar do` can only ask and then
refuse honestly. `send_message`, `message`, `whatsapp`, `sms` and `email` appear in no kind table and in
no front end's `may_plan`.

---

## 7. Not Phase 3 requirements — recorded so they are not reintroduced

None of the following appears in frozen Step 4 for Phase 3, and none was adopted:

- an accuracy percentage, success-rate threshold or last-N-commands metric
- a 50-utterance corpus (the frozen number is **five** commands)
- a second provider or a model fallback chain
- PID, WinEvent or UI Automation window-lineage tracking
- a union-typed wire schema (measured: +118% input tokens, so it was rejected on evidence)

---

## 8. Configuration at closeout

| Setting | Value | Why |
|---|---|---|
`brain.model` | `claude-sonnet-5-5` | **Model selection is RESOLVED** — it was the open Phase 2 item. Chosen for Structured Outputs support and verified pricing/lifecycle; one primary model, no fallback in Phase 3 |
`brain.thinking` | `between_tools` | Sonnet 5.5 has adaptive thinking ON by default and rejects `disabled`; `between_tools` is its lowest setting, so thinking tokens do not share the output budget |
`brain.max_output_tokens_per_request` | `3072` | Token limit. A reply that needs more is refused by the `max_tokens` stop reason rather than half-understood |
`brain.rate_limit_per_minute` | `30` | Rate limit |
`brain.daily_budget_usd` | `1.00` | Money budget, hard stop per calendar day |

---

## 9. Phase status

- **Phase 0 — Foundation: COMPLETE** (Done-when checklist passed 2026-09-16; tagged v0.1)
- **Phase 1 — Basic Computer Control: COMPLETE** (Done-when verified 2026-09-20)
- **Phase 2 — Voice: COMPLETE** (Done-when verified 2026-09-29; `docs/phase2-closeout.md`)
- **Phase 3 — Brain + Planner: COMPLETE** (Done-when verified 2026-10-02; this document)
- **Next: Phase 4 — Memory — DESIGN/AUDIT only, not started**

Model selection is no longer pending; see Section 8.

---

## 10. Deferred items, carried forward deliberately

Recorded so they are not lost, and so none of them is mistaken for Phase 3 incompleteness:

- one-round clarification wording — a choice question should ask the user to **name or select** A or B
  rather than inviting a yes/no that cannot be used
- `exit` / `cancel` / `no` at the semantic-correction prompt, which currently consumes the next line
- the honest lost-ownership message, replacing *"I didn't open it"* when a group was opened but can no
  longer be identified
- naming the window in the `close_app` confirmation — informed consent, **not** ownership proof
- more applications in `executor.apps`: the companion currently controls `notepad` and `calculator`,
  and the whole stack already generalises over that setting
