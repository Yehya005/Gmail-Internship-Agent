"""Label decisions for one or many emails.

Per the project's labeling-step contract this is where ALL the heavy
work for a labeling decision lives:

  1. Run the deterministic scam scorer on the email's full body.
  2. If scam-risk score >= SCAM_THRESHOLD → ['Scam Risk'] only
     (per the user rule: Scam Risk is exclusive — no topic labels go
     on a flagged email).
  3. Otherwise run the RAG matcher (cv_match) — encode the body,
     retrieve the top-k most-similar CV chunks.
  4. If the best chunk-similarity is below CV_MATCH_THRESHOLD, the email
     is off-topic for this candidate's CV → no labels.
  5. Otherwise the labels are the *union of topics* from every
     retrieved chunk above the per-chunk similarity threshold. Each
     chunk's topics are tagged at CV-load time in cv_match.py — see
     `_CHUNK_TOPIC_RULES` and `_resolve_chunk_topics` there.

Everything is deterministic; no LLM call. The agent imports
`classify_emails(emails)`; the same module is also runnable from the
command line via `python classifier.py emails.json`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cv_match import _CHUNK_TOPIC_RULES, match_dict as cv_match_dict
from scam_scorer import score_email_dict

SCAM_THRESHOLD = 0.5
CV_MATCH_THRESHOLD = 0.30
CHUNK_VOTE_THRESHOLD = 0.30  # minimum sim for a project chunk to vote at all
HIGH_SIM_THRESHOLD = 0.50    # at/above this, RAG signal is strong enough to
                             # bypass the body-keyword confirmation step —
                             # multiple high-scoring chunks for different
                             # topics will multi-label even on terse bodies


def classify_email(email: dict) -> list[str]:
    """Decide labels for one email. **Mutates** the dict in place to add
    `scam_features` and `cv_match` blocks (used by history.jsonl + UI).
    Returns the label list — possibly empty."""
    # 1. Scam scorer (deterministic, no LLM)
    email["scam_features"] = score_email_dict(email)
    if (email["scam_features"].get("score") or 0) >= SCAM_THRESHOLD:
        # 2. Scam Risk is exclusive — never combine with topic labels.
        email["cv_match"] = {"score": 0.0, "matched": [], "missing_skills": []}
        return ["Scam Risk"]

    # 3. RAG match against CV chunks
    body = email.get("body") or ""
    cv = cv_match_dict(body)
    email["cv_match"] = cv

    # 4. Threshold gate — off-topic emails get no labels at all
    if (cv.get("score") or 0) < CV_MATCH_THRESHOLD:
        return []

    # 5. Topics from PROJECT chunks above the per-chunk threshold,
    #    confirmed by a keyword match in the email body. Both must hold:
    #    RAG says "this CV project is relevant" AND the body literally
    #    mentions the topic. This stops a moderately-similar project
    #    (e.g. CPU Simulator getting 0.34 cosine) from dragging its
    #    Software Engineering label onto every tech email — the email
    #    has to actually be about software engineering for SE to stick.
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
