"""CLI: create a Gmail label via the agent's existing primitive.

    venv\\Scripts\\python create_label.py "<label name>"

Connects to the user's running Chrome over CDP (no second login),
clicks the sidebar '+' button next to the Labels heading, types the
name, and presses Enter. Idempotent — bails out early if the label
already exists.

Doesn't need the agent to be running; only Chrome with CDP open on
port 9222 (the same browser the agent uses).
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from playwright.async_api import async_playwright

from gmail_agent import _create_one_label, _label_in_sidebar

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Create a Gmail label.")
    parser.add_argument("name", help="Label name to create (e.g. 'test1').")
    parser.add_argument(
        "--cdp-url", default="http://localhost:9222",
        help="Chrome CDP endpoint (default: http://localhost:9222).",
    )
    args = parser.parse_args()

    pw = await async_playwright().start()
    try:
        try:
            browser = await pw.chromium.connect_over_cdp(args.cdp_url, timeout=3_000)
        except Exception as e:
            print(f"error: could not connect to Chrome at {args.cdp_url}: {e}",
                  file=sys.stderr)
            return 1

        ctx = browser.contexts[0]
        page = next(
            (p for p in ctx.pages if "mail.google.com" in p.url),
            ctx.pages[0] if ctx.pages else await ctx.new_page(),
        )
        if "mail.google.com" not in page.url:
            await page.goto("https://mail.google.com", wait_until="domcontentloaded")
        try:
            await page.wait_for_selector('[gh="cm"]', timeout=30_000)
        except Exception:
            print("error: Gmail compose button never appeared — sign in first.",
                  file=sys.stderr)
            return 1

        await asyncio.sleep(1.5)  # let the sidebar render
        await _create_one_label(page, args.name)
        await asyncio.sleep(0.8)
        ok = await _label_in_sidebar(page, args.name)
        print("ok" if ok else "failed")
        return 0 if ok else 1
    finally:
        try:
            await browser.close()
        except Exception:
            pass
        await pw.stop()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
