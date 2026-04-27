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
import re
import shutil
import subprocess
import sys
from pathlib import Path

_PROJECT = Path(__file__).parent
_CV_PATH = _PROJECT / "plan.txt"

# Anthropic-side defaults. Haiku 4.5 is fast + cheap and plenty for a
# 5-way label decision on a single email; flip to Sonnet for harder
# judgement calls.
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

# Known winget install path — used when `claude` isn't on PATH. winget
# installs Claude Code here on Windows but only updates PATH for new
# shells, so a child Python process spawned before logout may not see
# `claude` directly. Falling back to the absolute path keeps the LLM
# call working without a logout / reboot.
_CLAUDE_CLI_FALLBACK = Path(
    os.environ.get("LOCALAPPDATA", ""),
    "Microsoft", "WinGet", "Packages",
    "Anthropic.ClaudeCode_Microsoft.Winget.Source_8wekyb3d8bbwe",
    "claude.exe",
)


def _claude_cli_path() -> str | None:
    """Resolve the `claude` binary. Prefer PATH; fall back to the winget
    location if PATH hasn't been refreshed since install."""
    found = shutil.which("claude")
    if found:
        return found
    if _CLAUDE_CLI_FALLBACK.exists():
        return str(_CLAUDE_CLI_FALLBACK)
    return None


def _provider() -> str | None:
    """Return one of:
       - 'claude_cli' — preferred, uses the user's Claude Pro/Max
         subscription via the local CLI (no API-key billing).
       - 'anthropic'  — direct API call with ANTHROPIC_API_KEY.
       - 'openai'     — fallback to OPENAI_API_KEY.
       - None         — rule-based path will run.
    Resolved in priority order so a Pro user with a token gets the
    free-with-subscription path automatically."""
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") and _claude_cli_path():
        return "claude_cli"
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


def _decide_via_claude_cli(
    email: dict, scam_features: dict, cv_match: dict,
    allowed_labels: list[str], cv_text: str,
) -> list[str]:
    """Shell out to `claude -p` and parse a strict-JSON response.

    The standalone Claude Code CLI authenticates with the user's Pro/Max
    subscription via CLAUDE_CODE_OAUTH_TOKEN, so this path costs zero
    API credit. Trade-off vs. the SDK paths: Claude CLI doesn't expose
    response_format / forced tool-use, so we lean on a strict-JSON
    instruction in the prompt and post-validate against `allowed_labels`.
    Markdown-fenced output is tolerated — common with reasoning models."""
    cli = _claude_cli_path()
    if not cli:
        raise RuntimeError("claude CLI not found on PATH")

    # Combine system + user into one prompt — `claude -p <prompt>` is
    # one-shot; there's no separate system slot in --print mode.
    prompt = (
        f"{_system_prompt()}\n\n"
        f"{_user_prompt(email, scam_features, cv_match, allowed_labels, cv_text)}\n"
        f"\n---- OUTPUT ----\n"
        f"Reply with STRICT JSON ONLY — no prose, no markdown fences.\n"
        f"Schema: {{\"labels\": [\"...\"]}}.\n"
        f"`labels` may only contain values from {allowed_labels}.\n"
        f"Empty list [] if no labels apply."
    )

    # Pass the prompt on stdin to avoid Windows command-line length
    # limits and quoting issues. --output-format text yields the raw
    # model response (no SDK wrapper JSON).
    result = subprocess.run(
        [cli, "-p", "--output-format", "text"],
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        # The token is already in the parent env, but be explicit so
        # the call works even when invoked from a service that strips
        # parent env (e.g. some Streamlit deployments).
        env={**os.environ, "CLAUDE_CODE_OAUTH_TOKEN":
             os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")},
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude -p exited {result.returncode}: {result.stderr.strip()[:300]}"
        )

    raw = result.stdout.strip()
    # Strip ```json fences if the model added them despite instructions.
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL)
        raw = raw.strip()
    # If there's still surrounding prose, isolate the first JSON object.
    if not raw.startswith("{"):
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            raw = match.group(0)

    parsed = json.loads(raw)
    labels = parsed.get("labels") or []
    if not isinstance(labels, list):
        raise ValueError(f"claude returned non-list labels: {labels!r}")
    return labels


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
    if provider == "claude_cli":
        labels = _decide_via_claude_cli(
            email, scam_features, cv_match, allowed_labels, cv_text,
        )
    elif provider == "anthropic":
        labels = _decide_via_anthropic(
            email, scam_features, cv_match, allowed_labels, cv_text,
        )
    elif provider == "openai":
        labels = _decide_via_openai(
            email, scam_features, cv_match, allowed_labels, cv_text,
        )
    else:
        raise RuntimeError(
            "no LLM provider available — set CLAUDE_CODE_OAUTH_TOKEN, "
            "ANTHROPIC_API_KEY, or OPENAI_API_KEY"
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
        print("no LLM provider available — set CLAUDE_CODE_OAUTH_TOKEN, "
              "ANTHROPIC_API_KEY, or OPENAI_API_KEY.")
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
