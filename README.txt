============================================================
GMAIL INTERNSHIP MONITOR
A personal LLM agent that triages internship emails for you
============================================================

Course project for COE548/748 (Specialized LLM Agent).
Author: Yehya Mazloum, Lebanese American University.


====================================================================
WHAT THIS DOES 
====================================================================

You are a final-year engineering student. Your inbox fills up with
internship offers — some real, some scams, some not even matching
your skills. Reading them all is a waste of time.

This project runs a small program in the background that:

  1. Watches your Gmail inbox every 2 minutes(or any cycle time u chose).
  2. Reads each new email's full body.
  3. Decides if it's a scam (asks for "training fees", "deposits",
     fake urgency).
  4. Decides which of YOUR skills it matches (AI/ML, Research,
     Software Engineering, Embedded Systems, DevOps).
  5. Adds Gmail labels automatically: "AI/ML", "Research", "Scam
     Risk", etc.
  6. Lets you see everything in a small web dashboard at
     http://localhost:8501 — cards with the email, why it got each
     label, plus a chat box where you can ask questions about your
     past emails.

The program is fully automatic once it's started. You only sign
into Gmail manually, once per session.


====================================================================
WHAT YOU NEED BEFORE STARTING
====================================================================

  Operating system : Windows 10 or 11 (the project paths assume
                     Windows; macOS / Linux work with minor path
                     tweaks).
  Python           : version 3.11 or newer.
                     Check by running:  python --version
                     If 3.10 or lower, install 3.11+ from
                     https://www.python.org/downloads/
  Google Chrome    : installed at the standard location
                     (C:\Program Files\Google\Chrome\Application\chrome.exe).
  A Gmail account  : the inbox you want monitored. You will sign in
                     manually the first time the agent starts; the
                     session is reused after that.
  Disk space       : ~500 MB free (most of it is Playwright's
                     Chromium + the embedding model).
  Optional         : an OpenAI API key, if you want the LLM-backed
                     classifier path. The system runs end-to-end
                     without one — see the "LLM mode" section
                     below for details.


====================================================================
ONE-TIME SETUP (DO THIS ONCE PER MACHINE)
====================================================================

Step 1.  Get the code.

  git clone https://github.com/Yehya005/Gmail-Internship-Agent.git
  cd Gmail-Internship-Agent


Step 2.  Create a project-local Python environment.

  python -m venv venv

  This makes a "venv" folder inside the project. All Python
  dependencies live there. You never have to worry about polluting
  your global Python install.


Step 3.  Install dependencies.

  venv\Scripts\python -m pip install -r requirements.txt

  IMPORTANT:  use venv\Scripts\python -m pip, NOT bare "pip".
  Some machines have multiple Python versions and bare pip may
  install into the wrong one. The form above is unambiguous.

  This downloads (a) Playwright (browser automation), (b)
  sentence-transformers + torch (the embedding model), (c)
  Streamlit (the dashboard), (d) openai (the optional LLM client).
  Total download is a few hundred MB the first time. Subsequent
  installs are cached.


Step 4.  Download the Chromium binary that Playwright will drive.

  set PLAYWRIGHT_BROWSERS_PATH=%cd%\browsers
  venv\Scripts\python -m playwright install chromium

  This stores Chromium inside the project's "browsers" folder, not
  your AppData. Setting the env var inside the same shell ensures
  the install lands there. About 130 MB.

  NOTE: This step is required even though we ultimately drive your
  REAL Chrome (not Playwright's). Playwright still needs its
  client-side code on disk.


Step 5 (OPTIONAL).  Configure the LLM path.

  If you want the agent to call OpenAI for the labeling decision,
  get a key from https://platform.openai.com/api-keys and set:

    set OPENAI_API_KEY=sk-...your-key-here

  Without this variable, the agent runs the deterministic
  rule-based path instead and the system still works end-to-end.
  The rule-based path is the default for grading runs.

  Cost note: gpt-4o-mini costs about $0.00025 per labeled email
  (one call per email, 1500-token input, 30-token output). 1000
  emails ≈ $0.25. Failures fall back silently to the rule-based
  path so a missing or expired key cannot break the system.


Step 6.  Personalize the CV.

  Open plan.txt in any text editor. Replace the existing CV with
  your own — keep the same broad structure (Skills, Interests,
  Preferred Internship Types, Past Projects). The agent will:

    a. Split your CV into chunks (one per heading, one per
       project bullet).
    b. Tag each chunk with topic labels via a small keyword rule
       table in cv_match.py (which you can edit).
    c. Embed each chunk once at startup.

  At runtime each incoming email is encoded and compared against
  these chunks; the labels you get are grounded in the projects
  you actually have.


====================================================================
RUNNING IT
====================================================================

There are three ways to run, depending on what you're doing.


WAY 1 (RECOMMENDED FOR EVERYDAY USE) — DASHBOARD MODE
-----------------------------------------------------

This is the one you'll use 99% of the time.

  venv\Scripts\python -m streamlit run streamlit_app.py

You'll see something like:

    You can now view your Streamlit app in your browser.
    Local URL: http://localhost:8501

Open that URL. You'll see an empty dashboard plus a sidebar with
"Agent: 🔴 Stopped" and a "Start agent" button.

Click Start agent. Behind the scenes this:

  1. Spawns gmail_agent.py as a subprocess.
  2. The agent launches a fresh Chrome window pointed at gmail.com.
  3. **You log into Gmail manually**, once. The agent waits up to
     5 minutes for you to finish.
  4. The agent creates the six labels in your sidebar (AI/ML,
     Research, Software Engineering, Embedded Systems, DevOps,
     Scam Risk) if they don't already exist.
  5. The agent starts its 2-minute monitoring loop.

The dashboard auto-refreshes every 10 seconds, so as cycles run
you'll see new email cards appear.

To stop the agent, click "Stop agent" in the sidebar. The Chrome
window stays open (so your login session is preserved); just the
Python agent process exits.


WAY 2 — STANDALONE TERMINAL MODE
--------------------------------

Useful if you want to watch the raw cycle log scroll by, without
the dashboard.

  venv\Scripts\python -u gmail_agent.py
  venv\Scripts\python -u gmail_agent.py --interval 2     (custom cadence)

The "-u" flag forces unbuffered stdout so you see lines in real
time. The agent will still create the labels and start cycling —
but you have to watch the terminal yourself.

To stop, hit Ctrl+C in the terminal.


WAY 3 — INDIVIDUAL PIPELINE STEPS (DEBUGGING)
---------------------------------------------

Each pipeline module is also a standalone CLI. Useful if you want
to inspect what one step alone produces:

  venv\Scripts\python read_emails.py --interval 2 --out emails.json
  venv\Scripts\python classifier.py emails.json --output to_label.json
  venv\Scripts\python apply_labels.py --in to_label.json

Step 1 writes a JSON file with what would be processed this cycle.
Step 2 takes that file and decides labels. Step 3 reads the
decisions and applies them in Gmail. You can edit emails.json or
to_label.json by hand between steps if you want to override.


====================================================================
FIRST RUN — WHAT TO EXPECT
====================================================================

The first run takes longer than later runs because:

  1. The sentence-transformers model (about 80 MB) downloads from
     Hugging Face on first call. You'll see "Loading weights
     0%...100%" in the log.
  2. Chrome launches with a fresh empty profile. You manually log
     into Gmail. Login is detected automatically when Gmail's
     "Compose" button appears.
  3. Each of the 6 labels is created via the sidebar "+" button.
     You'll see "Label 'AI/ML' creation submitted." for each.

After cold-start, the agent prints:

    [4/4] Starting monitoring loop (every 2 min).

…and then every 2 minutes:

    Cycle N — 2026-04-26 21:27:41

If you have no new emails, you'll see:

    No new emails this cycle.

Send yourself a test internship email from another account (or a
second Gmail tab) to see a labeled cycle. The agent should report:

    [body-scrape] scraped 137 chars from #thread-f:...
    Read 1 email(s).
    [LABELED AI/ML] (your test subject)
    Applied 1/1 (thread, label) pair(s).

…and the label will appear next to the email in your Gmail
inbox.


====================================================================
THE DASHBOARD IN DETAIL
====================================================================

http://localhost:8501

TOP OF PAGE
  Five counters: emails seen, emails labeled, scam-flagged emails,
  average CV match score, and the timestamp of the most recent
  cycle. A free-text filter box that searches subject / sender /
  body / labels.

SIDEBAR — AGENT
  Status indicator (🟢 Running PID, or 🔴 Stopped). When stopped:
  a Start button + a cycle-interval input (0.5 to 30 minutes,
  default 2). When running: a Stop button.

SIDEBAR — LIVE UPDATES
  Auto-refresh toggle (default ON) + a 5–60 second interval
  slider. With auto-refresh on, the page reloads from
  history.jsonl on the slider's interval so new cycles show up
  without clicking anything.

CARDS (one per email)
  Subject, sender, time received, the label chips that were
  applied. A scam-risk score with the human-readable reasons that
  fired (e.g. "Asks for upfront payment: 'enrollment charge'",
  "Pressure language: 'seats are limited'"). A CV-match score
  with the top-3 retrieved CV chunks (with similarities) and any
  skills the email's job description named that aren't in your
  CV. The full body is in an expander.

CHAT PANEL (bottom of page)
  Type a question. The chat searches history.jsonl and answers
  using only what the agent actually wrote — no LLM call. Try:
    - "why was the latest scam labeled?"
    - "show me AI/ML emails"
    - "best CV match"
    - "anything from a sender named Mazloum?"
  The conversation is stored in your Streamlit session and
  persists across page refreshes (until you click Clear).


====================================================================
HOW IT WORKS UNDER THE HOOD
====================================================================

The system is structured as a thin orchestrator (gmail_agent.py)
that calls three single-purpose modules each cycle:

  STEP 1 — read_emails.py
    Navigates Gmail, scans the inbox rows, recency-filters to
    emails received within the last (cycle interval + 60 second
    safe zone) and not already in the seen-set. For each survivor
    it clicks the subject cell, scrapes div.a3s.aiL for the full
    body, navigates back. Returns a list of email dicts.

  STEP 2 — classifier.py
    For each email:
      - Run scam_scorer.py (rule-based heuristic scoring).
      - If scam score >= 0.5, emit ["Scam Risk"] only. Done.
      - Run cv_match.py: encode the body, cosine-compare against
        pre-encoded CV chunks, return the top-5 retrieved chunks
        with their similarities and topic tags.
      - If the best similarity is below 0.30, the email is
        off-topic for your CV and gets no labels.
      - Otherwise: try the LLM path (if OPENAI_API_KEY is set);
        on any LLM failure, fall back to the rule-based path.
    Both paths see the same data and follow the same rules.

  STEP 3 — apply_labels.py
    For each (thread_id, label) pair: tick the row's checkbox,
    click the bulk-toolbar three-dots overflow, hover "Label as",
    click the matching menuitemcheckbox (idempotent: it reads
    aria-checked first and skips if the label is already on),
    Escape to close the menu, untick the row. Then move to the
    next pair.

After all three steps run the orchestrator appends one record per
email to history.jsonl (full body + scam features + cv_match +
applied labels + timestamp). The dashboard renders from that
file.


====================================================================
FILE LAYOUT
====================================================================

CODE
  gmail_agent.py     Orchestrator. Cold-start (Chrome, login,
                     labels), monitoring loop, history append.
  read_emails.py     Step 1 — scan, recency filter, body scrape.
  classifier.py      Step 2 — scam first, then RAG, then label
                     decision (LLM or rule-based).
  apply_labels.py    Step 3 — per-thread Label-as flow in Gmail.
  scam_scorer.py     Deterministic scam-risk heuristic. Pure
                     Python, no LLM.
  cv_match.py        RAG matcher. Loads plan.txt, splits into
                     chunks, embeds with all-MiniLM-L6-v2, exposes
                     match(email_body) -> retrieved chunks +
                     similarities + missing skills.
  llm.py             Optional OpenAI call with json_schema
                     structured output. Used by classifier.py
                     when OPENAI_API_KEY is set.
  streamlit_app.py   Dashboard with cards, sidebar Start/Stop,
                     auto-refresh, chat panel.

CONFIG / DATA
  plan.txt           Your CV. Drives the RAG chunks and the topic
                     keyword tables.
  requirements.txt   Python dependencies for pip install.
  CLAUDE.md          Extended developer notes (more depth than
                     this README).
  Requriments/       The course's project-instructions PDF.
  report.md          The IEEE-format report draft.

RUNTIME ARTIFACTS (gitignored, created automatically)
  emails.json        Latest cycle's scanned emails (CLI debug runs).
  to_label.json      Override file the agent reads from when
                     individual CLIs are used.
  history.jsonl      Append-only per-cycle log. The dashboard
                     renders from this file. Contains every email
                     the agent has ever processed.
  agent.pid          PID of the agent currently spawned by the
                     dashboard's Start button.
  agent_output.log   Agent stdout/stderr. Useful when debugging.

GENERATED (gitignored)
  venv/              Project-local Python virtualenv.
  browsers/          Playwright Chromium download.
  __pycache__/       Python bytecode caches.


====================================================================
COMMON WORKFLOWS
====================================================================

CHANGE THE LABEL SET
  Edit the LABELS list at the top of gmail_agent.py. Add or
  remove categories. Restart the agent — it will create any new
  labels in Gmail automatically. Topic-keyword rules live in
  cv_match.py (_CHUNK_TOPIC_RULES dict); update those too if you
  add a topic.

CHANGE THE CYCLE CADENCE
  In the dashboard, change the cycle-interval input before
  clicking Start. From the terminal, pass --interval (in minutes,
  e.g. --interval 5).

USE A DIFFERENT LLM MODEL
  Set the LLM_MODEL environment variable, e.g.:
    set LLM_MODEL=gpt-4o
  Default is gpt-4o-mini. Any OpenAI Chat Completions model that
  supports json_schema response format will work.

STOP USING THE LLM PATH
  Unset OPENAI_API_KEY in your environment. The agent will fall
  back to the rule-based classifier on its next cycle.

INSPECT WHY AN EMAIL GOT A LABEL
  In the dashboard chat panel, type:
    "why was email <subject substring>"
  The chat will surface the scam reasons and retrieved CV
  evidence from history.jsonl.

FORGET ALREADY-PROCESSED EMAILS
  Delete history.jsonl. The agent will re-process every email in
  the recency window on its next cycle.

WIPE EVERYTHING AND START FRESH
  Stop the agent. Delete agent.pid, agent_output.log, emails.json,
  to_label.json, history.jsonl. Optionally remove the labels from
  your Gmail account via the sidebar. Restart.


====================================================================
TROUBLESHOOTING
====================================================================

"Connected to existing Chrome session — skipping login step." but
agent does nothing afterward.
  -> Your Chrome instance still has CDP enabled from a previous
     run. The agent connected fine. The next message should be
     about login detection. If it's not appearing, check the
     agent_output.log.

"node:events:486 ... Error: EPIPE: broken pipe ... Node.js v24"
  -> Playwright's Node driver crashed during init. Usually means
     stale CDP session. Close all Chrome windows and try again.

Agent says "[2/4] Waiting for Gmail login (up to 5 minutes)..."
and never proceeds.
  -> The agent waits for Gmail's "Compose" button to render.
     Sometimes Gmail's interstitial pages (consent prompts, the
     "Choose account" page) block this. Click through them in the
     Chrome window manually. The agent will detect the Compose
     button as soon as it appears.

"WARNING: '+' button not found (cannot create '<label>')."
  -> Gmail's UI changed or the sidebar isn't fully rendered.
     Refresh the Gmail tab manually and restart the agent. The
     "+" button is next to the "Labels" heading in the left
     sidebar.

Dashboard shows "🔴 Stopped" but Start button doesn't work.
  -> Check agent_output.log for tracebacks. The Start button
     spawns gmail_agent.py as a subprocess — if it crashes
     immediately, the PID file is cleaned and the dashboard shows
     Stopped again. The exit reason is in the log.

Cards show "scam 0.0" and "cv-match 0.0" for everything.
  -> Either the body scrape failed (you'd see the error in the
     log), or the email is genuinely off-topic for your CV. Open
     the Full Body expander to confirm the body actually has
     content.

"emails.json: 0 emails" but you just sent yourself one.
  -> The recency window is 2 min + 60 s = 3 min by default. If
     the email is older than that, it's intentionally excluded.
     Send a fresh one. Or temporarily increase --interval.

"Agent didn't open the email I sent at 21:42."
  -> Gmail's row tooltip is minute-precision, so an email
     received at 21:42:55 reads as 21:42:00 in the agent's parser.
     The 60-second safe zone is meant to absorb this; if it still
     misses your test email, try sending another one and waiting
     for the next cycle.

"All my emails got labeled with everything!"
  -> Earlier versions had a bulk-flow bug where Gmail's Label
     button applied to all scanned rows. The current per-thread
     three-dots flow is unambiguous. If you see this, you may be
     on stale code; pull from main and restart. Worst case, open
     the labels in your Gmail sidebar and bulk-remove the
     incorrect ones.

"OPENAI_API_KEY not set" or LLM-path 401/429 errors.
  -> Either set the variable or remove it. Both cases trigger the
     rule-based fallback automatically — the cycle still
     completes, the log just notes the LLM call failed.


====================================================================
LIMITATIONS / KNOWN ISSUES
====================================================================

1. Windows-only paths.  The Chrome path and venv\Scripts\python
   layout assume Windows. macOS/Linux work but you'll need to
   change CHROME_PATH in gmail_agent.py and use venv/bin/python.

2. Single-account.  The agent monitors one Gmail account at a
   time — whichever you sign into in the launched Chrome window.

3. Promotions tab.  The inbox scan only sees Gmail's "Primary"
   tab. Emails routed by Gmail to Promotions, Social, or Updates
   are invisible. Disable the tab system in Gmail's settings if
   you want to triage everything.

4. Labels added by the agent count toward Gmail's per-account
   label limit (10,000+). For typical use this is irrelevant.

5. The chat panel's grounded search is keyword-based plus a few
   intent rules — it's not an LLM. It will miss creative phrasings
   that no rule maps onto. Future work would add an LLM path
   here too, mirroring classifier.py.

6. Recency window can miss boundary cases.  Even with the 60-s
   safe zone, if the inbox scan happens at exactly the wrong
   millisecond, an email landing during the scan can slip through.
   Subsequent cycles will pick it up via the seen-set.

7. The agent does not READ outgoing email or Gmail's Sent folder.
   Only the inbox.


====================================================================
GOOD LUCK — AND YES, THE PROGRAM WILL TELL YOU IF SOMEBODY
ASKS YOU TO PAY $1000 FOR AN INTERNSHIP. THAT'S THE WHOLE POINT.
====================================================================
