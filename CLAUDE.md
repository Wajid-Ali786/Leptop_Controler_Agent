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

OWNER TESTING BACKLOG (2026-10-08): everything learned from the owner's real-machine testing of
  Slices 1-5 is recorded in /docs/owner-testing-improvement-backlog.md - what now WORKS (so a later
  session does not re-prove it), observed defects, missing capabilities, known limits that are working
  as designed, deferred decisions, and the future acceptance tests. It also holds the development
  checkpoint and the exact tree state.
  THAT FILE AUTHORIZES NOTHING. Its findings are DEFERRED and MUST NOT trigger automatic
  implementation. Improvement work on any of them resumes only when the owner explicitly authorizes
  it; naming an item there is not that authorization. Read it for context before proposing work, not
  as a work queue.

Current work: USABILITY PLAN (started 2026-10-06). Phase 5 and Phase 6 work is STOPPED by the owner
  after the first real day of use. The whole-project weakness audit of 2026-10-06 produced a ranked
  five-slice plan; build from that, not from the Phase 5 slice list below.
  Slice 1 — console prompt states: DONE (2026-10-06). One rule set for every nested prompt, in
    app/console.classify_answer(): a command typed at ANY prompt is queued unchanged and run once by
    the loop instead of being consumed; exit/help work at every prompt and end whatever was pending;
    cancel/no/stop abandon explicitly and say what they abandoned; a mistyped "yes" at the PLAN prompt
    only is re-asked once. The Medium-and-above confirmation keeps its exception - only the exact word
    "yes" approves, there is no re-ask there, and a command typed there denies the action first and is
    then queued as a new root command with its own gate. A clarification answer that cannot be the
    missing value ("yes" to "which browser?") is re-asked once locally without cashing the single
    Brain round, and an application choice question now names the configured apps. MAX_CLARIFICATIONS
    is still 1 and the allowance is still 3 per root command. A front end with no console loop
    (app/voice_console.py) is bit-identical: one `if not handoff` in classify_answer.
  Slice 2 — `open <app>` means available-and-in-front: DONE (2026-10-06). The launch decision is
    frozen: if exactly ONE usable window already exists, do not launch again - activate it. Three
    cases: nothing usable open -> launch, wait, own it exactly as before; one already open -> no launch
    and NO window wait, bring it forward, and it stays UNOWNED; launched but no new window proved ->
    look at what is actually there before reporting a failure, which is what fixes "chrome was
    started, but no new window appeared within 15 seconds" while Chrome was open all along. More than
    one pre-existing window REFUSES with the count, activates none and launches nothing; selection
    among several is deferred. There is now exactly ONE activation path, `_activate_to_front`, shared
    with the named click; a minimized window is still reported and never restored. "Available but not
    in front" is Outcome.NEEDS_USER - no new status, and the model forbids retrying into it.
    THE OWNERSHIP BOUNDARY DID NOT MOVE. A window that existed before the command never receives the
    ownership token. `_found_windows` records only that open_app selected it (app + handle, no token
    field, separate type); no ownership check reads it, close_app cannot reach it, and a named click
    still refuses an app that only appears there. A handle plus a title re-check is NOT an identity -
    Windows reuses handle numbers - so it is deliberately the weakest honest representation.
    CONSEQUENCE OF THE FROZEN RULE, recorded deliberately: the assistant can no longer acquire an
    OWNED window for an app the user already has open. For Chrome specifically that means `close
    chrome` will keep refusing and named clicks in the owner's Chrome stay blocked until Slice 3
    decides what an explicitly selected but unowned window may be used for.
  Slice 3 — per-capability authorization: DONE (2026-10-07). Ownership is no longer the gate for
    CLICKING; it remains the gate for CLOSING. close_app and window_control close are untouched and
    still require the token. click_target now accepts a named-or-FOUND window, behind the existing
    MEDIUM confirmation, which is now the authorization and therefore always names the window and says
    whether the assistant opened it or only found it ("the chrome window I opened (...)" vs "a chrome
    window I did NOT open (...)"). Every click result names the window it acted in, so provenance
    stays visible now that it is not a gate.
    A found window participates in the existing context chooser as an ordinary candidate: it cannot
    skip the ambiguity rule, and an OWNED window is always preferred over a found one for the same app.
    BEFORE a click in a found window, four independent facts are re-checked (after the confirmation,
    before any activation): the handle is still open, it still matches the app's configured title
    pattern, it is still the same top-level window class, and it is still the same executable - the
    same structural evidence _window_control_identity and the refresh path already use, and
    deliberately NOT the window title. It also refuses if the recorded handle no longer matches the
    one the confirmation named. What this cannot prove: that it is the same window OBJECT. Windows
    reuses handle numbers, so a second Chrome window created after the first closed could satisfy all
    four. Only the ownership token rules that out, which is exactly why a found window may be clicked
    behind a confirmation and may never be closed. An unreadable executable fails closed.
    `_ours_now` is unchanged and still requires both of its clauses. The DOM path, type_text,
    shortcut, scroll and refresh are untouched (Slice 4).
  Slice 4 — named targets: PARKED (2026-10-07), not cancelled. When it resumes it is `type_text`
    ONLY - the owner decided refresh/scroll/shortcut named targets are a smaller want. Three findings
    were measured before parking, so a later session resumes from them rather than re-deriving:
    (a) PROMPT BUDGET. SYSTEM_PROMPT was 3953 characters. Teaching `app` on four kinds fits at 3973
        by RESTRUCTURING, not by cutting: move the `app` explanation out of click_target's paragraph
        (-115 chars) into one shared line naming the kinds that accept it, and change type_text's and
        refresh's "the window in front" wording. A verbose first draft came to 4002 and did not fit.
        That variant is parked WITH the four kinds, since it exists to teach them.
    (b) WIRE COST IS ZERO. `app` is already a wire property: the intent schema is flat, all 12
        properties required on every intent, so adding `app` to the four Python arg dataclasses adds
        nothing to the wire. Counts stay {optional: 0, unions: 0, properties: 20}.
    (c) THE REAL COST, and why it is one kind per turn. `_focus_identity` is
        (window.handle, control_handle) - the focused CONTROL is half of it - and each of the four
        preparers snapshots verifier.active_target() at PREPARE time to build it. On a named path the
        window is deliberately not in front when the confirmation is shown, so control_handle cannot
        be known until after activation. Each kind therefore needs its prepare->run seam restructured
        so the "approved" snapshot is taken AFTER activation, and type_text must REFUSE when the
        post-activation focused control is not a safe typing destination. That is surgery on four
        seams, not a shared helper.
    Named window_control is recorded as a remaining usability gap and was never in Slice 4's scope.
  Slice 5 — assistant-browser navigation: DONE (2026-10-08). One new kind, `navigate`, which sends the
    LIVE assistant browser session to a web address the user typed. Assistant browser ONLY - personal
    Chrome needs the address bar, which is parked Slice 4.
    THE PROMPT BUDGET WAS RAISED ONCE, DELIBERATELY, from 4000 to 4400, with the measurement recorded
    in app/brain/logic.py: the limit is a DISCIPLINE limit, and 400 more characters cost about $0.0004
    accounted per request against a $1.00 daily budget. The prompt is 4239 (161 headroom).
    WIRE COST: one new required property, `url`. Counts go {optional: 0, unions: 0, properties: 20} ->
    {0, 0, 21}, well inside the documented 24-optional / 16-union limits, which do not move at all.
    `url` is its own property because `text` means "what to type" and `app` means "a configured
    application name" (40 chars) - carrying an address in either would make the schema lie.
    THE ADDRESS IS CHECKED TWICE, by two owners. app/planner/logic._url_provenance asks whether it came
    from the USER: the address, minus an http(s) scheme the model may have completed, must appear in
    what the user typed. That stops an invented domain, a site inferred from a business name, and a
    substituted URL; it does NOT prove the model preserved the whole path, which is stated in the
    function. build_plan's user_text defaults to "" so a caller that cannot supply it gets no
    navigation rather than an unchecked one. Then app/executor/logic._navigable_url checks the scheme
    is http or https BEFORE the session lookup and before any adapter call, so a refused address never
    reaches a function that can open a socket.
    RISK: MEDIUM, and the confirmation shows the FULL address. Reason: the page has not been seen, it
    may make further requests of its own, and the next click would act on whatever loaded. That is
    equally true of a perfectly typed address, and refresh-on-a-browser is already MEDIUM, so LOW would
    have made navigation less careful than reloading.
    TIMEOUT: its own setting, browser.navigate_timeout_seconds (20.0). query_timeout_seconds (2.0) is a
    per-query bound and far too short for a page load; launch_timeout_seconds has the right magnitude
    but means "how long starting a browser may take". A timeout NEVER claims the page loaded - it is
    Outcome.UNVERIFIED and says part of the page may be there - because goto() can navigate and then
    run out of time waiting for the load event, and the next click would act on that.
    LOGGED: metadata only - four fixed strings, none interpolating anything. The URL is in
    ExecutorAction.url and never in `target`, so log_label and plan_summary() cannot see it.
    browser_navigate joined safety_guards.BROWSER_BOUNDARIES, so the central guard refuses it offline.
    CONTEXT AUDIT RESULT, pinned not designed around: after navigating, an unqualified `click Login`
    reaches the assistant browser ONLY while it is the single candidate. With personal Chrome also
    available (owned or found in this session) it is TWO candidates and the existing ambiguity rule
    refuses, naming `in assistant browser` as the way out. The three-command workflow therefore reads
    as advertised only in a session where Chrome was not opened; otherwise the third command must
    qualify. The rule was NOT weakened.
    Voice cannot plan navigation this slice.

Phase 5 — Screen Understanding. IN PROGRESS; NOT COMPLETE; PAUSED (see Current work above).
  The privacy and test-isolation boundaries required before expanding observation capability
  (/docs/phase4-closeout.md Limitation G) were defined in Slice 1 and are in force: structural reads
  are redirected offline, content reads are refused, and pywinauto is blocked at import.
  Slice 1 — observation foundation + local UIA target resolution: DONE (2026-10-03).
  Slice 2 — UIA re-identification + safe action-target bridge: DONE (2026-10-04), owner-run real UIA
    smoke passed (Calculator "Seven": resolved, confirmed, re-identified, one click, 7 displayed).
  Slice 3 — Brain + Planner wiring for a named click: DONE (2026-10-04), owner-run typed-console
    smoke passed ("open calculator" then "click seven").
  Auto-focus inter-slice — a named click brings its own owned window to the front after the
    confirmation: DONE (2026-10-04). SetForegroundWindow only; a refusal is a retryable message and
    never an escalation, a minimized window is refused rather than restored, and every other action
    keeps the manual focus hand-over.
  DOM Slice 1 — assistant-owned ephemeral browser session + read-only DOM resolution: DONE
    (2026-10-04). NOT yet validated against a real browser: everything offline is faked, including
    Playwright, so channel="chrome" launching, the role allowlist matching real page semantics and
    get_by_role's behaviour are all UNPROVEN until a gated real_browser run. Playwright lives only in
    app/executor/adapter.py (it can click, unlike UIA); the decision is a pure function in
    app/verifier/observation.py. The owner's own Chrome profile is unreachable: non-persistent
    context, no user-data-dir, no storage state, no cookie import. Real browser access needs the
    real_browser marker AND RUN_REAL_BROWSER_TEST=1 - real_desktop does NOT grant it. No DOM action,
    no navigation, and no UIA->DOM orchestration yet: a later slice must first audit how a Playwright
    page maps to the top-level window UIA reads.
  DOM Slice 2 — one authorized DOM click: DONE (2026-10-05). Playwright locator.click() behind the
    Executor adapter, never a coordinate conversion and never a JS/dispatch click. The element token
    now maps (inside the adapter only) to the semantic description that found it, so the target is
    re-resolved AFTER the confirmation; a changed page, a vanished control, a duplicate or a changed
    role all click nothing. The page's URL is fingerprinted inside the adapter and never returned or
    logged. NOT yet validated against a real browser. The click cannot be interrupted mid-call: the
    emergency stop is checked immediately before and immediately after, which is the honest guarantee.
  Assistant-browser wiring — the DOM primitive reachable from the typed console: DONE (2026-10-05).
    Two args-free kinds (open_browser / close_browser), parsed deterministically from "open/close
    assistant browser" so they cost no model call. `app="assistant browser"` is a RESERVED context
    selector recognised before configured apps and before Memory aliases; `app="chrome"` still means
    the owner's configured personal Chrome and routes to UIA. One assistant browser at a time, headed
    (the user navigates it themselves — there is no navigation command). A named click chooses its
    context: one candidate is used, and an owned app window beside a live browser page REFUSES and
    names both rather than preferring either. NOT yet validated end to end from the real console.
  DOM readiness fix: DONE (2026-10-05), after the owner's console smoke hit a transient false
    NotFound (one attempt missed 'Login'; the identical target succeeded seconds later). Cause was
    NOT case - production normalises, so both attempts passed the same string - but that
    locator.count() is an instantaneous snapshot and nothing waited. DOM discovery now runs under ONE
    shared bounded deadline from browser.query_timeout_seconds: scan, and if nothing matched, one
    Playwright wait across all allowed roles combined with or_(), then one more scan. Worst case stays
    about the configured timeout, not nine times it; the same matcher serves initial resolution and
    post-confirmation re-resolution. Matching semantics are unchanged.
  KNOWN LIMITATION, recorded and deliberately not fixed: generic foreground-window actions (refresh,
    scroll, shortcut, window_control) do not distinguish the owner's personal Chrome from the
    assistant's Chrome - both are chrome.exe + Chrome_WidgetWin_1, and refresh identity deliberately
    excludes the title. The user's foreground selection plus the existing Safety confirmation remains
    authoritative. window_control close still refuses the assistant browser, because it has no
    ownership token.
  Layers 1 and 2 of the frozen five-layer hierarchy exist. For an explicitly selected
  assistant-browser page, page-content targets route straight to DOM; HWND mapping is not required
  for that scoped workflow, and browser chrome plus native window controls remain UIA territory. OCR,
  vision and coordinate fallback are later slices; exact UIA accessible-name matching is
  authoritative, so "7" does not find a control called "Seven", and control-name aliases are
  deliberately NOT built on Phase 4's application aliases.
Phase 0 — Foundation: COMPLETE (Done-when checklist passed 2026-09-16; tagged v0.1).
Phase 1 — Basic Computer Control: COMPLETE (Done-when verified 2026-09-20).
  Verified by TWO runs of scripts/phase1_checklist.py, because the real-desktop and
  real-elevated groups need opposite foreground conditions (see /docs/step4 Section 4):
    python scripts/phase1_checklist.py --real-desktop     # elevated Notepad MINIMIZED
    python scripts/phase1_checklist.py --real-elevated    # elevated Notepad IN FRONT
  Each exits 2 because the other real group isn't selected; that is expected, not a failure.
Phase 2 — Voice: COMPLETE (Done-when verified 2026-09-29). Evidence, and the four limitations it
  carries, are in /docs/phase2-closeout.md — read that before starting Phase 3. In short: spoken
  emergency stop is NOT implemented (Ctrl+Alt+Backspace stays authoritative), TYPE_TEXT from voice
  mode targets the console window, and Roman Urdu speech can come back in Devanagari or Urdu script,
  so the Brain will need three scripts rather than one.
Phase 3 — Brain + Planner: COMPLETE (Done-when verified 2026-10-02). Evidence, and the eight
  limitations it carries, are in /docs/phase3-closeout.md — read that before starting Phase 4. In
  short: Voice may plan only open_app and close_app until focus handover is redesigned; a window
  whose ownership token can't be attached is not automatically closable; a destroyed-and-recreated
  window refuses rather than guessing; persistent memory is Phase 4, broad conversational context
  Phase 6, and communication integrations Phase 10.
Phase 4 — Memory: COMPLETE (Done-when verified 2026-10-03). Evidence, and the seven limitations it
  carries, are in /docs/phase4-closeout.md — read that before starting Phase 5. In short: Memory data
  is never sent to Claude and any future egress needs its own privacy review; the People query boundary
  has no messaging caller because messaging is Phase 10; nine of the fifteen structures are
  persistence-oriented only; and the wider verifier observation reads (read_text, the clipboard reads,
  active_target, cursor_position, window_at, list_windows) are NOT centrally isolated yet.
Model selection is RESOLVED: brain.model = claude-sonnet-5-5 (config/config.yaml).

## Test safety rule (added 2026-10-01, after a real desktop side-effect incident)
- Never run a pytest or scratch probe that can reach a physical or provider boundary from
  outside the repository's protected pytest root. conftest.py and safety_guards.py at the
  repository root are the outer boundary; a file outside it gets no guards at all.
- Normal/offline tests must rely on the project-wide fail-closed guards, not on high-level
  mocks alone. A mock on the wrong level is not isolation: when a refactor moves the level
  being mocked, the tests keep passing while the machine is acted on.
- Never claim "no physical/provider action occurred" unless that specific boundary has
  evidence — from a guard that fired, or from an independent observation. Verify each
  boundary separately; never infer one from another.
