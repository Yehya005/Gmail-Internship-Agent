"""LLM-backed label decision (the project's "Custom LLM Agent" hook).

`decide_labels(email, scam_features, cv_match, allowed_labels)` builds a
prompt from the email body, the deterministic scam-scorer output, the
top-k retrieved CV chunks (RAG context), and the candidate's CV
summary, then asks an LLM to decide which labels apply and returns the
parsed list.

Provider preference: Anthropic (Claude) → OpenAI fallback. Whichever
key is set in the environment wins. Output is constrained at the API
level — Anthropic via a forced tool-use call whose input schema enums
the allowed labels; OpenAI via `response_format=json_schema`. Either
way the model can only emit labels from `allowed_labels`.

Rules baked into the system prompt match the rule-based fallback in
classifier.py so behaviour is consistent across paths:
    - Scam Risk is exclusive (when high-risk, no topic labels).
    - Off-topic emails (no CV signal at all) get [].
    - Otherwise, multi-label with whichever topics in `allowed_labels`
      apply, grounded in the retrieved CV evidence.

The classifier wraps the call in try/except so any LLM failure
(missing key, network, parse error) silently falls back to the
rule-based path. Keys come from `ANTHROPIC_API_KEY` /
`OPENAI_API_KEY` in the environment — never hard-coded.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_PROJECT = Path(__file__).parent
_CV_PATH = _PROJECT / "plan.txt"

# Anthropic-side defaults. Haiku 4.5 is fast + cheap and plenty for a
# 5-way label decision on a single email; flip to Sonnet for harder
# judgement calls.
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")


def _provider() -> str | None:
    """Return 'anthropic', 'openai', or None depending on which key is set.
    Anthropic wins if both are set — that's the project's preferred LLM."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return None


def llm_available() -> bool:
    """True iff at least one supported LLM provider has a key set."""
    return _provider() is not None


def _system_prompt() -> str:
    return (
        "You are an internship-email classifier for a candidate's Gmail "
        "inbox. You read one email at a time and assign it ZERO OR MORE "
        "labels from a fixed list, grounded in the candidate's CV.\n\n"
        "Rules:\n"
        "1. If the email looks like a scam (asks for upfront fees, "
        "registration deposits, dollar amounts tied to deposits/fees, "
        "skips standard hiring steps), return EXACTLY [\"Scam Risk\"]. "
        "Scam Risk is exclusive — never combine with topic labels.\n"
        "2. If the email is not actually an internship offer relevant "
        "to this candidate's CV (e.g. marketing pitch, unrelated "
        "domain), return [].\n"
        "3. Otherwise return every topic from the allowed labels whose "
        "subject matter is meaningfully present in the email AND is "
        "supported by the candidate's CV (use the retrieved chunks as "
        "evidence). Multi-label is encouraged when the email genuinely "
        "spans several topics.\n"
        "4. Only emit labels from the provided allowed-labels list — "
        "never invent new ones."
    )


def _user_prompt(
    email: dict,
    scam_features: dict,
    cv_match: dict,
    allowed_labels: list[str],
    cv_text: str,
) -> str:
    matched_chunks = "\n".join(
        f"- (sim {h.get('similarity', 0):.2f}, kind={h.get('kind', '?')}, "
        f"topics={h.get('topics') or []}) {(h.get('chunk') or '').strip()[:200]}"
        for h in (cv_match.get("matched") or [])
    ) or "(no chunks above retrieval threshold)"

    scam_reasons = "\n".join(
        f"- {r}" for r in (scam_features.get("reasons") or [])
    ) or "(no scam heuristic flags)"

    return (
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


def _decide_via_anthropic(
    email: dict, scam_features: dict, cv_match: dict,
    allowed_labels: list[str], cv_text: str,
) -> list[str]:
    """Force a tool call whose input schema enums the allowed labels.
    Anthropic's Messages API doesn't have OpenAI's `response_format=json_schema`,
    but `tool_choice={type: tool, name: ...}` + an input_schema enum gives
    the same guarantee — the model can only emit labels from the list."""
    from anthropic import Anthropic

    client = Anthropic()  # picks up ANTHROPIC_API_KEY
    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=512,
        system=_system_prompt(),
        messages=[{
            "role": "user",
            "content": _user_prompt(
                email, scam_features, cv_match, allowed_labels, cv_text,
            ),
        }],
        tools=[{
            "name": "submit_labels",
            "description": (
                "Submit the labels to apply to this email. "
                "Pass an empty array if no labels apply."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "labels": {
                        "type": "array",
                        "items": {"type": "string", "enum": allowed_labels},
                    },
                },
                "required": ["labels"],
            },
        }],
        tool_choice={"type": "tool", "name": "submit_labels"},
    )
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_labels":
            labels = block.input.get("labels") or []
            if not isinstance(labels, list):
                raise ValueError(f"Anthropic returned non-list labels: {labels!r}")
            return labels
    raise ValueError("Anthropic response had no tool_use block")


def _decide_via_openai(
    email: dict, scam_features: dict, cv_match: dict,
    allowed_labels: list[str], cv_text: str,
) -> list[str]:
    from openai import OpenAI

    client = OpenAI()  # picks up OPENAI_API_KEY
    completion = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": _user_prompt(
                email, scam_features, cv_match, allowed_labels, cv_text,
            )},
        ],
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
        raise ValueError(f"OpenAI returned non-list labels: {labels!r}")
    return labels


def decide_labels(
    email: dict,
    scam_features: dict,
    cv_match: dict,
    allowed_labels: list[str],
) -> list[str]:
    """Single LLM call → list of labels. Anthropic preferred, OpenAI as
    fallback. Raises on any failure; the caller (classifier.py) catches
    and falls back to rule-based."""
    cv_text = _CV_PATH.read_text(encoding="utf-8") if _CV_PATH.exists() else ""

    provider = _provider()
    if provider == "anthropic":
        labels = _decide_via_anthropic(
            email, scam_features, cv_match, allowed_labels, cv_text,
        )
    elif provider == "openai":
        labels = _decide_via_openai(
            email, scam_features, cv_match, allowed_labels, cv_text,
        )
    else:
        raise RuntimeError(
            "no LLM key in env (set ANTHROPIC_API_KEY or OPENAI_API_KEY)"
        )

    # Defensive: dedupe + drop anything outside allowed (the schema/tool
    # already enforces enum, but we trust-and-verify in case of provider drift).
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
        print("no LLM key in env (set ANTHROPIC_API_KEY or OPENAI_API_KEY).")
        sys.exit(1)
    print(f"using provider: {_provider()}")
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
