# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Course project (COE548/748): an agent that monitors a Gmail inbox every 10 minutes, extracts emails, and labels internship opportunities that match the user's profile. Claude Code acts as the intelligence layer — no external AI API is used.

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
3. "Internship Match" label created if it doesn't exist
4. Every 10 min: scan inbox → keep only emails **received in the last 10 minutes** → write `emails.json` → wait for `to_label.json` → apply labels

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
```

`--interval` is in minutes (float, default 10). Both the recency-window
and the inter-cycle sleep use this value, and the IPC wait is half the
cycle so a tight cadence can't hang on Claude Code analysis.

Run in background with Claude Code and monitor output with `tail -f <output_file>`.

## Current Status (session ended 2026-04-25)

### What works
- Step 1: Chrome auto-connects via CDP (or launches fresh)
- Step 2: Login detection via `[gh="cm"]` compose button
- Step 3: "Internship Match" label creation via the **sidebar `+` button**
  (`[aria-label="Create new label"][data-tooltip="Create new label"]`),
  then `input.type()` + Enter on the MDC default-action button.
- Step 4a: Inbox scan — single JS pass, with per-row timestamp parsed from
  `td.xW span[title]`. Recency filter on `received_ms`.
- Email analysis: Claude Code reads `emails.json`, picks internship matches
- IPC handshake: `emails.json` / `to_label.json` exchange
- `--interval <minutes>` CLI flag (default 10)

### KNOWN BUG — RESUME HERE
**`_apply_labels_from_inbox` over-labels.** When asked to label N specific
threads, Gmail ends up applying the label to *every email currently scanned
in the inbox view*. End-of-session state: ~42 inbox threads have the
"Internship Match" label even though only 3 were ever requested.

The agent reports `Labeled N/N emails.` honestly — `selected` only counts
checkbox-click successes — but the actual Gmail outcome is wrong. Root
cause not yet identified; possibilities:
- The bulk-toolbar Label button operates on something broader than the
  checked rows (e.g., the entire scanned conversation set).
- The label-picker click hits the wrong target.

We started exploring a per-conversation alternative (open the thread, then
use the conversation toolbar / `l` shortcut), but Gmail keyboard shortcuts
are off and the conversation toolbar is collapsed into "More email options"
with a zero-sized hitbox in the user's window size.

**Cleanup pending:** ~39 wrong threads still carry the label.

### Known thread IDs for test internship emails
```json
["#thread-f:1863448213768020020", "#thread-f:1863448178752084791",
 "#thread-f:1863452174359364087"]
```
- `#thread-f:1863448213768020020` — "Internship about AI in medicine"
- `#thread-f:1863448178752084791` — "Internship for Yehya"
- `#thread-f:1863452174359364087` — "Hello" (AI/medicine internship offer)

### Suggested next steps
1. Bulk-remove "Internship Match" from non-internship threads.
2. Replace bulk-checkbox flow with per-thread labeling (open conversation,
   click toolbar Label, pick label). Options:
   - Force a wider Playwright viewport so the toolbar isn't collapsed.
   - Or have the user enable Gmail keyboard shortcuts and use `l`.
3. Re-test labeling end-to-end before declaring it fixed again.

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
