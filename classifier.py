"""Label decisions for one or many emails.

Per cycle, the classifier:

  1. Runs the deterministic scam scorer on the email's full body.
  2. If scam-risk score >= SCAM_THRESHOLD → ['Scam Risk'] only.
     (Scam Risk is exclusive — never combined with topic labels.)
  3. Otherwise runs the RAG matcher (cv_match) — encode the body,
     retrieve the top-k most-similar CV chunks (each tagged with
     topics + 'project' / 'generic' kind at load time).
  4. If the best chunk-similarity is below CV_MATCH_THRESHOLD, the
     email is off-topic for this candidate's CV → no labels.
  5. Decide the topic labels. Two paths share this step:
        - LLM path (default when OPENAI_API_KEY is set): call
          llm.decide_labels with the email + scam features + retrieved
          CV chunks + allowed labels. The LLM returns a JSON list of
          labels. Falls through to the rule-based path on any error.
        - Rule-based path: project chunks above CHUNK_VOTE_THRESHOLD
          emit their tagged topics, with body-keyword confirmation for
          moderate similarities and a body-keyword safety net for
          topics no project covers.

The agent imports `classify_emails(emails)`. The module is also
runnable from the command line via `python classifier.py emails.json`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import llm
from cv_match import _CHUNK_TOPIC_RULES, match_dict as cv_match_dict
from scam_scorer import score_email_dict

SCAM_THRESHOLD = 0.5
CV_MATCH_THRESHOLD = 0.30
CHUNK_VOTE_THRESHOLD = 0.30  # minimum sim for a project chunk to vote at all
HIGH_SIM_THRESHOLD = 0.50    # at/above this, RAG signal is strong enough to
                             # bypass the body-keyword confirmation step —
                             # multiple high-scoring chunks for different
                             # topics will multi-label even on terse bodies

# All labels the LLM is allowed to emit. Mirrors gmail_agent.LABELS;
# duplicated here so the classifier can be invoked standalone (CLI) too.
ALLOWED_LABELS = [
    "AI/ML", "Research", "Software Engineering",
    "Embedded Systems", "DevOps", "Scam Risk",
]


def _classify_rule_based(email: dict) -> list[str]:
    """The deterministic fallback path. Same code that ran before the LLM
    integration: project chunks vote with body-keyword confirmation,
    plus a safety net for topics no project covers."""
    cv = email.get("cv_match") or {}
    body = (email.get("body") or "").lower()
    subject = (email.get("subject") or "").lower()
    text = f" {subject} {body} "

    labels: set[str] = set()
    for hit in cv.get("matched", []):
        if hit.get("kind") != "project":
            continue
        sim = hit.get("similarity") or 0
        if sim < CHUNK_VOTE_THRESHOLD:
            continue
        for topic in hit.get("topics") or []:
            if sim >= HIGH_SIM_THRESHOLD:
                # Strong RAG signal — multi-label even if the body is terse
                labels.add(topic)
                continue
            # Moderate signal — keep the body-keyword guard so generic
            # noise (e.g. CPU Simulator at 0.34 sim) can't drag its
            # Software Engineering label onto every tech email.
            keywords = _CHUNK_TOPIC_RULES.get(topic, [])
            if any(kw in text for kw in keywords):
                labels.add(topic)

    # 6. Body-keyword safety net: for any topic NOT already emitted, add
    #    it if the body explicitly names it. Catches two cases:
    #    - Topics not covered by any CV project (DevOps here) — there's
    #      no chunk that could vote, so keywords are the only signal.
    #    - Topics whose project chunk scored just below threshold even
    #      though the email mentions the topic (e.g. EEG Seizure
    #      Detection at sim 0.26 for a 'research using deep learning'
    #      email — ECG dominates retrieval but Research is in the body).
    #    The CV_MATCH_THRESHOLD gate above already confirmed the email
    #    is tech-relevant for this candidate, so the keyword fallback
    #    won't fire on off-topic emails like marketing pitches.
    for topic, keywords in _CHUNK_TOPIC_RULES.items():
        if topic in labels:
            continue
        if any(kw in text for kw in keywords):
            labels.add(topic)

    return sorted(labels)


def classify_email(email: dict) -> list[str]:
    """Decide labels for one email. **Mutates** the dict in place to add
    `scam_features` and `cv_match` blocks (used by history.jsonl + UI).
    Returns the label list — possibly empty.

    Tries the LLM path first when OPENAI_API_KEY is set; on any LLM
    failure (missing key, network error, parse error, schema mismatch)
    silently falls back to the deterministic rule-based logic."""
    # 1. Scam scorer (deterministic, runs in both paths).
    email["scam_features"] = score_email_dict(email)
    if (email["scam_features"].get("score") or 0) >= SCAM_THRESHOLD:
        # Scam Risk is exclusive — never combine with topic labels.
        # No need to run RAG / LLM for a clearly-scam email.
        email["cv_match"] = {"score": 0.0, "matched": [], "missing_skills": []}
        return ["Scam Risk"]

    # 2. RAG retrieval (deterministic). Always run so the dashboard +
    #    history.jsonl get the cv_match block, and so the LLM has
    #    grounded evidence to reason from.
    body = email.get("body") or ""
    cv = cv_match_dict(body)
    email["cv_match"] = cv

    # 3. Off-topic gate.
    if (cv.get("score") or 0) < CV_MATCH_THRESHOLD:
        return []

    # 4. Try the LLM path first; fall back to the rule-based one on any
    #    failure. Both paths see the same data (email + scam_features +
    #    retrieved cv_match), and both honour the same rules — Scam Risk
    #    exclusivity, off-topic returns [], multi-label is fine when the
    #    email genuinely spans multiple topics.
    if llm.llm_available():
        try:
            return llm.decide_labels(
                email, email["scam_features"], cv, ALLOWED_LABELS,
            )
        except Exception as e:
            print(f"  [classifier] LLM path failed ({e}); falling back to rules.")

    return _classify_rule_based(email)


def classify_emails(emails: list[dict]) -> dict[str, list[str]]:
    """Run `classify_email` over a list. Mutates each dict (adds
    scam_features + cv_match). Returns {thread_id: [labels]} for every
    email that received at least one label."""
    out: dict[str, list[str]] = {}
    for e in emails:
        labels = classify_email(e)
        if labels:
            out[e["thread_id"]] = labels
    return out


# ── CLI ──────────────────────────────────────────────────────────────────────

def _main() -> int:
    p = argparse.ArgumentParser(
        description="Decide labels for emails read from emails.json."
    )
    p.add_argument(
        "input", nargs="?", default="emails.json",
        help="Path to emails.json (default: ./emails.json).",
    )
    p.add_argument(
        "--output", "-o", default="to_label.json",
        help="Where to write the {thread_labels: ...} mapping. "
             "Default: ./to_label.json.",
    )
    args = p.parse_args()

    src = Path(args.input)
    if not src.exists():
        print(f"error: {src} not found", file=sys.stderr)
        return 1
    emails = json.loads(src.read_text(encoding="utf-8"))
    if not isinstance(emails, list):
        print("error: emails.json must contain a list", file=sys.stderr)
        return 1

    thread_labels = classify_emails(emails)
    out_path = Path(args.output)
    out_path.write_text(
        json.dumps({"thread_labels": thread_labels}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    pair_count = sum(len(v) for v in thread_labels.values())
    print(
        f"classified {len(emails)} email(s) → {len(thread_labels)} thread(s) "
        f"with {pair_count} (thread, label) pair(s) total"
    )
    return 0


if __name__ == "__main__":
    sys.exit(_main())
