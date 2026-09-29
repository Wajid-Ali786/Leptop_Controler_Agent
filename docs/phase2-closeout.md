# Phase 2 — Voice: CLOSEOUT RECORD

**Status: COMPLETE / CLOSED** — closed 2026-09-29.

This document records what Phase 2 delivered, the evidence for it, and what it deliberately did
not deliver. It is a record, not a plan.

**The frozen planning documents are unchanged and remain the source of truth for what Phase 2
required:** Master Plan v0.1, `step1-feasibility_risk.md`, `step2-dev-environment.md`,
`step3-workspace-architecture.md`, `step4-production-development.md`. Nothing in this file overrides
them; Section 5 of frozen Step 4 is still the definition of done for this phase.

---

## 1. Frozen Build bullets — all delivered

| Frozen bullet (Step 4 Section 5) | Where |
|---|---|
`app/listener/adapter.py` wrapping local `faster-whisper` | the only file importing `faster_whisper`/`ctranslate2`/`sounddevice`/`numpy`, each behind one lazy accessor; `local_files_only: true` is mandatory, so normal use never downloads |
English recognition | real acceptance sessions, repeatedly |
Urdu recognition | `listener.language` accepts `ur`; Urdu script is carried through the Listener unmodified (never transliterated) |
Hindi recognition | as above for `hi` — and demonstrated for real: a spoken utterance was returned in Devanagari with the language reported as Hindi (see Limitation C) |
Roman Urdu handling at the Listener boundary | Roman Urdu is treated as English-script text and never rewritten, translated or transliterated — the frozen requirement for this phase |
Mixed-language correction loop | `accept/a`, `correct/e`, `redictate/r`, `cancel/x`; span-based single-token replacement that preserves every other character, including Urdu, Hindi, Roman Urdu and mixed script |
TTS via `edge-tts` | `app/speaker/adapter.py`, online path, MP3 synthesised to a temporary file and played through winmm/MCI, deleted in a `finally` |
Offline TTS fallback (Windows/`pyttsx3`) | same adapter, local voice; `engine: auto` tries online first and falls back on an eligible failure |

---

## 2. Frozen Done-when — evidence

> **Frozen wording (Step 4 line 487):** *5 loosely-worded commands (including one deliberately
> mixed-language) are understood correctly or correctly clarified, at least one misheard word is fixed
> via the correction loop without restarting the command, and TTS output is audible in both the online
> and offline paths.*

| Clause | Verdict | Evidence |
|---|---|---|
**Five loosely-worded commands** | DONE | Real voice session, six English utterances, every one executed or correctly clarified with no silent misfire: `open notepad` ✓, `open calculator` ✓, `minimize the window` → *"minimize acts on the active window and takes nothing after it. Say minimize."*, `minimize` ✓, `close the calculator` → refused and listed the valid apps, `close calculator` → MEDIUM risk, typed `yes`, closed. Executor and Verifier confirmed every action. |
**One deliberately mixed-language command** | DONE, through the frozen **"correctly clarified"** branch | Real utterance returned as `open notepad खोलो`. The system named the unresolved target and the valid alternatives — unknown app `notepad खोलो`, apps available: calculator, notepad — and ran nothing. No silent misfire. This is clarification, **not** semantic understanding (Limitation D). |
**One misheard word fixed via the correction loop, without restarting** | DONE | See Section 3 — the evidence basis is stated precisely there. |
**TTS audible online and offline** | DONE | Wi-Fi on: **3 passed, 1 skipped** — offline `"ready"` audible; online `Spoken(engine="online", seconds≈5.58)` audible in the neural voice; injected online failure → `Spoken(engine="offline", seconds≈2.0)` audible in the local voice. Wi-Fi off: **3 passed, 1 skipped** — offline audible; injected fallback audible; **genuine network outage** → `Spoken(engine="offline", seconds≈1.83)` audible. Confirmed by ear in both sessions. |

### Supporting real evidence retained

Microphone capture works. Local faster-whisper transcription works. The model is preloaded **before**
the first capture (`preload_completed_at < first_capture_at`). A candidate is always shown before
anything runs, and the string displayed is the string that ran (asserted by object identity, not
equality). Typed MEDIUM-risk confirmation works inside a voice session. Executor and Verifier work.
The speaker is integrated at one call site, after the pipeline. The real harness's privacy and
persistence assertions pass: no audio file is created anywhere, and neither the transcript nor the
accepted command reaches the log.

### Final run figures

- Real speaker acceptance, Wi-Fi on: **3 passed, 1 skipped**
- Real speaker acceptance, Wi-Fi off: **3 passed, 1 skipped**
- Final real voice-console harness: **1 passed**
- Normal offline suite: **2125 passed, 44 skipped**

One honesty note about sequencing: the six-utterance session above produced valid evidence but its
pytest wrapper failed afterwards on an assertion that scanned all of `data/` for audio files — invalid
once a local model legitimately ships static `test_wavs/*.wav` fixtures (their mtimes are from January
2024, predating the session). Every clause-relevant assertion had already passed. The assertion was
replaced with a before/after ownership check, and the later targeted session passed cleanly.

---

## 3. The correction clause — evidence basis, stated precisely

- **Frozen Step 4 does not require an organic microphone error for this clause.** The document is
  explicit elsewhere when it wants a particular kind of evidence — *"mocked/local"* (Phase 0),
  *"measured, not assumed"* and *"on your machine"* (Phase 1), *"audible"* (the TTS clause in this
  same sentence). The correction clause names none of them. It names a behaviour: one word wrong,
  fixed *via the correction loop*, *without restarting the command*.
- **The mishearing phenomenon is proven on real hardware.** During acceptance the recognizer produced
  exactly the correctable shape: intended `open whatsapp`, heard `open workshop` — one token, wrong,
  fixable by replacement. Also observed: `open type calculator` (an insertion, which single-token
  replacement cannot fix) and the Devanagari case in Limitation C.
- **The correction behaviour itself is proven deterministically, through the real correction
  implementation**: the candidate is displayed, `e` is chosen, the numbered token is selected, only
  that span is replaced, the corrected candidate is displayed again, fresh acceptance is required, and
  the corrected candidate — not the original — reaches the normal command path, with no redictation
  and no restart.
- **That deterministic evidence is not hardware evidence, and is not presented as such.** It exercises
  the real voice-console correction path with an injected transcript and a recorded
  `handle_command`. Each boundary it sits between is separately proven real: microphone → Whisper →
  candidate in the acceptance UI, and accepted candidate → parser → Safety → Executor → Verifier. The
  correction path itself touches no device, no model and no network.
- **No further chance-based microphone session is required**, and none should be run for this clause.
  A completion criterion must not depend on luck when the frozen source does not ask for it.

---

## 4. Known limitations carried out of Phase 2

### A. Spoken emergency stop — KNOWN LIMITATION / DEFERRED SAFETY ENHANCEMENT

**Not implemented.** `Ctrl+Alt+Backspace` remains the authoritative emergency stop, measured at about
22 ms median, registered before the speech model loads and active for the whole voice session.
`listener.voice_stop_enabled` is `false` so the configuration does not advertise a feature that does
not exist.

Three detector technologies were measured and none was good enough: Whisper-small (11.5–11.9 s, 0/6
detections — rejected for this role), sherpa-onnx keyword spotting (2/5 and 3/5 recall at two
thresholds, 6/10 and 5/10 false positives — paused, not production-approved), and Vosk (harness and
model prepared, never installed — blocked on an sdist-only transitive dependency, never benchmarked).
All three remain intact as research artifacts.

**When this is revisited, start from the contract question, not from a fourth detector.** The strict
"standalone STOP only" contract may itself be the difficulty: for a safety trigger, *"please stop"*
and *"stop it"* are stop intents, while the negatives that genuinely matter are incidental
occurrences of the word — *stopped*, *stopping*, *full stop*, and the keyword inside dictated text.

### B. Voice-mode `TYPE_TEXT` focus — KNOWN LIMITATION

`app/console.py` sets `hands_over = focus is not None and needs_handover(action.kind)`, and voice mode
passes no focus object (`main.py` calls `run_voice_mode()` with none). Only `run_console` — typed mode,
`--console` — builds a `FocusHandover` and prompts the user to switch windows. So in voice mode the
active window at preparation time **is** the target, which is the console window you are driving. The
safety gate names it truthfully (*"type N characters into window 'Windows PowerShell'"*) and asks for
confirmation, so nothing is hidden — but there is no moment in a voice session when another window
could be focused. Typing from voice mode is therefore not useful yet.

Not fixed here. Its architectural home is already documented in frozen Step 4 Section 4: *"A future
command window (Phase 2/9) will itself be the active window, so it must hand focus back before
typing."*

### C. Roman Urdu / script behaviour — KNOWN LIMITATION / Phase 3 design input

**The Listener does not always yield Roman script for Roman Urdu speech.** Measured: `type mera naam
Wajid hai` was returned as `type मेरा नेम वाजिजली` — Devanagari, language reported as Hindi (0.62) —
and the content was wrong as well. Frozen Step 4 assumes Roman Urdu is *"English-script text the Brain
interprets contextually"*; with faster-whisper small that premise does not reliably hold.

Consequence for Phase 3: the Brain will need to handle **Urdu and Hindi script as well as Roman
Urdu**, and the language policy must cope with a script the user never intended.

### D. Mixed-language semantics — KNOWN LIMITATION / Phase 3 work

Phase 2 can safely **clarify** some mixed-language forms; it does not semantically **understand**
natural Roman Urdu commands. The deterministic parser is verb-first and English-only by design — its
own docstring says *"understanding loosely worded commands is Phase 3 (Brain + Planner)"* — and
clarification questions are a frozen Phase 3 Build bullet. Nothing was added to the parser's
vocabulary to make the mixed-language clause pass.

---

## 5. Not Phase 2 requirements — recorded so they are not reintroduced

None of the following appears in frozen Step 4 for Phase 2, and none was adopted:

- a 50-utterance corpus (the frozen number is **five** commands)
- any accuracy percentage or success-rate threshold — frozen Step 4 specifies none for Phase 2
- a last-20-command metric
- another spoken-stop detector
- another TTS benchmark
- repeated microphone sessions waiting for a convenient recognizer error

---

## 6. Scope lesson carried into Phase 3

Phase 2 spent roughly eight tasks on spoken emergency stop — a secondary safety feature — across
three detector technologies, while three frozen requirements (edge-tts, the offline fallback, and the
acceptance session) went untouched. The diminishing-return point was reached after the Whisper latency
measurement: the hotkey was already proven at about 22 ms, and no scenario could be named in which a
spoken stop beat it.

The work was not wasted — it closed out one architecture with evidence, recovered the sherpa trigger
condition from upstream source, and produced the contract insight in Limitation A. But the shape is
worth naming.

**The generalisable signal, in one line:** tasks chosen because *"the last measurement was
disappointing"* rather than because *"this frozen requirement is unmet"* are the drift.

### Process safeguards agreed for Phase 3

- roughly **3 implementation tasks per feature** before explicit review
- **one bounded research task** before escalation; a negative result closes the question rather than
  authorising a variant
- before each subtask: *does this materially move the whole companion toward usable completion, or are
  we optimising an edge case?*

These are process safeguards, **not** completion gates, and they introduce no numeric target. Frozen
Step 4 remains the only source of truth for completion.

---

## 7. Configuration at closeout

| Setting | Value | Why |
|---|---|---|
`listener.enabled` | `false` | voice input off by default; set `true` to use `python main.py --voice` |
`speaker.enabled` | `true` | the assistant speaks its reply; only `--voice` reaches the speaker, so typed mode stays silent |
`speaker.engine` | `auto` | online first, falling back to the local voice. Online **sends the reply text** to Microsoft's speech service; offline sends nothing |
`listener.voice_stop_enabled` | `false` | DEFERRED — see Limitation A |

---

## 8. Phase status

- **Phase 0 — Foundation: COMPLETE** (Done-when checklist passed 2026-09-16; tagged v0.1)
- **Phase 1 — Basic Computer Control: COMPLETE** (Done-when verified 2026-09-20)
- **Phase 2 — Voice: COMPLETE** (Done-when verified 2026-09-29; this document)
- **Next: Phase 3 — Brain + Planner — DESIGN/AUDIT only, not started**

Model selection remains PENDING and is a Phase 3 decision (`brain.model` in `config/config.yaml`).
