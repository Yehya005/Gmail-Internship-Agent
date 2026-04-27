"""CLI: prepare Chrome for a new Gmail account login.

    venv\\Scripts\\python switch_account.py

Two modes:

  1. Chrome is already running on CDP 9222 (e.g. left over from an
     agent run): close any existing Gmail / accounts.google.com tabs
     in that Chrome and open a fresh tab at Google's AddSession flow.
     Brings the new tab to the front so the user can sign in.

  2. Chrome isn't running: launch it ourselves with CDP open and a
     fresh temp profile (the same way gmail_agent's cold-start does)
     pointed at the AddSession URL. The user signs in there, then
     starts the agent from the dashboard.

Either way the user ends up with one Chrome window where they can
sign into the account they want monitored. After signing in, restart
the agent from the dashboard — it will pick up whichever Gmail tab
is open.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile

from playwright.async_api import async_playwright

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)


CDP_URL = "http://localhost:9222"
CDP_PORT = 9222
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
ADD_SESSION_URL = (
    "https://accounts.google.com/AddSession"
    "?continue=https://mail.google.com/mail/"
    "&service=mail"
)


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


async def main() -> int:
    pw = await async_playwright().start()
    try:
        # Mode 1 — try to connect to a running Chrome.
        try:
            browser = await pw.chromium.connect_over_cdp(CDP_URL, timeout=2_000)
        except Exception:
            browser = None

        if browser is not None:
            ctx = browser.contexts[0]
            closed = 0
            for p in list(ctx.pages):
                url = p.url or ""
                if "mail.google.com" in url or "accounts.google.com" in url:
                    try:
                        await p.close()
                        closed += 1
                    except Exception:
                        pass
            if closed:
                print(f"closed {closed} existing Gmail/account tab(s)")
            page = await ctx.new_page()
            await page.goto(ADD_SESSION_URL, wait_until="domcontentloaded")
            await page.bring_to_front()
            print(
                "opened the Add-Account flow in the existing Chrome — "
                "sign in to the new Gmail you want monitored, then "
                "restart the agent from the dashboard."
            )
            try:
                await browser.close()
            except Exception:
                pass
            return 0

        # Mode 2 — launch a fresh Chrome with CDP open at the
        # AddSession URL (cold-start path).
        print("Chrome not running — launching a fresh instance with CDP open.")
        temp_dir = tempfile.mkdtemp(prefix="gmail_monitor_")
        _start_chrome(temp_dir, ADD_SESSION_URL)
        # Wait a few seconds for CDP to come online so the user knows
        # the relaunch worked.
        for _ in range(10):
            try:
                browser = await pw.chromium.connect_over_cdp(CDP_URL, timeout=1_000)
                break
            except Exception:
                await asyncio.sleep(1)
        else:
            print("warning: Chrome started but CDP didn't open in 10 s.",
                  file=sys.stderr)
            return 1
        print(
            f"Chrome is up on CDP {CDP_PORT} with a fresh profile at "
            f"{temp_dir} — sign in to the Gmail you want monitored, "
            "then start the agent from the dashboard."
        )
        try:
            await browser.close()
        except Exception:
            pass
        return 0
    finally:
        await pw.stop()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
