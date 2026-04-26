# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Course project (COE548/748): an agent that monitors a Gmail inbox every few minutes, extracts emails, and labels internship opportunities that match the user's profile. Claude Code acts as the intelligence layer — no external AI API is used.

**Labels (5, multi-label per email):** `AI/ML`, `Research`, `Software Engineering`, `Embedded Systems`, `DevOps`. A single internship can carry several labels (e.g., an "ML research role using Docker" gets AI/ML + Research + DevOps).

**User profile:** Senior Computer Engineering student at LAU. Skills: Python, C, VHDL, SQL, JS, Deep Learning, ML, NLP, TensorFlow, PyTorch, Keras, Flask, React, Streamlit, Git, Linux, Docker. Target roles: AI/ML Engineering Intern, Research Intern (AI/Neuroscience/Bioinformatics), Software Engineering Intern, Embedded Systems Intern.

## Architecture

```
gmail_agent.py       ← single entry point, runs the full loop
venv/                ← project-local Python virtualenv (use venv/Scripts/python)
browsers/            ← Playwright's Chromium downloaded here (PLAYWRIGHT_BROWSERS_PATH)
emails.json          ← written each cycle by the agent; Claude Code reads and analyzes
to_label.json        ← written by Claude Code: {"thread_ids": [...]}; agent applies labels
```

**Flow per cycle:**
1. Real Chrome (no profile) opens at gmail.com via subprocess + CDP
2. User logs in manually
3. All 5 topic labels created if missing (idempotent)
4. Every 2 min (default; configurable via `--interval`): scan inbox → keep only emails **received in that window** → write `emails.json` → wait for `to_label.json` → apply each `(thread, label)` pair via the bulk-toolbar three-dots → "Label as" submenu

**IPC formats:**
- `emails.json` — list of `{thread_id, subject, sender, body, received_ms}`
- `to_label.json` — `{"thread_labels": {"<thread_id>": ["AI/ML", "Research", ...], ...}}`. Threads with no matching labels are simply omitted from the dict.

**Recency filter:** `_scan_inbox` extracts the per-row timestamp from `td.xW span[title]` (Gmail format `"Sat, 25 Apr 2026, 16:11"`) and the main loop drops anything older than `CYCLE_INTERVAL_SECONDS`. The cycle interval matches the freshness window, so this filter alone is sufficient — no `processed_ids` cache is needed.

**Why real Chrome (not Playwright Chromium):** Google blocks Gmail sign-in on Playwright's bundled Chromium. We launch `C:\Program Files\Google\Chrome\Application\chrome.exe` as a subprocess with `--remote-debugging-port=9222` and a temp `--user-data-dir`, then connect Playwright via CDP.

## Setup (run once on a new machine)

```bash
cd c:\Users\Yehya\Downloads\LLMIntern

# Create project-local venv
python -m venv venv

# Install dependencies INTO the venv (never use bare pip)
venv\Scripts\python -m pip install -r requirements.txt

# Download Playwright Chromium INTO the project browsers/ directory
set PLAYWRIGHT_BROWSERS_PATH=c:\Users\Yehya\Downloads\LLMIntern\browsers
venv\Scripts\python -m playwright install chromium
```

## Running

```bash
# Start the agent (always use venv python, always use -u for unbuffered output)
cd c:\Users\Yehya\Downloads\LLMIntern
venv\Scripts\python -u gmail_agent.py

# Custom cycle length, e.g. 2 minutes:
venv\Scripts\python -u gmail_agent.py --interval 2

# Start the dashboard in a SECOND terminal (default URL: http://localhost:8501)
venv\Scripts\python -m streamlit run streamlit_app.py
```

`--interval` is in minutes (float, default 2). Both the recency-window
and the inter-cycle sleep use this value, and the IPC wait is half the
cycle (min 90 s) so a tight cadence can't hang on Claude Code analysis.

The dashboard reads `history.jsonl`, which the agent appends to after
every cycle. Each record carries the email, scam-risk score with reasons,
CV-match score with retrieved evidence, and the labels actually applied.

Run in background with Claude Code and monitor output with `tail -f <output_file>`.

## Current Status (session 2026-04-26 — multi-label support)

### What works
- Step 1: Chrome auto-connects via CDP (or launches fresh)
- Step 2: Login detection via `[gh="cm"]` compose button
- Step 3: All five topic labels are created (idempotent) via the sidebar
  `+` button, `input.type()` + Enter on the MDC default-action button.
- Step 4a: Inbox scan — single JS pass, with per-row timestamp parsed
  from `td.xW span[title]`. Recency filter on `received_ms` with a 60 s
  grace to absorb Gmail's minute-only timestamp precision.
- Email analysis: Claude Code reads `emails.json`, decides which labels
  (zero or more) apply to each thread, writes `to_label.json` as
  `{"thread_labels": {"<id>": [...], ...}}`.
- **Step 4b:** `_apply_labels_from_inbox` loops over `(thread, label)`
  pairs. For each pair:
  1. Tick the row's checkbox.
  2. Click bulk-toolbar `[data-tooltip="More"]` (three-dots overflow).
  3. Hover `[role="menuitem"]:has-text("Label as")` (it's `aria-haspopup`,
     opens on hover not click).
  4. Click `[role="menuitemcheckbox"][title="<label>"]` with `force=True`
     (submenu animation defeats Playwright stability check, click still
     reaches Gmail).
  5. Escape twice + untick row before the next pair.
- `--interval <minutes>` CLI flag — default 2.

### Known thread IDs for test internship emails
```json
["#thread-f:1863448213768020020", "#thread-f:1863448178752084791",
 "#thread-f:1863452174359364087"]
```
- `#thread-f:1863448213768020020` — "Internship about AI in medicine"
- `#thread-f:1863448178752084791` — "Internship for Yehya"
- `#thread-f:1863452174359364087` — "Hello" (AI/medicine internship offer)

### Removing a label (verified 2026-04-26)
Same flow as adding, just clicks an already-checked menuitemcheckbox to
toggle it off. In this user's Gmail UI there is no separate "Apply"
button visible after the toggle — the click auto-applies when the menu
closes. (User-facing screenshots show an Apply menu item; that may be
older or only appear when filtering via the picker's search box.)

## Key Lessons (all saved in memory/)

1. **Always use `venv/Scripts/python -m pip install`**, never bare `pip` — machine has Python 3.11 (`python`) and 3.14 (`pip`) pointing to different envs.
2. **Use real Chrome via subprocess+CDP**, never Playwright's Chromium — Google blocks sign-in on automated browsers.
3. **Never use `wait_for_load_state("networkidle")`** on Gmail — SPA never reaches idle. Use `domcontentloaded` + element waits.
4. **Always set `sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)`** at top — Windows cp1252 crashes on non-ASCII print characters.
5. **Label picker**: try `[title=]`, `[aria-label=]`, `.J-M-Jz :text-is()`, `text=` in order — `[title=]` alone is unreliable.
6. **Label creation**: sidebar `+` only (Settings link is overlay-blocked); use `input.type()` + Enter, never `fill()` (MDC needs real keystroke events to enable Create button).
7. **All files must stay inside the project directory** — venv in `venv/`, browsers in `browsers/`.

## Course Evaluation Weights

Functionality 30% · Report Quality 30% · Code Quality 15% · Presentation 15% · Innovation 10%

Full requirements: `Requirements/Project_Instructions (1).pdf`
User profile: `plan.txt`
