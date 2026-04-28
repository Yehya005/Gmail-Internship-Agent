"""Streamlit dashboard for the Gmail Internship Monitor.

Reads `history.jsonl` (appended by gmail_agent.py each cycle) and renders
one card per email: subject + sender + applied labels + scam-risk score
with reasons + CV-match score with retrieved evidence + missing skills
+ full body. A topic filter and a manual refresh button live up top.

Run from a second terminal while the agent is running:

    venv\\Scripts\\python -m streamlit run streamlit_app.py
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import streamlit as st
from streamlit_autorefresh import st_autorefresh

import account
import llm

PROJECT = Path(__file__).parent
PID_FILE = PROJECT / "agent.pid"
AGENT_LOG = PROJECT / "agent_output.log"
# Use the venv python so the spawned agent has playwright + sentence-transformers.
VENV_PY = PROJECT / "venv" / ("Scripts" if os.name == "nt" else "bin") / (
    "python.exe" if os.name == "nt" else "python"
)

LABEL_COLORS = {
    "AI/ML":                "#7c3aed",   # purple
    "Research":             "#2563eb",   # blue
    "Software Engineering": "#0891b2",   # cyan
    "Embedded Systems":     "#65a30d",   # green
    "DevOps":               "#ea580c",   # orange
    "Scam Risk":            "#dc2626",   # red
}


def load_records() -> list[dict]:
    """Read the active account's per-cycle history. Resolved at call
    time so flipping `account_config.json` updates the view on the next
    refresh without restarting Streamlit."""
    history = account.get_active_history_path()
    if not history.exists():
        return []
    out: list[dict] = []
    for line in history.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def agent_status() -> tuple[bool, int | None]:
    """Return (is_running, pid). Auto-cleans a stale PID file."""
    if not PID_FILE.exists():
        return False, None
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        PID_FILE.unlink(missing_ok=True)
        return False, None
    try:
        # Signal 0 = liveness probe. On POSIX this is a clean no-op-or-OSError.
        # On Windows, os.kill against a bogus PID can raise SystemError
        # ("returned a result with an exception set") instead of OSError, so
        # catch broadly — any failure means the PID is unusable, treat as stale.
        os.kill(pid, 0)
        return True, pid
    except Exception:
        PID_FILE.unlink(missing_ok=True)
        return False, None


def start_agent(interval_min: float) -> int:
    """Spawn gmail_agent.py in the background. Returns the new PID.

    NOTE: do NOT pass creationflags=CREATE_NEW_PROCESS_GROUP or
    close_fds=True on Windows. Both interfere with Playwright's
    Node-driver pipes and the agent crashes with EPIPE before it can
    even print '[1/4] Setting up Chrome...'. Inheriting the parent's
    process group is fine — os.kill(pid, SIGTERM) on Windows uses
    TerminateProcess regardless of group membership.
    """
    log_handle = AGENT_LOG.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        [str(VENV_PY), "-u", "gmail_agent.py", "--interval", str(interval_min)],
        cwd=str(PROJECT),
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    return proc.pid


def stop_agent(pid: int) -> bool:
    """Kill the agent process by PID. Returns True if the kill went through."""
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    PID_FILE.unlink(missing_ok=True)
    return True


_TOPIC_LABELS = ["AI/ML", "Research", "Software Engineering", "Embedded Systems", "DevOps"]
_ALL_LABELS = _TOPIC_LABELS + ["Scam Risk"]


def search_history(query: str, records: list[dict]) -> tuple[str, list[dict]]:
    """Grounded search over history.jsonl. Returns (summary, matched_records).
    All facts come from the actual scam_features / cv_match / labels stored
    on each record — no LLM call. Recognized intents:
      - 'why' / 'explain' / 'reason' → focus on the most relevant single email
      - 'scam' → only emails labeled Scam Risk
      - one of the topic labels → only emails with that label
      - 'best' / 'highest' / 'top' → sort by cv_match score
      - otherwise → text search across subject / sender / body"""
    q = query.lower().strip()
    if not q:
        return "Type a question above.", []

    matched_labels = [l for l in _ALL_LABELS if l.lower() in q]
    is_why = any(w in q for w in ("why", "explain", "reason", "because"))
    is_scam = "scam" in q or matched_labels == ["Scam Risk"]
    is_best = any(w in q for w in ("best", "highest", "top match", "strongest"))

    # Filter
    if is_scam:
        results = [r for r in records if "Scam Risk" in r.get("labels_applied", [])]
    elif matched_labels:
        results = [
            r for r in records
            if any(lbl in r.get("labels_applied", []) for lbl in matched_labels)
        ]
    elif is_best and len(q.split()) <= 4:
        # Bare 'best CV match' / 'top match' — no other filter — return all
        # records and let the sort below pick the strongest.
        results = list(records)
    else:
        # Token-based text search across subject / sender / body. Strip
        # stop-words so 'anything about docker?' actually matches 'docker'.
        STOP = {
            "anything", "about", "show", "me", "the", "a", "any", "some",
            "what", "is", "are", "was", "were", "with", "for", "from",
            "tell", "of", "in", "on", "by",
        }
        tokens = [t.strip("?.!,") for t in q.split() if t.strip("?.!,")]
        tokens = [t for t in tokens if t and t not in STOP]
        if not tokens:
            results = list(records)
        else:
            results = [
                r for r in records
                if any(
                    t in (r.get(f) or "").lower()
                    for f in ("subject", "sender", "body")
                    for t in tokens
                )
            ]

    # Sort
    if is_best:
        results.sort(key=lambda r: (r.get("cv_match") or {}).get("score") or 0, reverse=True)

    if not results:
        return "No emails matched.", []

    if is_why:
        # Focus the answer on the single most relevant email — full reasoning.
        r = results[0]
        labels = r.get("labels_applied") or []
        labels_str = ", ".join(labels) if labels else "no labels"
        scam = r.get("scam_features") or {}
        cv = r.get("cv_match") or {}
        lines: list[str] = []
        lines.append(f"**'{r.get('subject') or '(no subject)'}'** was labeled **{labels_str}**.")
        if "Scam Risk" in labels:
            score = scam.get("score") or 0
            lines.append(f"Scam-risk score **{score}** — reasons:")
            for reason in (scam.get("reasons") or [])[:6]:
                lines.append(f"- {reason}")
        else:
            cv_score = cv.get("score") or 0
            lines.append(f"CV-match score **{cv_score}**. Top retrieved CV evidence:")
            for hit in (cv.get("matched") or [])[:3]:
                snippet = (hit.get("chunk") or "").replace("\n", " ")[:100]
                lines.append(f"- sim {hit.get('similarity', 0):.2f} *({hit.get('kind', '?')})* — {snippet}…")
            missing = cv.get("missing_skills") or []
            if missing:
                lines.append(f"Missing skills mentioned in JD: {', '.join(missing)}")
        return "\n".join(lines), [r]

    # Generic answer
    head = f"Found {len(results)} email(s)"
    if is_best:
        head += " (sorted by CV match)"
    head += ":"
    return head, results[:6]


def chip(label: str) -> str:
    color = LABEL_COLORS.get(label, "#6b7280")
    return (
        f"<span style='background:{color};color:#fff;padding:2px 10px;"
        f"border-radius:12px;margin-right:6px;font-size:12px;font-weight:500'>"
        f"{label}</span>"
    )


def fmt_received(ms) -> str:
    if not isinstance(ms, (int, float)):
        return "—"
    try:
        return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "—"


# ── Page setup ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Internship Agent",
    layout="wide",
    page_icon="📥",
)
st.title("Gmail Internship Monitor")
_active_email = account.get_active_email()
_active_history = account.get_active_history_path()
_provider = llm._provider()
_provider_label = {
    "claude_cli": "🧠 Claude (Pro subscription)",
    "anthropic":  "🧠 Claude (Anthropic API)",
    "openai":     "🧠 GPT (OpenAI)",
}.get(_provider, "⚙️ Rule-based fallback")
if _active_email:
    st.caption(
        f"Monitoring **{_active_email}**  ·  history: `{_active_history.name}`  ·  "
        f"classifier: **{_provider_label}**"
    )
else:
    st.caption(
        "No active account detected — run `start_monitoring.py` to sign in. "
        "Showing legacy `history.jsonl` if present."
    )

# Sidebar — agent process control + auto-refresh settings.
with st.sidebar:
    st.subheader("Agent")
    running, pid = agent_status()
    if running:
        st.success(f"🟢 Running  ·  PID {pid}")
        if st.button("Stop agent", type="secondary", use_container_width=True):
            if stop_agent(pid):
                st.toast("Stopped.", icon="🛑")
            else:
                st.toast("Process already gone.", icon="⚠️")
            st.rerun()
    else:
        st.error("🔴 Stopped")
        cycle_min = st.number_input(
            "Cycle interval (minutes)",
            min_value=0.5, max_value=30.0, value=2.0, step=0.5,
        )
        if st.button("Start agent", type="primary", use_container_width=True):
            new_pid = start_agent(float(cycle_min))
            st.toast(f"Started (PID {new_pid}).", icon="▶️")
            st.rerun()

    st.divider()

    st.subheader("Live updates")
    auto = st.toggle("Auto-refresh", value=True)
    interval_s = st.slider(
        "Interval (seconds)", min_value=5, max_value=60, value=10, step=5,
        disabled=not auto,
    )
    if auto:
        st_autorefresh(interval=interval_s * 1000, key="cycle_refresh")
        st.caption(f"Refreshing every {interval_s}s.")
    else:
        st.caption("Paused — use the Refresh button.")

records = load_records()
records.sort(key=lambda r: r.get("received_ms") or 0, reverse=True)

# ── Top metrics ──────────────────────────────────────────────────────────────

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Emails seen", len(records))
c2.metric("Labeled", sum(1 for r in records if r.get("labels_applied")))
c3.metric("Scam Risk", sum(1 for r in records if "Scam Risk" in r.get("labels_applied", [])))
c4.metric(
    "Avg CV match",
    f"{sum(r.get('cv_match', {}).get('score', 0) for r in records) / max(len(records), 1):.2f}",
)
c5.metric(
    "Last cycle",
    records[0].get("cycle_at", "—") if records else "—",
)

# ── Filter + refresh row ─────────────────────────────────────────────────────

fcol, rcol = st.columns([4, 1])
with fcol:
    q = st.text_input(
        "Filter by topic, sender, subject, or label",
        placeholder="e.g. AI/ML, scam, docker, mazloum",
    )
with rcol:
    st.write("")  # spacer
    if st.button("🔄 Refresh", use_container_width=True):
        st.rerun()

if q:
    ql = q.lower()
    records = [
        r for r in records
        if ql in (r.get("subject") or "").lower()
        or ql in (r.get("sender") or "").lower()
        or ql in " ".join(r.get("labels_applied", [])).lower()
        or ql in (r.get("body") or "").lower()
    ]

st.divider()

# ── Per-email cards ──────────────────────────────────────────────────────────

if not records:
    st.info(
        "No history yet for this account. Click **Start agent** in the sidebar "
        "(or run `venv\\Scripts\\python -u gmail_agent.py`) and a record will "
        "appear here after each cycle."
    )
else:
    for r in records[:50]:
        with st.container(border=True):
            head = (
                f"#### {r.get('subject') or '(no subject)'}\n"
                f"**From:** {r.get('sender') or '—'}  ·  "
                f"**Received:** {fmt_received(r.get('received_ms'))}  ·  "
                f"**Cycle:** {r.get('cycle_at', '—')}"
            )
            st.markdown(head)

            labels = r.get("labels_applied", [])
            if labels:
                st.markdown(
                    "**Labels:** " + "".join(chip(l) for l in labels),
                    unsafe_allow_html=True,
                )
            else:
                st.markdown("_No labels applied this cycle._")

            scam = r.get("scam_features", {}) or {}
            cv = r.get("cv_match", {}) or {}
            sc1, sc2 = st.columns(2)
            with sc1:
                score = scam.get("score", 0)
                color = "🔴" if score >= 0.5 else ("🟠" if score >= 0.3 else "🟢")
                st.markdown(f"**{color} Scam-risk score:** {score}")
                reasons = scam.get("reasons", [])
                if reasons:
                    for reason in reasons:
                        st.markdown(f"- {reason}")
                else:
                    st.caption("No heuristic flags triggered.")
            with sc2:
                cv_score = cv.get("score", 0)
                bar = "🟢" if cv_score >= 0.45 else ("🟠" if cv_score >= 0.30 else "🔴")
                st.markdown(f"**{bar} CV-match score:** {cv_score}")
                matched = cv.get("matched", [])
                if matched:
                    st.caption("Retrieved CV evidence:")
                    for m in matched[:3]:
                        snippet = m["chunk"].replace("\n", " ")[:110]
                        st.markdown(f"- `{m['similarity']:.2f}` — {snippet}…")
                missing = cv.get("missing_skills", [])
                if missing:
                    st.markdown(
                        "**Missing in CV:** " + " ".join(chip(s) for s in missing),
                        unsafe_allow_html=True,
                    )

            with st.expander("Full body"):
                st.text(r.get("body") or "")


# ── Chat panel ───────────────────────────────────────────────────────────────
# A grounded Q&A box at the bottom of the dashboard. When an LLM provider is
# configured, free-form questions go to the LLM (`llm.chat_about_history`)
# with the agent's history records inlined as evidence. Without a provider,
# falls back to the rule-based `search_history` summary. Either way the
# conversation persists across reruns via st.session_state.

st.divider()
st.subheader("Ask the agent")
_chat_caption = (
    "Routed through {p}. Ground every answer in the records below."
    if _provider else
    "Rule-based fallback (no LLM provider configured) — answers come from a "
    "scripted summary of `history.jsonl`."
)
st.caption(_chat_caption.format(p=_provider_label) if _provider else _chat_caption)
st.caption(
    "Try: *why was the latest scam labeled?* · *show me AI/ML emails* · "
    "*best CV match* · *anything from Mazloum?*"
)

if "chat_history" not in st.session_state:
    st.session_state.chat_history = []

# Replay the conversation so far.
for msg in st.session_state.chat_history:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        for r in msg.get("records", []):
            with st.container(border=True):
                labels = r.get("labels_applied") or []
                head = f"**{r.get('subject') or '(no subject)'}** — *{r.get('sender') or '—'}*"
                if labels:
                    head += "  " + "".join(chip(l) for l in labels)
                st.markdown(head, unsafe_allow_html=True)
                scam = r.get("scam_features") or {}
                cv = r.get("cv_match") or {}
                st.caption(
                    f"scam {scam.get('score', 0)}  ·  "
                    f"cv-match {cv.get('score', 0)}  ·  "
                    f"{r.get('cycle_at', '—')}"
                )

cols = st.columns([6, 1])
with cols[1]:
    if st.button("Clear", use_container_width=True):
        st.session_state.chat_history = []
        st.rerun()

# Two-phase chat handler so the LLM call survives Streamlit reruns.
#
# When the user submits a question we append BOTH the user message and a
# pending-placeholder assistant message in a single rerun cycle, then on the
# *next* render we detect the pending placeholder, run the LLM, and
# overwrite the placeholder with the real answer. Splitting the work like
# this means the autorefresh component can fire between the user submit
# and the LLM call without losing the user message — the placeholder is
# always paired with its user message, and the next render picks the work
# back up. The user also sees a "Thinking…" cue immediately rather than a
# 5-10 s blank wait.

if query := st.chat_input("Ask about the labeled emails…"):
    st.session_state.chat_history.append({"role": "user", "content": query})
    st.session_state.chat_history.append({
        "role": "assistant",
        "content": "_Thinking…_",
        "records": [],
        "pending": True,
    })
    st.rerun()

# Resolve any pending placeholder. We do this AFTER the replay loop above,
# so the placeholder is visible to the user during the LLM round-trip.
if (
    st.session_state.chat_history
    and st.session_state.chat_history[-1].get("pending")
    and len(st.session_state.chat_history) >= 2
):
    user_q = st.session_state.chat_history[-2]["content"]
    summary, results = search_history(user_q, records)

    # Only attach record cards under the answer when the question has a
    # clear intent to surface emails — explicit label name, "scam", "why",
    # "show me", "best CV match", etc. For free-form questions the LLM's
    # text answer is enough; the cards-by-default added noise (e.g. asking
    # "what is the capital of Lebanon" was matching every email subject
    # starting with "Hi" via substring fallback).
    ql = user_q.lower()
    intent_keywords = (
        "show", "list", "which", "what email", "any email", "any mail",
        "is there", "are there", "scam", "why", "explain", "reason",
        "best", "highest", "top match", "strongest", "from",
    )
    has_listing_intent = (
        any(lbl.lower() in ql for lbl in _ALL_LABELS)
        or any(kw in ql for kw in intent_keywords)
    )
    cards_to_show = results if (has_listing_intent and results) else []

    # The LLM still gets context — when results are empty (or filtered out
    # for cards), feed it a recent slice so it can reason about "any
    # internships from last week?" type questions even when the rule-based
    # pre-filter didn't catch them.
    candidates = results if results else records[:30]
    if llm.llm_available():
        try:
            answer = llm.chat_about_history(user_q, candidates[:30])
        except Exception as e:
            answer = (
                f"_(LLM call failed — falling back to rule-based summary: "
                f"`{type(e).__name__}: {str(e)[:120]}`)_\n\n{summary}"
            )
    else:
        answer = summary
    if not (answer or "").strip():
        answer = "_(empty response from the LLM — try rephrasing.)_"
    st.session_state.chat_history[-1] = {
        "role": "assistant",
        "content": answer,
        "records": cards_to_show,
    }
    st.rerun()
