"""Active-account config — single source of truth for which Gmail
account the agent and dashboard are currently monitoring.

`start_monitoring.py` writes the config after the user logs in. Every
other module (`gmail_agent.py`, `streamlit_app.py`) reads the config
at runtime so they share whatever account is active.

History files are partitioned by account email, so switching back to
a previously-monitored account picks up its prior records instead of
starting empty.
"""
from __future__ import annotations

import json
from pathlib import Path

PROJECT = Path(__file__).parent
CONFIG_PATH = PROJECT / "account_config.json"
DEFAULT_HISTORY = PROJECT / "history.jsonl"


def _sanitize(email: str) -> str:
    """Make `email` safe to embed in a filename. Keeps alphanumerics
    and a small set of structural chars (@ . - _)."""
    return "".join(c for c in email if c.isalnum() or c in "@.-_")


def history_path_for(email: str | None) -> Path:
    """Filename to use for the given email, or the legacy history.jsonl
    when no email is set (first-time / unknown-account fallback)."""
    if not email:
        return DEFAULT_HISTORY
    return PROJECT / f"history_{_sanitize(email)}.jsonl"


def get_active_email() -> str | None:
    if not CONFIG_PATH.exists():
        return None
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("email")
    except Exception:
        return None


def get_active_history_path() -> Path:
    return history_path_for(get_active_email())


def set_active_email(email: str) -> None:
    CONFIG_PATH.write_text(
        json.dumps({"email": email}, indent=2),
        encoding="utf-8",
    )
