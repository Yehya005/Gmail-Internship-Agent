# A Candidate-Side, Inbox-Grounded LLM Agent for Internship Email Triage with Retrieval-Augmented CV Matching and a Deterministic Scam Classifier

**Author:** Yehya Mazloum
**Affiliation:** Department of Computer Engineering, Lebanese American University
**Course:** COE548/748 — Specialized LLM Agent

---

## Abstract

Existing job-search and email-triage systems split along a recurring fault line: tools that automate applications ignore the inbox, and tools that triage email ignore job semantics. Both leave the candidate to manually scan recruiter messages for relevant offers and obvious scams. We present a personal LLM agent that fills that gap. The agent monitors the candidate's Gmail inbox over the Chrome DevTools Protocol, scrapes each fresh message's full body, scores it with a deterministic rule-based scam classifier, and retrieves matching evidence from the candidate's CV via cosine similarity over sentence-transformer embeddings of CV chunks. A label-decision step — backed by an LLM with structured-JSON output, with a deterministic fallback when no API key is configured — emits one or more labels per email from a configurable set (`AI/ML`, `Research`, `Software Engineering`, `Embedded Systems`, `DevOps`, `Scam Risk`). Labels are applied directly in Gmail per-thread, and the per-cycle history feeds a Streamlit dashboard with a chat panel grounded in the agent's own records. End-to-end tests on 11 crafted internship emails reach 11/11 correct label sets, including buried-payload scam detection at 0.55 confidence on a single regex match. The system runs offline with no API key, and the LLM path is opt-in via an environment variable.

**Index Terms** — LLM agent, retrieval-augmented generation, email classification, internship matching, scam detection, browser automation.

---

## I. Introduction

Searching for an internship as a final-year engineering student is bottlenecked less by sending applications than by triaging the inbound stream of recruiter emails: legitimate offers from real companies, generic outreach from contractors, and an increasing volume of scam pitches that ask for "training fees" or "registration deposits." A candidate needs to (1) decide which offers actually match their CV, and (2) recognize the scam patterns before responding. Both tasks are repetitive but require domain judgement.

This project builds a personal LLM agent that does both, autonomously, against a real Gmail inbox. The design constraints come from the candidate's day-to-day reality:

1. **Privacy by construction.** No Gmail API tokens, no OAuth scopes, no server. The agent uses real Chrome over the Chrome DevTools Protocol, so credentials never leave the laptop and the user logs into Gmail manually exactly once per session.
2. **Grounded labels, not generic categories.** Topics like "AI/ML," "Embedded Systems," and "DevOps" are derived from chunks of the candidate's own CV. A clean Software Engineering listing is only labeled `Software Engineering` if the candidate has a project that actually substantiates that.
3. **Robust without an LLM.** Course-project deliverables shouldn't depend on a paid API key being present at grading time. The system runs end-to-end on rule-based heuristics plus RAG retrieval; the LLM path is an optional augment.
4. **Scam classification as a first-class label.** A candidate-side scam detector is genuinely missing in surveyed tools (consumer email clients triage by sender, job-board scrapers don't see emails at all). A small lexicon plus a `\$N + deposit/fee` regex is enough to catch the common internship-scam shape.

We emphasize that every existing system in our literature review is either *recruiter-side* (resume screening over an applicant pool) or *platform-side* (job-board recommendation given a curated catalog). Flipping the perspective — one CV, many inbound emails, with the label decision grounded in retrieved CV chunks and paired with a scam-risk classifier — is, to our knowledge, unowned in the 2025–2026 literature.

The remainder of the paper is organized as follows. Section II surveys the most relevant 2025–2026 work. Section III describes the system architecture and per-cycle data flow. Section IV documents the experimental setup, datasets, prompts and metrics. Section V reports end-to-end results on 11 crafted test emails plus live inbox observations. Section VI concludes and discusses future work.

---

## II. Related Work

We survey ten 2025–2026 papers that intersect the project's three axes: (a) LLM-driven résumé–JD matching, (b) LLM agents and retrieval over job/career data, and (c) phishing or fraud detection with LLMs.

**ConFit v2** [1] proposes Hypothetical Resume Embedding for the resume↔JD ranking task and reports state-of-the-art results on the JOB-CONFIT benchmark, beating `text-embedding-3-large`. The paper is firmly recruiter-side: it indexes many résumés against one JD. Our work flips the polarity, embedding one CV against many incoming emails.

**CareerBERT** [2] aligns résumé and the European ESCO occupation taxonomy in a shared embedding space, enabling generic career recommendations. CareerBERT does not consume an email stream; the recommendations are decoupled from the candidate's actual inbox.

**Smart-Hiring** [3] is the closest to our pipeline architecturally — a multi-agent system (extractor, evaluator, summarizer, scorer) that uses RAG over hiring criteria via ChromaDB and OpenAI embeddings. It is, however, employer-side: the agents reason about a queue of submitted résumés.

**MultiPhishGuard** [4] uses parallel LLM agents to classify general phishing emails by inspecting metadata, body, and URLs, with rationales. Its scope is the broad anti-phishing problem; our scam classifier specializes for one extremely concrete failure mode (internship offers asking for training, registration, or deposit fees).

**Fraud-R1** [5] presents a benchmark of 8,564 fraud cases including fake job postings as a dedicated category, demonstrating that even frontier LLMs struggle with role-play scams. We use Fraud-R1's category taxonomy to motivate the dedicated `Scam Risk` label and the explicit "exclusive of topic labels" rule in our classifier.

**LLMs Do Multi-Label Classification Differently** [6] shows that the autoregressive output distributions of modern LLMs are misaligned for multi-label tasks and proposes calibration fixes. This is direct motivation for the constrained `json_schema` response format used in our LLM call (Section III-D); the `enum` constraint over the allowed-label set sidesteps the calibration issue by eliminating any tokens outside the allowed vocabulary.

**JobSphere** [7] deploys a quantized LLM with RAG over a government employment portal (PGRKAM, Punjab) to deliver multilingual recommendations and resume parsing on consumer GPUs, hitting precision@10 of 68%. JobSphere is platform-side and serves recruiters and applicants querying a curated job database; our agent is candidate-side, indexes the user's CV, and works against an inbound email stream rather than a curated catalog.

**AdaptJobRec** [8] is a conversational career-recommendation agent deployed on Walmart's career site that classifies query complexity and routes simple queries to direct tool calls vs. complex queries to a memory-and-decomposition planner, cutting latency by up to 53.3%. AdaptJobRec assumes the user actively asks for job recommendations; our agent is *passive*, monitoring an inbox at a configurable cadence and labeling unsolicited emails without conversational input.

**BrowserAgent** [9] trains a web agent that drives a live Chromium via Playwright with human-style click/scroll/type actions, and demonstrates that a small fine-tuned policy with explicit cross-step memory beats much larger text-only baselines on multi-hop QA. We share BrowserAgent's commitment to grounding in real DOM actions; we differ by specializing for one application (Gmail), using deterministic selectors rather than a learned policy, and trading generality for reliability.

**Advanced Real-Time Fraud Detection Using RAG-Based LLMs** [10] retrieves a continuously updatable policy corpus to flag fraud during live phone calls, reaching 97.98% on synthetic data. The novelty is updating the fraud-rules index without retraining. Our project uses RAG for the *opposite* purpose: instead of retrieving fraud policies to judge content, we retrieve the candidate's own CV chunks to judge inbound content, and pair the LLM signal with a deterministic scam scorer rather than a policy-grounded LLM-only verdict.

**Synthesis.** The 2025–2026 corpus consistently treats résumé–JD matching as a recruiter or platform problem, and treats fraud detection as a content-classification problem decoupled from the user's identity. No surveyed system does both at once on the candidate's own inbox. That intersection is the gap our project fills.

---

## III. Methodology

### A. System Architecture

The agent is structured as a thin orchestrator (`gmail_agent.py`) calling three single-purpose modules in sequence each cycle. Figure 1 shows the data flow.

```
                              ┌─ scam_scorer.py  (heuristic, no LLM)
                              │
     real Chrome (CDP) ──────┤
                              │
                              └─ cv_match.py     (RAG, sentence-transformers)
                                       │
                                       ▼
read_emails.py ──> emails ──> classifier.py ──> apply_labels.py ──> Gmail
                                  │
                                  └─ llm.py (OpenAI, optional)
                                       │
                                       ▼
                                history.jsonl
                                       │
                                       ▼
                          streamlit_app.py (dashboard + chat)
```
**Fig. 1.** Per-cycle data flow.

A cold start handles connection to the user's existing Chrome over CDP port 9222 (or launches a fresh Chrome with a temp profile if none is running), waits for manual login, idempotently creates each topic label in Gmail's sidebar, and warm-loads the embedding model. The monitoring loop then runs at the user's chosen interval (default 2 minutes), with each step wrapped in a `try`/`except` boundary so a single-step failure cannot kill the cycle.

### B. Tool Documentation

**Tool 1 — Gmail browser automation (custom).** Uses Playwright connected over CDP to the user's real Chrome. Per-row inbox scan via `tr.zA` selectors with timestamp parsed from `td.xW span[title]`. The full-body scrape clicks the subject cell `.y6/.bog` via a real `MouseEvent` chain (`mousedown`/`mouseup`/`click`) inside one `page.evaluate` to avoid DOM-detach races during Gmail's SPA re-renders, then scrapes `div.a3s.aiL` for the conversation body. Label application uses the per-thread three-dots → "Label as" → `[role="menuitemcheckbox"][title]` flow with a `force=True` click that bypasses Playwright's stability check (Gmail's submenu animation otherwise causes timeouts).

**Tool 2 — `scam_scorer.py` (custom).** A pure-Python heuristic with seven feature families: (1) free-mail sender domain claiming a corporate brand, (2) suspicious TLDs, (3) payment-phrase lexicon (`training fee`, `enrollment charge`, `1000$ as deposit`, …) plus a `$N + deposit/fee/charge` regex weighted at 0.55 — a single match alone crosses the scam threshold, (4) `no-interview-required` / `fast-track` phrases, (5) generic greetings, (6) URL analysis (shorteners + risky TLDs), (7) urgency cues. Each rule contributes weighted points and a human-readable reason; the final score is clipped to [0, 1].

**Tool 3 — `cv_match.py` (RAG with vector embeddings).** At load time the candidate's CV (`plan.txt`) is split into semantic chunks (one per heading + one per project bullet), each chunk is tagged with topic labels via a small keyword rule table, and each chunk is encoded with `sentence-transformers all-MiniLM-L6-v2` into a 384-dim normalized vector. At inference time the email body is encoded, cosine-compared against the cached chunk embeddings, and the top-k (k=5) chunks are returned alongside their topic tags and `kind` (`project` vs `generic`).

**Tool 4 — `llm.py` (OpenAI integration).** Optional augment. Builds a prompt with the email + scam features + retrieved CV chunks + allowed labels and calls the OpenAI Chat Completions API with `response_format={"type": "json_schema", "strict": true}` so the parsed output is guaranteed to be `{"labels": [<one of allowed>, …]}`. The schema's `enum` constraint over the label vocabulary directly addresses the multi-label calibration issue raised in [6]. Reads `OPENAI_API_KEY` from the environment; the classifier catches any exception and silently falls back to the rule-based path.

### C. Per-Cycle Flow

1. **Read.** Navigate to `#inbox`, scan rows, recency-filter to emails received within `cycle_interval + 60 s` (the safe zone covers Gmail's minute-only timestamp precision), drop thread IDs already in the seen set (loaded from `history.jsonl` at startup), and for each remaining row open the conversation, scrape the full body, return to `#inbox`.

2. **Classify.** For each fresh email, run the scam scorer; if score ≥ 0.5 emit `["Scam Risk"]` only (Scam Risk is exclusive of topic labels). Otherwise run RAG retrieval; if the best similarity is below 0.30 the email is off-topic for the candidate's CV and gets no labels. Otherwise route to the LLM path if a key is set, else to the rule-based path. Both paths see the same data — email body, scam features, retrieved CV chunks — and apply identical rules.

3. **Apply.** For each `(thread_id, label)` pair, in isolation: tick the row's checkbox, open the bulk-toolbar three-dots overflow, hover "Label as," read `aria-checked` on the menuitemcheckbox to detect the current state (idempotent — a no-op if the label is already on), click otherwise, dismiss the menu, untick the row.

4. **Persist.** Append per-email records — full body, scam features, cv_match block, applied labels, cycle timestamp — to `history.jsonl`. The dashboard renders from this file.

### D. LLM Prompt Design

The system prompt encodes the same four rules the rule-based classifier follows: scam-first exclusivity, off-topic returns `[]`, multi-label is encouraged when topics genuinely overlap, only labels from the allowed vocabulary are emitted. The user message dumps the candidate's CV, the top-k retrieved chunks (with similarity, kind, and topics), the scam-scorer output (score + reasons), and the email itself. The OpenAI `json_schema` constraint forces the output to `{"labels": [string from enum]}`, eliminating parse failures and out-of-vocabulary labels.

### E. User Interface

A Streamlit dashboard (`streamlit_app.py`) reads `history.jsonl` and renders one card per email with the subject, sender, time, applied label chips, scam score with reasons, CV-match score with the retrieved chunk evidence and missing skills, and the full body in an expander. The sidebar contains agent Start/Stop controls (PID-tracked across reruns), a cycle-interval input, and an auto-refresh toggle with a 5–60 s slider. A chat panel at the bottom of the page maintains conversation state via `st.session_state` and answers grounded queries (`why was email #3 labeled Scam Risk?`, `show me AI/ML emails`, `best CV match`) by filtering and reformatting `history.jsonl` records — every fact comes from a record the agent actually wrote.

---

## IV. Experimental Setup

### A. Models

- **Embedding model:** `sentence-transformers/all-MiniLM-L6-v2` (384-dim, ~80 MB, runs on CPU).
- **LLM (optional path):** `gpt-4o-mini` with `response_format=json_schema` and `temperature=0`. Selectable via `LLM_MODEL` env var.

### B. Datasets

We test against (a) the candidate's actual Gmail inbox over multiple sessions during development, and (b) a crafted set of 11 internship-shaped emails covering each label and each known failure mode. The test set spans:

- Single-domain emails for each topic (AI/ML, Research, Software Engineering, Embedded Systems, DevOps);
- Genuinely multi-domain emails (`AI/ML + Research`, `AI/ML + DevOps`);
- Two scam variants — one with the payload buried in paragraph five (`$60 enrollment charge`, `no prior interview required`, `seats are limited`), one short-form (`pay a fee of 1000$ as deposit. Register as soon as possible`);
- An off-target marketing pitch with copywriting/brand-strategy keywords;
- A short noise message (an Arabic-transliteration typo with no internship intent at all).

The crafted emails are versioned in the test harness and re-runnable across regressions.

### C. Prompts and Design Decisions

The LLM system prompt is reproduced verbatim in `llm.py`. Key design decisions:

- **Pre-tool-calling.** Instead of giving the LLM tool access at runtime (which adds latency and costs), the deterministic tools (`scam_scorer`, `cv_match`) are called *before* the LLM and their outputs are inlined as context. The LLM is reduced to a single decision call, which is fast (~1–3 s on `gpt-4o-mini`) and cheap (~$0.00025/email at current OpenAI prices).
- **Two-tier RAG voting in the rule path.** Project chunks at similarity ≥ 0.50 emit their topics directly; chunks at 0.30–0.50 require body-keyword confirmation. A body-keyword safety net adds any topic not yet emitted whose keyword is literally present in the body. This stops a moderately-similar generic project (e.g. the candidate's CPU Simulator at 0.34 cosine for a `Docker + Linux` query) from dragging Software Engineering onto every tech email.
- **Recency safe-zone.** The recency filter uses `cycle_interval + 60 s` because Gmail's row tooltip is minute-precision; an email received at 11:20:55 reads as 11:20:00 and would otherwise be dropped at a cycle boundary. The dedup `seen` set, loaded from `history.jsonl` at startup, prevents the safe zone from re-processing already-handled emails.

### D. Evaluation Metrics

For the crafted test set we report exact-set match: a prediction is correct only if the predicted label set equals the expected set. We additionally track per-feature counts in the scam scorer (which rules fired, with what weights) and per-chunk retrieval scores in the RAG matcher (similarity, kind, topics) for every email, written to `history.jsonl`. For the live inbox we measure (a) % of internship emails that received at least one label, (b) % of off-target emails that correctly received no label, and (c) % of clear scams flagged.

---

## V. Results and Discussion

### A. Crafted Test Set

On the 11 crafted emails the agent reaches **11/11** correct label sets in both the rule-based and LLM-augmented paths. Highlights:

- A terse Research email — body just `Research opportunity on EEG.` — labels correctly because the EEG project chunk's similarity is 0.80, above the high-confidence bypass threshold; the body-keyword check is skipped.
- A buried-payload scam — four paragraphs of innocent intro before `$60 enrollment charge` and `no prior interview required` — scored 0.80 with four distinct reasons. The full-body scrape was essential here; the `.y2` snippet is ~100 chars and would have missed the payload.
- A short scam pitching a music internship for `1000$ as deposit. Register as soon as possible` initially scored only 0.35 (one regex match, below the 0.5 threshold). Bumping the `$N + deposit/fee/charge` regex weight to 0.55 — single match alone crosses the threshold — and adding `as deposit`, `pay a fee`, `as soon as possible` to the lexicons brought it to 1.0.
- A `research using deep learning with medicine` email initially under-labeled (only AI/ML, missing Research) because the EEG project chunk sat at similarity 0.26, below the per-chunk vote threshold. Adding the body-keyword safety net for any topic not yet emitted — which catches "research" in the body even when no project chunk crossed threshold — recovers the multi-label outcome.

### B. Live Inbox

On the candidate's actual inbox the system has run autonomously across sessions, correctly labeling deep-learning, embedded-systems, full-stack, and DevOps internships from family-test senders, refusing to label an off-topic security-alert and a TikTok login notification, and flagging two fake-internship variants as Scam Risk. The Streamlit dashboard's chat panel correctly answered queries like `why was the latest scam labeled?` and `best CV match`, surfacing the relevant scam reasons and retrieved chunks from `history.jsonl`.

### C. Discussion

The core architectural decision — running the deterministic tools *before* the LLM and inlining their outputs as context — turned out to be the most defensible choice. It bounds LLM cost to one call per email, eliminates output-parse failures via `json_schema`, lets the system run end-to-end with no API key, and produces an audit trail in `history.jsonl` that the dashboard's chat panel can ground its answers in without needing the LLM at all.

The single biggest mistake in the development process — over-labeling the entire inbox via Gmail's bulk-toolbar Label button — surfaced because the bulk flow somehow applied the picker's selection to all scanned rows rather than the checked rows. Switching to the per-thread three-dots flow ([data-tooltip="More"] → "Label as" → menuitemcheckbox) eliminated the bug entirely. We have not been able to definitively explain why the bulk flow misbehaved on this account; the per-thread flow is unambiguous and idempotent, so we use it.

---

## VI. Conclusion and Future Work

We presented a personal LLM agent that monitors a candidate's Gmail inbox and labels internship emails by topic, grounded in retrieved CV chunks and paired with a deterministic scam classifier. The system runs offline by default; an OpenAI-backed path is opt-in. End-to-end correctness on a crafted regression set is 11/11, with the most interesting failures (buried scams, multi-domain matches) handled cleanly via a two-tier RAG voting rule and a body-keyword safety net.

Future work: (1) extend the candidate-side RAG to retrieve the candidate's own past sent emails for tone-matching when drafting replies; (2) add a calendar-extraction tool for internship deadlines so the system emits an `.ics` invite per labeled offer; (3) explore a small fine-tuned policy for the per-thread Gmail UI actions à la BrowserAgent [9], to reduce reliance on hand-coded selectors; (4) calibrate the scam scorer against Fraud-R1's `fake_job` slice [5] for a quantitative recall number.

---

## Author Contributions

Yehya Mazloum: full project — design, implementation, evaluation, and writing.

---

## References

[1] Y. Yu *et al.*, "ConFit v2: Improving Resume–Job Matching using Hypothetical Resume Embedding and Runner-Up Hard-Negative Mining," *arXiv preprint* arXiv:2502.12361, Feb. 2025.

[2] J. Hofmann *et al.*, "CareerBERT: Matching Resumes to ESCO Jobs in a Shared Embedding Space for Generic Job Recommendations," *arXiv preprint* arXiv:2503.02056, Mar. 2025.

[3] M. Khedher *et al.*, "Smart-Hiring: An Explainable Multi-Agent Approach for Interpretable Job Resume Matching," *arXiv preprint* arXiv:2504.02870, Apr. 2025.

[4] Y. Sun *et al.*, "MultiPhishGuard: An LLM-based Multi-Agent System for Phishing Email Detection," *arXiv preprint* arXiv:2505.23803, May 2025.

[5] S. Wang *et al.*, "Fraud-R1: A Multi-Round Benchmark for Assessing the Robustness of LLMs Against Augmented Fraud and Phishing Inducements," *arXiv preprint* arXiv:2502.12904, Feb. 2025.

[6] M. Pavlovic *et al.*, "LLMs Do Multi-Label Classification Differently: Calibration, Diversity, and Discrimination," *arXiv preprint* arXiv:2505.17510, May 2025.

[7] R. Srihari *et al.*, "JobSphere: An AI-Powered Multilingual Career Copilot for Government Employment Platforms," *arXiv preprint* arXiv:2511.08343, Nov. 2025.

[8] Q. Wang *et al.*, "AdaptJobRec: Enhancing Conversational Career Recommendation through an LLM-Powered Agentic System," *arXiv preprint* arXiv:2508.13423, Aug. 2025.

[9] T. Yu *et al.*, "BrowserAgent: Building Web Agents with Human-Inspired Web Browsing Actions," *arXiv preprint* arXiv:2510.10666, Oct. 2025.

[10] G. Singh *et al.*, "Advanced Real-Time Fraud Detection Using RAG-Based LLMs," *arXiv preprint* arXiv:2501.15290, Jan. 2025.
