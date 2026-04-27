"""All-in-one launcher: Chrome → wait for login → detect account →
launch dashboard pointed at the matching history file.

    venv\\Scripts\\python start_monitoring.py

Order of operations (the architectural rule the user asked for):

  1. Connect to a running Chrome on CDP 9222, or launch a fresh Chrome
     with CDP open and a temp profile pointed at gmail.com.
  2. Wait for the user to sign in (compose button appears).
  3. Read the signed-in account's email from the profile button's
     aria-label and write it to account_config.json — both the agent
     and the dashboard read this at runtime to pick the matching
     history file.
  4. Print whether the account has prior records or is fresh.
  5. Launch Streamlit and auto-open it in the default browser. The
     dashboard reads the active account from the config and shows
     the per-account history.

After this returns, the user clicks "Start agent" in the sidebar.
"""
from __future__ import annotations

import asyncio
import re
import subprocess
import sys
import tempfile
import time
import webbrowser
from pathlib import Path

from playwright.async_api import async_playwright

from account import history_path_for, set_active_email

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)


CDP_URL = "http://localhost:9222"
CDP_PORT = 9222
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

PROJECT = Path(__file__).parent
VENV_PY = PROJECT / "venv" / "Scripts" / "python.exe"
BOOT_LOG = PROJECT / "streamlit_boot.log"


def _start_chrome(temp_dir: str, url: str) -> subprocess.Popen:
    return subprocess.Popen(
        [
            CHROME_PATH,
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={temp_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def _wait_for_login_and_detect_email() -> str | None:
    """Connect to Chrome (launching one if needed), wait for Gmail
    login, then read the account email from the profile button's
    aria-label. Returns None on failure."""
    pw = await async_playwright().start()
    try:
        try:
            browser = await pw.chromium.connect_over_cdp(CDP_URL, timeout=2_000)
        except Exception:
            print("Launching Chrome with CDP open at gmail.com...")
            temp_dir = tempfile.mkdtemp(prefix="gmail_monitor_")
            _start_chrome(temp_dir, "https://mail.google.com")
            browser = None
            for _ in range(15):
                try:
                    browser = await pw.chromium.connect_over_cdp(
                        CDP_URL, timeout=1_000,
                    )
                    break
                except Exception:
                    await asyncio.sleep(1)
            if browser is None:
                print("error: Chrome didn't open CDP within 15 s",
                      file=sys.stderr)
                return None

        ctx = browser.contexts[0]
        page = next(
            (p for p in ctx.pages if "mail.google.com" in p.url),
            None,
        )
        if page is None:
            page = await ctx.new_page()
            await page.goto("https://mail.google.com", wait_until="domcontentloaded")
        await page.bring_to_front()

        print("Waiting for Gmail login (up to 5 minutes)...")
        try:
            await page.wait_for_selector('[gh="cm"]', timeout=300_000)
        except Exception:
            print("error: compose button never appeared — login not detected.",
                  file=sys.stderr)
            return None
        print("Login confirmed.")

        # Read the email from the profile button aria-label. Gmail's
        # signed-in profile element matches `a[aria-label*="Google Account:"]`
        # with text like "Google Account: Name (email@gmail.com)".
        await asyncio.sleep(2)  # profile button finishes rendering
        email = await page.evaluate(
            """() => {
                const btn = document.querySelector('a[aria-label*="Google Account:"]');
                if (!btn) return null;
                const al = btn.getAttribute('aria-label') || '';
                const m = al.match(/\\(([^)]+@[^)]+)\\)/);
                return m ? m[1] : null;
            }"""
        )

        try:
            await browser.close()
        except Exception:
            pass

        return email
    finally:
        await pw.stop()


def _launch_dashboard() -> str | None:
    """Start Streamlit detached, watch its log for the URL, return it."""
    log_handle = BOOT_LOG.open("w", encoding="utf-8")
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
    deadline = time.time() + 30
    while time.time() < deadline:
        time.sleep(0.5)
        try:
            text = BOOT_LOG.read_text(encoding="utf-8")
        except OSError:
            continue
        m = re.search(r"Local URL:\s*(http://localhost:\d+)", text)
        if m:
            return m.group(1)
    return None


async def main() -> int:
    email = await _wait_for_login_and_detect_email()
    if not email:
        print("warning: couldn't detect account — dashboard will use the "
              "legacy history.jsonl.")
    else:
        set_active_email(email)
        history_file = history_path_for(email)
        if history_file.exists():
            print(f"Welcome back — using existing history "
                  f"({history_file.name}, {history_file.stat().st_size} bytes)")
        else:
            print(f"First time monitoring this account — "
                  f"fresh history at {history_file.name}")

    print("Launching dashboard...")
    url = _launch_dashboard()
    if not url:
        print("warning: dashboard didn't print a URL within 30 s — "
              "check streamlit_boot.log.", file=sys.stderr)
        return 1
    print(f"dashboard at {url}")
    try:
        webbrowser.open(url, new=2)
        print("opened in default browser.")
    except Exception as e:
        print(f"warning: couldn't auto-open browser ({e}); "
              f"open {url} manually.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
