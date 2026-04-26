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

PROJECT = Path(__file__).parent
HISTORY = PROJECT / "history.jsonl"
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
    if not HISTORY.exists():
        return []
    out: list[dict] = []
    for line in HISTORY.read_text(encoding="utf-8").splitlines():
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
        # Signal 0 = liveness probe — raises if the process no longer exists.
        os.kill(pid, 0)
        return True, pid
    except OSError:
        PID_FILE.unlink(missing_ok=True)
        return False, None


def start_agent(interval_min: float) -> int:
    """Spawn gmail_agent.py in the background. Returns the new PID."""
    # Append to the same log the standalone runs use.
    log_handle = AGENT_LOG.open("a", encoding="utf-8")
    creationflags = (
        subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    )
    proc = subprocess.Popen(
        [str(VENV_PY), "-u", "gmail_agent.py", "--interval", str(interval_min)],
        cwd=str(PROJECT),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
        close_fds=True,
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
st.caption("Live view of every email the agent has scanned, classified, and labeled.")

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
        "No history yet. Start the agent (`venv\\Scripts\\python -u gmail_agent.py`) "
        "and a record will appear here after each cycle."
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
