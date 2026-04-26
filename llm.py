"""LLM-backed label decision (the project's "Custom LLM Agent" hook).

`decide_labels(email, scam_features, cv_match, allowed_labels)` builds a
prompt from the email body, the deterministic scam-scorer output, the
top-k retrieved CV chunks (RAG context), and the candidate's CV
summary, then calls OpenAI's structured-output API and returns the
parsed label list.

Rules baked into the system prompt match the rule-based fallback in
classifier.py so behaviour is consistent across paths:
    - Scam Risk is exclusive (when high-risk, no topic labels).
    - Off-topic emails (no CV signal at all) get [].
    - Otherwise, multi-label with whichever topics in `allowed_labels`
      apply, grounded in the retrieved CV evidence.

The classifier wraps the call in try/except so any LLM failure
(missing key, network, parse error) silently falls back to the
rule-based path. Keys come from `OPENAI_API_KEY` in the environment —
never hard-coded — per the project's API-keys-not-in-repo rule.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Lazy-import openai so the module file itself is always importable —
# only `decide_labels()` needs the SDK on disk.
_PROJECT = Path(__file__).parent
_CV_PATH = _PROJECT / "plan.txt"

DEFAULT_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")


def llm_available() -> bool:
    """True iff the agent should attempt the LLM path. Cheap to call —
    just a key probe. Lets classifier.py decide which path to log."""
    return bool(os.environ.get("OPENAI_API_KEY"))


def _build_messages(
    email: dict,
    scam_features: dict,
    cv_match: dict,
    allowed_labels: list[str],
    cv_text: str,
) -> list[dict]:
    """Compose the system + user messages. The system message states the
    rules; the user message dumps the email + tool outputs."""
    matched_chunks = "\n".join(
        f"- (sim {h.get('similarity', 0):.2f}, kind={h.get('kind', '?')}, "
        f"topics={h.get('topics') or []}) {(h.get('chunk') or '').strip()[:200]}"
        for h in (cv_match.get("matched") or [])
    ) or "(no chunks above retrieval threshold)"

    scam_reasons = "\n".join(
        f"- {r}" for r in (scam_features.get("reasons") or [])
    ) or "(no scam heuristic flags)"

    system = (
        "You are an internship-email classifier for a candidate's Gmail "
        "inbox. You read one email at a time and assign it ZERO OR MORE "
        "labels from a fixed list, grounded in the candidate's CV.\n\n"
        "Rules:\n"
        "1. If the email looks like a scam (asks for upfront fees, "
        "registration deposits, dollar amounts tied to deposits/fees, "
        "skips standard hiring steps), return EXACTLY {\"labels\": "
        "[\"Scam Risk\"]}. Scam Risk is exclusive — never combine with "
        "topic labels.\n"
        "2. If the email is not actually an internship offer relevant "
        "to this candidate's CV (e.g. marketing pitch, unrelated "
        "domain), return {\"labels\": []}.\n"
        "3. Otherwise return every topic from `allowed_labels` whose "
        "subject matter is meaningfully present in the email AND is "
        "supported by the candidate's CV (use the retrieved chunks as "
        "evidence). Multi-label is encouraged when the email genuinely "
        "spans several topics.\n"
        "4. Only emit labels from the provided `allowed_labels` list — "
        "never invent new ones. Output strict JSON only."
    )

    user = (
        f"Allowed labels: {allowed_labels}\n\n"
        f"---- Candidate CV ----\n{cv_text.strip()}\n\n"
        f"---- Retrieved CV evidence (top-k from RAG) ----\n{matched_chunks}\n\n"
        f"---- Deterministic scam-scorer output ----\n"
        f"score: {scam_features.get('score', 0)}\n"
        f"reasons:\n{scam_reasons}\n\n"
        f"---- Email ----\n"
        f"Subject: {email.get('subject', '')}\n"
        f"Sender: {email.get('sender', '')}\n"
        f"Body:\n{email.get('body', '')}\n"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def decide_labels(
    email: dict,
    scam_features: dict,
    cv_match: dict,
    allowed_labels: list[str],
) -> list[str]:
    """Single LLM call → list of labels. Raises on any failure; the
    caller (classifier.py) catches and falls back to rule-based."""
    from openai import OpenAI  # imported here so the module loads w/o key

    cv_text = _CV_PATH.read_text(encoding="utf-8") if _CV_PATH.exists() else ""

    client = OpenAI()  # picks up OPENAI_API_KEY from env automatically
    completion = client.chat.completions.create(
        model=DEFAULT_MODEL,
        messages=_build_messages(
            email, scam_features, cv_match, allowed_labels, cv_text,
        ),
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "label_decision",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "labels": {
                            "type": "array",
                            "items": {"type": "string", "enum": allowed_labels},
                        },
                    },
                    "required": ["labels"],
                    "additionalProperties": False,
                },
            },
        },
        temperature=0,
    )
    raw = completion.choices[0].message.content or "{}"
    parsed = json.loads(raw)
    labels = parsed.get("labels") or []
    if not isinstance(labels, list):
        raise ValueError(f"LLM returned non-list labels: {labels!r}")

    # Trust the schema's enum but still defensively dedupe + drop unknowns.
    seen: set[str] = set()
    out: list[str] = []
    for lbl in labels:
        if lbl in allowed_labels and lbl not in seen:
            seen.add(lbl)
            out.append(lbl)
    return out


# ── Self-test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    if not llm_available():
        print("OPENAI_API_KEY not set — set it in your environment to run the LLM.")
        sys.exit(1)
    sample_email = {
        "subject": "Hello",
        "sender": "Recruiter",
        "body": "We are offering a research internship in deep learning for medical imaging.",
    }
    print(decide_labels(
        sample_email,
        {"score": 0.0, "reasons": []},
        {"score": 0.55, "matched": [
            {"chunk": "Past Projects: ECG Arrhythmia Detection using CNN + Transformer (Deep Learning)",
             "similarity": 0.55, "kind": "project", "topics": ["AI/ML"]},
            {"chunk": "Past Projects: EEG Seizure Detection Research (ongoing)",
             "similarity": 0.40, "kind": "project", "topics": ["Research"]},
        ]},
        ["AI/ML", "Research", "Software Engineering", "Embedded Systems", "DevOps", "Scam Risk"],
    ))
