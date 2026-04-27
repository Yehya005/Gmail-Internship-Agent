"""CLI: archive the dashboard's history.jsonl so the new Gmail account
starts with a clean dashboard. Renames history.jsonl to a timestamped
backup; the agent will create a fresh empty one on its next cycle.

    venv\\Scripts\\python reset_history.py

Nothing is deleted. The archive sits in the project root as
history_<YYYY-MM-DD_HHMMSS>.jsonl.bak — restore by renaming back to
history.jsonl if you switch accounts again.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)


HISTORY = Path(__file__).parent / "history.jsonl"


def main() -> int:
    if not HISTORY.exists():
        print("history.jsonl doesn't exist — nothing to archive.")
        return 0
    if HISTORY.stat().st_size == 0:
        HISTORY.unlink(missing_ok=True)
        print("history.jsonl was empty — removed.")
        return 0

    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    archive = HISTORY.with_name(f"history_{stamp}.jsonl.bak")
    HISTORY.rename(archive)
    print(f"archived {HISTORY.name} -> {archive.name}")
    print("refresh the dashboard — it'll show 'No history yet'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
