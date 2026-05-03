# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Course project (COE548/748): an agent that monitors a Gmail inbox every few minutes, scrapes each fresh email's full body, scores it for scam risk, retrieves matching evidence from the user's CV (RAG), and applies one or more labels.

`gmail_agent.py` is now a thin **orchestrator**. Each cycle calls three single-purpose modules in order: `read_emails.read()` → `classifier.classify_emails()` → `apply_labels.apply()`. Each module is also runnable as a CLI (`python read_emails.py`, etc.) for debugging. The classifier's primary path is an LLM call — preferring Claude (Pro/Max subscription via the standalone `claude` CLI) when `CLAUDE_CODE_OAUTH_TOKEN` is set, falling back to direct API (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`), then to a deterministic rule-based path on any failure. If a step raises, the agent logs the error, runs whatever partial work succeeded, and continues to the next cycle.

**Labels** are an *editable list* in `gmail_agent.py` — `LABELS = [...]`. Add or remove categories there; the agent creates each missing one in Gmail at startup (idempotent). Today's default set covers the user's CV: `AI/ML`, `Research`, `Software Engineering`, `Embedded Systems`, `DevOps`, `Scam Risk`. `Scam Risk` is **exclusive** — when applied, no topic labels go on the same email.

**User profile:** Senior Computer Engineering student at LAU. Skills: Python, C, VHDL, SQL, JS, Deep Learning, ML, NLP, TensorFlow, PyTorch, Keras, Flask, React, Streamlit, Git, Linux, Docker. Target roles: AI/ML Engineering Intern, Research Intern (AI/Neuroscience/Bioinformatics), Software Engineering Intern, Embedded Systems Intern. Full CV in `plan.txt`.

## Architecture

```
# Pipeline
gmail_agent.py     ← orchestrator — cycle loop calls the three modules below
read_emails.py     ← step 1: scan inbox + recency + full-body scrape (with
                     dedup against the seen-set built from the active
                     account's history_<email>.jsonl)
classifier.py      ← step 2: heuristics-as-evidence → RAG → LLM-decided
                     labels (including Scam Risk). LLM path is primary
                     (claude_cli / anthropic / openai); deterministic
                     rule-based path with the old scam-score gate is the
                     fallback. Word-boundary keyword matching (`kw_match`
                     from cv_match) so 'ai' matches "in ai." but not
                     "email", and 'ml' stops matching "html".
apply_labels.py    ← step 3: per-thread three-dots → 'Label as' flow

# Decision-side helpers
scam_scorer.py     ← deterministic scam-risk heuristic (no LLM). When
                     the LLM path is active these features are passed to
                     the LLM as EVIDENCE only — the LLM is the sole scam
                     judge. When the LLM is off the heuristic gates the
                     decision (score >= 0.5 → ["Scam Risk"]).
cv_match.py        ← RAG matcher: embeds CV chunks (each tagged with topics +
                     'project'/'generic' kind), cosine retrieval. Exposes
                     kw_match() — a `\b{kw}\b` word-boundary helper used by
                     classifier.py for body-keyword scoring.
llm.py             ← LLM-backed label decider. Provider preference:
                       1. claude_cli — standalone `claude -p` CLI billing
                          via the user's Pro/Max subscription
                          (CLAUDE_CODE_OAUTH_TOKEN). No API-key needed.
                       2. anthropic  — direct API with ANTHROPIC_API_KEY,
                          enum-constrained via forced tool_use call.
                       3. openai    — direct API with OPENAI_API_KEY,
                          enum-constrained via response_format=json_schema.
                     Same RAG-grounded prompt across all three (full CV +
                     top-k retrieved chunks + scam features + email).

# UI
streamlit_app.py   ← dashboard: per-email cards + sidebar Start/Stop + auto-
                     refresh + chat panel grounded in history.jsonl. The chat
                     panel routes through llm.chat_about_history() (same
                     provider as the classifier) when configured, and falls
                     back to a keyword + intent-rule summary otherwise. Header
                     caption shows which classifier is live: 🧠 Claude (Pro
                     subscription) / 🧠 Claude (Anthropic API) / 🧠 GPT
                     (OpenAI) / ⚙️ Rule-based fallback.

# Operator CLIs (all use venv\Scripts\python <name>.py)
start_monitoring.py ← all-in-one launcher: opens Chrome → waits for login →
                      detects the signed-in email → writes account_config.json
                      → launches Streamlit at the matching per-account history
account.py          ← config helper: history_path_for(email),
                      get_active_email(), set_active_email()
create_label.py     ← create one Gmail label by name (idempotent)
switch_account.py   ← prep Chrome for signing into a different Gmail account
reset_history.py    ← archive history.jsonl with a timestamp so the dashboard
                      starts clean for a new account
start_dashboard.py  ← launch Streamlit AND open the URL in the default browser
md_to_docx.py       ← regenerate report.docx from report.md

# Inputs / docs
plan.txt           ← user's CV (drives both topic-keyword tables and CV-RAG)
README.txt        ← per-rubric, full first-time-user walkthrough
report.md          ← IEEE-format report draft (markdown)
report.docx        ← Word version, generated by md_to_docx.py
Requriments/       ← course requirements PDF + sample paper

# Generated / runtime (gitignored)
venv/                  project-local Python virtualenv
browsers/              Playwright's Chromium download
emails.json            step 1 writes; step 2 consumes; dashboard inspects
to_label.json          override file the agent reads when individual CLIs run
account_config.json    {"email": "..."} — written after login, read by agent
                       and dashboard to pick the matching history file
history_<email>.jsonl  per-account append-only per-cycle log (sanitized email
                       in the filename: history_user_at_gmail.com.jsonl)
history.jsonl          legacy fallback used only when no account is set
history_*.jsonl.bak    archives produced by reset_history.py
agent.pid              PID of the agent spawned by the dashboard's Start
agent_output.log       agent stdout/stderr
streamlit_boot.log     Streamlit's boot log; start_dashboard.py reads URL
```

**Per-account history.** `account.get_active_history_path()` is called at
runtime by both `gmail_agent.py` (for the seen-set + appending records) and
`streamlit_app.py` (for rendering cards). Both pick up whichever account
`account_config.json` currently points at, so signing into a previously-
monitored Gmail picks up its prior records instead of starting empty.

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
   - **Heuristic features (always run, no early-exit).** `scam_scorer.score_email_dict(...)` populates `scam_features` (score + reasons + flag dict). When the LLM path is active these are EVIDENCE for the model, not a verdict. When only the rule path runs they gate the final decision.
   - **RAG retrieval (always run).** `cv_match.match(body)` returns the top-5 retrieved CV chunks each with `{similarity, topics, kind}`. The `cv_match` block is attached to every email so the dashboard always has retrieval evidence to display, even for emails the LLM ends up flagging as Scam Risk.
   - **LLM path (primary).** When any provider is available (`llm.llm_available()` checks `CLAUDE_CODE_OAUTH_TOKEN` → `ANTHROPIC_API_KEY` → `OPENAI_API_KEY`), call `llm.decide_labels(email, scam_features, cv_match, allowed_labels)`. The model sees the full email + scam features + retrieved CV chunks (RAG context) + the candidate's CV + the allowed-labels enum, and returns the label list. **The LLM is the sole scam judge in this path** — there's no deterministic gate in front of it, so the model can flag scams the heuristic misses (subtler social engineering) and clear false positives where the lexicon misfires. Anthropic API constrains output via a forced tool-use input_schema enum; OpenAI via `response_format=json_schema`; the Claude CLI relies on a strict-JSON instruction + post-validation. On any LLM failure (parse error, rate-limit, network), fall through to the rule-based path.
   - **Rule-based fallback** — deterministic, no LLM:
     - **Scam gate.** If `scam_features.score ≥ 0.5`, return `["Scam Risk"]` only (Scam Risk is exclusive).
     - **Off-topic gate.** Else if the best CV-chunk similarity is below 0.30, return `[]`.
     - **Two-tier RAG voting** over project chunks at sim ≥ 0.30:
       - **High-confidence** (sim ≥ 0.50): emit each of the chunk's topics directly. Multiple high-scoring chunks for different topics will multi-label even on terse bodies.
       - **Moderate** (0.30 ≤ sim < 0.50): emit a topic only if the email body literally contains one of that topic's keywords. Stops a moderately-similar project (e.g. CPU Simulator at 0.34) from dragging Software Engineering onto every tech email.
     - **Body-keyword safety net.** For any topic not yet emitted, add it if the body literally mentions one of its keywords (using `kw_match` — a `\b{kw}\b` word-boundary regex so 'ai' matches "in ai." but not "email", and 'ml' stops matching "html"). Catches two cases: (a) topics not covered by any project chunk (DevOps in this CV); (b) topics whose project chunk ranked just below threshold even though the email mentions them. Off-topic pitches are still blocked by the cv_match threshold gate above, so this won't fire on marketing-style content.
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

# Pick ONE of these to activate the LLM classifier path. Without any
# of them, the rule-based path runs and the system still works end-to-end.

# Recommended — uses your Claude Pro/Max subscription, no API-key billing.
# 1) Install the standalone Claude Code CLI:
winget install Anthropic.ClaudeCode
# 2) Generate a long-lived OAuth token (browser opens once):
claude setup-token
# 3) Persist it for future shells:
setx CLAUDE_CODE_OAUTH_TOKEN "sk-ant-oat01-..."

# Alternative — direct Anthropic API (separate billing):
set ANTHROPIC_API_KEY=sk-ant-...

# Alternative — OpenAI API:
set OPENAI_API_KEY=sk-...
```

## Running

```bash
# A) Recommended — opens Chrome, waits for login, detects the email,
#    then opens the dashboard pointed at THAT account's history file.
venv\Scripts\python start_monitoring.py

# B) Dashboard alone (Chrome already open, account_config.json already set)
venv\Scripts\python start_dashboard.py
venv\Scripts\python -m streamlit run streamlit_app.py     # no auto-open

# C) Standalone agent (no dashboard)
venv\Scripts\python -u gmail_agent.py
venv\Scripts\python -u gmail_agent.py --interval 2

# D) Individual pipeline modules (debugging)
venv\Scripts\python read_emails.py --interval 2 --out emails.json
venv\Scripts\python classifier.py emails.json --output to_label.json
venv\Scripts\python apply_labels.py --in to_label.json

# E) Operator CLIs
venv\Scripts\python create_label.py "<name>"     # add a Gmail label
venv\Scripts\python switch_account.py             # prep Chrome for new Gmail
venv\Scripts\python reset_history.py              # archive old account's history
venv\Scripts\python md_to_docx.py                 # regenerate report.docx
```

`--interval` is in minutes (float, default 2) and applies to both the recency window and the inter-cycle sleep. Classification is in-process and synchronous (no IPC wait flag).

### Switching to a different Gmail account

Run:

```bash
venv\Scripts\python start_monitoring.py
```

Sign into whichever account you want to monitor in the Chrome window that
opens. After login is detected, `start_monitoring.py` writes the email to
`account_config.json`, picks the matching `history_<email>.jsonl` (creating
it if it's a first-time account, reusing it if you've monitored this account
before), and launches the dashboard pointed at that file. Click **Start agent**
in the dashboard sidebar.

There's no `reset_history.py` step in this flow — the dashboard never mixes
two accounts' records because each account has its own history file.

## Current Status (session 2026-04-27)

### What works end-to-end
- Real-Chrome connect via CDP; manual login detection.
- Variable-size `LABELS` set, idempotent creation via the sidebar `+` button.
- Per-row inbox scan with timestamp parsing + recency filter + 60-s safe zone.
- Full-body scrape per fresh email (subject-cell click via real-MouseEvent dispatch).
- `scam_scorer.py` — heuristic features (sender domain, payment-phrase lexicon incl. `$N fee/charge` regex, no-interview / fast-track signals, generic greetings, suspicious URLs, urgency, hyperbolic comp, all-caps subject), each with a human-readable reason.
- `cv_match.py` — RAG over CV chunks (sentence-transformers, cosine retrieval, top-k retrieved evidence + missing skills). Word-boundary keyword helper `kw_match()` shared with classifier.py.
- `classifier.py` — LLM-first decision step. `llm.decide_labels()` is called whenever any provider key is set and is the sole scam judge in that path (the deterministic 0.5 gate only kicks in when the LLM is off). Heuristic + RAG features always populate the email dict so the dashboard shows them either way.
- `llm.py` — three providers, picked by env: claude_cli (Pro subscription via standalone CLI) → anthropic (API key, forced tool-use enum) → openai (API key, response_format=json_schema). Same RAG-grounded prompt across all three.
- Per-thread label apply via three-dots → "Label as" → menuitemcheckbox.
- Per-account `history_<email>.jsonl` log (legacy `history.jsonl` is the no-account fallback).
- `start_monitoring.py` all-in-one launcher: opens Chrome → waits for login → detects email → opens dashboard at the matching per-account history. Login-gated: exits cleanly on timeout, never opens the UI without an account. Dashboard subprocess spawned with `CREATE_BREAKAWAY_FROM_JOB | CREATE_NEW_PROCESS_GROUP` so it survives the launcher's exit on Windows.
- Streamlit dashboard with auto-refresh + sidebar Start/Stop + free-text filter + chat panel + provider-aware caption (🧠 Claude Pro / Anthropic / OpenAI / ⚙️ Rules).

### Known thread IDs for test internship emails
```json
["#thread-f:1863448213768020020", "#thread-f:1863448178752084791",
 "#thread-f:1863452174359364087"]
```

### Removing a label (verified)
Same flow as adding — click an already-checked `menuitemcheckbox` to toggle it off; the menu auto-applies on close. No separate "Apply" button observed in this user's Gmail variant.

## Rubric status

- ✅ **Custom LLM Agent.** `llm.py` calls Claude (subscription via standalone CLI by default; Anthropic / OpenAI APIs as alternatives) with the RAG-retrieved CV chunks as grounding and an enum-constrained label output. The LLM is the **sole scam judge** in this path — `classifier.py` no longer short-circuits on the heuristic scam score; the deterministic features become EVIDENCE in the prompt and the model decides every label including Scam Risk. `classifier.py` routes through the LLM whenever any provider key is set; falls back to rule-based (with the original 0.5 scam gate) on any error.
- ✅ **RAG with vector embeddings.** `cv_match.py` — sentence-transformers `all-MiniLM-L6-v2`, cosine retrieval, project chunks tagged with topics.
- ✅ **3+ tools, ≥1 custom.** Gmail browser-automation (custom), `scam_scorer` (custom), `cv_match` RAG (uses HF Hub model), Claude / OpenAI API call from `llm.py` (external API).
- ✅ **UI.** Streamlit dashboard with per-email cards, auto-refresh, sidebar Start/Stop, and a chat panel grounded in `history.jsonl`.
- ✅ **Conversation history + error handling.** Chat panel persists via `st.session_state`; cycle persistence in `history.jsonl`; try/except wrappers around each pipeline step.
- ✅ **README.txt** — full first-time-user walkthrough.
- ✅ **IEEE report draft** (`report.md` + `report.docx`, 10 papers from 2025-2026).
- ❌ **7-min video demo** — needs a recording session in front of the dashboard.

## Recent additions

- **LLM is now the sole scam judge** when any provider is configured. `classifier.py` no longer short-circuits to `["Scam Risk"]` on `scam_score ≥ 0.5`; the heuristic features become EVIDENCE in the LLM's prompt, and the LLM decides everything (including scam). Verified end-to-end: a "pay 50 deposit, no interview, fast-track" email flagged correctly even though the deterministic scorer only gave it 0.1. The rule-based fallback keeps the original 0.5 gate so the system still works without a provider.
- `llm.py` system prompt strengthened: explicitly states the model is the sole scam judge, treats `scam_features` as hints rather than verdicts, and lists the patterns to watch for (upfront fees, no-interview / fast-track, urgency cues, payment-to-receive-offer).
- `llm.py` rewrite: three-provider routing (claude_cli / anthropic / openai). Default path uses the user's Claude Pro/Max subscription via the standalone `claude -p` CLI — no API-key billing.
- `classifier.py` LLM-first path with rule-based fallback.
- Word-boundary keyword matching (`kw_match` in cv_match.py): 'ai' matches "in ai." but not "email"; 'ml' stops matching "html". Shared between `_raw_chunk_topics` (chunk topic-tagging) and the classifier's body-keyword safety net.
- Chat panel in the dashboard with intent recognition (`why`, `scam`, `best`, label-name) over `history.jsonl`.
- Two-tier RAG voting: high-sim project chunks emit topics directly; moderate matches need body-keyword confirmation; body-keyword safety net for missed labels.
- `seen` set loaded from `history.jsonl` so cycle dedup survives restarts.
- `account.py` + per-account `history_<email>.jsonl` files — switching back to a previously-monitored Gmail picks up its prior records instead of starting empty.
- `start_monitoring.py` all-in-one launcher: opens Chrome → waits for login → detects the signed-in email → writes `account_config.json` → launches Streamlit at the matching per-account history. Login-gated, no orphan UI on timeout.
- Dashboard provider-aware caption (🧠 Claude Pro / Anthropic / OpenAI / ⚙️ Rules) so the demo viewer can see which classifier is live.
- Operator CLIs: `create_label.py`, `switch_account.py`, `reset_history.py`, `start_dashboard.py`, `md_to_docx.py`.

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
13. **Word-boundary keyword matching** (`\b{kw}\b`): bare 'ai' or 'ml' would otherwise either miss "in ai." or fire on "email" / "html". Use `kw_match()` from cv_match.py everywhere keywords are tested against text.
14. **Login → UI, never UI on login failure.** `start_monitoring.py` exits cleanly when login times out. Falling back to a legacy-history dashboard left an orphan tab pointed at the wrong account, and a re-run sat waiting for login behind that orphan.
15. **Windows job teardown breaks subprocess chains.** When `start_monitoring.py` is invoked from a PowerShell background task / VS Code task runner, every subprocess.Popen child joins the parent's Windows Job by default — and when the parent task ends, the OS tears down the entire Job. Streamlit + the agent it just spawned both die mid-import with `KeyboardInterrupt`. Spawn the dashboard with `CREATE_BREAKAWAY_FROM_JOB | CREATE_NEW_PROCESS_GROUP` so it escapes the parent's Job. The agent inherits Streamlit's no-Job status from there.
16. **Heuristic features as LLM evidence, not as a gate.** When the LLM is in charge, don't short-circuit on the deterministic scam score — pass the heuristic flags into the prompt and let the model decide. The scorer's lexicon misses subtle social engineering (low score, real scam) and over-fires on legitimate paid internships (high score, false positive). Caveat: keep the heuristic gate in the rule-based fallback so the no-LLM path still rejects obvious scams.

## Course Evaluation Weights

Functionality 30% · Report Quality 30% · Code Quality 15% · Presentation 15% · Innovation 10%

Full requirements: `Requrirments/Project_Instructions (1).pdf`
User profile: `plan.txt`
