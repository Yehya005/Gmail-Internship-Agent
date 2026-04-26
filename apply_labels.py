"""Cycle step 3 — apply each (thread, label) pair via the per-thread
three-dots → 'Label as' → menuitemcheckbox flow. Pure I/O — the labels
themselves were decided in classifier.py.

Public API:

    apply(page, thread_labels) -> tuple[int, int]
        Returns (applied_count, requested_count).

CLI:

    python apply_labels.py [--in to_label.json]

Reads `to_label.json` ({"thread_labels": {"<tid>": ["AI/ML", ...]}})
and applies each pair. Used by the agent's main loop and by anyone who
wants to apply a stored label decision in isolation.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import Page, async_playwright

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

CDP_URL = "http://localhost:9222"


_FIND_ROW_INDEX_JS = """(tid) => [...document.querySelectorAll('tr.zA')].findIndex(r =>
    (r.dataset.threadId
      || r.querySelector('[data-thread-id]')?.dataset.threadId
      || r.querySelector('[data-legacy-thread-id]')?.dataset.legacyThreadId) === tid)"""

_TOGGLE_ROW_CB_JS = """(idx) => {
    const row = document.querySelectorAll('tr.zA')[idx];
    if (!row) return false;
    const cb = row.querySelector('.oZ-jc') ||
               row.querySelector('[role="checkbox"]') ||
               row.querySelector('td.PF');
    if (cb) { cb.click(); return true; }
    return false;
}"""

_DISMISS_TOASTS_JS = (
    "() => document.querySelectorAll("
    "'[role=\"alertdialog\"], div.bAq.bAr, div.b8'"
    ").forEach(d => d.remove())"
)


# ── Public API ──────────────────────────────────────────────────────────────

async def _label_one_thread(page: Page, tid: str, label_name: str) -> bool:
    """Apply one label to one thread. Idempotent: reads `aria-checked`
    on the picker entry first, only clicks when the state needs to flip."""
    row_index: int = await page.evaluate(_FIND_ROW_INDEX_JS, tid)
    if row_index < 0:
        print(f"  [WARN] Thread {tid[:24]} not found in inbox view.")
        return False

    if not await page.evaluate(_TOGGLE_ROW_CB_JS, row_index):
        return False
    await asyncio.sleep(1.5)

    try:
        await page.evaluate(_DISMISS_TOASTS_JS)
        await page.locator('[data-tooltip="More"]').first.click(
            timeout=8_000, force=True,
        )
        await asyncio.sleep(0.8)
        await page.locator(
            '[role="menuitem"]:has-text("Label as")'
        ).first.hover(timeout=4_000)
        await asyncio.sleep(1.5)

        current = await page.evaluate(
            """(name) => {
                const el = [...document.querySelectorAll('[role="menuitemcheckbox"]')]
                    .find(e => e.getAttribute('title') === name);
                return el ? el.getAttribute('aria-checked') === 'true' : null;
            }""",
            label_name,
        )
        if current is None:
            print(f"  [WARN] Label '{label_name}' not in picker for {tid[:24]}.")
            return False
        if current is True:
            return True  # already applied — no-op

        await page.locator(
            f'[role="menuitemcheckbox"][title="{label_name}"]'
        ).first.click(timeout=8_000, force=True)
        await asyncio.sleep(0.6)
        return True
    except Exception as e:
        print(f"  [WARN] Could not apply '{label_name}' to {tid[:24]}: {e}")
        return False
    finally:
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.2)
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.4)
            await page.evaluate(_TOGGLE_ROW_CB_JS, row_index)
            await asyncio.sleep(0.3)
        except Exception:
            pass


async def apply(
    page: Page, thread_labels: dict[str, list[str]],
) -> tuple[int, int]:
    """Apply each thread's list of labels. Returns (applied, requested)."""
    if not thread_labels:
        return 0, 0

    await page.goto("https://mail.google.com/#inbox", wait_until="domcontentloaded")
    try:
        await page.wait_for_selector("tr.zA", timeout=15_000)
    except Exception:
        return 0, sum(len(v) for v in thread_labels.values())

    applied = 0
    requested = 0
    for tid, labels in thread_labels.items():
        for label_name in labels:
            requested += 1
            if await _label_one_thread(page, tid, label_name):
                applied += 1
    return applied, requested


# ── CLI ──────────────────────────────────────────────────────────────────────

async def _cli_main() -> int:
    p = argparse.ArgumentParser(description="Apply labels stored in to_label.json.")
    p.add_argument("--in", dest="src", default="to_label.json",
                   help="Input path. Default: ./to_label.json.")
    args = p.parse_args()

    src = Path(args.src)
    if not src.exists():
        print(f"error: {src} not found", file=sys.stderr)
        return 1
    data = json.loads(src.read_text(encoding="utf-8"))
    thread_labels = data.get("thread_labels", {})

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
        applied, requested = await apply(page, thread_labels)
    finally:
        await pw.stop()
    print(f"applied {applied}/{requested} (thread, label) pair(s)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_cli_main()))
