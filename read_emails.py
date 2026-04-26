"""Cycle step 1 — read and download every email received in the recency
window. Writes `emails.json` with one record per email, no scoring or
classification (those live in classifier.py).

Public API:

    read(page, *, cycle_seconds, safe_zone_seconds=60) -> list[dict]
        Returns a list of {thread_id, subject, sender, body, received_ms}.
        Partial results: if scraping a single email fails, that email is
        skipped (logged) but the rest of the batch still returns.

CLI:

    python read_emails.py [--interval 2] [--out emails.json]

Connects to the existing Chrome over CDP, runs `read`, writes the file,
prints a one-line summary. Used by the agent's main loop and by anyone
who wants to debug the read step in isolation.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from playwright.async_api import Page, async_playwright

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

CDP_URL = "http://localhost:9222"
EMAILS_PER_CYCLE = 50


# ── DOM scraping ────────────────────────────────────────────────────────────

_SCAN_INBOX_JS = """(limit) => {
    const rows = document.querySelectorAll('tr.zA');
    const results = [];
    for (const row of rows) {
        const id =
            row.dataset.threadId ||
            row.querySelector('[data-thread-id]')?.dataset.threadId ||
            row.querySelector('[data-legacy-thread-id]')?.dataset.legacyThreadId;
        if (!id) continue;

        const subjectEl = row.querySelector('.y6 > span') || row.querySelector('.bog');
        const subject = subjectEl?.innerText?.trim() || '';

        const senderEl = row.querySelector('.zF');
        const sender = senderEl?.getAttribute('name') ||
                       senderEl?.getAttribute('email') ||
                       senderEl?.innerText?.trim() || '';

        const dateEl = row.querySelector('td.xW span[title]');
        const titleStr = dateEl?.getAttribute('title') || '';
        const visibleStr = dateEl?.innerText?.trim() || '';
        let parsed = Date.parse(titleStr.replace(/,\\s*(\\d{1,2}:\\d{2})/, ' $1'));
        if (!Number.isFinite(parsed)) parsed = Date.parse(visibleStr);
        const received_ms = Number.isFinite(parsed) ? parsed : Date.now();

        results.push({ thread_id: id, subject, sender, received_ms });
        if (results.length >= limit) break;
    }
    return results;
}"""


_OPEN_AND_SCRAPE_JS = """(tid) => {
    const row = [...document.querySelectorAll('tr.zA')].find(r =>
        (r.dataset.threadId
          || r.querySelector('[data-thread-id]')?.dataset.threadId
          || r.querySelector('[data-legacy-thread-id]')?.dataset.legacyThreadId) === tid);
    if (!row) return 'no-row';
    const subj = row.querySelector('.y6, .bog');
    if (!subj) return 'no-subj';
    const r = subj.getBoundingClientRect();
    const x = r.x + r.width / 2;
    const y = r.y + r.height / 2;
    for (const type of ['mousedown', 'mouseup', 'click']) {
        subj.dispatchEvent(new MouseEvent(type, {
            bubbles: true, cancelable: true,
            button: 0, clientX: x, clientY: y,
        }));
    }
    return 'clicked';
}"""


_BODY_SCRAPE_JS = """() => {
    const sels = ['div.a3s.aiL', 'div.a3s', 'div.ii.gt > div'];
    const seen = new Set();
    const parts = [];
    for (const sel of sels) {
        for (const el of document.querySelectorAll(sel)) {
            if (seen.has(el)) continue;
            seen.add(el);
            const t = (el.innerText || '').trim();
            if (t) parts.push(t);
        }
    }
    return parts.join('\\n\\n---\\n\\n') || null;
}"""


# ── Public API ──────────────────────────────────────────────────────────────

async def _open_and_get_full_body(page: Page, tid: str) -> str | None:
    """Open the conversation for `tid` and return its full body, or None."""
    result = await page.evaluate(_OPEN_AND_SCRAPE_JS, tid)
    if result != "clicked":
        print(f"    [body-scrape] click prep failed for {tid[:24]}: {result}")
        return None
    await asyncio.sleep(2.0)  # message bodies render after navigation

    body = await page.evaluate(_BODY_SCRAPE_JS)
    print(f"    [body-scrape] scraped {len(body) if body else 0} chars from {tid[:24]}")

    await page.goto("https://mail.google.com/#inbox", wait_until="domcontentloaded")
    try:
        await page.wait_for_selector("tr.zA", timeout=15_000)
    except Exception:
        pass
    return body


async def read(
    page: Page,
    *,
    cycle_seconds: int,
    safe_zone_seconds: int = 60,
    max_emails: int = EMAILS_PER_CYCLE,
) -> list[dict]:
    """Scan inbox → recency-filter → open + scrape full body of each fresh
    email. Returns whatever succeeded; failures are logged and skipped
    so the next pipeline step can still run on the partial set."""
    await page.goto("https://mail.google.com/#inbox", wait_until="domcontentloaded")
    try:
        await page.wait_for_selector("tr.zA", timeout=15_000)
    except Exception:
        return []

    rows = await page.evaluate(_SCAN_INBOX_JS, max_emails)

    # Recency filter — cycle window + 1-minute safe zone for Gmail's
    # minute-precision timestamps and edge-case scan timing.
    now_ms = time.time() * 1_000
    cutoff_ms = now_ms - cycle_seconds * 1_000 - safe_zone_seconds * 1_000
    fresh = [
        r for r in rows
        if isinstance(r.get("received_ms"), (int, float))
        and r["received_ms"] >= cutoff_ms
    ]

    out: list[dict] = []
    for r in fresh:
        try:
            body = await _open_and_get_full_body(page, r["thread_id"])
        except Exception as e:
            print(f"    [body-scrape] {r['thread_id'][:24]} failed: {e}")
            continue
        if not body:
            continue
        r["body"] = body
        out.append(r)
    return out


# ── CLI ──────────────────────────────────────────────────────────────────────

async def _cli_main() -> int:
    p = argparse.ArgumentParser(description="Read fresh inbox emails to JSON.")
    p.add_argument(
        "--interval", type=float, default=2.0,
        help="Cycle length in minutes — defines the recency window. Default: 2.",
    )
    p.add_argument(
        "--out", default="emails.json",
        help="Output path. Default: ./emails.json.",
    )
    args = p.parse_args()

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        ctx = browser.contexts[0]
        page = next(
            (p for p in ctx.pages if "mail.google.com" in p.url),
            ctx.pages[0],
        )
        if "mail.google.com" not in page.url:
            await page.goto("https://mail.google.com/#inbox", wait_until="domcontentloaded")

        emails = await read(page, cycle_seconds=int(args.interval * 60))
    finally:
        await pw.stop()

    Path(args.out).write_text(
        json.dumps(emails, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    print(f"wrote {len(emails)} email(s) to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_cli_main()))
