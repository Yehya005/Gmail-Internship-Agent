Gmail Internship Monitor
========================

A personal Gmail agent that monitors the inbox every few minutes,
scrapes each fresh email's full body, scores it for scam risk, retrieves
matching evidence from the user's CV via RAG (sentence-transformers +
cosine over CV chunks), and applies one or more topic labels. An
optional OpenAI-backed classifier path replaces the rule-based decision
when an API key is present; without a key the rule-based path runs and
the system still works end-to-end.

Course project for COE548/748 (Specialized LLM Agent).


WHAT'S IN THIS REPOSITORY
-------------------------

  gmail_agent.py     orchestrator — runs the cycle loop
  read_emails.py     step 1 — scan inbox, recency-filter, scrape full bodies
  classifier.py      step 2 — scam → RAG → labels (LLM path or rule-based)
  apply_labels.py    step 3 — apply labels in Gmail per-thread
  scam_scorer.py     deterministic scam-risk heuristic (no LLM)
  cv_match.py        RAG matcher: embeds CV chunks, returns retrieved evidence
  llm.py             OpenAI structured-output call used by classifier
  streamlit_app.py   dashboard with cards, scam reasons, CV-match evidence,
                     auto-refresh, sidebar Start/Stop, and a chat panel
                     grounded in history.jsonl
  plan.txt           the candidate's CV (drives both keyword tables and
                     the CV-RAG chunks)
  requirements.txt   Python dependencies
  CLAUDE.md          extended developer notes (architecture, flow, lessons)
  Requriments/       course requirements PDF + sample paper


SETUP (run once on a new machine)
---------------------------------

  cd <project-root>

  # Project-local venv
  python -m venv venv

  # Install dependencies (playwright, sentence-transformers, streamlit,
  # openai, ...)
  venv\Scripts\python -m pip install -r requirements.txt

  # Download Playwright Chromium into the project's browsers/ folder
  set PLAYWRIGHT_BROWSERS_PATH=<project-root>\browsers
  venv\Scripts\python -m playwright install chromium

  # Optional — enables the LLM-backed classifier path. Without this var
  # the system runs the deterministic rule-based path.
  set OPENAI_API_KEY=[INSERT API KEY HERE]


RUNNING
-------

  # A) Standalone agent run
  venv\Scripts\python -u gmail_agent.py
  venv\Scripts\python -u gmail_agent.py --interval 2

  # B) Dashboard (recommended) — Start/Stop in the sidebar, live cards
  venv\Scripts\python -m streamlit run streamlit_app.py
  # Then open http://localhost:8501

  # C) Pipeline modules as standalone CLIs (debugging)
  venv\Scripts\python read_emails.py --interval 2 --out emails.json
  venv\Scripts\python classifier.py emails.json --output to_label.json
  venv\Scripts\python apply_labels.py --in to_label.json

The first time you run the agent it launches a fresh Chrome instance at
mail.google.com. Sign in once — the session is reused on subsequent
runs over the Chrome DevTools Protocol (port 9222). After login the
agent creates each label in the LABELS list at the top of
gmail_agent.py if it isn't already in the sidebar (idempotent).


HOW IT WORKS
------------

Cycle (default 2 min):
  1. Scan inbox. Extract per-row {thread_id, subject, sender,
     received_ms} and recency-filter to emails received in the last
     cycle window + a 60-second safe zone (Gmail timestamps are
     minute-precision).
  2. For each fresh email not previously processed, click the subject
     cell to open the conversation, scrape div.a3s.aiL for the full
     body, return to the inbox.
  3. Run the scam scorer: deterministic heuristics over sender domain,
     payment-phrase lexicon (incl. a "$N + deposit/fee" regex),
     no-interview / fast-track signals, generic greetings, suspicious
     URLs, urgency cues, hyperbolic comp claims. Returns score + a
     human-readable reason list.
  4. Run the CV-match RAG retriever: encode the body with
     all-MiniLM-L6-v2, cosine against pre-encoded CV chunks (each
     tagged with topic labels at load time). Returns top-k retrieved
     chunks + max similarity + missing-skills list.
  5. Decide labels:
        - Scam Risk score >= 0.5  ->  ["Scam Risk"] only (exclusive).
        - Otherwise, if OPENAI_API_KEY is set, llm.decide_labels(...)
          builds a prompt with the email + CV evidence + scam features
          and asks OpenAI for a JSON list of labels (json_schema-strict
          output).
        - On any LLM failure (no key, network, parse error), the
          rule-based path takes over: project chunks emit their topics
          when sim >= 0.50 unconditionally, or when a body keyword
          confirms moderate sim 0.30-0.50; a body-keyword safety net
          adds any remaining topic the body explicitly mentions.
  6. Apply each (thread, label) pair via the Gmail UI: tick the row
     checkbox, open the bulk-toolbar three-dots overflow, hover
     "Label as", click the menuitemcheckbox for the label (state-aware
     so it's idempotent), close the menu, untick the row. Skip pairs
     where the label is already on the thread.
  7. Append per-email records to history.jsonl (full email + scam
     features + cv_match + applied labels + cycle timestamp). The
     dashboard renders from this file.


DASHBOARD
---------

  http://localhost:8501

Top: counters (emails seen / labeled / scam-flagged / average CV match
/ last cycle time) and a free-text filter.

Sidebar:
  - Agent: status (Running PID / Stopped), cycle-interval input,
    Start/Stop buttons. PID tracked in agent.pid for the dashboard's
    own bookkeeping; stale PIDs auto-clean.
  - Live updates: auto-refresh toggle (default on) + 5-60 s interval.

Per-email cards: subject, sender, time, applied label chips, scam-risk
score with reasons, CV-match score with retrieved CV chunks + missing
skills, full body in an expander.

Chat panel: ask questions like "why was the latest scam labeled?",
"show me AI/ML emails", "best CV match". Answers are grounded in
history.jsonl — every fact comes from a record the agent actually
wrote.


KEY FILES PRODUCED AT RUNTIME (gitignored)
------------------------------------------

  emails.json       latest cycle's scanned emails (used by CLI debug runs)
  to_label.json     optional override file (CLI debug runs)
  history.jsonl     append-only per-cycle log; the dashboard renders
                    from this file
  agent.pid         PID of the agent spawned by the dashboard
  agent_output.log  agent stdout/stderr


REPRODUCIBILITY NOTES
---------------------

- Always use `venv\Scripts\python -m pip install`, never bare `pip` —
  this machine has Python 3.11 (`python`) and 3.14 (`pip`) which point
  to different environments.
- Never use Playwright's bundled Chromium for Gmail — Google blocks
  sign-in on automated browsers. We launch real Chrome as a subprocess
  and connect over CDP.
- Never use `wait_for_load_state("networkidle")` on Gmail; the SPA
  never reaches idle. Use `domcontentloaded` + element waits.
- All files stay inside the project directory: venv in `venv/`,
  Playwright Chromium in `browsers/`.

See CLAUDE.md for a longer write-up of the architecture, the per-cycle
flow, and the design lessons learned.
