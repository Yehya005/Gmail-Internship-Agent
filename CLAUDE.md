# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Course project (COE548/748): an agent that monitors a Gmail inbox every few minutes, scrapes each fresh email's full body, scores it for scam risk, retrieves matching evidence from the user's CV (RAG), and applies one or more labels.

`gmail_agent.py` is now a thin **orchestrator**. Each cycle calls three single-purpose modules in order: `read_emails.read()` → `classifier.classify_emails()` → `apply_labels.apply()`. Each module is also runnable as a CLI (`python read_emails.py`, etc.) for debugging. No IPC wait, no external decider — the rule-based classifier is in-process and synchronous. If a step raises, the agent logs the error, runs whatever partial work succeeded, and continues to the next cycle.

**Labels** are an *editable list* in `gmail_agent.py` — `LABELS = [...]`. Add or remove categories there; the agent creates each missing one in Gmail at startup (idempotent). Today's default set covers the user's CV: `AI/ML`, `Research`, `Software Engineering`, `Embedded Systems`, `DevOps`, `Scam Risk`. `Scam Risk` is **exclusive** — when applied, no topic labels go on the same email.

**User profile:** Senior Computer Engineering student at LAU. Skills: Python, C, VHDL, SQL, JS, Deep Learning, ML, NLP, TensorFlow, PyTorch, Keras, Flask, React, Streamlit, Git, Linux, Docker. Target roles: AI/ML Engineering Intern, Research Intern (AI/Neuroscience/Bioinformatics), Software Engineering Intern, Embedded Systems Intern. Full CV in `plan.txt`.

## Architecture

```
gmail_agent.py     ← orchestrator — cycle loop calls the three modules below
read_emails.py     ← step 1: scan inbox + recency + full-body scrape
classifier.py      ← step 2: scam-first → RAG → topic union with body confirm
apply_labels.py    ← step 3: per-thread three-dots → 'Label as' flow

scam_scorer.py     ← deterministic scam-risk heuristic (no LLM)
cv_match.py        ← RAG matcher: embeds CV chunks (with topic tags), cosine
streamlit_app.py   ← dashboard: per-email cards + sidebar Start/Stop + auto-refresh

plan.txt           ← user's CV (drives both topic-keyword tables and CV-RAG chunks)
venv/              ← project-local Python virtualenv (use venv/Scripts/python)
browsers/          ← Playwright's Chromium (PLAYWRIGHT_BROWSERS_PATH)

emails.json        ← step 1 writes; step 2 consumes; the dashboard inspects
history.jsonl      ← append-only per-cycle log; dashboard renders from this
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

6. **Step 1 — `read_emails.read(page, cycle_seconds)`.** Navigate to `#inbox`, query `tr.zA`, extract per-row `{thread_id, subject, sender, received_ms}` (timestamp parsed from `td.xW span[title]` with fallbacks). Recency-filter: keep `received_ms ≥ now − cycle − 60 s` (the 60-s safe zone covers Gmail's minute-only timestamps). For each survivor, dispatch a real `mousedown/mouseup/click` chain on `.y6/.bog` inside a single `page.evaluate` so DOM re-renders can't detach the locator; URL flips to `#inbox/FMfcgz...`; scrape `div.a3s.aiL` for the full body; navigate back. Returns the partial set on any per-email failure so step 2 can still run on what made it through.
7. **Step 2 — `classifier.classify_emails(emails)`.** Pure-Python decision, no Chrome. Per email:
   - `scam_scorer.score_email_dict(...)` → if score ≥ 0.5, label is `["Scam Risk"]` only and we stop (Scam Risk is exclusive).
   - Else `cv_match.match(body)` returns the top-5 retrieved CV chunks each with `{similarity, topics, kind}`. If max similarity < 0.30 the email is off-topic for this CV → no labels. Generic CV sections (Skills, Interests, …) carry no topics by design — they only contribute to the similarity score.
   - **Two-tier RAG voting** over project chunks at sim ≥ 0.30:
       - **High-confidence** (sim ≥ 0.50): emit each of the chunk's topics directly. Multiple high-scoring chunks for different topics will multi-label even on terse bodies.
       - **Moderate** (0.30 ≤ sim < 0.50): emit a topic only if the email body literally contains one of that topic's keywords. Stops a moderately-similar project (e.g. CPU Simulator at 0.34) from dragging Software Engineering onto every tech email.
   - **Body-keyword safety net.** For any topic not yet emitted, add it if the body literally mentions one of its keywords. Catches two cases: (a) topics not covered by any project chunk (DevOps in this CV); (b) topics whose project chunk ranked just below threshold even though the email mentions them (e.g. a 'research using deep learning' email where ECG dominates retrieval and EEG sits at sim 0.26). Off-topic pitches are still blocked by the cv_match threshold gate above, so this won't fire on marketing-style content.
   - Each email's dict is mutated in place to attach the `scam_features` and `cv_match` blocks (used by `history.jsonl` + the dashboard).
8. **Step 3 — `apply_labels.apply(page, thread_labels)`.** For each `(thread, label)` pair, in isolation:
   - Tick the row's checkbox via JS on `.oZ-jc` / `[role="checkbox"]`.
   - Dismiss any leftover Gmail toast, then `force`-click bulk-toolbar `[data-tooltip="More"]`.
   - Hover `[role="menuitem"]:has-text("Label as")` — `aria-haspopup="true"`, opens on hover not click.
   - Read `aria-checked` on `[role="menuitemcheckbox"][title="<label>"]`. Skip click if already in desired state (idempotent).
   - Otherwise `force`-click it (submenu animation defeats Playwright's stability check; click still reaches Gmail).
   - Escape twice + untick the row before the next pair.
9. **Append per-email records to `history.jsonl`** — full email + `labels_applied` + `cycle_at`. Streamlit reads this.
10. **Sleep `--interval` minutes**, loop back to step 6.

If any step raises, the agent's `_run_*` wrapper logs the exception and the cycle continues with whatever it has — partial reads still get classified, classification failures don't block the apply step (it just gets `{}`), apply failures don't block history logging.

### UI side (parallel)

14. **Streamlit dashboard** at `http://localhost:8501`:
    - Reads `history.jsonl` on every render. Renders one card per email with subject, sender, time, label chips, scam score + reason list, CV match score + retrieved CV evidence + missing skills, and the body in an expander.
    - Top: counters (emails seen, labeled, scam-flagged, average CV match, last cycle time) and a free-text filter.
    - **Sidebar — Agent.** Status (🟢 PID / 🔴 Stopped) + cycle-interval input + Start/Stop buttons. PID tracked in `agent.pid` so state survives Streamlit reruns. Stale PID files (process exited) auto-clean.
    - **Sidebar — Live updates.** Auto-refresh toggle (default on) + 5–60 s interval slider (default 10). Manual Refresh button when paused.

## File formats

- `emails.json` — written by `read_emails.read()` (when called via CLI or for inspection). List of `{thread_id, subject, sender, body, received_ms}`. The orchestrator passes the in-memory list to step 2 directly without round-tripping through disk.
- `history.jsonl` — append-only, one JSON object per line. Same shape as an emails.json entry plus the `scam_features`, `cv_match`, `labels_applied`, and `cycle_at` fields added during the cycle. The Streamlit dashboard renders from this file.

## Setup (run once on a new machine)

```bash
cd c:\Users\Yehya\Downloads\LLMIntern

# Project-local venv
python -m venv venv

# All deps (playwright, sentence-transformers, streamlit, openai, …)
venv\Scripts\python -m pip install -r requirements.txt

# Playwright Chromium into the project browsers/ directory
set PLAYWRIGHT_BROWSERS_PATH=c:\Users\Yehya\Downloads\LLMIntern\browsers
venv\Scripts\python -m playwright install chromium

# Optional — enable the LLM-backed classifier path. Without this var
# the rule-based path runs and the system still works end-to-end.
set OPENAI_API_KEY=[INSERT API KEY HERE]
```

## Running

```bash
# A) Standalone agent run
venv\Scripts\python -u gmail_agent.py
venv\Scripts\python -u gmail_agent.py --interval 2

# B) Dashboard (recommended) — handles Start/Stop in the sidebar
venv\Scripts\python -m streamlit run streamlit_app.py

# C) CLI access to the individual pipeline modules (for debugging)
venv\Scripts\python read_emails.py --interval 2 --out emails.json
venv\Scripts\python classifier.py emails.json --output to_label.json
venv\Scripts\python apply_labels.py --in to_label.json
```

`--interval` is in minutes (float, default 2) and applies to both the recency window and the inter-cycle sleep. The orchestrator no longer takes `--ipc-wait` — classification is in-process and synchronous now that the rule-based decision lives in `classifier.py`.

## Current Status (session 2026-04-26)

### What works end-to-end
- Real-Chrome connect via CDP; manual login detection.
- Variable-size `LABELS` set, idempotent creation via the sidebar `+` button.
- Per-row inbox scan with timestamp parsing + recency filter + 60-s safe zone.
- Full-body scrape per fresh email (subject-cell click via real-MouseEvent dispatch).
- `scam_scorer.py` — heuristic features (sender domain, payment-phrase lexicon incl. `$N fee/charge` regex, no-interview / fast-track signals, generic greetings, suspicious URLs, urgency, hyperbolic comp, all-caps subject), each with a human-readable reason.
- `cv_match.py` — RAG over CV chunks (sentence-transformers, cosine retrieval, top-k retrieved evidence + missing skills).
- `classifier.py` — autonomous decision step. Tries the LLM path first when `OPENAI_API_KEY` is set; falls back to the deterministic rule-based path on any error (missing key, network failure, parse mismatch, …) so the system always works.
- `llm.py` — single-shot OpenAI call with structured-JSON output. Builds a prompt from the email + scam features + retrieved CV chunks (RAG context) + allowed labels, returns the parsed label list.
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
11. **Two-tier classifier voting:** strong RAG signals (sim ≥ 0.50) are trusted directly so multi-label cases work on terse bodies; moderate matches (0.30–0.50) require a body-keyword confirmation; topics not yet emitted get a body-keyword safety net so EEG-Research-style misses on terse-CV chunks aren't dropped.
12. **All files inside the project directory** — venv in `venv/`, browsers in `browsers/`.

## Course Evaluation Weights

Functionality 30% · Report Quality 30% · Code Quality 15% · Presentation 15% · Innovation 10%

Full requirements: `Requrirments/Project_Instructions (1).pdf`
User profile: `plan.txt`
