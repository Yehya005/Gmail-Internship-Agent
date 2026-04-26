# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Course project (COE548/748): an agent that monitors a Gmail inbox every few minutes, scrapes each fresh email's full body, scores it for scam risk, retrieves matching evidence from the user's CV (RAG), and applies one or more labels. Two decision paths share the same Gmail-automation runtime:

- **Autonomous mode** — the bundled rule-based `classifier.py` decides labels using `scam_features` + `cv_match` + topic keywords. No LLM call, no API key. This is the default when the agent is launched from the Streamlit dashboard.
- **External-override mode** — any other process (Claude Code, an LLM-API caller) can drop `to_label.json` within the IPC window and overrule the local classifier.

**Labels** are an *editable list* in `gmail_agent.py` — `LABELS = [...]`. Add or remove categories there; the agent creates each missing one in Gmail at startup (idempotent). Today's default set covers the user's CV: `AI/ML`, `Research`, `Software Engineering`, `Embedded Systems`, `DevOps`, `Scam Risk`. `Scam Risk` is **exclusive** — when applied, no topic labels go on the same email.

**User profile:** Senior Computer Engineering student at LAU. Skills: Python, C, VHDL, SQL, JS, Deep Learning, ML, NLP, TensorFlow, PyTorch, Keras, Flask, React, Streamlit, Git, Linux, Docker. Target roles: AI/ML Engineering Intern, Research Intern (AI/Neuroscience/Bioinformatics), Software Engineering Intern, Embedded Systems Intern. Full CV in `plan.txt`.

## Architecture

```
gmail_agent.py     ← single entry point, runs the full loop
scam_scorer.py     ← deterministic scam-risk heuristic (no LLM)
cv_match.py        ← RAG matcher: embeds CV chunks, retrieves on each email
classifier.py      ← rule-based fallback for autonomous label decisions
streamlit_app.py   ← dashboard: per-email cards + sidebar Start/Stop + auto-refresh

plan.txt           ← user's CV (drives both topic-keyword tables and CV-RAG chunks)
venv/              ← project-local Python virtualenv (use venv/Scripts/python)
browsers/          ← Playwright's Chromium (PLAYWRIGHT_BROWSERS_PATH)

emails.json        ← written each cycle by the agent; consumers read it
to_label.json      ← optional override from an external decider; agent applies labels
history.jsonl      ← append-only per-cycle log; the dashboard renders from this file
agent.pid          ← PID of the agent spawned by the dashboard's Start button
agent_output.log   ← agent stdout/stderr (terminal runs + dashboard runs share it)
```

The number of labels and the number of CV chunks are both **variable** — they fall out of the `LABELS` constant and the contents of `plan.txt`. There's nothing in the code that hard-codes "5" or "6" or "13".

## Flow

### Cold start (per session)

1. **Chrome.** Agent connects to an existing Chrome on CDP port 9222 if one is running; otherwise launches `chrome.exe --remote-debugging-port=9222 --user-data-dir=<temp>` pointed at `mail.google.com`. Real Chrome (not Playwright Chromium) is required — Google blocks sign-in on Playwright's bundled browser.
2. **User logs in manually** — the only manual step. Agent waits up to 5 min, polling for the Compose button (`[gh="cm"]`).
3. **Label setup.** For each name in `LABELS` not already in the sidebar: click `[aria-label="Create new label"][data-tooltip="Create new label"]` → wait for `div.uW2Fw-JD` dialog → `input.type(name)` (NOT `fill` — Material Components watches keystroke events to enable the Create button) → press Enter (the dialog's MDC default-action button). Verify all are present in the sidebar.
4. **Load CV-match model.** `sentence-transformers all-MiniLM-L6-v2` loads (~80 MB, downloaded on first use). `plan.txt` is split into semantic chunks (one per heading + one per project bullet — count varies with CV length), each chunk embedded once and cached in RAM.
5. **Enter the monitoring loop.**

### Per cycle (default 2 min, set via `--interval`)

6. **Scan inbox.** Navigate to `#inbox`, query `tr.zA`. For each row extract `thread_id`, subject (`.y6 > span` / `.bog`), sender (`.zF[name]`), snippet (`.y2`), and `received_ms` (parse `td.xW span[title]`; fall back to visible text, then `Date.now()` so an unparseable tooltip never silently drops the email). Attach a snippet-level `scam_features` block from `scam_scorer.py`.
7. **Recency filter.** Keep emails with `received_ms ≥ now − cycle − 60 s`. The 60-s safe zone covers Gmail's minute-only timestamp precision. Same email may appear in two consecutive cycles — labelling is idempotent so this is harmless.
8. **Open each fresh email.** For every survivor:
   - Find the row's index, dispatch a real `mousedown / mouseup / click` chain on `.y6 / .bog` inside one `page.evaluate` so DOM re-renders can't detach the locator. Position-based row clicks were earlier hitting hover-revealed Archive icons.
   - URL changes to `#inbox/FMfcgz...`. Scrape `div.a3s.aiL` for the full conversation body.
   - Re-run `scam_scorer` on the full text (catches buried payloads).
   - Run `cv_match.match(body)`: encode the body, cosine against all CV chunks, return top-k retrieved chunks + max-similarity score + missing-skills list.
   - Navigate back to `#inbox`.
9. **Write `emails.json`** — list of `{thread_id, subject, sender, body, received_ms, scam_features, cv_match}`.
10. **Wait up to `--ipc-wait` seconds (default 20) for `to_label.json`.** If it arrives, parse `{"thread_labels": {"<id>": ["AI/ML", ...], ...}}` and use it. If it doesn't, run `classifier.py`:
    - `scam_features.score ≥ 0.5` → `["Scam Risk"]` only.
    - Else needs the word "internship" + `cv_match.score ≥ 0.30`.
    - Else emit every topic in `LABELS` whose keywords appear in subject + body.
11. **Apply labels per `(thread, label)` pair**, in isolation:
    - Tick row checkbox via JS on `.oZ-jc` / `[role="checkbox"]`.
    - Click bulk-toolbar `[data-tooltip="More"]` (three-dots overflow).
    - Hover `[role="menuitem"]:has-text("Label as")` — `aria-haspopup="true"`, opens on hover not click.
    - `force`-click `[role="menuitemcheckbox"][title="<label>"]` (submenu animation defeats Playwright's stability check, click still reaches Gmail).
    - Escape twice + untick the row before the next pair.
12. **Append a record per email to `history.jsonl`** — full email + `labels_applied` + `cycle_at`. Streamlit reads this.
13. **Sleep `--interval` minutes**, loop back to step 6.

### UI side (parallel)

14. **Streamlit dashboard** at `http://localhost:8501`:
    - Reads `history.jsonl` on every render. Renders one card per email with subject, sender, time, label chips, scam score + reason list, CV match score + retrieved CV evidence + missing skills, and the body in an expander.
    - Top: counters (emails seen, labeled, scam-flagged, average CV match, last cycle time) and a free-text filter.
    - **Sidebar — Agent.** Status (🟢 PID / 🔴 Stopped) + cycle-interval input + Start/Stop buttons. PID tracked in `agent.pid` so state survives Streamlit reruns. Stale PID files (process exited) auto-clean.
    - **Sidebar — Live updates.** Auto-refresh toggle (default on) + 5–60 s interval slider (default 10). Manual Refresh button when paused.

## IPC formats

- `emails.json` — list of `{thread_id, subject, sender, body, received_ms, scam_features, cv_match}`
- `to_label.json` — `{"thread_labels": {"<thread_id>": ["<label>", ...], ...}}`. Threads with no matching labels are omitted from the dict. Optional — skip writing it to let the local classifier decide.
- `history.jsonl` — one JSON object per line, same shape as `emails.json` entries plus `labels_applied: [...]` and `cycle_at: "YYYY-MM-DD HH:MM:SS"`.

## Setup (run once on a new machine)

```bash
cd c:\Users\Yehya\Downloads\LLMIntern

# Project-local venv
python -m venv venv

# All deps (playwright, sentence-transformers, streamlit, …)
venv\Scripts\python -m pip install -r requirements.txt

# Playwright Chromium into the project browsers/ directory
set PLAYWRIGHT_BROWSERS_PATH=c:\Users\Yehya\Downloads\LLMIntern\browsers
venv\Scripts\python -m playwright install chromium
```

## Running

```bash
# A) Standalone agent run
venv\Scripts\python -u gmail_agent.py
venv\Scripts\python -u gmail_agent.py --interval 2 --ipc-wait 20

# B) Dashboard (recommended) — handles Start/Stop in the sidebar
venv\Scripts\python -m streamlit run streamlit_app.py
```

`--interval` is in minutes (float, default 2) and applies to both the recency window and the inter-cycle sleep. `--ipc-wait` (default 20 s) is how long the agent waits for an external `to_label.json` before falling back to the local classifier — bump it if you have a slow LLM in the loop.

## Current Status (session 2026-04-26)

### What works end-to-end
- Real-Chrome connect via CDP; manual login detection.
- Variable-size `LABELS` set, idempotent creation via the sidebar `+` button.
- Per-row inbox scan with timestamp parsing + recency filter + 60-s safe zone.
- Full-body scrape per fresh email (subject-cell click via real-MouseEvent dispatch).
- `scam_scorer.py` — heuristic features (sender domain, payment-phrase lexicon incl. `$N fee/charge` regex, no-interview / fast-track signals, generic greetings, suspicious URLs, urgency, hyperbolic comp, all-caps subject), each with a human-readable reason.
- `cv_match.py` — RAG over CV chunks (sentence-transformers, cosine retrieval, top-k retrieved evidence + missing skills).
- `classifier.py` — autonomous fallback (scam-first → keyword routing gated on cv_match score).
- Per-thread label apply via three-dots → "Label as" → menuitemcheckbox.
- `history.jsonl` per-cycle log.
- Streamlit dashboard with auto-refresh + sidebar Start/Stop + free-text filter.

### Known thread IDs for test internship emails
```json
["#thread-f:1863448213768020020", "#thread-f:1863448178752084791",
 "#thread-f:1863452174359364087"]
```

### Removing a label (verified)
Same flow as adding — click an already-checked `menuitemcheckbox` to toggle it off; the menu auto-applies on close. No separate "Apply" button observed in this user's Gmail variant.

## What's left (to satisfy the rubric)

- **Real LLM integration.** `classifier.py` is rule-based. For the deliverable, swap (or augment) with a real LLM API call writing to `to_label.json`.
- **Conversation history / chat panel.** History is persisted in `history.jsonl`; an explicit chat panel ("why was email #3 labeled Scam Risk?") would close the rubric's history requirement.
- **README.txt** with run instructions for the grader.
- **IEEE report (≤ 6 pages, ≥ 10 papers from 2025-2026).** Six already scoped: ConFit v2 (2502.12361), CareerBERT (2503.02056), Smart-Hiring (2504.02870), MultiPhishGuard (2505.23803), Fraud-R1 (2502.12904), "LLMs do multi-label differently" (2505.17510). Need ~4 more.
- **7-min video demo.**

## Key Lessons (all saved in memory/)

1. **`venv/Scripts/python -m pip install`** — never bare `pip` (machine has Python 3.11 and 3.14 pointing to different envs).
2. **Real Chrome via subprocess + CDP** — Playwright's Chromium is blocked by Google's automation detection.
3. **Never `wait_for_load_state("networkidle")`** on Gmail — the SPA never reaches idle. Use `domcontentloaded` + element waits.
4. **`sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)`** at the top of every script — Windows cp1252 crashes on non-ASCII print characters.
5. **Label creation:** sidebar `+` only (Settings link is overlay-blocked); `input.type()` + Enter, never `fill()`.
6. **Label application:** per-thread three-dots → "Label as" → `[role="menuitemcheckbox"][title]` with `force=True`. The bulk-flow Label button over-labeled the entire scanned inbox.
7. **Scam Risk is exclusive** — never combine with topic labels.
8. **Recency filter** is the only dedup needed when cycle interval == freshness window. No `processed_ids` cache.
9. **Click targets in inbox rows:** `.y6 / .bog` (subject cell), not row-position — position clicks land on hover-revealed Archive icons.
10. **Full bodies via `div.a3s.aiL`** — row snippets are ~100 chars and let scammers bury the payload.
11. **All files inside the project directory** — venv in `venv/`, browsers in `browsers/`.

## Course Evaluation Weights

Functionality 30% · Report Quality 30% · Code Quality 15% · Presentation 15% · Innovation 10%

Full requirements: `Requrirments/Project_Instructions (1).pdf`
User profile: `plan.txt`
