"""CLI: launch the Streamlit dashboard AND open it in the default browser.

    venv\\Scripts\\python start_dashboard.py

Spawns Streamlit as a detached subprocess so it survives this script's
exit, waits for the boot log to print a localhost URL, then calls
webbrowser.open on that URL so the user doesn't have to copy-paste.

Streamlit auto-bumps the port if 8501 is already in use, so we always
read the actual URL from the log rather than assuming 8501.
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)


PROJECT = Path(__file__).parent
VENV_PY = PROJECT / "venv" / "Scripts" / "python.exe"
BOOT_LOG = PROJECT / "streamlit_boot.log"


def main() -> int:
    if not VENV_PY.exists():
        print(f"error: venv python not found at {VENV_PY}", file=sys.stderr)
        return 1

    # Truncate the boot log so we only read fresh output from this run.
    log_handle = BOOT_LOG.open("w", encoding="utf-8")

    # Detach the Streamlit subprocess by pointing stdout at our log file
    # rather than at this Python's stdout. When start_dashboard.py exits,
    # Streamlit keeps running because its file descriptor stays valid.
    subprocess.Popen(
        [
            str(VENV_PY), "-m", "streamlit", "run", "streamlit_app.py",
            "--server.headless", "true",
            "--browser.gatherUsageStats", "false",
        ],
        cwd=str(PROJECT),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )

    # Watch the log for the "Local URL: http://localhost:NNNN" line that
    # Streamlit prints once it's bound.
    deadline = time.time() + 30
    url: str | None = None
    while time.time() < deadline:
        time.sleep(0.5)
        try:
            text = BOOT_LOG.read_text(encoding="utf-8")
        except OSError:
            continue
        m = re.search(r"Local URL:\s*(http://localhost:\d+)", text)
        if m:
            url = m.group(1)
            break

    if not url:
        print("warning: didn't detect Streamlit URL within 30 s — "
              "check streamlit_boot.log for errors.", file=sys.stderr)
        return 1

    print(f"dashboard at {url}")
    try:
        webbrowser.open(url, new=2)  # new=2 → new tab if possible
        print("opened in default browser.")
    except Exception as e:
        print(f"warning: could not auto-open browser ({e}); "
              f"open {url} manually.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
