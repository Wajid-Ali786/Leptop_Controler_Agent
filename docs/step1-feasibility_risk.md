# Step 1 — Feasibility, Risks, Limitations & Requirements
*AI Desktop Companion project — sits between the Master Plan v0.1 and any production code*
*Prepared: 14 September 2026*

This document answers one question only: **is this project practical, and what constraints does it actually operate under?** It does not cover tooling, environment setup, or file structure — those belong to Step 2 and Step 3, and are handed off at the end of this document rather than discussed here.

---

## 1. Reliability map — what's easy, hard, unreliable, or likely to fail

This is the single most useful table in this document. Rate each part honestly *before* building it, so surprises happen on paper, not in your daily workflow.

| Capability | Planning reliability estimate | Why |
|---|---|---|
| Typed command → action (e.g. "open notepad") | **Easy, 95%+** | Deterministic; no perception or language ambiguity involved |
| Voice → text (English, clear speech) | **Easy, ~90–95%** | Whisper is mature and well-tested for English |
| Voice → text (Urdu / Hindi / clear Roman Urdu) | **Moderate, ~75–90%** | Supported language codes, but real-world accents and background noise reduce accuracy |
| Voice → text (mixed-language, code-switched mid-sentence) | **Unreliable, ~50–75%** | This is a genuinely hard speech-recognition problem industry-wide, not specific to your setup — budget for a correction loop, not perfect first-pass accuracy |
| Understanding loosely-worded intent (via Claude) | **Easy–Moderate, ~85–95%** | Claude is strong at this; edge cases are ambiguous phrasing, not the model failing |
| Browser automation via DOM (Playwright) | **Easy, ~90–95%** | Reading real page elements is inherently more reliable than guessing pixels |
| Native Windows app control via UI Automation tree | **Moderate, ~70–90%** | Depends heavily on whether the target app exposes a proper accessibility tree — many do, some (games, custom-rendered apps) don't |
| Screenshot + vision fallback ("click the blue button") | **Unreliable, ~60–80%** | Works surprisingly often, but layout changes, similar-looking buttons, and resolution/DPI differences break it more than people expect |
| Raw coordinate clicking | **Fragile, breaks on any UI change** | Only ever a last-resort fallback — treat any reliance on this as a known weak point, not a real solution |
| Short-term contextual memory ("tell him I'm busy" after "message Ali") | **Moderate, ~80–90%** once built | Achievable, but needs the structured memory design in the Build Plan (Section 6.8) — a loose implementation will feel inconsistent |
| Long-term learned patterns/corrections | **Moderate**, improves over time | Gets more reliable the longer you use it; will feel rough for the first 1–2 weeks of real use |
| WhatsApp/Slack automation | **Unreliable and risky, not a technical reliability number** | The bigger risk here isn't accuracy, it's account restrictions (see Section 5) |
| Emergency stop / interrupt mid-action | **Achievable, ~95%+ if built deliberately** | This is a simple, testable feature — no excuse for it being unreliable; treat any failure here as a blocking bug, not an edge case |

**Bottom line:** the "boring" parts (typed commands, browser DOM automation, structured memory) are the most reliable. The "impressive demo" parts (voice in noisy mixed-language conditions, clicking things purely by looking at a screenshot) are inherently the least reliable, industry-wide — not a sign you're doing something wrong.

---

## 2. Online vs. offline — what needs internet, what doesn't

| Component | Needs internet? | Notes |
|---|---|---|
| Claude API (Brain, Planner, vision fallback) | **Yes for the current cloud-hybrid design; basic local commands can still work when Claude is unavailable** | This is the core reasoning layer under the current design — no offline substitute exists at the same quality without a local LLM (see Section 6) |
| Whisper speech-to-text (local `faster-whisper`) | **No** | Runs fully offline on your machine |
| Edge-TTS (voice output) | **Yes** | It's a Microsoft cloud service, not local |
| Offline TTS fallback (Windows built-in / `pyttsx3`) | **No** | Exists specifically for when Edge-TTS/internet isn't available |
| Mouse/keyboard/window automation (`pyautogui`, `pywinauto`) | **No** | Purely local OS interaction |
| Browser automation (Playwright) | **No** (beyond whatever the page itself needs) | Local browser control; the target website may need internet, the automation itself doesn't |
| Memory (SQLite) | **No** | Fully local file |
| WhatsApp Web / Slack integrations | **Yes** | These are inherently online services |

**What this means practically:** without internet, your assistant can still hear you, speak back (via the offline TTS fallback), move your mouse, and recall memory — but it cannot *reason* about what you meant, since that's Claude's job. Design the "Claude/internet unavailable" case explicitly (Section 7) rather than letting it fail silently.

---

## 3. Windows permissions this project will need

- **Accessibility / UI Automation access** — required for reading native app buttons/fields reliably (this is what makes the observation hierarchy in the Build Plan work at all).
- **Microphone access** — for the Listener.
- **Startup/background run permission** — if you want it running automatically when Windows starts, which a tray-based assistant typically does.
- **File system access** — scoped to specific folders it needs (project folders, downloads), not blanket access to the whole drive.
- **Simulated input permission** — some newer Windows security features and some apps (especially games, some banking software, some enterprise tools) actively block simulated mouse/keyboard/UI Automation input as an anti-cheat/anti-fraud measure. Expect a small number of apps this simply won't be able to control — that's a Windows/app-level restriction, not something your code can work around.
- **Admin rights** — only needed for specific actions (installing/uninstalling apps, changing system settings); the assistant should not run elevated by default, since that would make every mistake a bigger risk than it needs to be.

---

## 4. Hardware you'll actually need

| Resource | Minimum | Comfortable |
|---|---|---|
| CPU | Any modern quad-core (last ~6 years) | 6+ cores |
| RAM | 8 GB | 16 GB |
| GPU | **Not required** — Whisper's smaller models (`base`/`small`) run fine on CPU, and Claude's reasoning happens in the cloud | A GPU only becomes relevant if you later add a local LLM fallback (Section 6) |
| Disk | A few GB free (Whisper model files + Python environment + logs) | 10+ GB free if you keep multiple Whisper model sizes around |
| Internet | Stable broadband for Claude API + Edge-TTS calls | — |

You do **not** need a gaming PC or a dedicated GPU for the plan as currently scoped — that was one of the reasons the cloud-hybrid brain decision (Build Plan Section 1) made sense in the first place.

**OS target, restated from the Build Plan:** Windows 11 = primary target; Windows 10 = compatibility where practical, not an equal target.

---

## 5. What may violate another app's rules

This is a real risk, not a formality:

- **WhatsApp** and most messaging apps restrict automated/bot-like use in their Terms of Service. Using WhatsApp Web through simulated clicks (rather than an official Business API) carries a real, if generally low for light personal use, risk of the account being flagged or restricted. This risk is *not eliminated* by good code — it's inherent to automating a platform that doesn't officially support it.
- **Slack** is more automation-friendly (it has official APIs/bots), so building against Slack's actual API rather than simulating clicks is both safer and more reliable.
- **Anti-cheat/anti-automation software** in games or some security-sensitive apps may actively detect and block simulated input, or in rarer cases flag the account/session.
- **Recommendation carried over from the Build Plan:** read each target app's automation/bot policy before building Phase 10 integrations, and prefer an official API over simulated clicks wherever one exists.

---

## 6. Cost — what's free, what isn't, and whether Claude Code Pro is enough

**Completely free, indefinitely:**
Python, Git, GitHub (private repo), Whisper (local), the offline TTS fallback, SQLite, `pyautogui`/`pywinauto`, Playwright, PyInstaller/Inno Setup.

**Costs money, and scales with use:**
- **Claude API** for the Brain/Planner/vision calls — billed separately from any Claude subscription (Build Plan Section 1). This is genuinely usage-based; there's no way to give you a real number without measuring your actual usage, which is why Section 5.5 of the Build Plan builds in rate/token/budget caps from day one.
- **Edge-TTS** is currently free to use, but it's a Microsoft-operated service, not something under your control — if that ever changes, the offline fallback is what protects you.

**Is Claude Code Pro enough for this project?**
Don't treat any specific price or limit as fixed — Anthropic's plans, session/weekly limits, and pricing structure change fairly often, so **check https://www.anthropic.com/pricing and https://support.claude.com immediately before purchasing**, rather than relying on numbers written here. Directionally, at the time of writing: Pro is a lower-cost tier that bundles Claude Code usage with Claude.ai chat and Cowork under a shared, metered allowance, and is generally positioned for light-to-moderate daily use rather than heavy parallel-agent workloads. For solo, feature-by-feature vibe-coding on this project, that tier is a reasonable starting point to test against your own usage — if you find yourself frequently hitting limits during heavy build days, a higher tier is the next step. Verify the actual current numbers before you commit to a plan.

**Which Claude API models/services does the project actually need?**
- A capable reasoning/chat model for the Brain and Planner (this is where accuracy on loosely-worded, mixed-language commands matters most).
- Vision capability for the screenshot-fallback layer (Build Plan Section 6.5) — only invoked when the UI-tree/DOM/OCR layers come up empty, so this should be a small fraction of total calls if the observation hierarchy is working as designed.
- No fine-tuning, batch processing, or specialized API features are required for the personal-use scope in this plan.

---

## 7. What local AI might be needed later — and when

Nothing local is required for the MVP (Phases 0–4). A local model becomes worth considering only if:
- You want the assistant to keep doing **basic** reasoning when Claude/internet is unavailable (Section 8's failure case), or
- API costs from heavy daily use become worth trading against the setup effort and hardware cost of running a model locally, or
- You reach Tier 3 (productized/shareable, Build Plan Section 9) and want to reduce a second user's dependency on your own API key.

If and when that point comes, that's a GPU-relevant decision — flag it as an "Open" item for a future revision of this document rather than solving it now.

---

## 8. Failure scenarios — what happens when things go wrong

| Scenario | What should happen |
|---|---|
| **Claude API / internet unavailable** | The assistant should clearly say so (via the offline TTS fallback) rather than silently failing or guessing — e.g. "I can't reach my reasoning service right now, so I can only do direct commands until it's back." Basic direct commands (Phase 1-level) can still work locally. |
| **The AI misunderstands you** | This is *exactly* what the correction system (Build Plan Section 6.6/6.8) exists for — "No, I said Ali, not Alif" should fix the current action immediately, without needing to repeat the whole command |
| **It clicks the wrong thing** | This is what the Verifier (Build Plan Section 2) is for — it should catch that the expected result didn't happen and stop, rather than continuing on a wrong screen state |
| **You need to stop it immediately, mid-action** | This needs a genuinely instant interrupt — a hotkey or a single Dashboard button that halts the Executor immediately, not "finish this step first." Build this in Phase 1, test it explicitly, and treat any lag here as a bug, not a rough edge |
| **A risky action is about to run** | The Permission & Safety gate stops it and asks first — this is the whole point of that component existing from Phase 1 (Build Plan Section 2) |

---

## 9. Protecting your contacts, files, and messages

- **Least-privilege by default:** the assistant should only be able to see/touch what a given task actually needs, not have blanket access "just in case."
- **Sensitive categories get their own memory rule** — the "Sensitive-data rules" field in the memory structure (Build Plan Section 6.8) exists specifically so contacts, message content, and file paths aren't all treated with the same casualness as, say, a remembered app alias.
- **No silent forwarding/sharing** — any action that sends a message, uploads a file, or shares contact information is, by definition, at least Medium risk and should go through Permission & Safety (Build Plan Section 2), never execute purely because the Planner decided it made sense.
- **Untrusted content stays untrusted** — see Section 10 below; this is directly relevant to protecting your data, since a manipulated webpage or message is the most realistic way someone could try to trick the assistant into leaking something.

---

## 10. Preventing prompt injection (expanded from Build Plan Section 6.7)

The core rule stands: anything the assistant *reads* (a webpage, an email, a message, a document) is data to reason about, never an instruction to obey. Practically, this means:
- The Brain/Planner should be structured so that "content read from an external source" and "your actual spoken/typed command" are never merged into one instruction stream the model can confuse.
- Any action suggested *because of something the assistant read*, rather than something you asked for, should be treated as at least Medium risk by Permission & Safety — even if the action itself seems ordinary (e.g. "this email asked me to forward it" should never be auto-obeyed).
- This can't be made 100% foolproof — prompt injection is an active, evolving area even for major AI labs — so the honest goal here is "meaningfully reduced risk with a hard rule in place," not "solved."

---

## 11. Honest reliability summary — what's realistically 90%+ and what isn't

**Realistically 90%+ reliable, achievable within this plan:**
Typed/clear-voice direct commands, browser automation via DOM, structured memory lookups, the emergency stop, Permission & Safety gating on clearly-risky actions.

**Realistically 70–90%, good but not perfect — plan for occasional correction:**
Native Windows app control via UI Automation, Claude's understanding of clearly-worded mixed-language commands, learned patterns after they've had time to build confidence.

**Realistically below 70% out of the box — design around this, don't expect it to just work:**
Rapid code-switched mixed-language speech recognition, screenshot-vision-based clicking on unfamiliar or frequently-changing UIs, and anything automating a platform (like WhatsApp) that wasn't built to be automated.

**The honest takeaway:** the parts of your vision that feel most "wow" in a demo (voice, vision-based clicking, WhatsApp) are the least reliable parts, structurally — not because of any implementation mistake, but because they're inherently harder problems. The parts that make the assistant *actually useful daily* (fast, correct execution of well-understood commands, real memory, and safe behavior) are the reliable parts, and they're exactly what the MVP (Phases 0–4) focuses on. That's not a compromise — it's the plan working as intended.

---

## 12. Testing/Measurement — replacing estimates with real numbers

Every percentage in Section 1 is a planning estimate based on how these technologies generally perform — not a measurement of your specific laptop, your voice, your accent, or your actual usage patterns. Once each phase is built, replace the relevant estimate with a real number from your own testing (the per-phase pass/fail tests in the Build Plan, Section 7, are exactly where this happens). Expect some numbers to come in higher than estimated and some lower — that's the point of measuring rather than assuming.

---

## 13. Decision Summary

**✅ Decided:**
- Cloud-hybrid brain using the Claude API (billed separately from any Claude subscription)
- Pure vibe-coding with Claude Code as the build method
- Eight-component architecture: Listener, Brain, Planner, Permission & Safety, Hands/Executor, Verifier, Memory, Dashboard
- Observation hierarchy for screen understanding (UI tree → DOM → OCR → vision → coordinates)
- Windows 11 as primary OS target, Windows 10 as compatibility-where-practical
- TTS split into online (Edge-TTS) and offline fallback
- Safety present from Phase 1 in minimal form, maturing through Phase 8
- Three-part cost control (rate limit, token limit, money budget) from day one
- Three-level learning model (temporary correction / learned pattern / explicit memory)
- Non-Goals defined for the initial personal version

**⏳ To decide later (owned by later steps, not this document):**
- Which Claude Code plan tier (Pro vs. Max) — depends on real usage once building starts
- Whether/when a local AI fallback becomes worth adding
- Exact Dashboard UI design
- Exact risk-classification rules for Permission & Safety (written as real actions are built)
- Whether to pursue Tier 2/Tier 3 (Build Plan Section 9) at all, versus stopping at Tier 1
- Development environment tool versions and install commands
- Exact project folder structure, modules, and architecture files

---

## Step 1 Complete — Inputs for Step 2 & Step 3

This document is now closed. It does not decide tooling, versions, or file structure — it hands the following constraints forward for those documents to build against:

**Feeds into Step 2 (Development Environment):**
- Cloud-hybrid brain → an Anthropic API key is required, separate from any Claude subscription
- Offline requirements (Section 2) → local speech-to-text and an offline TTS fallback must be installable without internet
- Windows 11 primary / Windows 10 22H2 compatible (Section 4) → environment setup should target both
- No GPU required for the current scope (Section 4)

**Feeds into Step 3 (Project Workspace & Architecture):**
- The eight-component architecture from the Build Plan (Listener, Brain, Planner, Permission & Safety, Hands/Executor, Verifier, Memory, Dashboard) → each becomes its own module
- The observation hierarchy (Section 1) → the screen-understanding module needs to support UI-tree, DOM, OCR, and vision layers, not just one
- The three-part cost control and minimal safety gate (Build Plan Section 5.5) → need to exist as real files/modules from the first commit, not added later
- The memory field structure (Build Plan Section 6.8) → shapes the actual schema Step 3 will define
- Untrusted-content handling (Section 10) → needs to be a structural rule in how the Brain/Planner module is built, not a comment or afterthought

Step 2 and Step 3 are separate documents. Nothing further is decided here.
