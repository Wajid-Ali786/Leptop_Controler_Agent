# Step 2 — Development Environment
*AI Desktop Companion project — software, versions, accounts, and tools only. No architecture or folder structure here — that's Step 3.*
*Prepared: 14 September 2026*

Everything below is what you'll actually install and run, in order. Where a tool's version or install method could change, that's flagged — check the linked source rather than trusting this document blindly a year from now.

---

## 1. The environment, at a glance

```
Your Laptop (Windows 11 primary / Windows 10 22H2 compatible)
│
├── Git for Windows           → gives you Git + a Bash shell Claude Code can use
├── Claude Code               → native installer, no Node.js required
├── Python 3.12                → your automation/AI glue-code runtime
├── VS Code (optional)         → editing, and Claude Code's VS Code extension
│
├── Project folder             → AI-Desktop-Companion/ (Step 3)
│   └── Python virtual environment (isolated per-project dependencies)
│
├── Claude API key             → separate from your Claude Code subscription
├── GitHub                     → private repo for version control
│
└── Python packages: faster-whisper, pyautogui, pywinauto, playwright,
    pystray, edge-tts, pyttsx3, python-dotenv, anthropic, pytest
```

---

## 2. Install order and exact commands

### Step A — Git for Windows
Needed for version control, and it also gives Claude Code a Bash shell to work in on native Windows (recommended, not strictly required).

- Download and run the installer from: https://git-scm.com/downloads/win
- Accept the defaults during setup.
- Verify:
  ```powershell
  git --version
  ```
- Set your identity (used on every commit):
  ```powershell
  git config --global user.name "Wajid Ali"
  git config --global user.email "your-email@example.com"
  ```

### Step B — Claude Code
Install directly — it does **not** need Node.js unless you specifically choose the npm install method (which needs Node 22+). The native installer is what Anthropic currently recommends.

Open **PowerShell** and run:
```powershell
irm https://claude.ai/install.ps1 | iex
```
Verify:
```powershell
claude --version
claude doctor
```
`claude doctor` runs a full health check of the install — worth running once now so any issue surfaces before you're mid-feature.

Claude Code needs a paid Claude plan (Pro, Max, Team, Enterprise) or a Console API key to actually run — the install itself is free. First run, `claude`, will prompt you to log in through the browser.

### Step C — Python
Python 3.12 is the recommended version for this project — it's stable, and has the widest current library compatibility for the packages you'll rely on (`faster-whisper`, `pywinauto`, `playwright`), which tend to lag slightly behind Python's very latest release. Newer versions (3.13/3.14) exist, but check each library's own supported-version list before jumping to them.

- Download from: https://www.python.org/downloads/ (get the latest 3.12.x installer)
- **During install, check "Add python.exe to PATH"** — this is the single most common Windows Python setup mistake.
- Verify:
  ```powershell
  python --version
  pip --version
  ```

### Step D — VS Code (optional, recommended)
Not required — Claude Code runs fine from a plain terminal — but useful for reading/editing code alongside it, and it has a Claude Code extension.
- Download from: https://code.visualstudio.com/

### Step E — GitHub
- Create a free account at https://github.com if you don't have one.
- Create a **private** repository named `AI-Desktop-Companion` (empty, no template files yet — Step 3 creates the structure).
- Authentication: GitHub's own setup wizard will guide you through either a Personal Access Token or the GitHub CLI (`gh auth login`) — both work fine; pick whichever the wizard suggests first.

### Step F — Anthropic API key
- Go to https://console.anthropic.com and create an API key.
- **Keep this separate from your Claude Code login** — this key is what your project's code will use to call Claude directly, and it's billed independently (Build Plan Section 1, Feasibility doc Section 6).
- Don't paste it anywhere yet — Step 3 sets up the `.env` file it belongs in.

---

## 3. Python packages this project will use

These get installed inside the project's virtual environment in Step 3, not globally — listed here so you know what's coming and why each one is in the stack (all already justified in the Build Plan, Section 4):

| Package | Purpose |
|---|---|
| `anthropic` | Official SDK for calling the Claude API |
| `faster-whisper` | Local, offline speech-to-text |
| `pyautogui` | Universal mouse/keyboard fallback |
| `pywinauto` | Native Windows app control via UI Automation |
| `playwright` | Browser automation via DOM |
| `pystray` | System tray icon |
| `edge-tts` | Online, natural-sounding text-to-speech |
| `pyttsx3` | Offline TTS fallback |
| `python-dotenv` | Loads the `.env` config file during development |
| `pytest` | Testing framework, used from Phase 1 onward per the Build Plan's vibe-coding loop |

Exact version pins will be captured in `requirements.txt` once the project folder exists (Step 3) — pinning now, before any code runs, would just guess at versions you haven't tested together yet.

---

## 4. Verifying the whole environment works, end to end

Before Step 3, confirm every piece is actually working — this is a checklist, not a formality, since a broken foundation here wastes far more time later:

```powershell
git --version          # Git installed
claude --version       # Claude Code installed
claude doctor          # Claude Code health check passes
python --version       # Python installed and on PATH
pip --version          # pip available
```

All five should return a version number or a clean pass with no errors. If any fail, resolve it here — don't carry a broken tool into Step 3.

---

## 5. What's still intentionally undecided

Consistent with the Feasibility doc's Decision Summary (⏳ To decide later):
- Exact package version pins — set once Step 3's `requirements.txt` is created and tested together.
- Whether you use VS Code's Claude Code extension or the plain terminal day-to-day — try both once Step 3 exists and see what feels better; it doesn't affect the project itself.
- Claude Code plan tier (Pro vs. Max) — revisit once you're actually building and can see real usage against real limits.

---

## Step 2 Complete — Inputs for Step 3

This document is now closed. It decided tools, versions, and accounts only — it does not decide folder structure, modules, or architecture files. It hands forward:

- A working Python 3.12 environment, Git, and Claude Code — ready for a project folder to be created inside
- The confirmed package list (Section 3) that Step 3's `requirements.txt` will pin exact versions for
- A private GitHub repo, ready for Step 3's first commit
- A separate Anthropic API key, ready for Step 3's `.env`/credential setup

Step 3 is a separate document. Nothing about folder structure or architecture is decided here.
